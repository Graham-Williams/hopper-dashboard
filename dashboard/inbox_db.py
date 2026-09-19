"""The Inbox store: a SEPARATE SQLite file from ``dashboard.db``.

Why a second file, stated plainly because it is the architectural decision of
this feature: ``dashboard.db`` has exactly ONE request-path writer (the
single-worker ingest process), and that is now *enforced* rather than merely
documented — ``db.connect_query_only`` makes the read role's request
connections ``PRAGMA query_only=ON``. The Inbox, though, is browser-driven:
Graham talks into his phone and the multi-worker public read role has to write
the row. Those writes go here instead, to ``inbox.db``, which has two
in-container writers (the read workers and the scheduler's GitHub mirror /
audio prune) coordinated by WAL + ``busy_timeout``.

The alternative — proxying writes to ingest — was rejected: it would push
multi-MB multipart bodies at the single process that also owns the scheduler
and its serial, up-to-240 s blocking rclone probes; every proxied request would
arrive from 127.0.0.1, collapsing ingest's peer-keyed rate limiter into one
bucket for the world; and it adds a timeout hop to a write Graham is watching
on his phone. Concurrency is a non-issue in the other direction: WAL plus a few
sub-millisecond inbox writes a day against a 60 s ticker.

**Audio is files, never BLOBs** (see :mod:`.inbox_audio`): the DB is snapshotted
and sha256-deduped on every backup, and megabytes of per-note audio would make
every snapshot byte-unique, defeat the dedup and bloat the local ring. The cost
of that choice is real and is paid here: files need orphan/dangling
reconciliation, which is a scheduler sweep (:func:`dangling_audio_items`,
:func:`known_audio_paths`), not a hope.

Every string that comes out of this module is UNTRUSTED — spoken transcripts,
GitHub issue titles, backlog lines. It is escaped where it is rendered, never
here.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from typing import Any, Iterable

from .db import _add_column, enable_wal, now_iso, to_iso  # noqa: F401
from .db import from_iso as from_iso_or_none  # noqa: F401  (never raises)

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

SOURCES = ("voice", "typed", "github", "backlog")
#: Locally-authored sources — the ones Hopper may file a GitHub issue FOR.
LOCAL_SOURCES = ("voice", "typed")
#: Mirrored sources — rows this app did not author and must not re-file.
MIRROR_SOURCES = ("github", "backlog")
ITEM_STATES = ("open", "closed")
ISSUE_STATES = ("open", "closed")
TITLE_SOURCES = ("derived", "manual")

#: FIVE values, and the two extra ones are load-bearing. Without ``pending`` the
#: UI cannot tell "transcribing…" from "typed, nothing to transcribe", and
#: without ``failed`` it cannot tell either from "this will never transcribe" —
#: and the worker has nothing to back off on. A ``failed`` row still shows on
#: the board with its audio playable.
TRANSCRIPT_PENDING = "pending"      # audio saved, no transcript yet
#: RETIRED as a value anything PRODUCES. The browser speech API that wrote it
#: streamed the microphone to Google/Apple for recognition, and Graham's ruling
#: was "drop it — nothing leaves the box"; every voice note is now created
#: ``pending`` and Whisper (on his Mac) fills it in. The value and its rendering
#: are kept because rows written before that change may still carry it, and
#: removing an enum value would be a migration for no gain. Nothing writes it.
TRANSCRIPT_LIVE = "live"            # legacy: the browser's speech API
TRANSCRIPT_WHISPER = "whisper"      # the Mac worker produced it
TRANSCRIPT_TYPED = "typed"          # Graham typed it; nothing to transcribe
TRANSCRIPT_FAILED = "failed"        # Whisper gave up after N attempts
TRANSCRIPT_STATUSES = (TRANSCRIPT_PENDING, TRANSCRIPT_LIVE, TRANSCRIPT_WHISPER,
                       TRANSCRIPT_TYPED, TRANSCRIPT_FAILED)
#: Statuses the Mac worker should still try. ``live`` stays in the set purely
#: for legacy rows (nothing produces it any more, see above): a stale browser
#: transcript is better than nothing but worse than Whisper. A failed one is
#: retried until the attempt counter stops it.
TRANSCRIBABLE_STATUSES = (TRANSCRIPT_PENDING, TRANSCRIPT_LIVE, TRANSCRIPT_FAILED)
#: Audio may only be deleted once the transcript is Whisper-quality. Anything
#: less and the audio is still the only accurate record of what was said.
PRUNABLE_TRANSCRIPT_STATUSES = (TRANSCRIPT_WHISPER,)

MAX_TEXT = 20000
MAX_TITLE = 200
MAX_PROJECT = 64
PROJECT_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")

MIRROR_GITHUB = "github"
MIRROR_BACKLOG = "backlog"


INBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_items (
    id                 TEXT PRIMARY KEY,          -- uuid4 hex; opaque, appears in URLs
    source             TEXT NOT NULL,             -- voice | typed | github | backlog
    title              TEXT NOT NULL,
    title_source       TEXT NOT NULL DEFAULT 'derived',   -- derived | manual
    body               TEXT,
    project            TEXT,                      -- NULL = unassigned
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    reviewed           INTEGER NOT NULL DEFAULT 0,  -- Graham read it and it is worth doing
    reviewed_at        TEXT,
    state              TEXT NOT NULL DEFAULT 'open',
    closed_at          TEXT,
    archived_at        TEXT,                      -- mirrored row that vanished upstream
    transcript_status  TEXT,
    transcript_at      TEXT,
    transcribe_attempts INTEGER NOT NULL DEFAULT 0,
    audio_path         TEXT,                      -- relative to settings.inbox_audio_dir
    audio_bytes        INTEGER,
    audio_mime         TEXT,
    audio_sha256       TEXT,
    audio_secs         REAL,
    audio_pruned_at    TEXT,
    mirror_key         TEXT,                      -- github:<owner>/<repo>#<n> | backlog:<sha>
    mirror_url         TEXT,
    mirror_seen_at     TEXT
);
-- The upsert key for both mirrors. Partial, so the hundreds of locally-authored
-- rows (mirror_key NULL) are not forced unique against each other.
CREATE UNIQUE INDEX IF NOT EXISTS inbox_items_mirror_key
    ON inbox_items (mirror_key) WHERE mirror_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS inbox_items_created
    ON inbox_items (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS inbox_items_source_state
    ON inbox_items (source, state, created_at DESC);
CREATE INDEX IF NOT EXISTS inbox_items_filing
    ON inbox_items (reviewed, state, created_at DESC);
CREATE INDEX IF NOT EXISTS inbox_items_queue
    ON inbox_items (transcript_status, created_at) WHERE audio_path IS NOT NULL;
CREATE TABLE IF NOT EXISTS inbox_issues (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT NOT NULL REFERENCES inbox_items(id) ON DELETE CASCADE,
    repo       TEXT NOT NULL,
    number     INTEGER NOT NULL,
    url        TEXT NOT NULL,
    title      TEXT,
    state      TEXT NOT NULL DEFAULT 'open',
    linked_at  TEXT NOT NULL,
    checked_at TEXT,
    closed_at  TEXT
);
-- One issue belongs to exactly ONE item. This is what stops the GitHub repo
-- scan cloning a voice row Hopper has already filed an issue for.
CREATE UNIQUE INDEX IF NOT EXISTS inbox_issues_repo_number
    ON inbox_issues (repo, number);
CREATE INDEX IF NOT EXISTS inbox_issues_item ON inbox_issues (item_id, id);
CREATE TABLE IF NOT EXISTS inbox_mirror_state (
    key            TEXT PRIMARY KEY,   -- e.g. github:<owner>/<repo>
    etag           TEXT,
    last_sync_at   TEXT,
    last_status    TEXT,
    last_error     TEXT,
    rate_remaining INTEGER,
    rate_reset_at  TEXT,
    backoff_until  TEXT
);
"""

