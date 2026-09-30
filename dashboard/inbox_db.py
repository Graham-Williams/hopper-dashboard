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
from contextlib import contextmanager
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

#: Draft lifecycle. ``pending`` = the transcript exists and the Mac has not
#: drafted it yet; ``ready`` = a draft (the machine's, or one Graham wrote);
#: ``failed`` = the Mac gave up after ``DRAFT_MAX_ATTEMPTS`` bad results. A
#: failed draft still NEEDS REVIEW, so nothing strands: the UI says so and lets
#: him write the draft by hand.
DRAFT_PENDING = "pending"
DRAFT_READY = "ready"
DRAFT_FAILED = "failed"
DRAFT_STATUSES = (DRAFT_PENDING, DRAFT_READY, DRAFT_FAILED)
DRAFT_MAX_ATTEMPTS = 3
#: Caps on what a draft may hold. PINNED against ``probes/inbox_draft.py`` by
#: tests/test_probes_inbox_transcribe.py — change both or neither.
DRAFT_MAX_TITLE = 120
DRAFT_MAX_BODY = 2000
#: Transcript statuses a draft can be made from: Whisper's, or a legacy
#: browser transcript. Never ``pending``/``failed`` (no text yet).
DRAFTABLE_TRANSCRIPT_STATUSES = (TRANSCRIPT_WHISPER, TRANSCRIPT_LIVE)

#: A backlog.txt line Hopper files a note as (issue #33). One line, capped — the server
#: truncates to this. Pinned by tests/test_inbox.py.
MAX_BACKLOG_LINE = 500
#: The tag the filing loop ends that line with, and the ONLY link between a filed note and
#: the backlog-mirror row the line later comes back as: ``(voice <first 8 chars of id>)``.
#: It counts ANYWHERE in the entry's What: line — "Fix X (voice abcd1234) — ✅ DONE …" still
#: links — and NOWHERE else (a tag quoted under Why:/Notes:/Context: links nothing). The What:
#: line is the FIRST line of the mirrored text: ``probes/backlog.py`` folds the What: value's
#: wrapped continuation onto it and puts every other field on the lines after. Every matcher
#: goes through ``backlog_tags`` (Python) or ``WHAT_LINE_SQL`` (SQL); a test pins the two.
VOICE_TAG_RE = re.compile(r"\(voice ([0-9a-f]{8})\)")
#: The What: line of a mirrored backlog row, in SQL: everything before the first newline.
WHAT_LINE_SQL = ("substr(inbox_items.body, 1,"
                 " instr(inbox_items.body || char(10), char(10)) - 1)")


def voice_tag(item_id: str) -> str:
    return "(voice %s)" % item_id[:8]


def what_line(text: str | None) -> str:
    """The What: line of a mirrored backlog entry: the first line of its text."""
    return (text or "").split("\n", 1)[0]


def backlog_tags(text: str | None) -> set[str]:
    """The ``(voice <id8>)`` tags in a backlog entry's What: line — the only place one links."""
    return set(VOICE_TAG_RE.findall(what_line(text)))


