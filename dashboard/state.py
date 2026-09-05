"""The per-job state machine (DESIGN.md "State machine").

Pure functions over a :class:`Facts` snapshot so every transition is unit-
testable with a fixed ``now``:

- ``OK``         last run success within cadence+grace AND (if probed) destination fresh
- ``LATE``       no heartbeat within cadence+grace (dead-man's switch)
- ``FAIL``       last heartbeat status=fail, or a container job missing an expected container
- ``STALE_DEST`` heartbeat says ok but the destination disagrees
- ``BEHIND``     manual job over its lag/age target, or a Drive mirror with pending work
- ``UNKNOWN``    never heard from

Precedence for scheduled kinds: LATE > FAIL > kind-specific (STALE_DEST / BEHIND) > OK.
A silent job is reported LATE even if its last word was "fail" — the silence is the
more urgent fact, and the last-run status stays visible on the card either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .db import from_iso
from .registry import Job

# Metric keys the Mac probe / scripts may use for "how far behind".
LAG_BYTES_KEYS = ("lag_bytes", "missing_bytes")
LAG_FILES_KEYS = ("lag_files", "missing_files")


@dataclass
class Facts:
    last_run: dict | None = None        # newest runs row (any status)
    last_success: dict | None = None    # newest ok/skipped run
    last_metrics: dict = field(default_factory=dict)
    last_metrics_at: str | None = None
    probe: dict | None = None           # newest probes row for this job


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _first_num(metrics: dict, keys) -> float | None:
    for k in keys:
        if k in metrics:
            v = _num(metrics[k])
            if v is not None:
                return v
    return None


def running_names(metrics: dict) -> set[str] | None:
    """Parse ``metrics.running`` — a comma-separated string or a list — into a
    set of container names. None when the metric is absent."""
    raw = metrics.get("running")
    if raw is None:
        return None
    if isinstance(raw, str):
        return {p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()}
    if isinstance(raw, (list, tuple)):
        return {str(p).strip() for p in raw if str(p).strip()}
    return None


def _newest_epoch(job: Job, f: Facts) -> float | None:
    if job.has_probe:
        if f.probe and f.probe.get("ok") and f.probe.get("newest_iso"):
            return from_iso(f.probe["newest_iso"])
        return None
    return from_iso(f.last_metrics.get("dest_newest_iso"))


def _newest_iso(job: Job, f: Facts) -> str | None:
    if job.has_probe:
        if f.probe and f.probe.get("ok"):
            return f.probe.get("newest_iso")
        return None
    v = f.last_metrics.get("dest_newest_iso")
    return v if isinstance(v, str) and from_iso(v) is not None else None


def _dest_count(job: Job, f: Facts) -> int | None:
    if job.has_probe:
        if f.probe and f.probe.get("ok"):
            return f.probe.get("count")
        return None
    v = _num(f.last_metrics.get("dest_count"))
    return int(v) if v is not None else None


def db_snapshot_stale(job: Job, f: Facts, now: float) -> tuple[bool | None, str]:
    """Dedup-aware destination check for ``db_snapshot`` jobs.

    Returns (stale, reason). ``stale`` is None when we cannot judge (no probe
    yet). An old newest object is fine when the DB has not changed since the
    last push — proven by the state-file sha (what was last pushed) equalling
    the heartbeat's ``db_sha256`` (what the DB is now). Without a heartbeat
    sha, fall back to the script's own push timestamp: if it claims a push
    newer than anything on Drive, Drive is stale.
    """
    newest = _newest_epoch(job, f)
    if newest is None:
        return None, "no destination probe yet"
    age = now - newest
    window = job.dest_fresh_s or 0
    if age <= window:
        return False, "newest object is recent"
    state_sha = (f.probe or {}).get("state_sha")
    hb_sha = f.last_metrics.get("db_sha256")
    if state_sha and isinstance(hb_sha, str) and hb_sha:
        if state_sha.strip().lower() == hb_sha.strip().lower():
            return False, "DB unchanged since last push (sha match)"
        return True, ("newest Drive object is old and the DB has changed since "
                      "it was pushed")
    push_epoch = (f.probe or {}).get("state_push_epoch")
    if push_epoch is not None:
        if float(push_epoch) > newest + 3600:
            return True, ("backup script recorded a push newer than anything "
                          "on Drive")
        return False, "Drive matches the script's last recorded push"
    return False, "newest object is old but no sha/push state to compare"


def lag_info(job: Job, f: Facts, now: float) -> dict | None:
    """Lag block for the JSON + card. Always present for manual jobs; present
    for other kinds only when lag metrics were reported."""
    bytes_ = _first_num(f.last_metrics, LAG_BYTES_KEYS)
    files = _first_num(f.last_metrics, LAG_FILES_KEYS)
    age_s = None
    if f.last_success:
        ts = from_iso(f.last_success.get("received_at"))
        if ts is not None:
            age_s = max(0, int(now - ts))
    if job.kind != "manual" and bytes_ is None and files is None:
        return None
    behind_reasons = []
    if job.max_age_s is not None and age_s is not None and age_s > job.max_age_s:
        behind_reasons.append("age")
    if (job.max_lag_bytes is not None and bytes_ is not None
            and bytes_ > job.max_lag_bytes):
        behind_reasons.append("bytes")
    return {
        "bytes": int(bytes_) if bytes_ is not None else None,
        "files": int(files) if files is not None else None,
        "age_s": age_s,
        "max_bytes": job.max_lag_bytes,
        "max_age_s": job.max_age_s,
        "behind": bool(behind_reasons),
        "behind_on": behind_reasons,
    }


def dest_info(job: Job, f: Facts, now: float) -> dict:
    newest_iso = _newest_iso(job, f)
    newest = _newest_epoch(job, f)
    count = _dest_count(job, f)
    fresh: bool | None = None
    if job.kind == "db_snapshot":
        stale, _ = db_snapshot_stale(job, f, now)
        fresh = None if stale is None else (not stale)
    elif job.kind == "rclone_copy_tree":
        lag = _first_num(f.last_metrics, LAG_BYTES_KEYS)
        if lag is not None:
            fresh = lag == 0
        elif newest is not None and job.dest_fresh_s:
            fresh = (now - newest) <= job.dest_fresh_s
    elif job.kind == "drive_mirror":
        pending = _first_num(f.last_metrics, ("pending",))
        mismatch = _first_num(f.last_metrics, ("mismatch",))
        if pending is not None or mismatch is not None:
            fresh = (pending or 0) + (mismatch or 0) == 0
    elif job.kind == "manual":
        if newest is not None and job.max_age_s:
            fresh = (now - newest) <= job.max_age_s
    return {"newest": newest_iso, "count": count, "fresh": fresh,
            "probed_at": (f.probe or {}).get("probed_at") if job.has_probe else None,
            "probe_error": ((f.probe or {}).get("error")
                            if job.has_probe and f.probe and not f.probe.get("ok")
                            else None)}


def compute_state(job: Job, f: Facts, now: float) -> tuple[str, str]:
    """Return (STATE, human reason)."""
    lr = f.last_run
    if lr is None and not f.last_metrics:
        return "UNKNOWN", "never heard from"

    if job.kind == "manual":
        if lr and lr.get("status") == "fail":
            return "FAIL", f"last run failed ({lr.get('reason') or 'no reason'})"
        lag = lag_info(job, f, now) or {}
        if lag.get("behind"):
            parts = []
            if "age" in lag["behind_on"]:
                parts.append("not done within its max age")
            if "bytes" in lag["behind_on"]:
                parts.append("more bytes behind than allowed")
            return "BEHIND", "; ".join(parts)
        if lr is None:
            return "OK", "metrics within target (never run explicitly)"
        return "OK", "within target"

    # Scheduled kinds: dead-man's switch first.
    if lr is None:
        return "UNKNOWN", "metrics received but no heartbeat yet"
    received = from_iso(lr.get("received_at"))
    deadline = job.deadline_s or 0
    if received is None or now - received > deadline:
        return "LATE", (job.late_means
                        or f"no heartbeat for over {deadline}s")
    if lr.get("status") == "fail":
        return "FAIL", f"last run failed ({lr.get('reason') or 'no reason'})"

    if job.kind == "container":
        running = running_names(f.last_metrics)
        if running is not None:
            missing = [n for n in job.expect if n not in running]
            if missing:
                return "FAIL", "not running: " + ", ".join(missing)
    elif job.kind == "drive_mirror":
        pending = _first_num(f.last_metrics, ("pending",)) or 0
        mismatch = _first_num(f.last_metrics, ("mismatch",)) or 0
        if pending + mismatch > 0:
            return "BEHIND", (f"{int(pending)} pending upload(s), "
                              f"{int(mismatch)} local/cloud mismatch(es)")
    elif job.kind == "db_snapshot":
        stale, why = db_snapshot_stale(job, f, now)
        if stale:
            return "STALE_DEST", why
        if stale is False:
            return "OK", f"heartbeat on time; {why}"
    elif job.kind == "rclone_copy_tree":
        lag = _first_num(f.last_metrics, LAG_BYTES_KEYS)
        if lag is not None and lag > 0:
            return "STALE_DEST", (f"job reported ok but {int(lag)} bytes are "
                                  f"missing at the destination")
    return "OK", "heartbeat on time"