# Additive ``inbox_items`` columns, oldest first. Empty for v1 — but the
# machinery stays, because ``create_app`` runs the migration for BOTH roles and
# ``entrypoint.sh`` starts every gunicorn together: the first column added here
# without the BEGIN IMMEDIATE + duplicate-tolerant guard would kill the loser of
# that race and restart-loop the container. Same rule as ``db.JOBS_COLUMNS``.
INBOX_COLUMNS: tuple[tuple[str, str], ...] = ()


def connect(db_path: str) -> sqlite3.Connection:
    """A writable connection to ``inbox.db``.

    Deliberately NOT ``db.connect`` with a different path: that one is about the
    jobs store, and keeping them apart makes "which file is this connection
    writing?" answerable by reading the import.
    """
    import os
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # busy_timeout first, then WAL through `db.enable_wal` — which retries,
    # because the WAL switch itself takes an exclusive lock that does not always
    # go through the busy handler. Two roles × several gunicorn workers open
    # this file within milliseconds of each other at boot, so a simultaneous
    # open is the normal case here, not the rare one.
    conn.execute("PRAGMA busy_timeout=10000")
    enable_wal(conn)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_inbox_schema(conn: sqlite3.Connection) -> None:
    """Create + migrate. Safe to run from several processes at once — see the
    note on :data:`INBOX_COLUMNS` and ``db.init_schema``."""
    conn.executescript(INBOX_SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(inbox_items)")}
        for column, decl in INBOX_COLUMNS:
            if column not in cols:
                _add_column(conn, "inbox_items", column, decl)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def new_id() -> str:
    """Opaque row id. It appears in URLs (``/inbox/audio/<id>``) and is the ONLY
    thing an audio path is ever built from — a client filename never is."""
    return uuid.uuid4().hex


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def clean_text(value: Any, max_len: int = MAX_TEXT) -> str:
    """Trim, cap, and strip C0 control characters except tab/newline.

    Spoken transcripts arrive from Whisper and GitHub titles from strangers'
    repos; neither has any business carrying NUL or ESC. This is not
    the XSS defence (that is escaping at render time) — it is keeping the store
    free of bytes that render as garbage in a log line or a terminal.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = "".join(c for c in text
                   if c in "\t\n" or (ord(c) >= 0x20 and ord(c) != 0x7f))
    return text.strip()[:max_len]


_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def derive_title(text: str, max_len: int = MAX_TITLE) -> str:
    """A one-line label for a spoken note: the first line, or the first
    sentence of it, trimmed. Never empty — an untitled row is unfindable."""
    text = clean_text(text, MAX_TEXT)
    if not text:
        return "(untitled)"
    first_line = text.splitlines()[0].strip()
    candidate = (_SENTENCE_END.split(first_line, 1)[0] or first_line).strip()
    if len(candidate) > max_len:
        cut = candidate[:max_len].rsplit(" ", 1)[0] or candidate[:max_len]
        candidate = cut.rstrip(" ,;:-") + "…"
    return candidate or "(untitled)"


def normalise_backlog_key(what: str) -> str:
    """``backlog:<sha256(normalised What: line)[:16]>``.

    backlog.txt has NO stable identifiers — an entry is addressed only by its
    ``What:`` text — so the key has to be derived from that text. Whitespace and
    case are normalised so re-wrapping a line does not orphan its row; anything
    else IS an edit, and an edited entry archives its row and creates a new one
    (honest and cheap for v1; a fuzzy "same item, edited" heuristic is a
    follow-up).
    """
    norm = " ".join(clean_text(what, MAX_TEXT).lower().split())
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return f"{MIRROR_BACKLOG}:{digest}"


def github_key(repo: str, number: int) -> str:
    return f"{MIRROR_GITHUB}:{repo}#{int(number)}"


def valid_project(value: str | None) -> bool:
    return value is None or bool(PROJECT_RE.match(value)) and len(value) <= MAX_PROJECT


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

def create_item(conn: sqlite3.Connection, *, source: str, text: str = "",
                title: str | None = None, project: str | None = None,
                now: str | None = None, item_id: str | None = None,
                transcript_status: str | None = None,
                audio: dict | None = None,
                mirror_key: str | None = None,
                mirror_url: str | None = None,
                title_source: str | None = None) -> str:
    """Insert one item and return its id.

    ``title`` given explicitly defaults to ``manual`` and is then never
    re-derived — that is what stops the Whisper backfill overwriting a title
    Graham typed. A MIRRORED row passes ``title_source='derived'`` explicitly:
    its title belongs upstream and must keep tracking it until Graham edits it.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    if transcript_status is not None and transcript_status not in TRANSCRIPT_STATUSES:
        raise ValueError(f"unknown transcript_status {transcript_status!r}")
    now = now or now_iso()
    item_id = item_id or new_id()
    body = clean_text(text, MAX_TEXT)
    if title:
        title_text = clean_text(title, MAX_TITLE) or derive_title(body)
        title_source = title_source or "manual"
    else:
        title_text, title_source = derive_title(body), "derived"
    if title_source not in TITLE_SOURCES:
        raise ValueError(f"unknown title_source {title_source!r}")
    audio = audio or {}
    conn.execute(
        "INSERT INTO inbox_items (id, source, title, title_source, body, project,"
        " created_at, updated_at, reviewed, state, transcript_status,"
        " transcript_at, audio_path, audio_bytes, audio_mime, audio_sha256,"
        " audio_secs, mirror_key, mirror_url, mirror_seen_at)"
        " VALUES (?,?,?,?,?,?,?,?,0,'open',?,?,?,?,?,?,?,?,?,?)",
        (item_id, source, title_text, title_source, body, project, now, now,
         transcript_status,
         now if transcript_status in (TRANSCRIPT_TYPED, TRANSCRIPT_LIVE) else None,
         audio.get("path"), audio.get("bytes"), audio.get("mime"),
         audio.get("sha256"), audio.get("secs"),
         mirror_key, mirror_url, now if mirror_key else None))
    return item_id


_PATCHABLE = ("title", "project", "body", "reviewed", "state")


def update_item(conn: sqlite3.Connection, item_id: str, changes: dict,
                now: str | None = None) -> dict | None:
    """Apply a validated PATCH. Returns the updated row, or None if unknown.

    Editing the title makes it ``manual`` — the Whisper backfill then leaves it
    alone for ever, which is the whole point of ``title_source``.
    """
    row = get_item(conn, item_id)
    if row is None:
        return None
    now = now or now_iso()
    sets: list[str] = []
    args: list[Any] = []
    for key in _PATCHABLE:
        if key not in changes:
            continue
        value = changes[key]
        if key == "title":
            sets += ["title=?", "title_source='manual'"]
            args.append(clean_text(value, MAX_TITLE) or row["title"])
        elif key == "project":
            sets.append("project=?")
            args.append(value or None)
        elif key == "body":
            sets.append("body=?")
            args.append(clean_text(value, MAX_TEXT))
        elif key == "reviewed":
            sets += ["reviewed=?", "reviewed_at=?"]
            args += [1 if value else 0, now if value else None]
        elif key == "state":
            sets += ["state=?", "closed_at=?"]
            args += [value, now if value == "closed" else None]
    if not sets:
        return row
    sets.append("updated_at=?")
    args.append(now)
    args.append(item_id)
    conn.execute(f"UPDATE inbox_items SET {', '.join(sets)} WHERE id=?", args)
    return get_item(conn, item_id)


def set_transcript(conn: sqlite3.Connection, item_id: str, *, text: str,
                   status: str = TRANSCRIPT_WHISPER, now: str | None = None,
                   metrics: dict | None = None) -> dict | None:
    """Record a transcript for an item that has audio.

    Re-derives the title ONLY when ``title_source='derived'``. A manual title is
    Graham's; a machine must never take it back.
    """
    row = get_item(conn, item_id)
    if row is None:
        return None
    now = now or now_iso()
    body = clean_text(text, MAX_TEXT)
    sets = ["body=?", "transcript_status=?", "transcript_at=?", "updated_at=?"]
    args: list[Any] = [body, status, now, now]
    if row.get("title_source") != "manual":
        sets.append("title=?")
        args.append(derive_title(body))
    secs = (metrics or {}).get("duration_s")
    if isinstance(secs, (int, float)) and secs >= 0:
        sets.append("audio_secs=?")
        args.append(float(secs))
    args.append(item_id)
    conn.execute(f"UPDATE inbox_items SET {', '.join(sets)} WHERE id=?", args)
    return get_item(conn, item_id)


def note_transcribe_attempt(conn: sqlite3.Connection, item_id: str, *,
                            failed: bool, max_attempts: int = 3,
                            now: str | None = None) -> dict | None:
    """Count one worker attempt; flip to ``failed`` once it has given up enough
    times. Without the counter a permanently undecodable clip is retried on
    every queue poll for ever."""
    row = get_item(conn, item_id)
    if row is None:
        return None
    now = now or now_iso()
    attempts = int(row.get("transcribe_attempts") or 0) + 1
    status = row.get("transcript_status")
    if failed and attempts >= max_attempts:
        status = TRANSCRIPT_FAILED
    conn.execute("UPDATE inbox_items SET transcribe_attempts=?, "
                 "transcript_status=?, updated_at=? WHERE id=?",
                 (attempts, status, now, item_id))
    return get_item(conn, item_id)


def link_issue(conn: sqlite3.Connection, item_id: str, *, repo: str,
               number: int, url: str, title: str | None = None,
               now: str | None = None) -> dict:
    """Link a real GitHub issue to an item. Idempotent on ``(repo, number)``.

    The uniqueness is the load-bearing half: an issue Hopper filed FOR a voice
    row must never also arrive as a fresh mirrored row when the repo is next
    scanned.
    """
    now = now or now_iso()
    existing = conn.execute(
        "SELECT * FROM inbox_issues WHERE repo=? AND number=?",
        (repo, int(number))).fetchone()
    if existing is not None:
        conn.execute("UPDATE inbox_issues SET url=?, title=COALESCE(?, title), "
                     "checked_at=? WHERE id=?",
                     (url, title, now, existing["id"]))
        return row_to_dict(conn.execute("SELECT * FROM inbox_issues WHERE id=?",
                                        (existing["id"],)).fetchone())
    conn.execute(
        "INSERT INTO inbox_issues (item_id, repo, number, url, title, state,"
        " linked_at, checked_at) VALUES (?,?,?,?,?, 'open', ?, ?)",
        (item_id, repo, int(number), url, title, now, now))
    conn.execute("UPDATE inbox_items SET updated_at=? WHERE id=?", (now, item_id))
    return row_to_dict(conn.execute(
        "SELECT * FROM inbox_issues WHERE repo=? AND number=?",
        (repo, int(number))).fetchone())


def upsert_mirror_item(conn: sqlite3.Connection, *, mirror_key: str,
                       source: str, title: str, body: str = "",
                       project: str | None = None, url: str | None = None,
                       now: str | None = None) -> str:
    """Insert-or-refresh a mirrored row, keyed on ``mirror_key``.

    Refreshing deliberately does NOT touch ``reviewed`` or ``state``: those are
    Graham's, and a re-sync that un-ticked a reviewed row would be the mirror
    overwriting a decision.
    """
    now = now or now_iso()
    row = conn.execute("SELECT id, title_source FROM inbox_items WHERE mirror_key=?",
                       (mirror_key,)).fetchone()
    title = clean_text(title, MAX_TITLE) or "(untitled)"
    body = clean_text(body, MAX_TEXT)
    if row is not None:
        sets = ["body=?", "mirror_url=?", "mirror_seen_at=?", "updated_at=?",
                "archived_at=NULL"]
        args: list[Any] = [body, url, now, now]
        if row["title_source"] != "manual":
            sets.append("title=?")
            args.append(title)
        if project is not None:
            sets.append("project=?")
            args.append(project)
        args.append(row["id"])
        conn.execute(f"UPDATE inbox_items SET {', '.join(sets)} WHERE id=?", args)
        return row["id"]
    return create_item(conn, source=source, text=body, title=title,
                       title_source="derived", project=project, now=now,
                       mirror_key=mirror_key, mirror_url=url)


def _like_prefix(prefix: str) -> str:
    """``prefix%`` with LIKE's own wildcards escaped — a repo named ``a_b`` must
    match itself, not ``axb``."""
    return (prefix.replace("\\", "\\\\").replace("%", "\\%")
            .replace("_", "\\_") + "%")


def archive_missing(conn: sqlite3.Connection, *, prefix: str,
                    seen_keys: Iterable[str], now: str | None = None) -> int:
    """Archive mirrored rows whose upstream key was NOT seen in a COMPLETE sync.

    Archive, never delete: the row may carry a reviewed tick, a manual title or
    linked issues, and "the upstream list no longer mentions it" is not a reason
    to destroy any of that. Callers must only reach here after an error-free
    full fetch — a partial page must archive nothing.
    """
    now = now or now_iso()
    seen = list(dict.fromkeys(seen_keys))
    sql = ("UPDATE inbox_items SET archived_at=?, updated_at=? "
           "WHERE mirror_key LIKE ? ESCAPE '\\' AND archived_at IS NULL")
    args: list[Any] = [now, now, _like_prefix(prefix)]
    if seen:
        sql += f" AND mirror_key NOT IN ({','.join('?' * len(seen))})"
        args += seen
    return int(conn.execute(sql, args).rowcount or 0)


def close_missing_mirror_items(conn: sqlite3.Connection, *, prefix: str,
                               seen_keys: Iterable[str],
                               now: str | None = None) -> int:
    """Close — not archive — mirrored rows absent from a COMPLETE open-issue
    scan.

    The GitHub mirror only ever asks for ``state=open``, so a key that is no
    longer in the answer is closed, transferred or deleted; "closed" is the
    useful rendering, and it keeps the row on the board with a badge. That is
    the point of the feature: nothing disappears once it has been filed.

    (backlog.txt is the other way round — see :func:`archive_missing` — because
    a backlog entry that vanished from the file was *removed*, not completed.)
    Callers must only reach here after an error-free full fetch.
    """
    now = now or now_iso()
    seen = list(dict.fromkeys(seen_keys))
    sql = ("UPDATE inbox_items SET state='closed', closed_at=?, updated_at=? "
           "WHERE state='open' AND mirror_key LIKE ? ESCAPE '\\'")
    args: list[Any] = [now, now, _like_prefix(prefix)]
    if seen:
        sql += f" AND mirror_key NOT IN ({','.join('?' * len(seen))})"
        args += seen
    return int(conn.execute(sql, args).rowcount or 0)


def reopen_mirror_item(conn: sqlite3.Connection, mirror_key: str,
                       now: str | None = None) -> None:
    """An issue that is open upstream again is open here again."""
    now = now or now_iso()
    conn.execute("UPDATE inbox_items SET state='open', closed_at=NULL, "
                 "updated_at=? WHERE mirror_key=? AND state='closed'",
                 (now, mirror_key))


def linked_issue(conn: sqlite3.Connection, repo: str,
                 number: int) -> dict | None:
    """The ``inbox_issues`` row for one issue, if this Inbox already knows it.

    The repo scan calls this BEFORE creating a mirrored row: an issue Hopper
    filed for a voice note is already represented on the board by that note, and
    mirroring it again would put the same piece of work on the page twice.
    """
    return row_to_dict(conn.execute(
        "SELECT * FROM inbox_issues WHERE repo=? AND number=?",
        (repo, int(number))).fetchone())


def refresh_issue(conn: sqlite3.Connection, repo: str, number: int, *,
                  title: str | None = None, url: str | None = None,
                  state: str = "open", now: str | None = None) -> None:
    """Keep a linked issue's title/state current from a repo scan."""
    now = now or now_iso()
    conn.execute(
        "UPDATE inbox_issues SET title=COALESCE(?, title), "
        "url=COALESCE(?, url), state=?, checked_at=?, "
        "closed_at=CASE WHEN ?='closed' THEN COALESCE(closed_at, ?) ELSE NULL END "
        "WHERE repo=? AND number=?",
        (title, url, state, now, state, now, repo, int(number)))


def close_items_whose_issues_all_closed(conn: sqlite3.Connection,
                                        repo: str,
                                        now: str | None = None) -> int:
    """Close every OPEN item all of whose linked issues are closed.

    "All", not "any": a voice note can spawn issues in two repos, and one of
    them being done is not the note being done.
    """
    now = now or now_iso()
    return int(conn.execute(
        "UPDATE inbox_items SET state='closed', closed_at=?, updated_at=? "
        "WHERE state='open' AND EXISTS ("
        "   SELECT 1 FROM inbox_issues i WHERE i.item_id = inbox_items.id"
        "     AND i.repo = ?)"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM inbox_issues i WHERE i.item_id = inbox_items.id"
        "     AND i.state != 'closed')",
        (now, now, repo)).rowcount or 0)


def mark_issues_closed(conn: sqlite3.Connection, repo: str,
                       open_numbers: Iterable[int],
                       now: str | None = None) -> int:
    """After a COMPLETE, error-free scan of ``repo``: anything linked from it
    and not in ``open_numbers`` is closed upstream."""
    now = now or now_iso()
    numbers = [int(n) for n in open_numbers]
    sql = ("UPDATE inbox_issues SET state='closed', closed_at=?, checked_at=? "
           "WHERE repo=? AND state!='closed'")
    args: list[Any] = [now, now, repo]
    if numbers:
        sql += f" AND number NOT IN ({','.join('?' * len(numbers))})"
        args += numbers
    return int(conn.execute(sql, args).rowcount or 0)


# --------------------------------------------------------------------------- #
# Audio lifecycle
# --------------------------------------------------------------------------- #

def prunable_audio(conn: sqlite3.Connection, cutoff_iso: str,
                   hard_cutoff_iso: str | None = None) -> list[dict]:
    """Rows whose audio may be deleted. TWO independent rules, OR'd together.

    **The convenience prune** needs all three of:

    1. ``transcript_status = 'whisper'`` — the text is as good as it will get,
    2. ``reviewed = 1`` — Graham has read it and confirmed the transcript,
    3. older than ``cutoff_iso``.

    Together they mean the words are safely in the DB (and so in the DB backup)
    before the only recording of them is destroyed. That is the right rule for
    "delete it once we no longer need it".

    **The privacy ceiling** (``hard_cutoff_iso``) needs only age, and overrides
    every one of the three above. It exists because those three conditions are
    all things that can simply never happen: Graham never ticks Reviewed,
    Whisper failed (``failed`` is deliberately outside
    :data:`PRUNABLE_TRANSCRIPT_STATUSES`), or the Mac worker never ran. Without
    a backstop, ``INBOX_AUDIO_RETENTION_DAYS`` reads like a maximum and behaves
    like a minimum, and a recording of Graham's voice is kept for ever by
    default. Past this date the audio goes, transcript or no transcript — the
    row keeps its metadata and its ``audio_pruned_at`` stamp, so the board says
    honestly that there WAS a recording and it is gone.

    ``hard_cutoff_iso=None`` disables the ceiling; callers in the app always
    pass one (see ``inbox_audio.prune_audio``).
    """
    placeholders = ",".join("?" * len(PRUNABLE_TRANSCRIPT_STATUSES))
    soft = (f"(transcript_status IN ({placeholders})"
            f" AND reviewed = 1 AND created_at < ?)")
    args: list[Any] = [*PRUNABLE_TRANSCRIPT_STATUSES, cutoff_iso]
    rule = soft
    if hard_cutoff_iso is not None:
        rule = f"({soft} OR created_at < ?)"
        args.append(hard_cutoff_iso)
    return _rows(conn.execute(
        f"SELECT id, audio_path FROM inbox_items"
        f" WHERE audio_path IS NOT NULL AND audio_pruned_at IS NULL"
        f"   AND {rule}"
        f" ORDER BY created_at LIMIT 500", args))


def delete_item(conn: sqlite3.Connection, item_id: str) -> dict | None:
    """Delete one row outright, returning it (so the caller can delete its
    audio file) or ``None`` if it was not there.

    The ONLY destructive operation in this store, and it exists for one reason:
    a voice note is a recording of Graham's voice, and "there is no way to
    delete it" is not an acceptable answer for personal data. Everything else
    here archives or closes.

    ``inbox_issues`` goes with it via ``ON DELETE CASCADE`` (``connect`` sets
    ``PRAGMA foreign_keys=ON``). The audio FILE is the caller's job — and if
    that half fails, the scheduler's orphan sweep collects it, which is what
    makes this ordering safe.
    """
    row = get_item(conn, item_id)
    if row is None:
        return None
    conn.execute("DELETE FROM inbox_items WHERE id=?", (item_id,))
    return row


def mark_audio_pruned(conn: sqlite3.Connection, item_id: str,
                      now: str | None = None) -> None:
    """The row keeps its ``audio_bytes``/``audio_secs``/``audio_sha256`` on
    purpose — "there was a 41 s recording and it was deleted on this date" is a
    better answer than a row that looks as if it never had audio."""
    now = now or now_iso()
    conn.execute("UPDATE inbox_items SET audio_path=NULL, audio_pruned_at=?, "
                 "updated_at=? WHERE id=?", (now, now, item_id))


def known_audio_paths(conn: sqlite3.Connection) -> set[str]:
    """Every path the DB still believes in — the orphan sweep's allow-list."""
    return {r["audio_path"] for r in conn.execute(
        "SELECT audio_path FROM inbox_items WHERE audio_path IS NOT NULL")}


def dangling_audio_items(conn: sqlite3.Connection) -> list[dict]:
    """Rows that claim an audio file; the caller checks which ones still exist.
    The other half of the reconciliation files-not-BLOBs costs us."""
    return _rows(conn.execute(
        "SELECT id, audio_path FROM inbox_items WHERE audio_path IS NOT NULL"))


def clear_audio_path(conn: sqlite3.Connection, item_id: str,
                     now: str | None = None) -> None:
    """The file is gone but the row still pointed at it — a 404 waiting to
    happen on the one control Graham taps to check a transcript."""
    now = now or now_iso()
    conn.execute("UPDATE inbox_items SET audio_path=NULL, updated_at=? "
                 "WHERE id=?", (now, item_id))


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def get_item(conn: sqlite3.Connection, item_id: str) -> dict | None:
    if not isinstance(item_id, str) or not ID_RE.match(item_id):
        return None
    return row_to_dict(conn.execute("SELECT * FROM inbox_items WHERE id=?",
                                    (item_id,)).fetchone())


def get_item_by_mirror_key(conn: sqlite3.Connection, key: str) -> dict | None:
    return row_to_dict(conn.execute(
        "SELECT * FROM inbox_items WHERE mirror_key=?", (key,)).fetchone())


# "Awaiting filing" = Graham ticked it, it is still open, it was authored here
# (a mirrored GitHub row IS already an issue), and nothing has been filed for it
# yet. That set is the whole point of the review tick, so it sorts first.
_AWAITING_FILING_SQL = (
    "(reviewed = 1 AND state = 'open' AND archived_at IS NULL"
    " AND source IN ('voice','typed')"
    " AND NOT EXISTS (SELECT 1 FROM inbox_issues i WHERE i.item_id = inbox_items.id))")
_AWAITING_TRANSCRIPTION_SQL = (
    "(audio_path IS NOT NULL AND transcript_status IN "
    "('" + "','".join(TRANSCRIBABLE_STATUSES) + "'))")

MAX_LIMIT = 500
_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def list_items(conn: sqlite3.Connection, *, q: str | None = None,
               source: str | None = None, state: str | None = None,
               reviewed: bool | None = None, project: str | None = None,
               awaiting: str | None = None, include_archived: bool = False,
               limit: int = 200, offset: int = 0) -> list[dict]:
    """The board query. Filter + search over ~100 rows is the whole value;
    sorting is deliberately CUT from v1. Default order: awaiting-filing first,
    then newest."""
    where = []
    args: list[Any] = []
    if not include_archived:
        where.append("archived_at IS NULL")
    if source in SOURCES:
        where.append("source = ?")
        args.append(source)
    if state in ITEM_STATES:
        where.append("state = ?")
        args.append(state)
    if reviewed is not None:
        where.append("reviewed = ?")
        args.append(1 if reviewed else 0)
    if project:
        where.append("project = ?")
        args.append(project)
    if awaiting == "filing":
        where.append(_AWAITING_FILING_SQL)
    elif awaiting == "transcription":
        where.append(_AWAITING_TRANSCRIPTION_SQL)
    if q:
        needle = f"%{clean_text(q, 200).translate(_LIKE_ESCAPE)}%"
        where.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' "
                     "OR project LIKE ? ESCAPE '\\')")
        args += [needle, needle, needle]
    sql = (f"SELECT *, {_AWAITING_FILING_SQL} AS awaiting_filing,"
           f" {_AWAITING_TRANSCRIPTION_SQL} AS awaiting_transcription"
           f" FROM inbox_items")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY awaiting_filing DESC, created_at DESC, id DESC LIMIT ? OFFSET ?"
    args += [max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))]
    return _rows(conn.execute(sql, args))