@contextmanager
def transaction(conn: sqlite3.Connection):
    """A REAL transaction. ``connect`` opens autocommit connections (``isolation_level=None``),
    on which ``with conn:`` is not one: every statement commits as it runs. Anything that
    must land whole — the backlog push, a GitHub scan — runs inside this."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


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

# Additive ``inbox_items`` columns, oldest first. ``create_app`` runs the
# migration for BOTH roles and ``entrypoint.sh`` starts every gunicorn together,
# so every column added here goes through the BEGIN IMMEDIATE +
# duplicate-tolerant guard in ``init_inbox_schema`` — without it the loser of
# that race kills a worker and restart-loops the container. Same rule as
# ``db.JOBS_COLUMNS``. Never reorder or remove an entry; append only.
#
# The ``draft_*`` columns (the Hub makeover) hold the AI draft of a voice note,
# made on the Mac by ``probes/inbox_draft.py``. They live ALONGSIDE the note,
# never in it: the Mac never writes ``title``/``body``/``project`` (``body`` is
# the Whisper transcript, and the audio prune depends on it). Ticking Reviewed
# is what copies the draft across — see ``update_item``.
INBOX_COLUMNS: tuple[tuple[str, str], ...] = (
    ("draft_title", "TEXT"),
    ("draft_body", "TEXT"),
    ("draft_project", "TEXT"),
    ("draft_status", "TEXT"),          # NULL | pending | ready | failed
    ("draft_at", "TEXT"),
    ("draft_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("draft_model", "TEXT"),
    ("draft_src_sha", "TEXT"),         # sha256 of the transcript it was made from
    ("draft_edited_at", "TEXT"),       # Graham edited it: no machine may overwrite
    # Filed to backlog.txt instead of a GitHub issue (issue #33): not repo work.
    ("filed_backlog_at", "TEXT"),
    ("filed_backlog_line", "TEXT"),
    # When a review first copied the draft into title/project. A re-tick copies the draft
    # project again only if this is NULL (``reviewed_at`` cannot say it: an untick clears it).
    ("draft_copied_at", "TEXT"),
    # WHO closed the row, when a rule did: 'backlog' = its backlog.txt line was removed or
    # marked ✅ DONE (``apply_filed_backlog_rule``); 'issues' = a scan closed its last open
    # linked issue (``apply_issue_transitions``). NULL for a hand close; cleared by every hand
    # state change — so each rule's reopen can only undo its own close.
    ("closed_by", "TEXT"),
    # A note filed to backlog.txt: how many consecutive COMPLETE pushes have not carried its
    # tag (capped at BACKLOG_ABSENT_PUSHES). "Removed" needs two in a row, so one truncated
    # read of the file (or a filing call that beat the line into the file) closes nothing.
    ("backlog_absent_pushes", "INTEGER NOT NULL DEFAULT 0"),
)


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
        # Backfill draft_copied_at for voice notes reviewed before it existed: their review
        # already happened, so a re-tick must count as a RE-tick. Idempotent (NULLs only).
        conn.execute("UPDATE inbox_items SET draft_copied_at = reviewed_at"
                     " WHERE source = 'voice' AND reviewed = 1"
                     " AND draft_copied_at IS NULL AND reviewed_at IS NOT NULL")
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
                title_source: str | None = None,
                reviewed: bool = False) -> str:
    """Insert one item and return its id.

    ``title`` given explicitly defaults to ``manual`` and is then never
    re-derived — that is what stops the Whisper backfill overwriting a title
    Graham typed. A MIRRORED row passes ``title_source='derived'`` explicitly:
    its title belongs upstream and must keep tracking it until Graham edits it.

    ``reviewed=True`` is for a TYPED note: typing it was the deliberate act the
    Reviewed tick exists to capture for a voice note, so it is born reviewed and
    Hopper's filing loop picks it up without a second tap.
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
    # The capture form's project SEEDS the draft's project for a voice note, so
    # the Mac's draft starts from what Graham said it was about.
    draft_project = project if source == "voice" else None
    conn.execute(
        "INSERT INTO inbox_items (id, source, title, title_source, body, project,"
        " created_at, updated_at, reviewed, reviewed_at, state, transcript_status,"
        " transcript_at, audio_path, audio_bytes, audio_mime, audio_sha256,"
        " audio_secs, mirror_key, mirror_url, mirror_seen_at, draft_project)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, source, title_text, title_source, body, project, now, now,
         1 if reviewed else 0, now if reviewed else None,
         transcript_status,
         now if transcript_status in (TRANSCRIPT_TYPED, TRANSCRIPT_LIVE) else None,
         audio.get("path"), audio.get("bytes"), audio.get("mime"),
         audio.get("sha256"), audio.get("secs"),
         mirror_key, mirror_url, now if mirror_key else None, draft_project))
    return item_id


_PATCHABLE = ("title", "project", "body", "reviewed", "state",
              "draft_title", "draft_body", "draft_project")
#: Draft fields Graham may edit (voice notes only; the route enforces that).
DRAFT_FIELDS = ("draft_title", "draft_body", "draft_project")


