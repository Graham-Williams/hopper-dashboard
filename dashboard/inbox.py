"""The Inbox: ``/inbox`` plus ``/api/v1/inbox/*``, on the READ role.

One table showing everything pending across every project — spoken notes, open
GitHub issues, backlog.txt entries — so there is one place to look instead of
six. Graham talks a bug into his phone; the row appears; he ticks "reviewed"
when he has read it and it is genuinely worth doing, and that tick is the green
light for Hopper to file a real issue. Filed rows STAY, with their issue links
and live open/closed state: it is a one-stop shop, so nothing disappears.

Auth, per endpoint (there are three credentials and they are not
interchangeable):

===========================================  ======  =====================
route                                        auth    notes
===========================================  ======  =====================
``GET  /inbox``                              S       HTML; works with JS off
``POST /api/v1/inbox/items``                 S       + origin pin + limiter
``PATCH /api/v1/inbox/items/<id>``           S       + origin pin + limiter
``GET  /api/v1/inbox/items``                 S | R   Hopper may read it
``GET  /inbox/audio/<id>``                   S | I
``GET  /api/v1/inbox/transcribe/queue``      I
``POST /api/v1/inbox/items/<id>/transcript`` I
``POST /api/v1/inbox/items/<id>/issues``     I
``POST /api/v1/inbox/mirror/backlog``        I
===========================================  ======  =====================

**S** = browser session ONLY — a ``READ_TOKEN`` bearer is explicitly refused on
the two mutating routes, because READ_TOKEN is a read credential that Hopper's
watch carries and it must not become a write one. **I** = ``INBOX_TOKEN``
bearer; empty = fail closed (401), exactly like ``INGEST_TOKEN``.

Everything a row carries is UNTRUSTED text: transcripts from a browser speech
API, titles from GitHub, lines from backlog.txt. It is escaped at render (Jinja
autoescape for HTML, ``jsonify`` for JSON) and the page's JS uses
``textContent`` only — there is no path from a stored string to markup.
"""

from __future__ import annotations

import logging
import os
import time

from flask import (Blueprint, Response, current_app, jsonify, redirect,
                   render_template, request, url_for)

from . import inbox_audio, inbox_db
from .web import client_ip, require_session

log = logging.getLogger(__name__)

bp = Blueprint("inbox", __name__)

#: Endpoints ``INBOX_TOKEN`` may authenticate. ``web.auth_kind`` consults this
#: (via ``app.extensions``) so the machine token is scoped to exactly these and
#: can never act as a second read token for the rest of the API.
MACHINE_ENDPOINTS = frozenset({
    "inbox.audio",
    "inbox.transcribe_queue",
    "inbox.post_transcript",
    "inbox.post_issues",
    "inbox.mirror_backlog",
})

#: Multipart framing + the text/title/project fields, on top of the audio cap.
#: Small on purpose: the point is to lift the body limit for ONE route by the
#: least that works, never to raise the global 64 KB cap that protects the rest.
MULTIPART_OVERHEAD = 64 * 1024

#: The only URL shape an issue link may have. The board renders it as an
#: ``href``, and Jinja's escaping does nothing about a ``javascript:`` scheme —
#: escaping stops markup, not navigation.
ISSUE_URL_PREFIX = "https://github.com/"
MAX_URL = 300
MAX_REPO = 140
MAX_ISSUE_NUMBER = 10 ** 7
MAX_BACKLOG_ITEMS = 2000
QUEUE_MAX = 50
TRANSCRIBE_MAX_ATTEMPTS = 3


def _settings():
    return current_app.config["SETTINGS"]


def _conn():
    """A WRITABLE connection — to ``inbox.db``, never to ``dashboard.db``
    (which the read role now holds ``query_only``; see db.connect_query_only)."""
    return inbox_db.connect(_settings().inbox_db_path)