def issues_for(conn: sqlite3.Connection,
               item_ids: Iterable[str]) -> dict[str, list[dict]]:
    ids = [i for i in item_ids if isinstance(i, str)]
    if not ids:
        return {}
    out: dict[str, list[dict]] = {}
    # Chunked so a 500-row page cannot blow SQLite's variable limit.
    for start in range(0, len(ids), 200):
        chunk = ids[start:start + 200]
        for row in conn.execute(
                f"SELECT * FROM inbox_issues WHERE item_id IN "
                f"({','.join('?' * len(chunk))}) ORDER BY item_id, id", chunk):
            out.setdefault(row["item_id"], []).append(dict(row))
    return out


def transcribe_queue(conn: sqlite3.Connection, limit: int = 20,
                     max_attempts: int = 3) -> list[dict]:
    """Oldest first: rows with audio still on disk and no Whisper transcript.

    A row that has already been tried ``max_attempts`` times is excluded, so a
    clip ffmpeg simply cannot decode stops consuming the worker for ever.
    """
    placeholders = ",".join("?" * len(TRANSCRIBABLE_STATUSES))
    return _rows(conn.execute(
        f"SELECT id, source, title, created_at, transcript_status,"
        f" transcribe_attempts, audio_bytes, audio_mime, audio_secs"
        f" FROM inbox_items"
        f" WHERE audio_path IS NOT NULL"
        f"   AND (transcript_status IS NULL OR transcript_status IN ({placeholders}))"
        f"   AND transcribe_attempts < ?"
        f" ORDER BY created_at, id LIMIT ?",
        (*TRANSCRIBABLE_STATUSES, int(max_attempts),
         max(1, min(int(limit), 100)))))


