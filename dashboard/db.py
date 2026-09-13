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
# Epoch range we accept from any ISO input: 1970-01-01 .. 9999-12-31T23:59:59Z.
# Anything outside (e.g. a poisoned "0001-01-01T00:00:00+14:00", which makes
# datetime.timestamp() raise OverflowError) is treated as unparseable.
EPOCH_MIN = 0.0
EPOCH_MAX = 253402300799.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    state           TEXT NOT NULL DEFAULT 'UNKNOWN',
    since           TEXT,
    state_reason    TEXT,
    last_metrics    TEXT,               -- JSON object, shallow-merged over time
    last_metrics_at TEXT,
    updated_at      TEXT,
    created_at      TEXT,               -- first seen in jobs.yml; drives UNKNOWN -> LATE for never-pinged jobs
    bad_since       TEXT,               -- start of the current continuously-not-OK episode (NULL = no episode)
    alerted_at      TEXT                -- when THIS episode was paged for (NULL = not paged; one page per episode)
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
-- Insert order, which is what "the newest probe" is actually decided on (see
-- `last_probe`). `probes_job_at` cannot serve those queries: its rows are
-- ordered by probed_at first, so an `ORDER BY id DESC` over one job would fall
-- back to sorting up to `prune`'s 2000 rows per job on every recompute.
CREATE INDEX IF NOT EXISTS probes_job_seq ON probes (job_id, id DESC);
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
    Returns None for empty, unparseable, or out-of-range input (outside
    ``[EPOCH_MIN, EPOCH_MAX]``) — never raises, so a poisoned timestamp in a
    metric or probe row can't 500 the board."""
    if not text or not isinstance(text, str):
        return None
    s = text.strip()
    if len(s) > 64:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        epoch = dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    if not (EPOCH_MIN <= epoch <= EPOCH_MAX):
        return None
    return epoch


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


# Additive `jobs` columns, oldest first. Every one of these must also be in
# SCHEMA (for a fresh DB); this list is what migrates a live box DB in place.
# The alert-episode pair gets no backfill: NULL means "not currently in an
# episode", so a job that is already broken at upgrade time starts a fresh
# episode on the next recompute and pages one threshold later. That is the safe
# direction — a late page, never a silent one.
JOBS_COLUMNS = (
    ("state_reason", "TEXT"),   # pre-0.1 databases
    ("created_at", "TEXT"),
    ("bad_since", "TEXT"),
    ("alerted_at", "TEXT"),
)