def update_item(conn: sqlite3.Connection, item_id: str, changes: dict,
                now: str | None = None) -> dict | None:
    """Apply a validated PATCH. Returns the updated row, or None if unknown. Raises
    ``Conflict`` when a conditional write finds the row changed underneath it.

    Editing the title makes it ``manual`` — the Whisper backfill then leaves it
    alone for ever, which is the whole point of ``title_source``.

    Drafts (voice notes):

    - Editing a ``draft_*`` field stamps ``draft_edited_at`` (no machine draft may
      overwrite it after that) and makes the draft ``ready``. On a note that is
      ALREADY reviewed the edit also writes ``title`` (manual) / ``project``, so what
      gets filed is what Graham sees.
    - Editing ``body`` on an unreviewed voice note whose draft Graham has not edited
      makes the draft ``pending`` with zero attempts (``NULL`` if the body is now
      empty — there is nothing to draft, so the row never reads "Drafting…" for ever).
    - **Ticking Reviewed copies the draft across**: ``title := draft_title`` (made
      ``manual``; only over a still-derived title, or a draft edited in the same
      request) and ``project := draft_project`` (only on the FIRST copy,
      ``draft_copied_at IS NULL``, or when ``draft_project`` is in the same request —
      so a re-tick never takes back a project Graham changed). Hopper's filing loop
      reads ``title``/``project``.

    Writes that act on the draft are CONDITIONAL (the draft values read, ``reviewed``,
    ``draft_edited_at``), so a machine draft landing between the read and the write is
    a ``Conflict``, never a silently stale copy.
    """
    row = get_item(conn, item_id)
    if row is None:
        return None
    now = now or now_iso()
    voice = row["source"] == "voice"
    sets: list[str] = []
    args: list[Any] = []
    guards: list[str] = []
    guard_args: list[Any] = []
    draft_edit = any(k in changes for k in DRAFT_FIELDS)
    if draft_edit:
        sets += ["draft_edited_at=?", "draft_status=?"]
        args += [now, DRAFT_READY]
    elif ("body" in changes and voice and not row["reviewed"]
          and not row.get("draft_edited_at")):
        has_text = bool(clean_text(changes["body"], MAX_TEXT))
        sets += ["draft_status=?", "draft_attempts=0"]
        args.append(DRAFT_PENDING if has_text else None)
        guards += ["reviewed = 0", "draft_edited_at IS NULL"]
    for key in _PATCHABLE:
        if key not in changes:
            continue
        value = changes[key]
        if key == "draft_title":
            sets.append("draft_title=?")
            args.append(clean_draft_title(value) or None)
        elif key == "draft_body":
            sets.append("draft_body=?")
            args.append(clean_text(value, DRAFT_MAX_BODY) or None)
        elif key == "draft_project":
            sets.append("draft_project=?")
            args.append(value or None)
        elif key == "title":
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
            # A hand close/reopen is Graham's decision: clearing closed_by stops the
            # backlog rule from ever reopening (or re-closing on) what he did.
            sets += ["state=?", "closed_at=?", "closed_by=NULL"]
            args += [value, now if value == "closed" else None]

    d_title = (clean_draft_title(changes["draft_title"])
               if "draft_title" in changes else row.get("draft_title"))
    d_project = (changes["draft_project"] if "draft_project" in changes
                 else row.get("draft_project"))
    copy_draft = voice and changes.get("reviewed") is True and not row["reviewed"]
    edit_after_review = (voice and draft_edit and row["reviewed"]
                         and changes.get("reviewed") is not False)
    if copy_draft:
        # Only over a still-DERIVED title (or a draft edited in this same request):
        # once a title is manual — typed at capture, a previous review's copy, or a
        # rename — a re-tick must not take it back.
        if (d_title and "title" not in changes
                and (row.get("title_source") != "manual" or "draft_title" in changes)):
            sets += ["title=?", "title_source='manual'"]
            args.append(d_title)
        # The same rule for the project: the first copy, or an edit in this request.
        if (d_project and "project" not in changes
                and (not row.get("draft_copied_at") or "draft_project" in changes)):
            sets.append("project=?")
            args.append(d_project)
        if not row.get("draft_copied_at"):
            sets.append("draft_copied_at=?")
            args.append(now)
        # Copy exactly the draft that was read: a machine draft landing in between
        # must not be copied unseen.
        guards += ["reviewed = 0", "draft_title IS ?", "draft_project IS ?"]
        guard_args += [row.get("draft_title"), row.get("draft_project")]
    elif edit_after_review:
        # Already reviewed: the filing loop reads title/project, so the edit goes there
        # too — otherwise Graham would file something other than what he sees.
        if "draft_title" in changes and d_title and "title" not in changes:
            sets += ["title=?", "title_source='manual'"]
            args.append(d_title)
        if "draft_project" in changes and "project" not in changes:
            sets.append("project=?")
            args.append(d_project or None)
        guards.append("reviewed = 1")
    if not sets:
        return row
    sets.append("updated_at=?")
    args.append(now)
    where = " AND ".join(["id=?"] + guards)
    cur = conn.execute(f"UPDATE inbox_items SET {', '.join(sets)} WHERE {where}",
                       args + [item_id] + guard_args)
    if cur.rowcount == 0:
        raise Conflict(item_id)
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
    # A new transcript means a (re)draft is due — unless Graham has written or
    # edited the draft himself, which no machine may ever overwrite.
    if (row["source"] == "voice" and not row.get("draft_edited_at")
            and not row["reviewed"] and status in DRAFTABLE_TRANSCRIPT_STATUSES):
        sets += ["draft_status=?", "draft_attempts=0"]
        args.append(DRAFT_PENDING if body else None)
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


