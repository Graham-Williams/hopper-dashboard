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
         "manual", "probe")
# Kinds that run on a schedule and therefore have a dead-man's switch.
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
             "expect", "late_means"}
_PROBE_KEYS = {"rclone_path", "state_dir", "interval_s"}
_MANUAL_KEYS = {"max_age_s", "max_lag_bytes"}

# Multiplier on cadence past which a destination's newest object counts as
# old (DESIGN.md: "newest object age < cadence*N").
DEST_FRESH_MULTIPLIER = 12


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
    expect: tuple[str, ...] = field(default_factory=tuple)
    late_means: str | None = None

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
        """A manual job with no thresholds: shown, never alerted on."""
        return (self.kind == "manual" and self.max_age_s is None
                and self.max_lag_bytes is None)


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


def _pos_int(container: dict, key: str, ref: str, required: bool) -> int | None:
    if key not in container or container[key] is None:
        if required:
            raise _err(ref, f"'{key}' is required for this kind")
        return None
    val = container[key]
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        raise _err(ref, f"'{key}' must be a positive integer")
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
        raise _err(ref, "manual jobs have no schedule; drop cadence_s/grace_s")

    probe_raw = raw.get("probe")
    rclone_path = state_dir = None
    probe_interval = None
    if probe_raw is not None:
        if not isinstance(probe_raw, dict):
            raise _err(ref, "'probe' must be a mapping")
        _check_keys(probe_raw, _PROBE_KEYS, ref, "probe")
        rclone_path = _opt_str(probe_raw, "rclone_path", ref)
        state_dir = _opt_str(probe_raw, "state_dir", ref)
        probe_interval = _pos_int(probe_raw, "interval_s", ref, required=False)
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

    return Job(
        id=job_id, name=name, machine=machine, kind=kind, protects=protects,
        method=method, destination=destination, cadence_s=cadence,
        grace_s=grace, probe_rclone_path=rclone_path, probe_state_dir=state_dir,
        probe_interval_s=probe_interval,
        max_age_s=max_age, max_lag_bytes=max_lag, expect=expect,
        late_means=late_means,
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