def _err(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _require_inbox_token():
    """Machine-only endpoints. Returns a 401 response, or None to carry on."""
    from .web import auth_kind
    if auth_kind() == "inbox":
        return None
    return _err("this endpoint needs the Inbox machine token", 401)


def _limited(name: str, key: str) -> bool:
    limiter = current_app.extensions.get(name)
    return limiter is not None and not limiter.hit(key)


# --------------------------------------------------------------------------- #
# Request shaping
# --------------------------------------------------------------------------- #

@bp.before_request
def _lift_the_body_cap_for_the_create_route_only():
    """Flask 3.1 lets ``max_content_length`` be set PER REQUEST, and that is the
    only reason an 8 MB voice note can reach a view at all: the app-wide
    ``MAX_CONTENT_LENGTH`` is 64 KB for both roles, so every upload would 413
    before any code of ours ran.

    The global cap is deliberately NOT raised. It protects ``/api/v1/ping`` and
    every other route on both roles, and one route needing more is not a reason
    to hand the same allowance to all of them. ``max_form_memory_size`` (500 kB,
    non-file fields only) is left alone — ``text`` is capped at 20 000
    characters long before it gets near that.
    """
    if request.endpoint == "inbox.create_item":
        request.max_content_length = (_settings().inbox_audio_max_bytes
                                      + MULTIPART_OVERHEAD)


@bp.errorhandler(413)
def _too_large(_exc):
    return _err("body too large", 413)


# --------------------------------------------------------------------------- #
# Validation helpers (pure; unit-tested directly)
# --------------------------------------------------------------------------- #

def clean_project(raw) -> str | None:
    """``None`` for "unassigned"; raises ValueError for anything malformed.

    Constrained to ``^[A-Za-z0-9._/-]+$`` because it is also a filter value and
    a label on the page — not because it is ever interpolated anywhere unsafe
    (it is not; every query is parameterised).
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    if len(value) > inbox_db.MAX_PROJECT or not inbox_db.PROJECT_RE.match(value):
        raise ValueError("project must be 1-64 chars of [A-Za-z0-9._/-]")
    return value


def clean_issue_url(raw) -> str:
    value = str(raw or "").strip()
    if len(value) > MAX_URL or not value.startswith(ISSUE_URL_PREFIX):
        raise ValueError(f"url must start with {ISSUE_URL_PREFIX}")
    if any(ord(c) < 0x20 or ord(c) == 0x7f or c.isspace() for c in value):
        raise ValueError("url must not contain whitespace or control characters")
    return value


def clean_repo(raw) -> str:
    value = str(raw or "").strip()
    if not value or len(value) > MAX_REPO:
        raise ValueError("repo is required")
    from .config import GITHUB_REPO_RE
    if not GITHUB_REPO_RE.match(value):
        raise ValueError("repo must look like owner/repo")
    return value


def clean_issue_number(raw) -> int:
    try:
        number = int(raw)
    except (TypeError, ValueError):
        raise ValueError("number must be an integer") from None
    if not 0 < number < MAX_ISSUE_NUMBER:
        raise ValueError("number out of range")
    return number


def _truthy(raw) -> bool:
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _float_or_none(raw, cap: float = 24 * 3600) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value < 0 or value > cap:   # NaN, negative, absurd
        return None
    return value


# --------------------------------------------------------------------------- #
# View models
# --------------------------------------------------------------------------- #

def item_json(row: dict, issues: list[dict] | None = None) -> dict:
    """One row, as both the JSON API and the template see it.

    ``has_audio`` rather than a path: the relative path is an internal detail
    and there is no reason for it to leave the box.
    """
    issues = issues or []
    # Computed HERE, from the row plus its issues, rather than read off the
    # SELECT in list_items: every route that returns a single item (create,
    # patch, transcript, issues) fetches it with `get_item`, which has no such
    # column — so reading the column meant a freshly created voice note came
    # back claiming it was NOT awaiting transcription. The SQL expression still
    # exists, but only for the ORDER BY it was written for.
    awaiting_transcription = bool(
        row["audio_path"]
        and row["transcript_status"] in inbox_db.TRANSCRIBABLE_STATUSES)
    awaiting_filing = bool(
        row["reviewed"] and row["state"] == "open" and not row["archived_at"]
        and row["source"] in inbox_db.LOCAL_SOURCES and not issues)
    return {
        "id": row["id"],
        "source": row["source"],
        "title": row["title"],
        "title_source": row["title_source"],
        "body": row["body"] or "",
        "project": row["project"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "reviewed": bool(row["reviewed"]),
        "reviewed_at": row["reviewed_at"],
        "state": row["state"],
        "closed_at": row["closed_at"],
        "archived_at": row["archived_at"],
        "transcript_status": row["transcript_status"],
        "transcript_at": row["transcript_at"],
        "transcribe_attempts": int(row["transcribe_attempts"] or 0),
        "has_audio": bool(row["audio_path"]),
        "audio_pruned_at": row["audio_pruned_at"],
        "audio_secs": row["audio_secs"],
        "audio_bytes": row["audio_bytes"],
        "mirror_key": row["mirror_key"],
        "mirror_url": row["mirror_url"],
        "awaiting_filing": awaiting_filing,
        "awaiting_transcription": awaiting_transcription,
        "issues": [{"repo": i["repo"], "number": i["number"], "url": i["url"],
                    "title": i["title"], "state": i["state"]} for i in issues],
    }


def _list_args(args) -> dict:
    reviewed = args.get("reviewed")
    try:
        limit = int(args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = int(args.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    return {
        "q": (args.get("q") or "").strip() or None,
        "source": args.get("source") or None,
        "state": args.get("state") or None,
        "reviewed": None if reviewed in (None, "") else _truthy(reviewed),
        "project": (args.get("project") or "").strip() or None,
        "awaiting": args.get("awaiting") or None,
        "limit": limit,
        "offset": offset,
    }


def _load_page(conn, args: dict) -> dict:
    rows = inbox_db.list_items(conn, **args)
    issues = inbox_db.issues_for(conn, [r["id"] for r in rows])
    return {
        "generated_at": inbox_db.now_iso(),
        "counts": inbox_db.counts(conn),
        "projects": inbox_db.projects(conn),
        "items": [item_json(r, issues.get(r["id"], [])) for r in rows],
    }


# --------------------------------------------------------------------------- #
# Browser routes
# --------------------------------------------------------------------------- #

@bp.get("/inbox")
def board():
    """The HTML board. Every row is rendered SERVER-SIDE: the page is fully
    usable with JavaScript off, and the script only shows/hides rows that are
    already in the DOM. Nothing about a transcript is ever handed to JS as a
    JSON blob inside a <script> tag."""
    conn = _conn()
    try:
        page = _load_page(conn, _list_args(request.args))
    finally:
        conn.close()
    return render_template("inbox.html", page=page, now=time.time(),
                           filters=_list_args(request.args),
                           audio_max_bytes=_settings().inbox_audio_max_bytes,
                           retention_days=_settings().inbox_audio_retention_days,
                           sources=inbox_db.SOURCES)


@bp.get("/api/v1/inbox/items")
def list_items():
    """S | R — Hopper reads this with the same bearer it reads the board with."""
    conn = _conn()
    try:
        return jsonify(_load_page(conn, _list_args(request.args)))
    finally:
        conn.close()


@bp.post("/api/v1/inbox/items")
def create_item():
    """S only. Multipart: ``text``, optional ``title``/``project``/``audio``.

    The transcript status is decided here and it is the contract the Mac worker
    reads: audio with a live browser transcript is ``live`` (Whisper will
    improve on it and may replace the body), audio with nothing said yet is
    ``pending``, and a note with no audio is ``typed`` and is never queued.
    A MANUAL title is preserved through all of that — which is why anything
    Graham types that must survive the backfill belongs in the title.
    """
    denied = require_session()
    if denied is not None:
        return denied
    if _limited("inbox_create_limiter", client_ip()):
        return _err("too many submissions — try again in a few minutes", 429)
    settings = _settings()
    text = inbox_db.clean_text(request.form.get("text"), inbox_db.MAX_TEXT)
    title = (request.form.get("title") or "").strip() or None
    try:
        project = clean_project(request.form.get("project"))
    except ValueError as exc:
        return _err(str(exc))
    upload = request.files.get("audio")
    data = upload.read() if upload is not None else b""
    if not data and not text:
        return _err("say something or type something — the row would be empty")

    now = inbox_db.now_iso()
    item_id = inbox_db.new_id()
    audio_row = None
    status = inbox_db.TRANSCRIPT_TYPED
    if data:
        try:
            stored = inbox_audio.save(
                settings.inbox_audio_dir, item_id=item_id, data=data,
                declared_mime=upload.mimetype, max_bytes=settings.inbox_audio_max_bytes,
                created_at=now)
        except inbox_audio.AudioRejected as exc:
            return _err(exc.message, exc.status)
        audio_row = stored.as_row(secs=_float_or_none(request.form.get("audio_secs")))
        status = (inbox_db.TRANSCRIPT_LIVE if text
                  else inbox_db.TRANSCRIPT_PENDING)

    conn = _conn()
    try:
        with conn:
            inbox_db.create_item(conn, source="voice" if data else "typed",
                                 text=text, title=title, project=project,
                                 now=now, item_id=item_id,
                                 transcript_status=status, audio=audio_row)
        row = inbox_db.get_item(conn, item_id)
    except Exception:                                    # noqa: BLE001
        # The row is what matters; a file with no row is an orphan the
        # scheduler's sweep collects. Never leave one behind on purpose.
        if audio_row:
            inbox_audio.delete(settings.inbox_audio_dir, audio_row["path"])
        conn.close()
        raise
    conn.close()
    if _wants_html():
        # A plain browser form post (JavaScript off): send them back to the
        # board rather than showing them a page of JSON. Capture keeps working
        # with no JS at all; only the microphone needs it.
        return redirect(url_for("inbox.board"), code=303)
    return jsonify(item_json(row)), 201


def _wants_html() -> bool:
    """True for a real <form> submission, False for the page's own fetch().

    Decided on the ACCEPT header, which a form sends as ``text/html,…`` and
    ``fetch`` (in inbox.js) sets to ``application/json``. Not on a custom
    header, because the point is to serve the no-JS path correctly.
    """
    accept = request.headers.get("Accept", "")
    return "text/html" in accept and "application/json" not in accept


@bp.patch("/api/v1/inbox/items/<item_id>")
def patch_item(item_id: str):
    """S only, + the origin pin (extended to PATCH before this route existed)."""
    denied = require_session()
    if denied is not None:
        return denied
    if _limited("inbox_write_limiter", client_ip()):
        return _err("too many writes — slow down", 429)
    doc = request.get_json(silent=True)
    if not isinstance(doc, dict):
        return _err("body must be a JSON object")
    unknown = sorted(set(doc) - {"reviewed", "title", "project", "body", "state"})
    if unknown:
        return _err(f"unknown field(s): {', '.join(unknown)}")
    changes: dict = {}
    if "reviewed" in doc:
        if not isinstance(doc["reviewed"], bool):
            return _err("reviewed must be true or false")
        changes["reviewed"] = doc["reviewed"]
    if "state" in doc:
        if doc["state"] not in inbox_db.ITEM_STATES:
            return _err(f"state must be one of {', '.join(inbox_db.ITEM_STATES)}")
        changes["state"] = doc["state"]
    if "title" in doc:
        if not isinstance(doc["title"], str) or not doc["title"].strip():
            return _err("title must be a non-empty string")
        changes["title"] = doc["title"]
    if "body" in doc:
        if not isinstance(doc["body"], str):
            return _err("body must be a string")
        changes["body"] = doc["body"]
    if "project" in doc:
        try:
            changes["project"] = clean_project(doc["project"])
        except ValueError as exc:
            return _err(str(exc))
    conn = _conn()
    try:
        with conn:
            row = inbox_db.update_item(conn, item_id, changes)
        if row is None:
            return _err("no such item", 404)
        issues = inbox_db.issues_for(conn, [row["id"]]).get(row["id"], [])
        return jsonify(item_json(row, issues))
    finally:
        conn.close()


@bp.get("/inbox/audio/<item_id>")
def audio(item_id: str):
    """S | I — the browser plays it back from HERE, not from a ``blob:`` URL.

    A blob URL would need ``media-src blob:`` in the CSP, and the CSP is not
    being loosened for a preview. Serving from the origin costs one round trip
    and keeps ``default-src 'self'`` exactly as it is.
    """
    conn = _conn()
    try:
        row = inbox_db.get_item(conn, item_id)
    finally:
        conn.close()
    if row is None:
        return _err("no such item", 404)
    if row["audio_path"] is None:
        # 410, not 404: the audio EXISTED and was deliberately pruned once its
        # Whisper transcript was reviewed. "Gone" is the honest answer.
        if row["audio_pruned_at"]:
            return _err("audio was pruned after its transcript was reviewed", 410)
        return _err("this item has no audio", 404)
    full = inbox_audio.open_path(_settings().inbox_audio_dir, row["audio_path"])
    if full is None:
        return _err("audio file is missing", 410)
    with open(full, "rb") as fh:
        payload = fh.read()
    resp = Response(payload, mimetype=inbox_audio.mime_for(row["audio_path"]))
    resp.headers["Content-Length"] = str(len(payload))
    # private + no-store: a voice note is not something to leave in a shared
    # cache, and the tunnel sits in front of this.
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Disposition"] = f'inline; filename="{row["id"]}.' \
                                          f'{row["audio_path"].rsplit(".", 1)[-1]}"'
    return resp


# --------------------------------------------------------------------------- #
# Machine routes (INBOX_TOKEN)
# --------------------------------------------------------------------------- #

@bp.get("/api/v1/inbox/transcribe/queue")
def transcribe_queue():
    denied = _require_inbox_token()
    if denied is not None:
        return denied
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    conn = _conn()
    try:
        rows = inbox_db.transcribe_queue(conn, limit=max(1, min(limit, QUEUE_MAX)),
                                         max_attempts=TRANSCRIBE_MAX_ATTEMPTS)
    finally:
        conn.close()
    return jsonify({"generated_at": inbox_db.now_iso(),
                    "max_attempts": TRANSCRIBE_MAX_ATTEMPTS,
                    "items": [dict(r) for r in rows]})


@bp.post("/api/v1/inbox/items/<item_id>/transcript")
def post_transcript(item_id: str):
    """The Whisper backfill lands here.

    ``{"text": "...", "engine": "whisper", "model": "...", "duration_s": 12.3}``
    writes the transcript; ``{"failed": true, "error": "..."}`` records an
    attempt instead, and the row flips to ``failed`` once it has given up
    ``TRANSCRIBE_MAX_ATTEMPTS`` times — without that a clip ffmpeg cannot
    decode is retried on every poll for ever.

    The title is re-derived ONLY when ``title_source='derived'``.
    """
    denied = _require_inbox_token()
    if denied is not None:
        return denied
    doc = request.get_json(silent=True)
    if not isinstance(doc, dict):
        return _err("body must be a JSON object")
    conn = _conn()
    try:
        if inbox_db.get_item(conn, item_id) is None:
            return _err("no such item", 404)
        if doc.get("failed"):
            with conn:
                row = inbox_db.note_transcribe_attempt(
                    conn, item_id, failed=True,
                    max_attempts=TRANSCRIBE_MAX_ATTEMPTS)
            log.warning("transcription failed for %s: %s", item_id,
                        str(doc.get("error"))[:200])
            return jsonify(item_json(row))
        text = doc.get("text")
        if not isinstance(text, str) or not text.strip():
            return _err("text is required (or send failed: true)")
        engine = str(doc.get("engine") or "whisper").strip().lower()
        if engine != "whisper":
            return _err("engine must be 'whisper'")
        with conn:
            row = inbox_db.set_transcript(
                conn, item_id, text=text,
                status=inbox_db.TRANSCRIPT_WHISPER,
                metrics={"duration_s": _float_or_none(doc.get("duration_s"))})
        return jsonify(item_json(row))
    finally:
        conn.close()


@bp.post("/api/v1/inbox/items/<item_id>/issues")
def post_issues(item_id: str):
    """Hopper records the issue it filed for a reviewed row. Idempotent on
    ``(repo, number)`` — which is also what stops the GitHub mirror later
    cloning this same issue as a fresh row."""
    denied = _require_inbox_token()
    if denied is not None:
        return denied
    doc = request.get_json(silent=True)
    if not isinstance(doc, dict):
        return _err("body must be a JSON object")
    try:
        repo = clean_repo(doc.get("repo"))
        number = clean_issue_number(doc.get("number"))
        url = clean_issue_url(doc.get("url"))
    except ValueError as exc:
        return _err(str(exc))
    title = inbox_db.clean_text(doc.get("title"), inbox_db.MAX_TITLE) or None
    conn = _conn()
    try:
        if inbox_db.get_item(conn, item_id) is None:
            return _err("no such item", 404)
        with conn:
            issue = inbox_db.link_issue(conn, item_id, repo=repo, number=number,
                                        url=url, title=title)
        row = inbox_db.get_item(conn, item_id)
        issues = inbox_db.issues_for(conn, [item_id]).get(item_id, [])
        return jsonify({"item": item_json(row, issues), "issue": issue}), 201
    finally:
        conn.close()


@bp.post("/api/v1/inbox/mirror/backlog")
def mirror_backlog():
    """The Mac posts the WHOLE of backlog.txt, parsed, in one call.

    ``{"complete": true, "items": [{"key": …, "text": …, "project": …}]}``

    ``complete`` is the caller asserting it read the entire file; only then may
    keys that are absent be archived. An EMPTY list is refused outright unless
    ``allow_empty`` is set, because "the file was unreadable" and "Graham
    emptied the backlog" arrive looking identical and one of them must not
    archive every row.
    """
    denied = _require_inbox_token()
    if denied is not None:
        return denied
    doc = request.get_json(silent=True)
    if not isinstance(doc, dict):
        return _err("body must be a JSON object")
    items = doc.get("items")
    if not isinstance(items, list):
        return _err("items must be a list")
    if len(items) > MAX_BACKLOG_ITEMS:
        return _err(f"more than {MAX_BACKLOG_ITEMS} items")
    complete = bool(doc.get("complete"))
    if not items and not doc.get("allow_empty"):
        return _err("refusing to sync an empty backlog — an unreadable file and "
                    "an emptied one look identical here; pass allow_empty:true "
                    "if you really mean it")
    now = inbox_db.now_iso()
    seen: list[str] = []
    conn = _conn()
    try:
        with conn:
            for raw in items:
                if not isinstance(raw, dict):
                    return _err("each item must be an object")
                text = inbox_db.clean_text(raw.get("text"), inbox_db.MAX_TEXT)
                if not text:
                    continue
                key = raw.get("key")
                if not isinstance(key, str) or not key.startswith(
                        inbox_db.MIRROR_BACKLOG + ":"):
                    key = inbox_db.normalise_backlog_key(text)
                try:
                    project = clean_project(raw.get("project"))
                except ValueError as exc:
                    return _err(str(exc))
                inbox_db.upsert_mirror_item(
                    conn, mirror_key=key, source="backlog",
                    title=inbox_db.derive_title(text), body=text,
                    project=project, now=now)
                seen.append(key)
            archived = 0
            if complete:
                archived = inbox_db.archive_missing(
                    conn, prefix=inbox_db.MIRROR_BACKLOG + ":",
                    seen_keys=seen, now=now)
        return jsonify({"synced": len(seen), "archived": archived,
                        "complete": complete})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Scheduler-side maintenance (called from scheduler.py, never in a request)
# --------------------------------------------------------------------------- #

def prune_audio(settings, now: float | None = None) -> dict:
    """Delete audio whose transcript is safe, then reconcile files ↔ rows.

    Three conditions for a delete, all required (see
    ``inbox_db.prunable_audio``): the transcript is Whisper-quality, Graham has
    reviewed it, and it is past the retention window. By then the words are in
    the DB and therefore in the DB backup, which is what makes deleting the only
    recording of them tolerable.

    The reconciliation is the price of storing audio as files rather than BLOBs,
    and it runs in both directions: a row pointing at a file that is gone has
    its ``audio_path`` cleared (otherwise the one control Graham taps to check a
    transcript 404s), and a file no row points at is removed. Deliberately NOT
    shaped like ``db.prune``, whose correlated DELETE was measured at 113 s.
    """
    now = time.time() if now is None else now
    cutoff = inbox_db.to_iso(now - settings.inbox_audio_retention_days * 86400)
    audio_dir = settings.inbox_audio_dir
    pruned = cleared = 0
    conn = inbox_db.connect(settings.inbox_db_path)
    try:
        for row in inbox_db.prunable_audio(conn, cutoff):
            inbox_audio.delete(audio_dir, row["audio_path"])
            with conn:
                inbox_db.mark_audio_pruned(conn, row["id"])
            pruned += 1
        for row in inbox_db.dangling_audio_items(conn):
            if inbox_audio.open_path(audio_dir, row["audio_path"]) is None:
                with conn:
                    inbox_db.clear_audio_path(conn, row["id"])
                cleared += 1
        known = inbox_db.known_audio_paths(conn)
    finally:
        conn.close()
    orphans = inbox_audio.sweep_orphans(audio_dir, known)
    if pruned or cleared or orphans:
        log.info("inbox audio prune: %d pruned, %d dangling cleared, %d orphan "
                 "file(s) removed", pruned, cleared, len(orphans))
    return {"pruned": pruned, "cleared": cleared, "orphans": len(orphans)}


def audio_disk_bytes(settings) -> int:
    """Total bytes under the audio tree — a metric for the mirror's heartbeat."""
    total = 0
    for dirpath, _dirs, files in os.walk(settings.inbox_audio_dir):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total