def transcript_sha(body: str | None) -> str:
    """sha256 of the transcript a draft was made from. A draft whose sha no
    longer matches the note's body is STALE: the transcript changed under it."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def clean_draft_title(value: Any) -> str:
    """One line, control characters stripped, at most DRAFT_MAX_TITLE chars."""
    text = clean_text(value, MAX_TEXT)
    text = " ".join(text.split())
    return text[:DRAFT_MAX_TITLE].rstrip()


class Conflict(Exception):
    """A conditional write lost a race: the row changed between the read and the write.
    The route answers 409 and nothing was written."""


class DraftRefused(Exception):
    """``set_draft`` would overwrite something it must not. ``reason`` is one of
    ``missing``, ``reviewed``, ``edited``, ``stale``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def set_draft(conn: sqlite3.Connection, item_id: str, *, title: str,
              body: str, project: str | None, src_sha: str,
              model: str | None = None, now: str | None = None) -> dict:
    """Store the Mac's draft. Refuses (``DraftRefused``) when the note is gone,
    already reviewed, edited by Graham, or the transcript changed since the
    draft was made (``src_sha`` stale) — the last one burns no attempt: the
    note is simply redrafted from the new text on the next pass.

    A MANUAL title wins: if Graham typed a title at capture, ``draft_title`` is
    forced to it, so "a manual title is never overwritten" holds through the
    review copy as well.
    """
    row = get_item(conn, item_id)
    if row is None:
        raise DraftRefused("missing")
    if row["reviewed"]:
        raise DraftRefused("reviewed")
    if row.get("draft_edited_at"):
        raise DraftRefused("edited")
    if src_sha != transcript_sha(row["body"]):
        raise DraftRefused("stale")
    now = now or now_iso()
    d_title = clean_draft_title(title)
    if row.get("title_source") == "manual":
        d_title = clean_draft_title(row["title"])
    # CONDITIONAL on exactly what was checked above, so a review, an edit or a new
    # transcript that lands between the read and this write wins instead of being
    # overwritten.
    cur = conn.execute(
        "UPDATE inbox_items SET draft_title=?, draft_body=?, draft_project=?,"
        " draft_status=?, draft_at=?, draft_model=?, draft_src_sha=?,"
        " updated_at=? WHERE id=? AND reviewed = 0 AND draft_edited_at IS NULL"
        " AND body IS ?",
        (d_title or None, clean_text(body, DRAFT_MAX_BODY) or None,
         project or row.get("draft_project"), DRAFT_READY, now,
         clean_text(model, 64) or None, src_sha, now, item_id, row["body"]))
    if cur.rowcount == 0:
        raise DraftRefused("conflict")
    return get_item(conn, item_id)


def note_draft_attempt(conn: sqlite3.Connection, item_id: str, *,
                       max_attempts: int = DRAFT_MAX_ATTEMPTS,
                       final: bool = False,
                       now: str | None = None) -> dict | None:
    """One BAD draft result (invalid structured output). ``failed`` once it
    has happened ``max_attempts`` times — a note that can never be drafted must
    not be retried on every run for ever. A failed draft still needs review.

    ``final=True`` burns ALL remaining attempts at once: the worker's circuit
    breaker sends it after three tripped runs in a row, when the note has already
    failed three times without anything being counted."""
    row = get_item(conn, item_id)
    if row is None:
        return None
    if row["reviewed"] or row.get("draft_edited_at"):
        return row
    now = now or now_iso()
    before = int(row.get("draft_attempts") or 0)
    attempts = max(before + 1, max_attempts) if final else before + 1
    status = DRAFT_FAILED if attempts >= max_attempts else DRAFT_PENDING
    conn.execute("UPDATE inbox_items SET draft_attempts=?, draft_status=?,"
                 " updated_at=? WHERE id=? AND reviewed = 0"
                 " AND draft_edited_at IS NULL AND draft_attempts = ?",
                 (attempts, status, now, item_id, before))
    return get_item(conn, item_id)


def mark_filed_backlog(conn: sqlite3.Connection, item_id: str, *, line: str,
                       now: str | None = None) -> dict | None:
    """Record that Hopper filed this note as a backlog.txt line (issue #33).

    Idempotent: a repeat keeps the FIRST ``filed_backlog_at`` and takes the latest line. The
    note leaves awaiting-filing (``_AWAITING_FILING_SQL``), and the backlog-mirror row the line
    comes back as — matched by its ``(voice <id8>)`` tag — is hidden from the default list, so
    the note does not show twice.
    """
    row = get_item(conn, item_id)
    if row is None:
        return None
    now = now or now_iso()
    conn.execute("UPDATE inbox_items SET filed_backlog_at=COALESCE(filed_backlog_at, ?),"
                 " filed_backlog_line=?, updated_at=? WHERE id=?",
                 (now, line, now, item_id))
    return get_item(conn, item_id)