def _add_column(conn: sqlite3.Connection, table: str, column: str,
                decl: str) -> None:
    """``ALTER TABLE … ADD COLUMN`` that tolerates having already lost the race.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, and the loser of a concurrent
    migration gets ``OperationalError: duplicate column name``. Anything else
    still raises — a real schema problem must stay loud.
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the schema and apply the additive migrations. Safe to run from
    several processes at once.

    ``create_app`` calls this for BOTH roles and ``entrypoint.sh`` starts the
    ingest worker and the read workers together, so on the deploy that
    introduces a column those processes race ``PRAGMA table_info`` →
    ``ALTER TABLE``. The losers used to raise out of ``create_app`` and kill a
    worker, which takes the whole container down (the entrypoint stops the
    other gunicorn when either dies) — and "the dashboard is down" is the
    loudest silence there is. Two guards: the migration runs inside
    ``BEGIN IMMEDIATE`` so only one process reads-then-writes at a time, and a
    duplicate column is tolerated anyway in case something migrated outside
    that lock.
    """
    conn.executescript(SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        for column, decl in JOBS_COLUMNS:
            if column not in cols:
                _add_column(conn, "jobs", column, decl)
        # Backfill: the best "first seen" we have for an old row is its UNKNOWN
        # `since`, else updated_at, else now.
        conn.execute("UPDATE jobs SET created_at = COALESCE(since, updated_at, ?) "
                     "WHERE created_at IS NULL", (now_iso(),))
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def ensure_jobs(conn: sqlite3.Connection, job_ids: Iterable[str]) -> None:
    """Make sure every declared job has a state row (UNKNOWN until heard from)."""
    now = now_iso()
    with conn:
        for jid in job_ids:
            conn.execute(
                "INSERT OR IGNORE INTO jobs (id, state, since, updated_at, "
                "created_at) VALUES (?, 'UNKNOWN', ?, ?, ?)", (jid, now, now, now))


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


def set_alert_episode(conn: sqlite3.Connection, job_id: str,
                      bad_since: str | None, alerted_at: str | None) -> None:
    """Record where the job stands in its current not-OK episode.

    ``bad_since`` is when the job last stopped being OK (NULL once the episode
    is over); ``alerted_at`` is when THIS episode was paged for (NULL until it
    crosses the job's threshold — that NULL is what caps an episode at one
    page). Deliberately does not touch ``updated_at``: that column is the
    board's "the scheduler is alive" signal and belongs to :func:`set_state`.
    """
    conn.execute("UPDATE jobs SET bad_since=?, alerted_at=? WHERE id=?",
                 (bad_since, alerted_at, job_id))


def prune(conn: sqlite3.Connection, keep_runs: int = 2000,
          keep_probes: int = 2000) -> None:
    """Bound table growth per job (a 5-minute job writes ~105k rows/year).

    One statement per table: delete every row whose id is not among the
    newest ``keep`` ids of its own job. The correlated ``LIMIT`` subquery
    walks each job's rows once via the ``(job_id, …)`` index — the previous
    "count the rows newer than me" form was O(n²) per job and got slower with
    every probe cycle.
    """
    for table, keep in (("runs", keep_runs), ("probes", keep_probes)):
        conn.execute(
            f"DELETE FROM {table} WHERE id NOT IN ("
            f"  SELECT k.id FROM {table} k WHERE k.job_id = {table}.job_id "
            f"  ORDER BY k.id DESC LIMIT ?)", (keep,))


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
    """The newest probe row for this job — **by insert order, not by clock.**

    Every "which probe row is current" query below orders by ``id``, and that
    is load-bearing rather than a style choice. ``probed_at`` is the writer's
    wall clock, so a clock that was ahead when a row was written (a box RTC
    booting wrong, NTP stepping back afterwards — the same step this branch
    already heals for ``bad_since``) leaves a row dated in the FUTURE, and a
    future row outranks every real one after it, permanently. Proved: with one
    `NOW+2h` ok row followed by a genuine failure, ``failing_probe_job_ids``
    returned an empty set and ``probe_fail_streak`` returned 0 — so the damped-
    OK hold was disabled and PR #8's damping could never un-damp.

    Clamping ``probed_at`` at INSERT cannot fix that: at insert time the value
    IS "now" by definition; it only becomes the future when the clock later
    steps back. The rowid is the one monotonic sequence we control, and
    ``probes`` has exactly one writer (the ingest process's scheduler thread),
    so insert order is the true order of observations. ``probed_at`` is still
    what every DURATION is measured from — this only decides *which row*.
    """
    return row_to_dict(conn.execute(
        "SELECT * FROM probes WHERE job_id=? ORDER BY id DESC "
        "LIMIT 1", (job_id,)).fetchone())


def last_ok_probe(conn: sqlite3.Connection, job_id: str) -> dict | None:
    """Newest successful probe row — "when was this destination last actually
    listed", which is what the no-success backstop in services.py measures.
    Insert order, for the reason in :func:`last_probe`."""
    return row_to_dict(conn.execute(
        "SELECT * FROM probes WHERE job_id=? AND ok=1 ORDER BY "
        "id DESC LIMIT 1", (job_id,)).fetchone())


def oldest_probe(conn: sqlite3.Connection, job_id: str) -> dict | None:
    """Oldest retained probe row. Used only as the reference point for a job
    that has never had a successful probe. Insert order, like every other
    ordering over this table (:func:`last_probe`) — and it matches ``prune``,
    which already keeps rows by ``id DESC``."""
    return row_to_dict(conn.execute(
        "SELECT * FROM probes WHERE job_id=? ORDER BY id ASC "
        "LIMIT 1", (job_id,)).fetchone())


def probe_fail_streak(conn: sqlite3.Connection, job_id: str,
                      limit: int = 500) -> int:
    """How many of this job's most recent probes failed in an unbroken run.

    This is the damping state, derived rather than stored: it is per-job, it
    survives a restart, a cycle in which the job was not due cannot reset it
    (no row is written), and — unlike a metric on the jobs table — the ingest
    route cannot write the `probes` table at all, so the streak is not
    forgeable by anything holding INGEST_TOKEN.

    Counted in insert order (:func:`last_probe`): a single future-dated row
    otherwise sits at the head of this scan for ever and reads as "the last
    probe was fine".
    """
    rows = conn.execute(
        "SELECT ok FROM probes WHERE job_id=? ORDER BY id DESC "
        "LIMIT ?", (job_id, max(1, int(limit)))).fetchall()
    streak = 0
    for row in rows:
        if row["ok"]:
            break
        streak += 1
    return streak


def failing_probe_job_ids(conn: sqlite3.Connection,
                          job_ids: Iterable[str]) -> set[str]:
    """Of the given jobs, the ones whose NEWEST probe row is a failure.

    The same fact ``services.Core.probe_trouble`` is built on, in one query and
    without the per-job detail — used on every recompute to tell a genuinely
    healthy ``dashboard-probes`` heartbeat apart from one that is only reading
    ``ok`` because a transient probe failure is being damped. Read from the
    ``probes`` table rather than from a metric because the ingest route cannot
    write ``probes`` at all (see ``services.LEGACY_METRIC_KEYS``).

    Partitioned by insert order, for the reason in :func:`last_probe`, and
    deliberately the SAME ordering ``probe_trouble`` uses: if the two disagreed
    about which row is newest, one would hold an episode open while the other
    computed the self-job as healthy.
    """
    ids = [str(j) for j in job_ids]
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT job_id FROM (SELECT job_id, ok, ROW_NUMBER() OVER ("
        f"  PARTITION BY job_id ORDER BY id DESC) AS rn "
        f"  FROM probes WHERE job_id IN ({marks})) WHERE rn = 1 AND ok = 0",
        ids).fetchall()
    return {r["job_id"] for r in rows}


def last_non_ok_state(conn: sqlite3.Connection, job_id: str) -> str | None:
    """The most recent state this job was in that was not OK.

    Only used to name the FROM half of a *deferred* verdict: an episode held
    open past the job's return to OK (the OK dwell, or an OK we could not
    verify) may finally page or recover while the job's previous state is
    already OK, and "job: OK → OK" would be nonsense.
    """
    row = conn.execute(
        "SELECT to_state FROM state_changes WHERE job_id=? AND to_state<>'OK' "
        "ORDER BY changed_at DESC, id DESC LIMIT 1", (job_id,)).fetchone()
    return row["to_state"] if row else None


def forget_metrics(conn: sqlite3.Connection, job_id: str,
                   keys: Iterable[str]) -> None:
    """Drop named keys from a job's ``last_metrics``.

    Used to retire ``fail_streak``: it was load-bearing damping state stored in
    an ingest-writable field, and a value left behind (legacy or forged) would
    still be displayed on the job page.
    """
    row = conn.execute("SELECT last_metrics FROM jobs WHERE id=?",
                       (job_id,)).fetchone()
    current = _load_json(row["last_metrics"]) if row else {}
    if not any(k in current for k in keys):
        return
    for k in keys:
        current.pop(k, None)
    conn.execute("UPDATE jobs SET last_metrics=? WHERE id=?",
                 (json.dumps(current), job_id))


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
