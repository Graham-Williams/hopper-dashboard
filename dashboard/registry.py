"""Job registry: load + strictly validate ``jobs.yml``.

The registry is the only source of *which* jobs exist. Ingest refuses pings for
undeclared ids (404), so a typo in a heartbeat URL can never create a phantom
"healthy" job. Validation fails loudly at startup with the offending job id and
field so a bad edit to ``jobs.yml`` is caught by the container refusing to come
up, not by a card quietly never turning LATE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

ID_RE = re.compile(r"^[a-z0-9-]+$")
MACHINES = ("box", "mac")
KINDS = ("db_snapshot", "rclone_copy_tree", "drive_mirror", "container",
         "manual", "probe", "disk")
# Kinds that run on a schedule and therefore have a dead-man's switch.
# ``disk`` is deliberately NOT here: it is a gauge, not a job, and the machine's
# own ``probe`` job (mac-probe / box-containers) is already the liveness signal
# that the disk metrics ride in on — a second dead-man's switch for the same
# silence would only double the alerts.
SCHEDULED_KINDS = ("db_snapshot", "rclone_copy_tree", "drive_mirror",
                   "container", "probe")
STATES = ("OK", "LATE", "FAIL", "STALE_DEST", "BEHIND", "UNKNOWN")
# Kinds that may carry a ``probe`` block (the container lists the destination
# itself). Required for db_snapshot; optional for copy trees and manual jobs
# whose destination the box's read-only remote can see — when present, the
# card's newest-object time + count come from the box instead of the Mac's
# heartbeat metrics (which carry a count but no newest time).
PROBEABLE_KINDS = ("db_snapshot", "rclone_copy_tree", "manual")

_TOP_KEYS = {"id", "name", "machine", "kind", "protects", "method",
             "destination", "cadence_s", "grace_s", "probe", "manual",
             "disk", "expect", "late_means", "alert_after_s", "alert"}
_PROBE_KEYS = {"rclone_path", "state_dir", "interval_s"}
_MANUAL_KEYS = {"max_age_s", "max_lag_bytes"}
_DISK_KEYS = {"min_free_bytes", "max_used_pct"}

# A used-percentage threshold is a percentage: anything above this is a typo
# (bytes pasted into the wrong field), not a looser threshold.
MAX_USED_PCT = 100

# Multiplier on cadence past which a destination's newest object counts as
# old (DESIGN.md: "newest object age < cadence*N").
DEST_FRESH_MULTIPLIER = 12

# ---------------------------------------------------------------------------
# Alert policy (DESIGN.md "Alerting rules")
#
# How long a job must be CONTINUOUSLY not-OK before ONE ntfy alert is sent,
# when jobs.yml declares nothing at all. Deliberately conservative: the failure
# mode of the threshold layer must be a LATE page, never a silent one, so an
# undeclared job pages a day late rather than never.
DEFAULT_ALERT_AFTER_S = 86400
# Upper bound, enforced as loudly as every other schema rule. Magnitude is the
# one hostile input this validator would otherwise accept: `alert_after_s:
# 864000000` parses as a perfectly good non-negative int and silently means
# "never page". One extra digit is the whole failure, so a value past the point
# of usefulness is a typo, not a policy — say `alert: never` if you mean never.
MAX_ALERT_AFTER_S = 2_592_000   # 30 days
# The same rule for the other magnitude the file accepts. `probe.interval_s:
# 18000000` means "list the destination once at boot and then never again" —
# 208 days — and it is INVISIBLE, because a cycle with nothing due still records
# `dashboard-probes` as ok and the card keeps whatever the last probe said. The
# semantic cap below (half the freshness window) only covers `db_snapshot`; this
# one covers the two kinds DEPLOY.md §1d tells the operator to hand-edit,
# `rclone_copy_tree` and `manual`, which had no magnitude bound at all. A day is
# already far past useful for a destination check.
MAX_PROBE_INTERVAL_S = 86_400   # 24 hours
# The only value `alert:` accepts today — an explicit opt-out for jobs whose
# not-OK states are lag metrics nobody should be woken for.
ALERT_NEVER = "never"
# Why a job ended up with the policy it has, surfaced on /api/v1/status so the
# resolution is inspectable rather than inferred from the file.
ALERT_SOURCE_EXPLICIT = "alert_after_s"     # the job declared a threshold
ALERT_SOURCE_NEVER = "alert"                # the job declared `alert: never`
ALERT_SOURCE_INFORMATIONAL = "informational"  # a gauge/manual job with no thresholds
ALERT_SOURCE_DEFAULT = "default"            # nothing declared → DEFAULT_ALERT_AFTER_S


def _is_informational(kind: str, max_age_s: int | None, max_lag_bytes: int | None,
                      min_free_bytes: int | None, max_used_pct: int | None) -> bool:
    """A manual or disk job with no thresholds of its own: shown, never alerted
    on. Shared by :attr:`Job.informational` and the parse-time alert-policy
    resolution so the two can never disagree."""
    if kind == "manual":
        return max_age_s is None and max_lag_bytes is None
    if kind == "disk":
        return min_free_bytes is None and max_used_pct is None
    return False


class RegistryError(ValueError):
    """Raised when jobs.yml is missing, unparseable, or violates the schema."""


@dataclass(frozen=True)
class Job:
    id: str
    name: str
    machine: str
    kind: str
    protects: str
    method: str
    destination: str | None = None
    cadence_s: int | None = None
    grace_s: int | None = None
    probe_rclone_path: str | None = None
    probe_state_dir: str | None = None
    probe_interval_s: int | None = None
    max_age_s: int | None = None
    max_lag_bytes: int | None = None
    min_free_bytes: int | None = None
    max_used_pct: int | None = None
    expect: tuple[str, ...] = field(default_factory=tuple)
    late_means: str | None = None
    # Resolved alert policy — never inferred at use time (see parse_job).
    # ``alert_never`` wins; ``alert_after_s`` is meaningless when it is set.
    alert_after_s: int = DEFAULT_ALERT_AFTER_S
    alert_never: bool = False
    alert_source: str = ALERT_SOURCE_DEFAULT

    @property
    def scheduled(self) -> bool:
        return self.kind in SCHEDULED_KINDS

    @property
    def deadline_s(self) -> int | None:
        """Seconds of silence after which the job is LATE."""
        if self.cadence_s is None or self.grace_s is None:
            return None
        return self.cadence_s + self.grace_s

    @property
    def dest_fresh_s(self) -> int | None:
        if self.cadence_s is None:
            return None
        return self.cadence_s * DEST_FRESH_MULTIPLIER

    @property
    def has_probe(self) -> bool:
        return bool(self.probe_rclone_path)

    @property
    def informational(self) -> bool:
        """A manual or disk job with no thresholds: shown, never alerted on.

        This used to be a promise nothing kept — no caller consulted it, so an
        "informational" job pushed to ntfy on every transition like any other.
        It is now real: an informational job with no explicit ``alert``/
        ``alert_after_s`` resolves to ``alert_never`` at parse time (see
        :func:`parse_job`). An explicit threshold still wins over it.
        """
        return _is_informational(self.kind, self.max_age_s, self.max_lag_bytes,
                                 self.min_free_bytes, self.max_used_pct)


class Registry:
    def __init__(self, jobs: list[Job]):
        self._jobs = {j.id: j for j in jobs}
        self._order = [j.id for j in jobs]

    def __len__(self) -> int:
        return len(self._order)

    def __contains__(self, job_id: object) -> bool:
        return job_id in self._jobs

    def __iter__(self):
        return (self._jobs[i] for i in self._order)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def by_machine(self) -> dict[str, list[Job]]:
        out: dict[str, list[Job]] = {m: [] for m in MACHINES}
        for job in self:
            out[job.machine].append(job)
        return out

    def probed(self) -> list[Job]:
        return [j for j in self if j.has_probe]


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _err(job_ref: str, msg: str) -> RegistryError:
    return RegistryError(f"jobs.yml: job {job_ref}: {msg}")


def _req_str(raw: dict, key: str, ref: str, max_len: int = 200) -> str:
    val = raw.get(key)
    if not isinstance(val, str) or not val.strip():
        raise _err(ref, f"'{key}' is required and must be a non-empty string")
    if len(val) > max_len:
        raise _err(ref, f"'{key}' is longer than {max_len} characters")
    return val.strip()


def _opt_str(raw: dict, key: str, ref: str, max_len: int = 200) -> str | None:
    if key not in raw or raw[key] is None:
        return None
    val = raw[key]
    if not isinstance(val, str) or not val.strip():
        raise _err(ref, f"'{key}' must be a non-empty string when given")
    if len(val) > max_len:
        raise _err(ref, f"'{key}' is longer than {max_len} characters")
    return val.strip()


def _pos_int(container: dict, key: str, ref: str, required: bool,
             max_value: int | None = None) -> int | None:
    if key not in container or container[key] is None:
        if required:
            raise _err(ref, f"'{key}' is required for this kind")
        return None
    val = container[key]
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        raise _err(ref, f"'{key}' must be a positive integer")
    if max_value is not None and val > max_value:
        raise _err(ref, f"'{key}' is {val}, which is over the {max_value} "
                        f"second maximum — that is almost certainly a typo, "
                        f"and a probe that never runs again is invisible: the "
                        f"card keeps reporting whatever the last one saw")
    return val


def _nonneg_int(container: dict, key: str, ref: str) -> int | None:
    """Like :func:`_pos_int` but 0 is a legal value.

    Only ``alert_after_s`` uses it: ``0`` means "page as soon as this job
    leaves OK" (the pre-0.2 behaviour, still capped at ONE page per episode).
    Not recommended for a job whose destination probe can blip — that is the
    flapping this threshold layer exists to stop.
    """
    if key not in container or container[key] is None:
        return None
    val = container[key]
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        raise _err(ref, f"'{key}' must be a non-negative integer")
    if val > MAX_ALERT_AFTER_S:
        raise _err(ref, f"'{key}' is {val}, which is over the "
                        f"{MAX_ALERT_AFTER_S} second (30 day) maximum — a "
                        f"threshold that long is silence, not a policy; use "
                        f"'alert: never' if that is what you mean")
    return val


def _check_keys(raw: dict, allowed: set[str], ref: str, where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise _err(ref, f"unknown {where} key(s): {', '.join(unknown)} "
                        f"(allowed: {', '.join(sorted(allowed))})")


def parse_job(raw: Any, index: int) -> Job:
    ref = f"#{index + 1}"
    if not isinstance(raw, dict):
        raise _err(ref, "each job must be a mapping")
    job_id = raw.get("id")
    if isinstance(job_id, str) and job_id:
        ref = repr(job_id)
    _check_keys(raw, _TOP_KEYS, ref, "job")

    if not isinstance(job_id, str) or not ID_RE.match(job_id):
        raise _err(ref, "'id' must match ^[a-z0-9-]+$")
    if len(job_id) > 64:
        raise _err(ref, "'id' is longer than 64 characters")

    name = _req_str(raw, "name", ref)
    machine = _req_str(raw, "machine", ref)
    if machine not in MACHINES:
        raise _err(ref, f"'machine' must be one of {', '.join(MACHINES)}")
    kind = _req_str(raw, "kind", ref)
    if kind not in KINDS:
        raise _err(ref, f"'kind' must be one of {', '.join(KINDS)}")
    protects = _req_str(raw, "protects", ref, max_len=300)
    method = _req_str(raw, "method", ref, max_len=300)
    destination = _opt_str(raw, "destination", ref)
    late_means = _opt_str(raw, "late_means", ref)

    scheduled = kind in SCHEDULED_KINDS
    cadence = _pos_int(raw, "cadence_s", ref, required=scheduled)
    grace = _pos_int(raw, "grace_s", ref, required=scheduled)
    if not scheduled and (cadence is not None or grace is not None):
        raise _err(ref, f"{kind} jobs have no schedule; drop cadence_s/grace_s")

    # Mutual exclusion is checked on the KEYS, not their parsed values: with an
    # `is not None` test, `alert: never` + `alert_after_s: null` parsed to
    # "never" — an accidental silence that reads, in the file, like somebody
    # set a threshold.
    if "alert" in raw and "alert_after_s" in raw:
        raise _err(ref, "'alert' and 'alert_after_s' are mutually exclusive; "
                        "keep one (even when the other is null)")
    alert_after_s = _nonneg_int(raw, "alert_after_s", ref)
    alert_mode = _opt_str(raw, "alert", ref, max_len=16)
    if alert_mode is not None and alert_mode != ALERT_NEVER:
        raise _err(ref, f"'alert' must be {ALERT_NEVER!r} (the only mode "
                        f"today); use 'alert_after_s' for a threshold")

    probe_raw = raw.get("probe")
    rclone_path = state_dir = None
    probe_interval = None
    if probe_raw is not None:
        if not isinstance(probe_raw, dict):
            raise _err(ref, "'probe' must be a mapping")
        _check_keys(probe_raw, _PROBE_KEYS, ref, "probe")
        rclone_path = _opt_str(probe_raw, "rclone_path", ref)
        state_dir = _opt_str(probe_raw, "state_dir", ref)
        probe_interval = _pos_int(probe_raw, "interval_s", ref, required=False,
                                  max_value=MAX_PROBE_INTERVAL_S)
        if rclone_path is None:
            raise _err(ref, "'probe.rclone_path' is required when 'probe' is given")
        if kind not in PROBEABLE_KINDS:
            raise _err(ref, f"kind {kind} cannot have a 'probe' block")
        # db_snapshot is the one kind whose STALE_DEST verdict is computed from
        # the probe's newest-object time against cadence_s * DEST_FRESH_MULTIPLIER.
        # A probe row that is itself older than that window would report a fresh
        # destination as stale, so refuse an interval that could get close: cap it
        # at half the staleness window. Other kinds judge staleness from heartbeat
        # metrics, so a slow probe only delays a cosmetic newest/count refresh.
        if kind == "db_snapshot" and probe_interval is not None and cadence:
            cap = cadence * DEST_FRESH_MULTIPLIER // 2
            if probe_interval > cap:
                raise _err(ref, f"'probe.interval_s' must be <= {cap} for a "
                                f"db_snapshot with cadence_s {cadence} (half of "
                                f"cadence_s * {DEST_FRESH_MULTIPLIER}); a probe "
                                f"row older than the freshness window would read "
                                f"a fresh destination as STALE_DEST")
    if kind == "db_snapshot" and rclone_path is None:
        raise _err(ref, "db_snapshot requires probe.rclone_path")
    if kind == "rclone_copy_tree" and destination is None:
        raise _err(ref, "rclone_copy_tree requires 'destination'")

    manual_raw = raw.get("manual")
    max_age = max_lag = None
    if manual_raw is not None:
        if kind != "manual":
            raise _err(ref, "'manual' block is only valid for kind: manual")
        if not isinstance(manual_raw, dict):
            raise _err(ref, "'manual' must be a mapping")
        _check_keys(manual_raw, _MANUAL_KEYS, ref, "manual")
        max_age = _pos_int(manual_raw, "max_age_s", ref, required=False)
        max_lag = _pos_int(manual_raw, "max_lag_bytes", ref, required=False)

    disk_raw = raw.get("disk")
    min_free = max_used = None
    if disk_raw is not None:
        if kind != "disk":
            raise _err(ref, "'disk' block is only valid for kind: disk")
        if not isinstance(disk_raw, dict):
            raise _err(ref, "'disk' must be a mapping")
        _check_keys(disk_raw, _DISK_KEYS, ref, "disk")
        min_free = _pos_int(disk_raw, "min_free_bytes", ref, required=False)
        max_used = _pos_int(disk_raw, "max_used_pct", ref, required=False)
        if max_used is not None and max_used > MAX_USED_PCT:
            raise _err(ref, f"'max_used_pct' is a percentage; must be 1-{MAX_USED_PCT}")

    expect_raw = raw.get("expect")
    expect: tuple[str, ...] = ()
    if kind == "container":
        if (not isinstance(expect_raw, list) or not expect_raw
                or not all(isinstance(e, str) and e.strip() for e in expect_raw)):
            raise _err(ref, "container requires a non-empty 'expect' list of "
                            "container names")
        expect = tuple(e.strip() for e in expect_raw)
        if len(set(expect)) != len(expect):
            raise _err(ref, "'expect' has duplicate names")
    elif expect_raw is not None:
        raise _err(ref, "'expect' is only valid for kind: container")

    # Resolve the alert policy ONCE, here, so nothing downstream has to decide
    # what an absent key means. In precedence order:
    #   `alert: never`        → never pages
    #   `alert_after_s: N`    → pages after N seconds continuously not-OK, and
    #                           an explicit value wins even on an informational
    #                           job (that is the only way to alert on one)
    #   nothing + informational → never pages (the promise `informational` has
    #                           always made on the board and never kept)
    #   nothing otherwise     → DEFAULT_ALERT_AFTER_S, because a job nobody
    #                           thought about must page late, not never.
    informational = _is_informational(kind, max_age, max_lag, min_free, max_used)
    if alert_mode == ALERT_NEVER:
        alert_never, alert_secs, alert_source = True, DEFAULT_ALERT_AFTER_S, ALERT_SOURCE_NEVER
    elif alert_after_s is not None:
        alert_never, alert_secs, alert_source = False, alert_after_s, ALERT_SOURCE_EXPLICIT
    elif informational:
        alert_never, alert_secs, alert_source = (True, DEFAULT_ALERT_AFTER_S,
                                                 ALERT_SOURCE_INFORMATIONAL)
    else:
        alert_never, alert_secs, alert_source = (False, DEFAULT_ALERT_AFTER_S,
                                                 ALERT_SOURCE_DEFAULT)

    return Job(
        id=job_id, name=name, machine=machine, kind=kind, protects=protects,
        method=method, destination=destination, cadence_s=cadence,
        grace_s=grace, probe_rclone_path=rclone_path, probe_state_dir=state_dir,
        probe_interval_s=probe_interval,
        max_age_s=max_age, max_lag_bytes=max_lag, min_free_bytes=min_free,
        max_used_pct=max_used, expect=expect, late_means=late_means,
        alert_after_s=alert_secs, alert_never=alert_never,
        alert_source=alert_source,
    )


def parse_registry(doc: Any) -> Registry:
    if not isinstance(doc, dict) or "jobs" not in doc:
        raise RegistryError("jobs.yml: top level must be a mapping with a "
                            "'jobs' list")
    extra = sorted(set(doc) - {"jobs"})
    if extra:
        raise RegistryError(f"jobs.yml: unknown top-level key(s): "
                            f"{', '.join(extra)}")
    jobs_raw = doc["jobs"]
    if not isinstance(jobs_raw, list) or not jobs_raw:
        raise RegistryError("jobs.yml: 'jobs' must be a non-empty list")
    jobs = [parse_job(item, i) for i, item in enumerate(jobs_raw)]
    seen: set[str] = set()
    for job in jobs:
        if job.id in seen:
            raise RegistryError(f"jobs.yml: duplicate job id {job.id!r}")
        seen.add(job.id)
    return Registry(jobs)


def load_registry(path: str) -> Registry:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise RegistryError(f"jobs file not found: {path} (set JOBS_FILE; "
                            f"copy jobs.example.yml to get started)") from exc
    except yaml.YAMLError as exc:
        raise RegistryError(f"jobs.yml: YAML parse error: {exc}") from exc
    return parse_registry(doc)