def backlog_copies(conn: sqlite3.Connection, item_ids: Iterable[str]) -> dict[str, dict]:
    """For filed notes, the live backlog-mirror row whose What: line carries their
    ``(voice <id8>)`` tag, as ``{note_id: {"id", "mirror_key"}}``. Matched in Python over the (few) backlog rows that
    carry any tag, so a line that arrives in the mirror before or after the filing call links
    either way."""
    wanted = {i[:8]: i for i in item_ids if isinstance(i, str) and len(i) >= 8}
    if not wanted:
        return {}
    out: dict[str, dict] = {}
    for row in conn.execute(
            "SELECT id, mirror_key, body FROM inbox_items WHERE source = 'backlog'"
            " AND archived_at IS NULL AND body LIKE '%(voice %'"):
        for tag in sorted(backlog_tags(row["body"])):
            note = wanted.get(tag)
            if note and note not in out:
                out[note] = {"id": row["id"], "mirror_key": row["mirror_key"]}
    return out


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
                       now: str | None = None, state: str | None = None) -> str:
    """Insert-or-refresh a mirrored row, keyed on ``mirror_key``.

    Refreshing deliberately does NOT touch ``reviewed``: that is Graham's, and a
    re-sync that un-ticked a reviewed row would be the mirror overwriting a
    decision. ``state`` is left alone too UNLESS the caller passes one — the
    backlog mirror does, because a backlog row's state IS its text (✅ DONE,
    :func:`backlog_is_done`), and setting it from the text also clears
    ``closed_by`` (no rule owns a state the text decides); the GitHub mirror has
    its own close/reopen passes.

    WRITES NOTHING when nothing changed — not even ``updated_at`` or
    ``mirror_seen_at`` (which therefore means "last changed by the mirror"). An
    unchanged sync must leave ``inbox.db`` byte-identical, or the backup re-snapshots
    and re-uploads it on every cycle.
    """
    now = now or now_iso()
    row = conn.execute(
        "SELECT id, title, title_source, body, project, mirror_url, archived_at, state,"
        " closed_at, closed_by FROM inbox_items WHERE mirror_key=?", (mirror_key,)).fetchone()
    title = clean_text(title, MAX_TITLE) or "(untitled)"
    body = clean_text(body, MAX_TEXT)
    if row is not None:
        sets: list[str] = []
        args: list[Any] = []
        if (row["body"] or "") != body:
            sets.append("body=?")
            args.append(body)
        if row["mirror_url"] != url:
            sets.append("mirror_url=?")
            args.append(url)
        if row["archived_at"] is not None:
            sets.append("archived_at=NULL")
        if row["title_source"] != "manual" and row["title"] != title:
            sets.append("title=?")
            args.append(title)
        if project is not None and row["project"] != project:
            sets.append("project=?")
            args.append(project)
        if state in ITEM_STATES and (row["state"] != state or row["closed_by"] is not None):
            sets += ["state=?", "closed_at=?", "closed_by=NULL"]
            args += [state, (row["closed_at"] or now) if state == "closed" else None]
        if sets:
            sets += ["mirror_seen_at=?", "updated_at=?"]
            args += [now, now, row["id"]]
            conn.execute(f"UPDATE inbox_items SET {', '.join(sets)} WHERE id=?", args)
        return row["id"]
    item_id = create_item(conn, source=source, text=body, title=title,
                          title_source="derived", project=project, now=now,
                          mirror_key=mirror_key, mirror_url=url)
    if state == "closed":
        conn.execute("UPDATE inbox_items SET state='closed', closed_at=? WHERE id=?",
                     (now, item_id))
    return item_id


#: SQLite's compile-time SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds and
#: 32766 on newer ones, and which one a container gets is not ours to choose.
#: `issues_for` has always chunked at 200 for exactly that reason; these three
#: NOT IN clauses had not, and each can carry up to 2000 parameters (the backlog
#: mirror's MAX_BACKLOG_ITEMS, or a repo's whole open-issue list). Chunking a
#: NOT IN is sound because `x NOT IN A AND x NOT IN B` is `x NOT IN (A | B)`.
PARAM_CHUNK = 200


def _not_in_chunks(column: str, values: list) -> tuple[str, list]:
    """``AND col NOT IN (…) AND col NOT IN (…)`` — one clause per chunk."""
    clauses: list[str] = []
    args: list[Any] = []
    for start in range(0, len(values), PARAM_CHUNK):
        chunk = values[start:start + PARAM_CHUNK]
        clauses.append(f" AND {column} NOT IN ({','.join('?' * len(chunk))})")
        args += chunk
    return "".join(clauses), args


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
    clause, extra = _not_in_chunks("mirror_key", seen)
    sql += clause
    args += extra
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
    clause, extra = _not_in_chunks("mirror_key", seen)
    sql += clause
    args += extra
    return int(conn.execute(sql, args).rowcount or 0)


CLOSED_BY_BACKLOG = "backlog"
CLOSED_BY_ISSUES = "issues"

#: A backlog.txt entry is DONE when its What: line carries a done marker ANYWHERE — the
#: file does both "✅ DONE 2026-08-21 — …" (prefix) and "… — ✅ DONE 2026-07-08 via …"
#: (suffix), and "✅ RESOLVED". The emoji may carry its variation selector (U+FE0F);
#: case-insensitive. Only the What: line counts (a "✅ DONE" under Why: is just words).
BACKLOG_DONE_RE = re.compile(r"✅\ufe0f?\s*(DONE|RESOLVED)\b", re.IGNORECASE)
#: Consecutive complete pushes a filed note's tag must be missing from before the note is
#: treated as removed from backlog.txt (see ``backlog_absent_pushes``).
BACKLOG_ABSENT_PUSHES = 2


def backlog_is_done(text: str | None) -> bool:
    """True when a backlog entry's What: line (the first line of its text) is marked done."""
    return bool(BACKLOG_DONE_RE.search(what_line(text)))


