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

RCLONE_TIMEOUT_S = 90
MAX_ERROR_LEN = 300


@dataclass
class ProbeResult:
    ok: bool
    newest_iso: str | None = None
    count: int | None = None
    state_sha: str | None = None
    state_push_epoch: int | None = None
    error: str | None = None


class ProbeError(RuntimeError):
    pass


def run_rclone_lsjson(path: str, timeout: int = RCLONE_TIMEOUT_S
                      ) -> tuple[str | None, int]:
    """Return (newest ModTime as ISO, file count) for an rclone path."""
    argv = ["rclone", "lsjson", "--recursive", "--files-only",
            "--no-mimetype", path]
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


def probe_job(job: Job) -> ProbeResult:
    """Probe one job's destination. Never raises."""
    if not job.probe_rclone_path:
        return ProbeResult(ok=False, error="job has no probe configured")
    sha, epoch = read_state_dir(job.probe_state_dir)
    try:
        newest, count = run_rclone_lsjson(job.probe_rclone_path)
    except ProbeError as exc:
        return ProbeResult(ok=False, state_sha=sha, state_push_epoch=epoch,
                           error=str(exc)[:MAX_ERROR_LEN])
    except Exception as exc:  # noqa: BLE001 — probes never crash the scheduler
        log.exception("probe %s crashed", job.id)
        return ProbeResult(ok=False, state_sha=sha, state_push_epoch=epoch,
                           error=f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LEN])
    return ProbeResult(ok=True, newest_iso=newest, count=count,
                       state_sha=sha, state_push_epoch=epoch)
