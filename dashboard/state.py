"""The per-job state machine (DESIGN.md "State machine").

Pure functions over a :class:`Facts` snapshot so every transition is unit-
testable with a fixed ``now``:

- ``OK``         last run success within cadence+grace AND (if probed) destination fresh
- ``LATE``       no heartbeat within cadence+grace (dead-man's switch)
- ``FAIL``       last heartbeat status=fail, or a container job missing an expected container
- ``STALE_DEST`` heartbeat says ok but the destination disagrees
- ``BEHIND``     manual job over its lag/age target, a Drive mirror with pending work, or a
                 ``disk`` job under its free-space floor / over its used-percent ceiling
- ``UNKNOWN``    never heard from — but only for ``cadence+grace`` after the job was
                 first registered (``jobs.created_at``); a scheduled job that has NEVER
                 pinged then goes LATE, so a mis-installed heartbeat can't stay silent forever

Precedence for scheduled kinds: LATE > FAIL > kind-specific (STALE_DEST / BEHIND) > OK.
A silent job is reported LATE even if its last word was "fail" — the silence is the
more urgent fact, and the last-run status stays visible on the card either way.

``rclone_copy_tree`` distinguishes **missing** (never uploaded → STALE_DEST) from
**differ** (edited locally since the last copy → informational lag, shown on the card).
The nightly copy always trails a busy working tree by a few edited files; only bytes that
have never reached the destination are a stale destination.
"""

from __future__ import annotations

import math

from dataclasses import dataclass, field

from .db import from_iso
from .humanize import human_gib
from .registry import Job

# Largest magnitude any metric may have (mirrors ingest.INT_ABS_MAX; see _num).
METRIC_ABS_MAX = float(2 ** 63)

# Metric keys the Mac probe / scripts may use for "how far behind".
LAG_BYTES_KEYS = ("lag_bytes", "missing_bytes")
LAG_FILES_KEYS = ("lag_files", "missing_files")
# rclone_copy_tree: only never-uploaded bytes make the destination stale.
MISSING_BYTES_KEYS = ("missing_bytes",)
MISSING_FILES_KEYS = ("missing_files",)
DIFFER_BYTES_KEYS = ("differ_bytes",)
DIFFER_FILES_KEYS = ("differ_files",)
# disk capacity. The probes post the ``disk_*`` pair; the bare spelling is accepted
# so an ad-hoc script's metrics are understood too (same rule as LAG_BYTES_KEYS).
DISK_FREE_KEYS = ("disk_free_bytes", "free_bytes")
DISK_TOTAL_KEYS = ("disk_total_bytes", "total_bytes")
# How old a capacity reading may get before the gauge itself is the finding.
#
# A `disk` job has no cadence (see SCHEDULED_KINDS) because its machine's probe
# job carries the same silence — but that only covers TOTAL silence. The box's
# disk probe can stop running on its own (renamed, moved, interpreter gone: the
# wrapper records a non-zero rc in the journal and that is all) while
# box-containers, posted by a different script on the same timer, keeps saying
# OK. Nothing is posted then, so the fail branch below cannot fire; the reading's
# own age is the only signal left.
#
# 48 h is deliberately generous. The feeders run every 5 min (box) and hourly
# (Mac), so this is ~576 / ~48 consecutive misses — unambiguous. It also sits
# above mac-probe's ~15 h LATE deadline, so a Mac merely switched off for a day
# does not page twice for one fact (the machine-offline rule in services.py
# mutes the sibling alert only while the probe job is itself LATE; a 24 h
# ceiling would reintroduce exactly the double-alerting that rule avoids).
DISK_METRIC_MAX_AGE_S = 48 * 3600


@dataclass
class Facts:
    last_run: dict | None = None        # newest runs row (any status)
    last_success: dict | None = None    # newest ok/skipped run
    last_metrics: dict = field(default_factory=dict)
    last_metrics_at: str | None = None
    probe: dict | None = None           # newest probes row for this job
    created_at: str | None = None       # jobs.created_at (first seen in jobs.yml)