def filed_line_status(conn: sqlite3.Connection) -> dict[str, str]:
    """For every ``(voice <id8>)`` tag in any backlog-mirror row's What: line: ``open`` (some
    live tagged row is open), ``done`` (live tagged rows exist and all are closed — ✅ DONE),
    or ``gone`` (only archived tagged rows: the line was removed). A tag no row has ever
    carried is absent. Matched in Python (``backlog_tags``) over the few rows that mention a
    tag at all."""
    status: dict[str, str] = {}
    for row in conn.execute(
            "SELECT state, archived_at, body FROM inbox_items WHERE source = 'backlog'"
            " AND body LIKE '%(voice %'"):
        for tag in backlog_tags(row["body"]):
            cur = status.get(tag, "gone")
            if row["archived_at"] is None:
                cur = "open" if (row["state"] == "open" or cur == "open") else "done"
            status[tag] = cur
    return status


def apply_filed_backlog_rule(conn: sqlite3.Connection, *, before: dict[str, str],
                             now: str | None = None) -> tuple[int, int]:
    """Close / reopen notes filed to backlog.txt from the state of their line(s).

    The backlog twin of the issues rule. Call ONLY at the end of a COMPLETE backlog push —
    after every upsert AND the archive, never per row, so an edit that swaps keys within one
    push cannot flap a note — with ``before`` = :func:`filed_line_status` taken at the start
    of that push. For every filed note (voice or typed), by its tag's status now:

    - ``open`` (a live tagged line that is not done): reset the absence count; REOPEN the
      note only if this rule closed it (``closed_by='backlog'``).
    - ``done`` (every live tagged line is ✅ DONE/RESOLVED): CLOSE it at once — on the
      TRANSITION into done (``before`` was not done), so a hand reopen of a done note sticks.
    - no live tagged line (removed, or it never reached the file): count the push in
      ``backlog_absent_pushes``; CLOSE on the push that makes it BACKLOG_ABSENT_PUSHES (2) in
      a row. One truncated read of the file, or a filing call that beat its line into the
      file, therefore closes nothing. The count is capped, so a steady state writes nothing
      and a note reopened by hand is not closed again until its line comes back and goes.

    A closing rule never touches a closed note, and a reopen never touches one it did not
    close: hand changes clear ``closed_by``, and the issues rule writes its own value. Every
    write is by primary key. Returns ``(closed, reopened)``.
    """
    now = now or now_iso()
    after = filed_line_status(conn)
    closed = reopened = 0

    def close(note_id: str) -> int:
        return int(conn.execute(
            "UPDATE inbox_items SET state='closed', closed_at=?, closed_by=?, updated_at=?"
            " WHERE id=? AND state='open'",
            (now, CLOSED_BY_BACKLOG, now, note_id)).rowcount or 0)

    notes = conn.execute(
        "SELECT id, state, closed_by, backlog_absent_pushes FROM inbox_items"
        " WHERE filed_backlog_at IS NOT NULL AND source IN ('voice','typed')").fetchall()
    for note in notes:
        tag = note["id"][:8]
        status = after.get(tag)
        absent = int(note["backlog_absent_pushes"] or 0)
        if status in ("open", "done"):
            if absent:
                conn.execute("UPDATE inbox_items SET backlog_absent_pushes=0 WHERE id=?",
                             (note["id"],))
            if status == "open":
                if note["state"] == "closed" and note["closed_by"] == CLOSED_BY_BACKLOG:
                    reopened += int(conn.execute(
                        "UPDATE inbox_items SET state='open', closed_at=NULL, closed_by=NULL,"
                        " updated_at=? WHERE id=? AND state='closed' AND closed_by=?",
                        (now, note["id"], CLOSED_BY_BACKLOG)).rowcount or 0)
            elif note["state"] == "open" and before.get(tag) != "done":
                closed += close(note["id"])
        elif absent < BACKLOG_ABSENT_PUSHES:
            absent += 1
            conn.execute("UPDATE inbox_items SET backlog_absent_pushes=? WHERE id=?",
                         (absent, note["id"]))
            if absent == BACKLOG_ABSENT_PUSHES and note["state"] == "open":
                closed += close(note["id"])
    return closed, reopened


def apply_backlog_push(conn: sqlite3.Connection, entries: list[tuple[str, str, str | None]],
                       *, complete: bool, now: str | None = None) -> dict:
    """One backlog.txt push, ``entries`` = already-validated ``(key, text, project)``, as ONE
    transaction: the upserts (each row's state from its What: line), then — only when the
    caller read the WHOLE file (``complete``) — the archive of absent keys and the filed-note
    rule. An unchanged push writes nothing (see :func:`upsert_mirror_item`)."""
    now = now or now_iso()
    seen: list[str] = []
    archived = closed = reopened = 0
    with transaction(conn):
        # Taken BEFORE any upsert: the filed-note rule acts on what this whole push changed.
        before = filed_line_status(conn) if complete else {}
        for key, text, project in entries:
            upsert_mirror_item(conn, mirror_key=key, source=MIRROR_BACKLOG,
                               title=derive_title(text), body=text, project=project, now=now,
                               state="closed" if backlog_is_done(text) else "open")
            seen.append(key)
        if complete:
            archived = archive_missing(conn, prefix=MIRROR_BACKLOG + ":", seen_keys=seen,
                                       now=now)
            # After the WHOLE push (upserts + archive), never per row.
            closed, reopened = apply_filed_backlog_rule(conn, before=before, now=now)
    return {"synced": len(seen), "archived": archived, "complete": complete,
            "closed_notes": closed, "reopened_notes": reopened}


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


