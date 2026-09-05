"""pa-backup: parse the nightly backup log written by scripts/backup-personal-assistant.sh.

Log format (one line per event): ``YYYY-MM-DD HH:MM:SS <msg>``
  success → msg contains ``backup complete``
  failure → msg starts with ``ERROR:``
The script's EXIT trap guarantees a line is written on every failure, so the LAST line is the
outcome of the most recent run. We only report a line once (dedup via the state JSON).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, Optional

from probes.common import local_naive_to_iso

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)$")
STATE_KEY = "pa_backup"


@dataclass
class LogEntry:
    raw: str
    timestamp: Optional[str]  # 'YYYY-MM-DD HH:MM:SS' or None if unparseable
    message: str
    status: str  # ok | fail | unknown

    @property
    def finished_at_iso(self) -> Optional[str]:
        return local_naive_to_iso(self.timestamp) if self.timestamp else None

    @property
    def reason(self) -> str:
        if self.status == "ok":
            return "pushed"
        if self.status == "fail":
            return "error"
        return "unparseable-log"


def classify_message(msg: str) -> str:
    m = msg.strip()
    if m.startswith("ERROR:"):
        return "fail"
    if "backup complete" in m:
        return "ok"
    return "unknown"


def parse_line(line: str) -> Optional[LogEntry]:
    """Parse one log line. Returns None for an empty/whitespace line. A line without the
    timestamp prefix is returned with timestamp=None and status='unknown' (garbage is still a
    signal — the log was tampered with or the script changed format)."""
    raw = line.rstrip("\r\n")
    if not raw.strip():
        return None
    m = LINE_RE.match(raw)
    if not m:
        return LogEntry(raw=raw, timestamp=None, message=raw.strip(), status="unknown")
    ts, msg = m.group(1), m.group(2).strip()
    return LogEntry(raw=raw, timestamp=ts, message=msg, status=classify_message(msg))


def last_nonempty_line(path: str, max_tail_bytes: int = 65536) -> Optional[str]:
    """Return the last non-empty line of a (possibly large) log file, or None if the file is
    missing/empty. Reads only the tail."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size == 0:
        return None
    with open(path, "rb") as fh:
        fh.seek(max(0, size - max_tail_bytes))
        tail = fh.read().decode("utf-8", "replace")
    for line in reversed(tail.splitlines()):
        if line.strip():
            return line
    return None


def read_last_entry(path: str) -> Optional[LogEntry]:
    line = last_nonempty_line(path)
    return parse_line(line) if line is not None else None


def is_new(entry: LogEntry, state: Dict[str, object]) -> bool:
    """True if this log line has not been reported yet (dedup key = the raw line)."""
    prev = state.get(STATE_KEY) if isinstance(state, dict) else None
    if not isinstance(prev, dict):
        return True
    return prev.get("last_reported_line") != entry.raw


def mark_reported(entry: LogEntry, state: Dict[str, object], reported_at_iso: str) -> Dict[str, object]:
    state = dict(state)
    state[STATE_KEY] = {"last_reported_line": entry.raw, "reported_at": reported_at_iso}
    return state


def entry_to_ping_fields(entry: LogEntry) -> Dict[str, object]:
    """Fields for build_ping(): status/finished_at/reason/note. 'unknown' lines are reported as
    fail so a corrupted log surfaces instead of hiding behind a stale 'ok'."""
    status = "ok" if entry.status == "ok" else "fail"
    fields: Dict[str, object] = {"status": status, "reason": entry.reason, "note": entry.message}
    if entry.timestamp:
        fields["finished_at"] = entry.finished_at_iso
    return fields
