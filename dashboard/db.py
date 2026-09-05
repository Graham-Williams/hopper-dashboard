"""SQLite store: schema, connections, and all queries.

Two gunicorn processes (read :8080, ingest :8081) share one file, so the
database runs in WAL mode with a generous ``busy_timeout``. Only the ingest
process writes; the read process only ever SELECTs. Timestamps are stored as
ISO-8601 UTC strings with second precision (``2026-09-04T12:00:00Z``) — they
sort lexicographically and read well in the raw DB.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Iterable

ISO = "%Y-%m-%dT%H:%M:%SZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    state           TEXT NOT NULL DEFAULT 'UNKNOWN',
    since           TEXT,
    state_reason    TEXT,
    last_metrics    TEXT,               -- JSON object, shallow-merged over time
    last_metrics_at TEXT,
    updated_at      TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    received_at TEXT NOT NULL,          -- server clock; drives the dead-man's switch
    started_at  TEXT,
    finished_at TEXT,
    status      TEXT NOT NULL,          -- ok | fail | skipped
    reason      TEXT,
    exit_code   INTEGER,
    note        TEXT,
    metrics     TEXT,                   -- JSON object as sent with this run
    source      TEXT NOT NULL DEFAULT 'ping'   -- ping | form | scheduler
);
CREATE INDEX IF NOT EXISTS runs_job_recv ON runs (job_id, received_at DESC);
CREATE TABLE IF NOT EXISTS probes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           TEXT NOT NULL,
    probed_at        TEXT NOT NULL,
    ok               INTEGER NOT NULL,
    newest_iso       TEXT,
    count            INTEGER,
    state_sha        TEXT,
    state_push_epoch INTEGER,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS probes_job_at ON probes (job_id, probed_at DESC);
CREATE TABLE IF NOT EXISTS state_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state   TEXT NOT NULL,
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS sc_job_at ON state_changes (job_id, changed_at DESC);
"""


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #

def to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(ISO)


def from_iso(text: str | None) -> float | None:
    """Parse ISO-8601 (with or without offset / fractional seconds) to epoch.
    Returns None for empty or unparseable input."""
    if not text:
        return None
    s = text.strip()
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def now_iso() -> str:
    return to_iso(time.time())


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #

def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "state_reason" not in cols:  # pre-0.1 databases
        conn.execute("ALTER TABLE jobs ADD COLUMN state_reason TEXT")


def ensure_jobs(conn: sqlite3.Connection, job_ids: Iterable[str]) -> None:
    """Make sure every declared job has a state row (UNKNOWN until heard from)."""
    now = now_iso()
    with conn:
        for jid in job_ids:
            conn.execute(
                "INSERT OR IGNORE INTO jobs (id, state, since, updated_at) "
                "VALUES (?, 'UNKNOWN', ?, ?)", (jid, now, now))


# --------------------------------------------------------------------------- #
# Writes (ingest process only)
# --------------------------------------------------------------------------- #

def insert_run(conn: sqlite3.Connection, job_id: str, *, received_at: str,
               status: str, started_at: str | None = None,
               finished_at: str | None = None, reason: str | None = None,
               exit_code: int | None = None, note: str | None = None,
               metrics: dict | None = None, source: str = "ping") -> int:
    cur = conn.execute(
        "INSERT INTO runs (job_id, received_at, started_at, finished_at, status,"
        " reason, exit_code, note, metrics, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (job_id, received_at, started_at, finished_at, status, reason,
         exit_code, note, json.dumps(metrics) if metrics else None, source))
    return int(cur.lastrowid)


def merge_metrics(conn: sqlite3.Connection, job_id: str, metrics: dict,
                  at: str) -> dict:
    """Shallow-merge ``metrics`` into the job's ``last_metrics`` and return the
    merged dict. Merging (not replacing) lets a bare systemd ``ExecStopPost``
    ping coexist with the richer script heartbeat that carries ``db_sha256``,
    and lets the Mac probe's lag metrics coexist with a box-side heartbeat."""
    row = conn.execute("SELECT last_metrics FROM jobs WHERE id=?",
                       (job_id,)).fetchone()
    current = _load_json(row["last_metrics"]) if row else {}
    current.update(metrics)
    conn.execute(
        "UPDATE jobs SET last_metrics=?, last_metrics_at=?, updated_at=? "
        "WHERE id=?", (json.dumps(current), at, at, job_id))
    return current


