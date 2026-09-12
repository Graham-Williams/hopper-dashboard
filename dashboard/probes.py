"""Destination probes that run inside the container.

Only the box's own ``gdrive:`` remote is reachable from here (see DESIGN.md
"SCOPE"), so the in-container probe is deliberately small: ``rclone lsjson
--recursive`` on a folder → newest ModTime + object count, plus the backup
script's state files (``last_drive.sha256`` / ``last_drive_push.epoch``) from a
``:ro`` bind mount. Everything else (Mac-only destinations, Drive mirror
state) arrives as heartbeat metrics.

``rclone`` is invoked with an explicit argv (never a shell) and a hard timeout;
a probe can fail but must never crash the scheduler.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass

from .db import from_iso, to_iso
from .registry import Job

log = logging.getLogger(__name__)

# Default only; the real value comes from Settings.rclone_timeout_s (env
# RCLONE_TIMEOUT_S) and is passed in by Core.run_probe_cycle. 90 s was not
# enough for `gdrive:Backups` (~1000 objects): Drive pages a recursive listing
# 1000 objects at a time and rclone's own low-level retries back off on
# rateLimitExceeded, so the call routinely ran past the limit and the timeout
# itself became the commonest probe failure.
RCLONE_TIMEOUT_S = 240
MAX_ERROR_LEN = 300

# Substrings (matched case-insensitively against the error text) that mean
# "Drive pushed back / we ran out of time", not "the destination is wrong".
# Quota and timeout errors clear on their own; a missing file does not.
TRANSIENT_MARKERS = (
    "ratelimitexceeded",        # incl. userRateLimitExceeded
    "rate limit",
    "quotaexceeded",
    "too many requests",
    "timed out",
    "timeout",
    "deadline exceeded",
    "429",
    "500 internal error",
    "503",
    "backenderror",
    "connection reset",
    "temporarily unavailable",
)
TRANSIENT_LABEL = "transient (Drive quota/timeout)"


def is_transient_error(text: str | None) -> bool:
    """True when an error text looks like a rate limit / timeout rather than a
    real problem with the destination."""
    if not text:
        return False
    low = text.lower()
    return any(m in low for m in TRANSIENT_MARKERS)


@dataclass
class ProbeResult:
    ok: bool
    newest_iso: str | None = None
    count: int | None = None
    state_sha: str | None = None
    state_push_epoch: int | None = None
    error: str | None = None
    # Set on a failure whose error text matches TRANSIENT_MARKERS.
    transient: bool = False


class ProbeError(RuntimeError):
    pass


def run_rclone_lsjson(path: str, timeout: int | None = None
                      ) -> tuple[str | None, int]:
    """Return (newest ModTime as ISO, file count) for an rclone path."""
    timeout = RCLONE_TIMEOUT_S if timeout is None else max(5, int(timeout))
    # "--" ends option parsing so a path from jobs.yml can never be read as a flag.
    argv = ["rclone", "lsjson", "--recursive", "--files-only",
            "--no-mimetype", "--", path]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise ProbeError("rclone binary not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"rclone timed out after {timeout}s") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        tail = err[-1] if err else "no stderr"
        raise ProbeError(f"rclone exit {proc.returncode}: {tail[:MAX_ERROR_LEN]}")
    try:
        items = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        raise ProbeError("rclone returned unparseable JSON") from exc
    return summarize_listing(items)


def summarize_listing(items) -> tuple[str | None, int]:
    newest: float | None = None
    count = 0
    for it in items:
        if not isinstance(it, dict) or it.get("IsDir"):
            continue
        count += 1
        ts = from_iso(it.get("ModTime"))
        if ts is not None and (newest is None or ts > newest):
            newest = ts
    return (to_iso(newest) if newest is not None else None), count


def _read_small(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read(256).strip()
    except OSError:
        return None


def read_state_dir(state_dir: str | None) -> tuple[str | None, int | None]:
    """Read the backup script's ``last_drive.sha256`` and
    ``last_drive_push.epoch`` if the directory is mounted."""
    if not state_dir or not os.path.isdir(state_dir):
        return None, None
    sha = _read_small(os.path.join(state_dir, "last_drive.sha256"))
    if sha:
        sha = sha.split()[0].lower()
        if not all(c in "0123456789abcdef" for c in sha) or len(sha) != 64:
            sha = None
    epoch_raw = _read_small(os.path.join(state_dir, "last_drive_push.epoch"))
    epoch: int | None = None
    if epoch_raw:
        try:
            epoch = int(float(epoch_raw.split()[0]))
        except ValueError:
            epoch = None
    return sha, epoch


def _failed(sha, epoch, raw: str) -> ProbeResult:
    """A failed ProbeResult, with a transient error labelled in its own text so
    "Drive rate-limited us" never reads like "the destination is missing files"
    on the card, in the job page's probe table, or in the self-job's note."""
    transient = is_transient_error(raw)
    text = f"{TRANSIENT_LABEL}: {raw}" if transient else raw
    return ProbeResult(ok=False, state_sha=sha, state_push_epoch=epoch,
                       error=text[:MAX_ERROR_LEN], transient=transient)


def probe_job(job: Job, timeout: int | None = None) -> ProbeResult:
    """Probe one job's destination. Never raises."""
    if not job.probe_rclone_path:
        return ProbeResult(ok=False, error="job has no probe configured")
    sha, epoch = read_state_dir(job.probe_state_dir)
    try:
        newest, count = run_rclone_lsjson(job.probe_rclone_path, timeout)
    except ProbeError as exc:
        return _failed(sha, epoch, str(exc))
    except Exception as exc:  # noqa: BLE001 — probes never crash the scheduler
        log.exception("probe %s crashed", job.id)
        return _failed(sha, epoch, f"{type(exc).__name__}: {exc}")
    return ProbeResult(ok=True, newest_iso=newest, count=count,
                       state_sha=sha, state_push_epoch=epoch)