def issue_states(conn: sqlite3.Connection, repo: str) -> dict[int, tuple[str, str]]:
    """``{number: (state, item_id)}`` for every issue linked from ``repo`` — the STORED
    state that :func:`apply_issue_transitions` compares a scan against."""
    return {int(r["number"]): (r["state"], r["item_id"]) for r in conn.execute(
        "SELECT number, state, item_id FROM inbox_issues WHERE repo=?", (repo,))}


def apply_issue_transitions(conn: sqlite3.Connection, repo: str, *,
                            before: dict[int, tuple[str, str]],
                            now: str | None = None) -> tuple[int, int]:
    """The issues rule for NOTES, EDGE-triggered. Returns ``(closed, reopened)``.

    Call at the end of a COMPLETE scan of ``repo`` (after :func:`refresh_issue` and
    :func:`mark_issues_closed`), with ``before`` = :func:`issue_states` taken at its start.
    Only what THIS scan changed counts, read off the stored ``inbox_issues.state``, so a
    transition that happened during a mirror outage is still seen by the first good scan:

    - an issue moved open → closed, and the note now has NO open linked issue (in any
      repo — "all", not "any": a note can spawn issues in two repos) → close it,
      ``closed_by='issues'``;
    - an issue moved closed → open → reopen the note, ONLY if this rule closed it.

    Being edge-triggered is the point: a hand reopen of a note whose issues are all closed
    sticks until an issue next changes upstream (it used to be re-closed by every scan), and
    a hand close is never undone (hand changes clear ``closed_by``). An issue linked since
    ``before`` was taken counts as open before (``link_issue`` inserts it open). Mirrored
    ``github`` rows are not touched here: they follow upstream on every scan by design.
    """
    now = now or now_iso()
    closing: set[str] = set()
    reopening: set[str] = set()
    for number, (state, item_id) in issue_states(conn, repo).items():
        was = before.get(number, ("open", item_id))[0]
        if was != "closed" and state == "closed":
            closing.add(item_id)
        elif was == "closed" and state != "closed":
            reopening.add(item_id)
    reopened = closed = 0
    for item_id in sorted(reopening):
        reopened += int(conn.execute(
            "UPDATE inbox_items SET state='open', closed_at=NULL, closed_by=NULL, updated_at=?"
            " WHERE id=? AND state='closed' AND closed_by=?",
            (now, item_id, CLOSED_BY_ISSUES)).rowcount or 0)
    for item_id in sorted(closing):
        closed += int(conn.execute(
            "UPDATE inbox_items SET state='closed', closed_at=?, closed_by=?, updated_at=?"
            " WHERE id=? AND state='open' AND source IN ('voice','typed')"
            " AND NOT EXISTS (SELECT 1 FROM inbox_issues i"
            "   WHERE i.item_id = inbox_items.id AND i.state != 'closed')",
            (now, CLOSED_BY_ISSUES, now, item_id)).rowcount or 0)
    return closed, reopened


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
    clause, extra = _not_in_chunks("number", numbers)
    sql += clause
    args += extra
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


def audio_bytes_total(conn: sqlite3.Connection) -> int:
    """Bytes of audio the ROWS account for — the cheap read of the aggregate
    cap, used on the upload path so it does not ``stat`` the whole tree.

    It is not the same number as ``inbox_audio.tree_bytes``: a file with no row
    (an interrupted upload, an orphan the sweep has not collected yet) is
    invisible here, so this is a LOWER BOUND on what is really on disk. That is
    exactly why the caller only trusts it while there is a wide margin left,
    and walks the tree for real anywhere near the cap.
    """
    row = conn.execute("SELECT COALESCE(SUM(audio_bytes), 0) FROM inbox_items"
                       " WHERE audio_path IS NOT NULL").fetchone()
    return int(row[0] or 0)


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
    " AND source IN ('voice','typed') AND filed_backlog_at IS NULL"
    " AND NOT EXISTS (SELECT 1 FROM inbox_issues i WHERE i.item_id = inbox_items.id))")
_AWAITING_TRANSCRIPTION_SQL = (
    "(audio_path IS NOT NULL AND transcript_status IN "
    "('" + "','".join(TRANSCRIBABLE_STATUSES) + "'))")

# "Needs review" = a voice note Graham has not ticked yet whose draft is either
# ready or has failed (failed too, so nothing strands — he can write it by hand).
_NEEDS_REVIEW_SQL = (
    "(source = 'voice' AND state = 'open' AND archived_at IS NULL"
    " AND reviewed = 0 AND (draft_status IN ('ready','failed')"
    " OR transcript_status = 'failed'))")