def _num(value) -> float | None:
    """Coerce a metric to a finite, plausibly-sized float, or None.

    Strings are accepted (probes may send numbers as text) but ``"nan"``,
    ``"inf"`` and ``"1e999"`` parse to non-finite floats that later blow up
    ``int()`` in the state computation and 500 every reader until the metric
    is overwritten — so anything non-finite is treated as absent.

    Magnitude is capped for the same reason: ``ingest`` rejects a numeric
    ``1e308``, but a metric sent as the *string* ``"1e308"`` is a legal short
    string, and ``int()`` on it yields a 309-digit integer that lands in
    ``/api/v1/status`` and ~310 characters in the gauge's heading. Nothing real
    — bytes, files, seconds — comes near 9.2e18, so treat it as absent too.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        try:
            f = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return f if math.isfinite(f) and abs(f) <= METRIC_ABS_MAX else None


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


def _probe_ok(job: Job, f: Facts) -> bool:
    """A probed job with a good box listing to read newest/count from. Until the
    first successful probe (or after a failed one) the heartbeat metrics stand in
    — so a copy tree keeps its Mac-reported ``dest_count`` and a db_snapshot
    (whose heartbeat carries no dest_* metrics) simply has nothing to judge."""
    return bool(job.has_probe and f.probe and f.probe.get("ok"))


def _newest_epoch(job: Job, f: Facts) -> float | None:
    if _probe_ok(job, f):
        return from_iso(f.probe.get("newest_iso"))
    return from_iso(f.last_metrics.get("dest_newest_iso"))


def _newest_iso(job: Job, f: Facts) -> str | None:
    if _probe_ok(job, f):
        return f.probe.get("newest_iso")
    v = f.last_metrics.get("dest_newest_iso")
    return v if isinstance(v, str) and from_iso(v) is not None else None


def _dest_count(job: Job, f: Facts) -> int | None:
    if _probe_ok(job, f):
        return f.probe.get("count")
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


def copy_tree_missing(metrics: dict) -> tuple[float | None, float | None]:
    """(missing_bytes, missing_files) for an rclone_copy_tree job — never
    uploaded, as opposed to `differ` (edited since the last copy)."""
    return (_first_num(metrics, MISSING_BYTES_KEYS),
            _first_num(metrics, MISSING_FILES_KEYS))


def copy_tree_stale(metrics: dict) -> bool | None:
    """None = can't judge (no missing_* reported), else True when anything is
    missing at the destination. ``differ`` never makes the destination stale."""
    b, n = copy_tree_missing(metrics)
    if b is None and n is None:
        return None
    return (b or 0) > 0 or (n or 0) > 0


def lag_info(job: Job, f: Facts, now: float) -> dict | None:
    """Lag block for the JSON + card. Always present for manual jobs; present
    for other kinds only when lag metrics were reported. For
    ``rclone_copy_tree`` the headline ``bytes``/``files`` are the MISSING
    (never-uploaded) figures and ``differ_*`` carry the informational lag."""
    differ_bytes = differ_files = None
    if job.kind == "rclone_copy_tree":
        bytes_, files = copy_tree_missing(f.last_metrics)
        differ_bytes = _first_num(f.last_metrics, DIFFER_BYTES_KEYS)
        differ_files = _first_num(f.last_metrics, DIFFER_FILES_KEYS)
    else:
        bytes_ = _first_num(f.last_metrics, LAG_BYTES_KEYS)
        files = _first_num(f.last_metrics, LAG_FILES_KEYS)
    age_s = None
    if f.last_success:
        ts = from_iso(f.last_success.get("received_at"))
        if ts is not None:
            age_s = max(0, int(now - ts))
    if (job.kind != "manual" and bytes_ is None and files is None
            and differ_bytes is None and differ_files is None):
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
        "differ_bytes": int(differ_bytes) if differ_bytes is not None else None,
        "differ_files": int(differ_files) if differ_files is not None else None,
        "age_s": age_s,
        "max_bytes": job.max_lag_bytes,
        "max_age_s": job.max_age_s,
        "behind": bool(behind_reasons),
        "behind_on": behind_reasons,
    }


def disk_info(job: Job, f: Facts) -> dict | None:
    """Capacity block for the JSON + card, or None for any kind but ``disk``.

    ``used_pct`` is simply ``(total - available) / total`` from what ``statvfs``
    reported. The free BYTES match ``df``'s Avail exactly; the PERCENTAGE will
    not match ``df``'s (on macOS/APFS, measured: 79.5% here vs 78% there — ``df``
    divides by a larger free figure that ``statvfs`` never hands Python). That is
    expected, not a rounding bug: a 90% ceiling here trips around 88.5% as ``df``
    prints it. See ``probes/common.disk_free``.

    It is None when the total is absent or zero: a container bind-mount and a
    statvfs on a path that went away both report 0 blocks, and dividing by that
    would 500 the whole board.
    """
    if job.kind != "disk":
        return None
    free = _first_num(f.last_metrics, DISK_FREE_KEYS)
    total = _first_num(f.last_metrics, DISK_TOTAL_KEYS)
    used = used_pct = None
    if free is not None and total is not None and total > 0:
        used = max(0.0, total - free)
        used_pct = round(used / total * 100, 1)
    low_on = []
    if (job.min_free_bytes is not None and free is not None
            and free < job.min_free_bytes):
        low_on.append("free")
    if (job.max_used_pct is not None and used_pct is not None
            and used_pct > job.max_used_pct):
        low_on.append("used_pct")
    return {
        "measured_at": f.last_metrics_at,
        "free_bytes": int(free) if free is not None else None,
        "total_bytes": int(total) if total is not None else None,
        "used_bytes": int(used) if used is not None else None,
        "used_pct": used_pct,
        "min_free_bytes": job.min_free_bytes,
        "max_used_pct": job.max_used_pct,
        "low": bool(low_on),
        "low_on": low_on,
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
        stale = copy_tree_stale(f.last_metrics)
        if stale is not None:
            fresh = not stale
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


def _never_pinged_late(job: Job, f: Facts, now: float) -> bool:
    """A scheduled job with no run yet is UNKNOWN only until cadence+grace
    has elapsed since it was registered; after that its silence is LATE
    (a heartbeat that was never wired up must not look merely "new" forever)."""
    if not job.scheduled or job.deadline_s is None:
        return False
    created = from_iso(f.created_at)
    return created is not None and now - created > job.deadline_s


def compute_state(job: Job, f: Facts, now: float) -> tuple[str, str]:
    """Return (STATE, human reason)."""
    lr = f.last_run
    if lr is None and _never_pinged_late(job, f, now):
        return "LATE", (job.late_means
                        or f"never pinged, and registered more than {job.deadline_s}s ago"
                        " — is the heartbeat installed?")
    if lr is None and not f.last_metrics:
        return "UNKNOWN", "never heard from"

    if job.kind == "disk":
        # A gauge, not a job: judged on the newest metrics, with no cadence-based
        # dead-man's switch (see SCHEDULED_KINDS). Two things still outrank the
        # figures, in this order:
        #   1. the probe saying it could not read the filesystem at all, and
        #   2. the figures being too old to mean anything (DISK_METRIC_MAX_AGE_S).
        d = disk_info(job, f) or {}
        metrics_at = from_iso(f.last_metrics_at)
        # Newest word wins. Mirrors the `manual` branch's fail check, but a disk
        # probe posts success as a `metric` ping — which is NOT a run — so the
        # failed run stays the newest `runs` row for ever; without comparing it
        # against last_metrics_at one transient statvfs error would pin the card
        # to FAIL while healthy readings flowed in behind it.
        failed_at = (from_iso(lr.get("received_at"))
                     if lr and lr.get("status") == "fail" else None)
        if failed_at is not None and (metrics_at is None or failed_at >= metrics_at):
            why = lr.get("note") or lr.get("reason") or "no reason"
            return "FAIL", f"capacity unreadable ({str(why)[:120]})"
        if d.get("free_bytes") is None:
            return "UNKNOWN", "no disk metrics reported yet"
        if metrics_at is not None and now - metrics_at > DISK_METRIC_MAX_AGE_S:
            # LATE, not FAIL: this IS a dead-man's switch, and reusing LATE gets
            # the machine-offline alert suppression in services.py for free.
            return "LATE", (f"no capacity reading in over "
                            f"{DISK_METRIC_MAX_AGE_S // 3600}h — whatever feeds "
                            f"this gauge has stopped (probe moved or renamed?) or "
                            f"the machine has been off that long")
        if d["low"]:
            parts = []
            if "free" in d["low_on"]:
                parts.append(f"only {human_gib(d['free_bytes'])} free, below the "
                             f"{human_gib(d['min_free_bytes'])} floor")
            if "used_pct" in d["low_on"]:
                parts.append(f"{d['used_pct']}% used, over the "
                             f"{d['max_used_pct']}% ceiling")
            return "BEHIND", "; ".join(parts)
        used = f"{d['used_pct']}% used, " if d["used_pct"] is not None else ""
        return "OK", f"{used}{human_gib(d['free_bytes'])} free"

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
        if copy_tree_stale(f.last_metrics):
            mb, mf = copy_tree_missing(f.last_metrics)
            return "STALE_DEST", (f"job reported ok but {int(mf or 0)} file(s) / "
                                  f"{int(mb or 0)} bytes have never reached the destination")
        df = _first_num(f.last_metrics, DIFFER_FILES_KEYS)
        if df:
            return "OK", (f"heartbeat on time; {int(df)} file(s) edited since the "
                          f"last copy (normal lag, not stale)")
    return "OK", "heartbeat on time"