def counts(conn: sqlite3.Connection) -> dict:
    """Headline numbers for the board's summary row."""
    row = conn.execute(
        f"SELECT COUNT(*) AS total,"
        f" SUM(state='open') AS open,"
        f" SUM(reviewed=1) AS reviewed,"
        f" SUM({_AWAITING_FILING_SQL}) AS awaiting_filing,"
        f" SUM({_AWAITING_TRANSCRIPTION_SQL}) AS awaiting_transcription"
        f" FROM inbox_items WHERE archived_at IS NULL").fetchone()
    return {k: int(row[k] or 0) for k in
            ("total", "open", "reviewed", "awaiting_filing",
             "awaiting_transcription")}


def projects(conn: sqlite3.Connection) -> list[str]:
    return [r["project"] for r in conn.execute(
        "SELECT DISTINCT project FROM inbox_items WHERE project IS NOT NULL"
        " AND archived_at IS NULL ORDER BY project")]


# --------------------------------------------------------------------------- #
# Mirror bookkeeping
# --------------------------------------------------------------------------- #

def get_mirror_state(conn: sqlite3.Connection, key: str) -> dict:
    row = conn.execute("SELECT * FROM inbox_mirror_state WHERE key=?",
                       (key,)).fetchone()
    return dict(row) if row is not None else {"key": key}


def set_mirror_state(conn: sqlite3.Connection, key: str, **fields) -> None:
    allowed = ("etag", "last_sync_at", "last_status", "last_error",
               "rate_remaining", "rate_reset_at", "backoff_until")
    values = {k: fields.get(k) for k in allowed if k in fields}
    if not values:
        return
    conn.execute("INSERT OR IGNORE INTO inbox_mirror_state (key) VALUES (?)",
                 (key,))
    sets = ", ".join(f"{k}=?" for k in values)
    conn.execute(f"UPDATE inbox_mirror_state SET {sets} WHERE key=?",
                 [*values.values(), key])


def all_mirror_states(conn: sqlite3.Connection) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM inbox_mirror_state ORDER BY key"))