# A backlog-mirror row that IS a filed note's line (its What: line carries the note's voice
# tag). Hidden from the default list and the counts WHENEVER that note exists, in any state —
# it is the same piece of work, and the note is where its state is shown. Shown again only if
# the note is deleted. Still listed when the source filter is explicitly `backlog`.
_FILED_COPY_SQL = (
    "(source = 'backlog' AND EXISTS (SELECT 1 FROM inbox_items n"
    " WHERE n.filed_backlog_at IS NOT NULL AND n.source IN ('voice','typed')"
    f" AND instr({WHAT_LINE_SQL}, '(voice ' || substr(n.id, 1, 8) || ')') > 0))")

MAX_LIMIT = 500
_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def list_items(conn: sqlite3.Connection, *, q: str | None = None,
               source: str | None = None, state: str | None = None,
               reviewed: bool | None = None, project: str | None = None,
               awaiting: str | None = None, include_archived: bool = False,
               limit: int = 200, offset: int = 0) -> list[dict]:
    """The board query. Filter + search over ~100 rows is the whole value;
    sorting is deliberately CUT from v1. Default order: awaiting-filing first,
    then newest. ``awaiting='review'`` filters to Needs review, which sorts
    ahead of awaiting-filing."""
    where = []
    args: list[Any] = []
    if not include_archived:
        where.append("archived_at IS NULL")
    if source != "backlog":
        where.append(f"NOT {_FILED_COPY_SQL}")
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
    elif awaiting == "review":
        where.append(_NEEDS_REVIEW_SQL)
    if q:
        needle = f"%{clean_text(q, 200).translate(_LIKE_ESCAPE)}%"
        where.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' "
                     "OR project LIKE ? ESCAPE '\\' OR draft_title LIKE ? ESCAPE '\\' "
                     "OR draft_body LIKE ? ESCAPE '\\')")
        args += [needle] * 5
    sql = (f"SELECT *, {_AWAITING_FILING_SQL} AS awaiting_filing,"
           f" {_AWAITING_TRANSCRIPTION_SQL} AS awaiting_transcription,"
           f" {_NEEDS_REVIEW_SQL} AS needs_review"
           f" FROM inbox_items")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (" ORDER BY needs_review DESC, awaiting_filing DESC, created_at DESC,"
            " id DESC LIMIT ? OFFSET ?")
    args += [max(1, min(int(limit), MAX_LIMIT)), max(0, int(offset))]
    return _rows(conn.execute(sql, args))


def issues_for(conn: sqlite3.Connection,
               item_ids: Iterable[str]) -> dict[str, list[dict]]:
    ids = [i for i in item_ids if isinstance(i, str)]
    if not ids:
        return {}
    out: dict[str, list[dict]] = {}
    # Chunked so a 500-row page cannot blow SQLite's variable limit.
    for start in range(0, len(ids), PARAM_CHUNK):
        chunk = ids[start:start + PARAM_CHUNK]
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
        f" SUM({_AWAITING_TRANSCRIPTION_SQL}) AS awaiting_transcription,"
        f" SUM({_NEEDS_REVIEW_SQL}) AS needs_review"
        f" FROM inbox_items WHERE archived_at IS NULL AND NOT {_FILED_COPY_SQL}").fetchone()
    return {k: int(row[k] or 0) for k in
            ("total", "open", "reviewed", "awaiting_filing",
             "awaiting_transcription", "needs_review")}


def draft_queue(conn: sqlite3.Connection, limit: int = 5,
                max_attempts: int = DRAFT_MAX_ATTEMPTS) -> list[dict]:
    """Voice notes the Mac should draft, oldest first.

    A transcribed, open, unreviewed voice note that Graham has not edited the
    draft of and that has not burned ``max_attempts``, AND either has no
    ready draft yet (no draft at all is the BACKFILL case: notes transcribed
    before drafting existed) or has one made from an older transcript (stale
    ``draft_src_sha``). The sha is computed here in Python — SQLite has no
    sha256 — over a candidate set the SQL has already narrowed.
    """
    placeholders = ",".join("?" * len(DRAFTABLE_TRANSCRIPT_STATUSES))
    candidates = _rows(conn.execute(
        f"SELECT id, title, title_source, body, project, draft_project,"
        f" draft_status, draft_src_sha, created_at FROM inbox_items"
        f" WHERE source = 'voice' AND state = 'open' AND archived_at IS NULL"
        f"   AND reviewed = 0 AND draft_edited_at IS NULL"
        f"   AND draft_attempts < ?"
        f"   AND (transcript_status IN ({placeholders}) OR draft_status = 'pending')"
        f"   AND body IS NOT NULL AND body != ''"
        f" ORDER BY created_at, id",
        (int(max_attempts), *DRAFTABLE_TRANSCRIPT_STATUSES)))
    out: list[dict] = []
    for row in candidates:
        sha = transcript_sha(row["body"])
        if row["draft_status"] == DRAFT_READY and row["draft_src_sha"] == sha:
            continue
        row["sha"] = sha
        out.append(row)
        if len(out) >= max(1, min(int(limit), 50)):
            break
    return out


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