def insert_probe(conn: sqlite3.Connection, job_id: str, *, probed_at: str,
                 ok: bool, newest_iso: str | None = None,
                 count: int | None = None, state_sha: str | None = None,
                 state_push_epoch: int | None = None,
                 error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO probes (job_id, probed_at, ok, newest_iso, count, "
        "state_sha, state_push_epoch, error) VALUES (?,?,?,?,?,?,?,?)",
        (job_id, probed_at, 1 if ok else 0, newest_iso, count, state_sha,
         state_push_epoch, error))


def set_state(conn: sqlite3.Connection, job_id: str, new_state: str,
              at: str, reason: str | None = None) -> str | None:
    """Persist a computed state. Returns the previous state if it changed
    (and records the transition), else None."""
    row = conn.execute("SELECT state FROM jobs WHERE id=?",
                       (job_id,)).fetchone()
    prev = row["state"] if row else "UNKNOWN"
    if prev == new_state:
        conn.execute("UPDATE jobs SET updated_at=?, state_reason=? WHERE id=?",
                     (at, reason, job_id))
        return None
    conn.execute(
        "UPDATE jobs SET state=?, since=?, state_reason=?, updated_at=? WHERE id=?",
        (new_state, at, reason, at, job_id))
    conn.execute(
        "INSERT INTO state_changes (job_id, changed_at, from_state, to_state, "
        "reason) VALUES (?,?,?,?,?)", (job_id, at, prev, new_state, reason))
    return prev


def prune(conn: sqlite3.Connection, keep_runs: int = 2000,
          keep_probes: int = 2000) -> None:
    """Bound table growth per job (a 5-minute job writes ~105k rows/year)."""
    for table, keep in (("runs", keep_runs), ("probes", keep_probes)):
        conn.execute(
            f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} t "
            f"WHERE (SELECT COUNT(*) FROM {table} u WHERE u.job_id=t.job_id "
            f"AND u.id>t.id) >= ?)", (keep,))


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def _load_json(text: str | None) -> dict:
    if not text:
        return {}
    try:
        val = json.loads(text)
    except ValueError:
        return {}
    return val if isinstance(val, dict) else {}


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    if "metrics" in d:
        d["metrics"] = _load_json(d["metrics"])
    if "last_metrics" in d:
        d["last_metrics"] = _load_json(d["last_metrics"])
    if "ok" in d:
        d["ok"] = bool(d["ok"])
    return d


def job_row(conn: sqlite3.Connection, job_id: str) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM jobs WHERE id=?",
                                    (job_id,)).fetchone())


def all_job_rows(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["id"]: row_to_dict(r) for r in
            conn.execute("SELECT * FROM jobs").fetchall()}


def last_run(conn: sqlite3.Connection, job_id: str) -> dict | None:
    return row_to_dict(conn.execute(
        "SELECT * FROM runs WHERE job_id=? ORDER BY received_at DESC, id DESC "
        "LIMIT 1", (job_id,)).fetchone())


def last_success(conn: sqlite3.Connection, job_id: str) -> dict | None:
    """Most recent run that completed without failing. ``skipped`` counts: a
    backup that ran, found the DB unchanged and skipped the push did its job."""
    return row_to_dict(conn.execute(
        "SELECT * FROM runs WHERE job_id=? AND status IN ('ok','skipped') "
        "ORDER BY received_at DESC, id DESC LIMIT 1", (job_id,)).fetchone())


def recent_runs(conn: sqlite3.Connection, job_id: str,
                limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM runs WHERE job_id=? ORDER BY received_at DESC, id DESC "
        "LIMIT ?", (job_id, limit)).fetchall()
    return [row_to_dict(r) for r in rows]


def last_probe(conn: sqlite3.Connection, job_id: str) -> dict | None:
    return row_to_dict(conn.execute(
        "SELECT * FROM probes WHERE job_id=? ORDER BY probed_at DESC, id DESC "
        "LIMIT 1", (job_id,)).fetchone())


def recent_state_changes(conn: sqlite3.Connection, job_id: str,
                         limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM state_changes WHERE job_id=? ORDER BY changed_at DESC, "
        "id DESC LIMIT ?", (job_id, limit)).fetchall()
    return [dict(r) for r in rows]


def run_history_all(conn: sqlite3.Connection, limit_per_job: int = 30
                    ) -> dict[str, list[dict]]:
    """Last N runs for every job in one query (for the board's history strips)."""
    rows = conn.execute(
        "SELECT job_id, status, received_at FROM ("
        "  SELECT job_id, status, received_at, "
        "         ROW_NUMBER() OVER (PARTITION BY job_id "
        "                            ORDER BY received_at DESC, id DESC) AS rn "
        "  FROM runs) WHERE rn <= ? ORDER BY job_id, received_at ASC",
        (limit_per_job,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["job_id"], []).append(dict(r))
    return out


def json_default(obj: Any) -> Any:
    return str(obj)
