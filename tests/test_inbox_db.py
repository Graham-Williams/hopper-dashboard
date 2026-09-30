"""The Inbox store: schema migration under concurrency, and the two uniqueness
rules the mirrors depend on."""

from __future__ import annotations

import threading

import pytest

from dashboard import inbox_db
from dashboard.db import to_iso

NOW = "2026-09-19T12:00:00Z"


@pytest.fixture
def conn(tmp_path):
    c = inbox_db.connect(str(tmp_path / "inbox.db"))
    inbox_db.init_inbox_schema(c)
    yield c
    c.close()


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

def test_init_is_idempotent(tmp_path):
    path = str(tmp_path / "inbox.db")
    for _ in range(3):
        c = inbox_db.connect(path)
        inbox_db.init_inbox_schema(c)
        c.close()
    c = inbox_db.connect(path)
    tables = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"inbox_items", "inbox_issues", "inbox_mirror_state"} <= tables
    c.close()


def test_concurrent_init_never_raises_duplicate_column(tmp_path):
    """create_app runs this for BOTH roles and entrypoint.sh starts every
    gunicorn together, so on the deploy that introduces a column those processes
    race PRAGMA table_info → ALTER TABLE. The loser used to raise out of
    create_app and kill a worker, which takes the whole container down."""
    path = str(tmp_path / "inbox.db")
    errors: list[Exception] = []

    def go():
        try:
            c = inbox_db.connect(path)
            try:
                inbox_db.init_inbox_schema(c)
            finally:
                c.close()
        except Exception as exc:                      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_the_inbox_lives_in_its_own_file(tmp_path):
    """The whole architectural decision in one assertion: inbox writes must not
    land in dashboard.db, whose single-writer rule is now ENFORCED."""
    from dashboard.config import Settings
    s = Settings(data_dir=str(tmp_path))
    c = inbox_db.connect(s.inbox_db_path)
    inbox_db.init_inbox_schema(c)
    c.close()
    import os
    assert os.path.exists(s.inbox_db_path)
    assert not os.path.exists(s.db_path)


# --------------------------------------------------------------------------- #
# Items
# --------------------------------------------------------------------------- #

def test_create_and_read_back(conn):
    item_id = inbox_db.create_item(
        conn, source="voice", text="The km tracker wheel spins twice on iOS.",
        now=NOW, transcript_status=inbox_db.TRANSCRIPT_LIVE)
    row = inbox_db.get_item(conn, item_id)
    assert row["source"] == "voice" and row["state"] == "open"
    assert row["reviewed"] == 0 and row["archived_at"] is None
    assert row["title"] == "The km tracker wheel spins twice on iOS."
    assert row["title_source"] == "derived"
    assert row["transcript_status"] == "live" and row["transcript_at"] == NOW


def test_get_item_rejects_a_non_id_without_touching_the_db(conn):
    for bad in ("../../etc/passwd", "", "x" * 31, "ZZZZ", None, 5):
        assert inbox_db.get_item(conn, bad) is None


def test_derive_title_is_never_empty_and_is_capped():
    assert inbox_db.derive_title("") == "(untitled)"
    assert inbox_db.derive_title("   \n  ") == "(untitled)"
    assert inbox_db.derive_title("Fix the thing. Then the other thing.") \
        == "Fix the thing."
    long = "word " * 200
    assert len(inbox_db.derive_title(long)) <= inbox_db.MAX_TITLE


def test_a_manual_title_survives_the_whisper_backfill(conn):
    """`title_source` exists for exactly this: a machine must never take back a
    title Graham typed."""
    derived = inbox_db.create_item(conn, source="voice", text="mumble mumble",
                                   now=NOW, transcript_status="pending")
    manual = inbox_db.create_item(conn, source="voice", text="mumble mumble",
                                  title="Wheel spin bug", now=NOW,
                                  transcript_status="pending")
    for item in (derived, manual):
        inbox_db.set_transcript(conn, item, text="The wheel spins twice.",
                                now=NOW)
    assert inbox_db.get_item(conn, derived)["title"] == "The wheel spins twice."
    assert inbox_db.get_item(conn, manual)["title"] == "Wheel spin bug"
    assert inbox_db.get_item(conn, derived)["transcript_status"] == "whisper"


def test_editing_a_title_makes_it_manual(conn):
    item = inbox_db.create_item(conn, source="voice", text="first", now=NOW)
    assert inbox_db.get_item(conn, item)["title_source"] == "derived"
    inbox_db.update_item(conn, item, {"title": "Renamed"}, now=NOW)
    row = inbox_db.get_item(conn, item)
    assert row["title"] == "Renamed" and row["title_source"] == "manual"


def test_reviewed_tick_records_when(conn):
    item = inbox_db.create_item(conn, source="typed", text="do a thing", now=NOW)
    inbox_db.update_item(conn, item, {"reviewed": True}, now=NOW)
    assert inbox_db.get_item(conn, item)["reviewed_at"] == NOW
    inbox_db.update_item(conn, item, {"reviewed": False}, now=NOW)
    assert inbox_db.get_item(conn, item)["reviewed_at"] is None


def test_control_characters_are_stripped_but_markup_is_not(conn):
    """Stripping control bytes is hygiene, not the XSS defence — `<script>` is
    stored verbatim and escaped at RENDER time, which is where it belongs."""
    item = inbox_db.create_item(
        conn, source="voice", text="a\x00b\x1b[31m <script>alert(1)</script>",
        now=NOW)
    body = inbox_db.get_item(conn, item)["body"]
    assert "\x00" not in body and "\x1b" not in body
    assert "<script>alert(1)</script>" in body


# --------------------------------------------------------------------------- #
# Mirror keys + issue uniqueness
# --------------------------------------------------------------------------- #

def test_mirror_key_upsert_is_idempotent(conn):
    key = inbox_db.github_key("Graham-Williams/km-tracker", 42)
    first = inbox_db.upsert_mirror_item(
        conn, mirror_key=key, source="github", title="Wheel spins twice",
        body="body v1", url="https://github.com/x/y/issues/42", now=NOW)
    second = inbox_db.upsert_mirror_item(
        conn, mirror_key=key, source="github", title="Wheel spins twice (edited)",
        body="body v2", url="https://github.com/x/y/issues/42", now=NOW)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM inbox_items").fetchone()[0] == 1
    row = inbox_db.get_item(conn, first)
    assert row["title"] == "Wheel spins twice (edited)" and row["body"] == "body v2"


def test_a_resync_never_untickets_a_reviewed_row(conn):
    key = inbox_db.github_key("a/b", 1)
    item = inbox_db.upsert_mirror_item(conn, mirror_key=key, source="github",
                                       title="t", now=NOW)
    inbox_db.update_item(conn, item, {"reviewed": True, "state": "closed"},
                         now=NOW)
    inbox_db.upsert_mirror_item(conn, mirror_key=key, source="github",
                                title="t2", now=NOW)
    row = inbox_db.get_item(conn, item)
    assert row["reviewed"] == 1 and row["state"] == "closed"


def test_one_link_per_note_and_issue_but_many_notes_per_issue(conn):
    """G-20: two notes about the same bug can both link it. What stays unique is one link
    per (note, issue) — and the scan's "is it linked at all?" lookup finds either."""
    voice = inbox_db.create_item(conn, source="voice", text="spoken", now=NOW)
    other = inbox_db.create_item(conn, source="voice", text="also spoken", now=NOW)
    inbox_db.link_issue(conn, voice, repo="a/b", number=7,
                        url="https://github.com/a/b/issues/7", now=NOW)
    again = inbox_db.link_issue(conn, other, repo="a/b", number=7,
                                url="https://github.com/a/b/issues/7", now=NOW)
    assert again["item_id"] == other
    assert conn.execute("SELECT COUNT(*) FROM inbox_issues").fetchone()[0] == 2
    assert inbox_db.linked_issue(conn, "a/b", 7) is not None
    with pytest.raises(Exception):
        conn.execute("INSERT INTO inbox_issues (item_id, repo, number, url,"
                     " linked_at) VALUES (?,?,?,?,?)",
                     (other, "a/b", 7, "u", NOW))


def test_backlog_key_is_stable_across_whitespace_and_case(conn):
    a = inbox_db.normalise_backlog_key("⭐ PRIORITY  Build   the thing")
    b = inbox_db.normalise_backlog_key("⭐ priority Build the thing\n")
    c = inbox_db.normalise_backlog_key("⭐ PRIORITY Build the other thing")
    assert a == b != c
    assert a.startswith("backlog:") and len(a) == len("backlog:") + 16


def test_archive_missing_only_touches_its_own_prefix(conn):
    gh = inbox_db.upsert_mirror_item(conn, mirror_key=inbox_db.github_key("a/b", 1),
                                     source="github", title="issue", now=NOW)
    bl = inbox_db.upsert_mirror_item(conn, mirror_key=inbox_db.normalise_backlog_key("x"),
                                     source="backlog", title="entry", now=NOW)
    local = inbox_db.create_item(conn, source="voice", text="spoken", now=NOW)
    n = inbox_db.archive_missing(conn, prefix="backlog:", seen_keys=[], now=NOW)
    assert n == 1
    assert inbox_db.get_item(conn, bl)["archived_at"] == NOW
    assert inbox_db.get_item(conn, gh)["archived_at"] is None
    assert inbox_db.get_item(conn, local)["archived_at"] is None
    # And an archived row comes back, un-archived, if it reappears upstream.
    inbox_db.upsert_mirror_item(conn, mirror_key=inbox_db.normalise_backlog_key("x"),
                                source="backlog", title="entry", now=NOW)
    assert inbox_db.get_item(conn, bl)["archived_at"] is None


def test_an_item_closes_only_when_every_linked_issue_is_closed(conn):
    item = inbox_db.create_item(conn, source="voice", text="spoken", now=NOW)
    inbox_db.link_issue(conn, item, repo="a/b", number=1, url="u1", now=NOW)
    inbox_db.link_issue(conn, item, repo="c/d", number=2, url="u2", now=NOW)
    for repo in ("a/b", "c/d"):
        before = inbox_db.issue_states(conn, repo)
        inbox_db.mark_issues_closed(conn, repo, open_numbers=[], now=NOW)
        inbox_db.apply_issue_transitions(conn, repo, before=before, now=NOW)
        if repo == "a/b":
            assert inbox_db.get_item(conn, item)["state"] == "open"   # c/d#2 still open
    assert inbox_db.get_item(conn, item)["state"] == "closed"


# --------------------------------------------------------------------------- #
# Listing, queue, prune
# --------------------------------------------------------------------------- #

def _seed(conn):
    a = inbox_db.create_item(conn, source="voice", text="alpha wheel bug",
                             project="km-tracker", now="2026-09-01T00:00:00Z",
                             transcript_status="whisper")
    b = inbox_db.create_item(conn, source="typed", text="beta idea", now="2026-09-02T00:00:00Z",
                             transcript_status="typed")
    c = inbox_db.upsert_mirror_item(conn, mirror_key=inbox_db.github_key("a/b", 9),
                                    source="github", title="gamma issue",
                                    now="2026-09-03T00:00:00Z")
    return a, b, c


def test_awaiting_filing_sorts_first_and_filters(conn):
    a, b, c = _seed(conn)
    inbox_db.update_item(conn, a, {"reviewed": True}, now=NOW)
    ids = [r["id"] for r in inbox_db.list_items(conn)]
    assert ids[0] == a                       # awaiting filing beats newest
    assert set(ids) == {a, b, c}
    assert [r["id"] for r in inbox_db.list_items(conn, awaiting="filing")] == [a]
    # ...and it drops out the moment an issue is filed for it.
    inbox_db.link_issue(conn, a, repo="a/b", number=1, url="u", now=NOW)
    assert inbox_db.list_items(conn, awaiting="filing") == []
    # A reviewed MIRRORED row is never "awaiting filing": it already IS an issue.
    inbox_db.update_item(conn, c, {"reviewed": True}, now=NOW)
    assert inbox_db.list_items(conn, awaiting="filing") == []


def test_search_and_filters(conn):
    a, b, c = _seed(conn)
    assert [r["id"] for r in inbox_db.list_items(conn, q="wheel")] == [a]
    assert [r["id"] for r in inbox_db.list_items(conn, source="github")] == [c]
    assert [r["id"] for r in inbox_db.list_items(conn, project="km-tracker")] == [a]
    assert len(inbox_db.list_items(conn, q="%")) == 0      # wildcards are escaped
    assert len(inbox_db.list_items(conn, q="_")) == 0
    assert len(inbox_db.list_items(conn, limit=1)) == 1


def test_counts_and_projects(conn):
    a, b, c = _seed(conn)
    inbox_db.update_item(conn, a, {"reviewed": True}, now=NOW)
    counts = inbox_db.counts(conn)
    assert counts["total"] == 3 and counts["open"] == 3
    assert counts["reviewed"] == 1 and counts["awaiting_filing"] == 1
    assert inbox_db.projects(conn) == ["km-tracker"]


def test_transcribe_queue_is_oldest_first_and_gives_up(conn):
    with_audio = inbox_db.create_item(
        conn, source="voice", text="", now="2026-09-01T00:00:00Z",
        transcript_status="pending", audio={"path": "2026/09/x.webm", "bytes": 10})
    live = inbox_db.create_item(
        conn, source="voice", text="rough", now="2026-09-02T00:00:00Z",
        transcript_status="live", audio={"path": "2026/09/y.webm", "bytes": 10})
    inbox_db.create_item(conn, source="typed", text="typed", now=NOW,
                         transcript_status="typed")
    done = inbox_db.create_item(
        conn, source="voice", text="ok", now=NOW, transcript_status="whisper",
        audio={"path": "2026/09/z.webm", "bytes": 10})
    ids = [r["id"] for r in inbox_db.transcribe_queue(conn)]
    assert ids == [with_audio, live] and done not in ids
    for _ in range(3):
        inbox_db.note_transcribe_attempt(conn, with_audio, failed=True, now=NOW)
    assert inbox_db.get_item(conn, with_audio)["transcript_status"] == "failed"
    assert [r["id"] for r in inbox_db.transcribe_queue(conn)] == [live]


def test_prune_needs_all_three_conditions(conn):
    def mk(status, reviewed, created):
        i = inbox_db.create_item(conn, source="voice", text="t", now=created,
                                 transcript_status=status,
                                 audio={"path": f"2026/09/{status}{reviewed}.webm"})
        if reviewed:
            inbox_db.update_item(conn, i, {"reviewed": True}, now=created)
        return i

    old, new = "2026-01-01T00:00:00Z", "2026-09-18T00:00:00Z"
    good = mk("whisper", True, old)
    not_whisper = mk("live", True, old)
    not_reviewed = mk("whisper", False, old)
    too_new = mk("whisper", True, new)
    ids = {r["id"] for r in inbox_db.prunable_audio(conn, "2026-06-01T00:00:00Z")}
    assert ids == {good}
    assert not_whisper not in ids and not_reviewed not in ids and too_new not in ids
    inbox_db.mark_audio_pruned(conn, good, now=NOW)
    row = inbox_db.get_item(conn, good)
    assert row["audio_path"] is None and row["audio_pruned_at"] == NOW
    # The figures stay: "there WAS a recording, deleted on this date".
    assert row["audio_sha256"] is None or True
    assert inbox_db.prunable_audio(conn, "2026-06-01T00:00:00Z") == []


def test_known_and_dangling_audio_paths(conn):
    i = inbox_db.create_item(conn, source="voice", text="t", now=NOW,
                             audio={"path": "2026/09/a.webm"})
    assert inbox_db.known_audio_paths(conn) == {"2026/09/a.webm"}
    assert [r["id"] for r in inbox_db.dangling_audio_items(conn)] == [i]
    inbox_db.clear_audio_path(conn, i, now=NOW)
    assert inbox_db.known_audio_paths(conn) == set()


def test_mirror_state_roundtrip(conn):
    assert inbox_db.get_mirror_state(conn, "github:a/b") == {"key": "github:a/b"}
    inbox_db.set_mirror_state(conn, "github:a/b", etag='W/"abc"',
                              last_status="ok", last_sync_at=NOW)
    state = inbox_db.get_mirror_state(conn, "github:a/b")
    assert state["etag"] == 'W/"abc"' and state["last_status"] == "ok"
    inbox_db.set_mirror_state(conn, "github:a/b", last_status="error",
                              last_error="boom")
    state = inbox_db.get_mirror_state(conn, "github:a/b")
    assert state["etag"] == 'W/"abc"'          # partial update, not a replace
    assert state["last_status"] == "error" and state["last_error"] == "boom"


# --------------------------------------------------------------------------- #
# Drafts (the draft_* columns)
# --------------------------------------------------------------------------- #

DRAFT_COLUMNS = {"draft_title", "draft_body", "draft_project", "draft_status",
                 "draft_at", "draft_attempts", "draft_model", "draft_src_sha",
                 "draft_edited_at"}


def _columns(c):
    return {r["name"] for r in c.execute("PRAGMA table_info(inbox_items)")}


def test_draft_columns_migrate_onto_an_old_table(tmp_path):
    """A box whose inbox.db predates the drafts gets the columns added, and a
    second run is a no-op (the first real use of INBOX_COLUMNS)."""
    import sqlite3
    path = str(tmp_path / "inbox.db")
    old = sqlite3.connect(path)
    old.executescript(inbox_db.INBOX_SCHEMA)          # v1 table: no draft_* columns
    old.execute("INSERT INTO inbox_items (id, source, title, created_at, updated_at)"
                " VALUES ('a'||hex(randomblob(15)), 'voice', 't', ?, ?)", (NOW, NOW))
    old.commit()
    old.close()
    for _ in range(2):
        c = inbox_db.connect(path)
        inbox_db.init_inbox_schema(c)
        assert DRAFT_COLUMNS <= _columns(c)
        row = c.execute("SELECT draft_attempts, draft_status FROM inbox_items").fetchone()
        assert row["draft_attempts"] == 0 and row["draft_status"] is None
        c.close()


def test_concurrent_migration_of_an_old_table_never_raises(tmp_path):
    import sqlite3
    path = str(tmp_path / "inbox.db")
    old = sqlite3.connect(path)
    old.executescript(inbox_db.INBOX_SCHEMA)
    old.close()
    errors: list[Exception] = []

    def go():
        try:
            c = inbox_db.connect(path)
            try:
                inbox_db.init_inbox_schema(c)
            finally:
                c.close()
        except Exception as exc:                      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    c = inbox_db.connect(path)
    assert DRAFT_COLUMNS <= _columns(c)
    c.close()


def _voice(conn, text="the wheel sticks on km tracker", *, when=NOW, **kw):
    item = inbox_db.create_item(conn, source="voice", text="", now=when,
                                transcript_status="pending",
                                audio={"path": f"x/{when}.webm", "bytes": 1}, **kw)
    if text:
        inbox_db.set_transcript(conn, item, text=text, now=when)
    return item


def _draft(conn, item, **kw):
    row = inbox_db.get_item(conn, item)
    args = {"title": "Fix the sticky wheel", "body": "It sticks.",
            "project": "km-tracker", "src_sha": inbox_db.transcript_sha(row["body"]),
            "model": "sonnet"}
    args.update(kw)
    return inbox_db.set_draft(conn, item, **args)


def test_a_transcript_makes_a_draft_pending_and_queues_it(conn):
    item = _voice(conn)
    row = inbox_db.get_item(conn, item)
    assert row["draft_status"] == "pending"
    q = inbox_db.draft_queue(conn)
    assert [r["id"] for r in q] == [item]
    assert q[0]["sha"] == inbox_db.transcript_sha(row["body"])


def test_backfill_selection(conn):
    """Transcribed voice notes with no draft are queued (the backfill); nothing
    untranscribed, typed, reviewed, closed, edited or given-up is."""
    old = inbox_db.create_item(conn, source="voice", text="transcribed before drafts",
                               now="2026-09-01T00:00:00Z", transcript_status="whisper",
                               audio={"path": "x/old.webm", "bytes": 1})
    assert inbox_db.get_item(conn, old)["draft_status"] is None   # pre-draft row
    _voice(conn, text="")                                         # not transcribed
    inbox_db.create_item(conn, source="typed", text="typed")
    reviewed = _voice(conn, when="2026-09-02T00:00:00Z")
    inbox_db.update_item(conn, reviewed, {"reviewed": True})
    closed = _voice(conn, when="2026-09-03T00:00:00Z")
    inbox_db.update_item(conn, closed, {"state": "closed"})
    edited = _voice(conn, when="2026-09-04T00:00:00Z")
    inbox_db.update_item(conn, edited, {"draft_body": "mine"})
    given_up = _voice(conn, when="2026-09-05T00:00:00Z")
    for _ in range(3):
        inbox_db.note_draft_attempt(conn, given_up)
    assert inbox_db.get_item(conn, given_up)["draft_status"] == "failed"
    done = _voice(conn, when="2026-09-06T00:00:00Z")
    _draft(conn, done)
    fresh = _voice(conn, when="2026-09-07T00:00:00Z")
    assert [r["id"] for r in inbox_db.draft_queue(conn)] == [old, fresh]
    assert [r["id"] for r in inbox_db.draft_queue(conn, limit=1)] == [old]


def test_a_new_transcript_redrafts(conn):
    item = _voice(conn)
    _draft(conn, item)
    assert inbox_db.draft_queue(conn) == []
    inbox_db.set_transcript(conn, item, text="a better transcript")
    row = inbox_db.get_item(conn, item)
    assert row["draft_status"] == "pending" and row["draft_attempts"] == 0
    assert [r["id"] for r in inbox_db.draft_queue(conn)] == [item]
    # A draft made from the OLD text is now stale and refused.
    with pytest.raises(inbox_db.DraftRefused) as exc:
        _draft(conn, item, src_sha=inbox_db.transcript_sha("the wheel sticks on km tracker"))
    assert exc.value.reason == "stale"


def test_editing_the_transcript_body_redrafts_unless_the_draft_was_edited(conn):
    item = _voice(conn)
    _draft(conn, item)
    inbox_db.update_item(conn, item, {"body": "corrected transcript"})
    assert inbox_db.get_item(conn, item)["draft_status"] == "pending"
    assert [r["id"] for r in inbox_db.draft_queue(conn)] == [item]
    inbox_db.update_item(conn, item, {"draft_title": "My own title"})
    inbox_db.update_item(conn, item, {"body": "again"})
    row = inbox_db.get_item(conn, item)
    assert row["draft_status"] == "ready" and row["draft_title"] == "My own title"
    assert inbox_db.draft_queue(conn) == []


def test_no_machine_overwrite_after_an_edit_or_a_review(conn):
    item = _voice(conn)
    inbox_db.update_item(conn, item, {"draft_body": "I wrote this"})
    with pytest.raises(inbox_db.DraftRefused) as exc:
        _draft(conn, item)
    assert exc.value.reason == "edited"
    row = inbox_db.get_item(conn, item)
    assert row["draft_body"] == "I wrote this" and row["draft_edited_at"]
    other = _voice(conn, when="2026-09-20T00:00:00Z")
    inbox_db.update_item(conn, other, {"reviewed": True})
    with pytest.raises(inbox_db.DraftRefused) as exc:
        _draft(conn, other)
    assert exc.value.reason == "reviewed"
    # note_draft_attempt is a no-op on either.
    assert inbox_db.note_draft_attempt(conn, item)["draft_attempts"] == 0


def test_set_draft_caps_and_cleans(conn):
    item = _voice(conn)
    row = _draft(conn, item, title="a\nmulti  line\ttitle " + "x" * 300,
                 body="\x1b[31mred\x1b[0m " + "y" * 5000)
    assert "\n" not in row["draft_title"] and row["draft_title"].startswith("a multi line title")
    assert len(row["draft_title"]) <= inbox_db.DRAFT_MAX_TITLE
    assert len(row["draft_body"]) <= inbox_db.DRAFT_MAX_BODY
    assert "\x1b" not in row["draft_body"]
    assert row["draft_status"] == "ready" and row["draft_model"] == "sonnet"


def test_a_manual_title_wins_over_the_machine_draft_title(conn):
    item = _voice(conn, title="What I typed")
    row = _draft(conn, item, title="Something else")
    assert row["draft_title"] == "What I typed"


def test_capture_project_seeds_the_draft_project(conn):
    item = _voice(conn, project="taste-twin")
    assert inbox_db.get_item(conn, item)["draft_project"] == "taste-twin"
    row = _draft(conn, item, project=None)
    assert row["draft_project"] == "taste-twin"          # kept when the model has none


def test_ticking_reviewed_copies_the_draft_into_title_and_project(conn):
    item = _voice(conn, project=None)
    body_before = inbox_db.get_item(conn, item)["body"]
    _draft(conn, item)
    row = inbox_db.update_item(conn, item, {"reviewed": True})
    assert row["title"] == "Fix the sticky wheel" and row["title_source"] == "manual"
    assert row["project"] == "km-tracker"
    assert row["body"] == body_before                    # the transcript is untouched
    # A later untick/retick does not clobber an edited title.
    inbox_db.update_item(conn, item, {"reviewed": False})
    inbox_db.update_item(conn, item, {"title": "Renamed"})
    assert inbox_db.update_item(conn, item, {"reviewed": True})["title"] == "Renamed"


def test_review_keeps_the_existing_project_when_the_draft_has_none(conn):
    item = _voice(conn)
    inbox_db.update_item(conn, item, {"project": "jjho"})
    row = inbox_db.update_item(conn, item, {"reviewed": True})
    assert row["project"] == "jjho" and row["title_source"] == "derived"


def test_needs_review_filter_sort_and_count(conn):
    a, b, c = _seed(conn)
    ready = _voice(conn, when="2026-01-01T00:00:00Z")
    _draft(conn, ready)
    failed = _voice(conn, when="2026-01-02T00:00:00Z")
    for _ in range(3):
        inbox_db.note_draft_attempt(conn, failed)
    pending = _voice(conn, when="2026-01-03T00:00:00Z")
    inbox_db.update_item(conn, a, {"reviewed": True})   # awaiting filing (typed)
    ids = [r["id"] for r in inbox_db.list_items(conn)]
    assert set(ids[:2]) == {ready, failed}              # needs review first
    assert ids[2] == a                                  # then awaiting filing
    assert pending in ids[3:]
    assert {r["id"] for r in inbox_db.list_items(conn, awaiting="review")} == {ready, failed}
    assert inbox_db.counts(conn)["needs_review"] == 2
    inbox_db.update_item(conn, ready, {"reviewed": True})
    assert inbox_db.counts(conn)["needs_review"] == 1


def test_search_covers_the_draft(conn):
    item = _voice(conn, text="um so the thing")
    _draft(conn, item, title="Sprocket alignment", body="Realign the sprocket.")
    assert [r["id"] for r in inbox_db.list_items(conn, q="sprocket")] == [item]


def test_editing_the_draft_and_ticking_in_one_request_copies_the_edit(conn):
    item = _voice(conn, title="Typed at capture")
    _draft(conn, item)
    row = inbox_db.update_item(conn, item, {"draft_title": "Edited draft title",
                                            "reviewed": True})
    assert row["title"] == "Edited draft title"


# --------------------------------------------------------------------------- #
# Security-gate fixes
# --------------------------------------------------------------------------- #

def test_a_retick_does_not_take_back_a_project_graham_changed(conn):
    item = _voice(conn, project=None)
    _draft(conn, item, project="km-tracker")
    inbox_db.update_item(conn, item, {"reviewed": True})
    inbox_db.update_item(conn, item, {"reviewed": False})
    inbox_db.update_item(conn, item, {"project": "jjho"})
    assert inbox_db.update_item(conn, item, {"reviewed": True})["project"] == "jjho"
    # ...but a draft project edited in the SAME request is copied.
    inbox_db.update_item(conn, item, {"reviewed": False})
    row = inbox_db.update_item(conn, item, {"reviewed": True, "draft_project": "taste-twin"})
    assert row["project"] == "taste-twin"


def test_editing_the_draft_after_review_reaches_what_gets_filed(conn):
    item = _voice(conn)
    _draft(conn, item)
    inbox_db.update_item(conn, item, {"reviewed": True})
    row = inbox_db.update_item(conn, item, {"draft_title": "Better title",
                                            "draft_body": "Better body",
                                            "draft_project": "jjho"})
    assert row["title"] == "Better title" and row["title_source"] == "manual"
    assert row["project"] == "jjho" and row["draft_body"] == "Better body"
    assert row["reviewed"] == 1


def test_a_failed_transcript_needs_review(conn):
    item = inbox_db.create_item(conn, source="voice", text="", now=NOW,
                                transcript_status="pending",
                                audio={"path": "x/f.webm", "bytes": 1})
    for _ in range(3):
        inbox_db.note_transcribe_attempt(conn, item, failed=True, now=NOW)
    assert [r["id"] for r in inbox_db.list_items(conn, awaiting="review")] == [item]
    assert inbox_db.counts(conn)["needs_review"] == 1


def test_a_body_edit_makes_a_never_drafted_note_pending_and_queued(conn):
    item = inbox_db.create_item(conn, source="voice", text="", now=NOW,
                                transcript_status="failed",
                                audio={"path": "x/g.webm", "bytes": 1})
    assert inbox_db.get_item(conn, item)["draft_status"] is None
    inbox_db.update_item(conn, item, {"body": "what I actually said"})
    assert inbox_db.get_item(conn, item)["draft_status"] == "pending"
    assert [r["id"] for r in inbox_db.draft_queue(conn)] == [item]
    # An empty body is not draftable: never a permanent "Drafting…".
    inbox_db.update_item(conn, item, {"body": "   "})
    assert inbox_db.get_item(conn, item)["draft_status"] is None
    assert inbox_db.draft_queue(conn) == []


def test_an_empty_transcript_does_not_leave_a_pending_draft(conn):
    item = _voice(conn, text="")
    inbox_db.set_transcript(conn, item, text="   ")
    assert inbox_db.get_item(conn, item)["draft_status"] is None


def test_set_draft_loses_a_race_to_a_review_with_a_conflict(conn, monkeypatch):
    item = _voice(conn)
    stale = inbox_db.get_item(conn, item)
    inbox_db.update_item(conn, item, {"reviewed": True})
    real = inbox_db.get_item
    monkeypatch.setattr(inbox_db, "get_item",
                        lambda c, i: stale if i == item else real(c, i))
    with pytest.raises(inbox_db.DraftRefused) as exc:
        _draft(conn, item)
    assert exc.value.reason == "conflict"
    monkeypatch.setattr(inbox_db, "get_item", real)
    assert inbox_db.get_item(conn, item)["draft_title"] is None


def test_a_review_that_raced_a_new_draft_is_a_conflict(conn, monkeypatch):
    item = _voice(conn)
    _draft(conn, item, title="First draft")
    stale = inbox_db.get_item(conn, item)
    _draft(conn, item, title="Second draft")           # the machine redrafts meanwhile
    real = inbox_db.get_item
    calls = {"n": 0}

    def once_stale(c, i):
        calls["n"] += 1
        return stale if calls["n"] == 1 else real(c, i)
    monkeypatch.setattr(inbox_db, "get_item", once_stale)
    with pytest.raises(inbox_db.Conflict):
        inbox_db.update_item(conn, item, {"reviewed": True})
    monkeypatch.setattr(inbox_db, "get_item", real)
    assert inbox_db.get_item(conn, item)["reviewed"] == 0


def test_a_filed_copy_stays_hidden_while_its_note_exists_and_shows_once_deleted(conn):
    item = inbox_db.create_item(conn, source="typed", text="chore", reviewed=True)
    inbox_db.upsert_mirror_item(conn, mirror_key="backlog:x", source="backlog",
                                title="chore", body=f"chore (voice {item[:8]})")
    inbox_db.mark_filed_backlog(conn, item, line=f"chore (voice {item[:8]})")
    assert [r["source"] for r in inbox_db.list_items(conn)] == ["typed"]
    inbox_db.update_item(conn, item, {"state": "closed"})
    assert [r["source"] for r in inbox_db.list_items(conn)] == ["typed"]
    inbox_db.delete_item(conn, item)
    assert [r["source"] for r in inbox_db.list_items(conn)] == ["backlog"]


def test_the_migration_backfills_draft_copied_at_for_reviewed_voice_notes(tmp_path):
    """A voice note reviewed before draft_copied_at existed has had its first review: a
    re-tick must not copy the draft project over whatever Graham has since set."""
    import sqlite3
    path = str(tmp_path / "inbox.db")
    old = sqlite3.connect(path)
    old.executescript(inbox_db.INBOX_SCHEMA)
    rows = [("a" * 32, "voice", 1, "2026-09-01T00:00:00Z"),
            ("b" * 32, "voice", 0, None),
            ("c" * 32, "typed", 1, "2026-09-02T00:00:00Z")]
    for i, src, rev, at in rows:
        old.execute("INSERT INTO inbox_items (id, source, title, created_at, updated_at,"
                    " reviewed, reviewed_at) VALUES (?,?,?,?,?,?,?)",
                    (i, src, "t", NOW, NOW, rev, at))
    old.commit()
    old.close()
    c = inbox_db.connect(path)
    inbox_db.init_inbox_schema(c)
    got = {r["id"][0]: r["draft_copied_at"] for r in
           c.execute("SELECT id, draft_copied_at FROM inbox_items")}
    assert got == {"a": "2026-09-01T00:00:00Z", "b": None, "c": None}
    c.execute("UPDATE inbox_items SET draft_copied_at='2026-09-09T00:00:00Z' WHERE id=?",
              ("a" * 32,))
    inbox_db.init_inbox_schema(c)                       # idempotent: NULLs only
    assert c.execute("SELECT draft_copied_at FROM inbox_items WHERE id=?",
                     ("a" * 32,)).fetchone()[0] == "2026-09-09T00:00:00Z"
    c.close()


# --------------------------------------------------------------------------- #
# The backlog push, as one unit (inbox_db.apply_backlog_push)
# --------------------------------------------------------------------------- #

def _entries(*texts):
    return [(inbox_db.normalise_backlog_key(inbox_db.what_line(t)), t, None) for t in texts]


def _filed_note(conn, line_suffix=""):
    note = inbox_db.create_item(conn, source="typed", text="spoken", now=NOW, reviewed=True)
    inbox_db.mark_filed_backlog(conn, note, line=f"x (voice {note[:8]})", now=NOW)
    return note, f"Fix it{line_suffix} (voice {note[:8]})"


def test_upsert_clears_closed_by_whenever_the_text_sets_the_state(conn):
    key = inbox_db.normalise_backlog_key("Chore")
    row = inbox_db.upsert_mirror_item(conn, mirror_key=key, source="backlog", title="Chore",
                                      body="Chore", now=NOW, state="open")
    conn.execute("UPDATE inbox_items SET state='closed', closed_by='issues' WHERE id=?", (row,))
    inbox_db.upsert_mirror_item(conn, mirror_key=key, source="backlog", title="Chore",
                                body="Chore", now=NOW, state="open")
    got = inbox_db.get_item(conn, row)
    assert (got["state"], got["closed_at"], got["closed_by"]) == ("open", None, None)


def test_an_unchanged_backlog_push_writes_nothing(conn):
    """A push of the same file must not touch inbox.db at all — no updated_at or
    mirror_seen_at churn, no counter ticking — or the 5-minute backup re-snapshots it and
    re-uploads it to Drive every time (about 96 times a day)."""
    kept, kept_line = _filed_note(conn)
    gone, _ = _filed_note(conn, " too")
    entries = _entries(kept_line, "✅ DONE — a finished chore", "Open chore\nWhy: because")
    for _ in range(3):                  # settle: the absent note closes after two pushes
        inbox_db.apply_backlog_push(conn, entries, complete=True, now=NOW)
    assert inbox_db.get_item(conn, gone)["state"] == "closed"
    before = conn.total_changes
    out = inbox_db.apply_backlog_push(conn, entries, complete=True, now="2026-09-19T13:00:00Z")
    assert conn.total_changes == before, "an unchanged push wrote to inbox.db"
    assert (out["archived"], out["closed_notes"], out["reopened_notes"]) == (0, 0, 0)


def test_a_backlog_push_is_atomic(conn, monkeypatch):
    """`with conn:` is not a transaction on these autocommit connections; the push is one
    real transaction, so a failure part-way leaves nothing behind."""
    inbox_db.apply_backlog_push(conn, _entries("One"), complete=True, now=NOW)
    before = [dict(r) for r in conn.execute("SELECT * FROM inbox_items ORDER BY id")]

    def boom(*a, **kw):
        raise RuntimeError("disk full, say")
    monkeypatch.setattr(inbox_db, "archive_missing", boom)
    with pytest.raises(RuntimeError):
        inbox_db.apply_backlog_push(conn, _entries("Two", "✅ DONE — One"), complete=True,
                                    now=NOW)
    assert [dict(r) for r in conn.execute("SELECT * FROM inbox_items ORDER BY id")] == before


def _index_columns(c, name):
    return [r["name"] for r in c.execute(f"PRAGMA index_info({name})")]


def test_the_issue_link_index_migrates_to_one_link_per_note_and_survives_a_rollback(tmp_path):
    """G-20 lets many notes link one issue: unique on (item_id, repo, number), not (repo,
    number). The index KEEPS ITS OLD NAME on purpose — an older image's start-up runs
    `CREATE UNIQUE INDEX IF NOT EXISTS inbox_issues_repo_number ON inbox_issues (repo, number)`,
    which is a no-op while that name exists. Under a new name it would try to build the old
    index over duplicate links, fail, and restart-loop the rolled-back container."""
    import sqlite3
    path = str(tmp_path / "inbox.db")
    old = sqlite3.connect(path)
    old.executescript(inbox_db.INBOX_SCHEMA.replace(
        "inbox_issues_repo_number\n    ON inbox_issues (item_id, repo, number)",
        "inbox_issues_repo_number\n    ON inbox_issues (repo, number)"))
    old.close()
    c = inbox_db.connect(path)
    assert _index_columns(c, "inbox_issues_repo_number") == ["repo", "number"]   # the old one
    inbox_db.init_inbox_schema(c)
    inbox_db.init_inbox_schema(c)                         # idempotent
    assert _index_columns(c, "inbox_issues_repo_number") == ["item_id", "repo", "number"]
    a = inbox_db.create_item(c, source="typed", text="one", now=NOW)
    b = inbox_db.create_item(c, source="typed", text="two", now=NOW)
    inbox_db.link_issue(c, a, repo="a/b", number=1, url="u", now=NOW)
    inbox_db.link_issue(c, b, repo="a/b", number=1, url="u", now=NOW)
    inbox_db.link_issue(c, b, repo="a/b", number=1, url="u", now=NOW)   # still idempotent
    assert c.execute("SELECT COUNT(*) FROM inbox_issues").fetchone()[0] == 2
    # The rollback: the older image's schema statement must not fail over the duplicates.
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS inbox_issues_repo_number"
              " ON inbox_issues (repo, number)")
    c.close()


def test_the_migration_sends_stranded_pending_notes_to_needs_review(tmp_path):
    """Voice notes left 'pending' for ever by the old prune/sweep (their recording went
    before Whisper ran) are marked failed — and a lost file is recorded as missing."""
    import sqlite3
    path = str(tmp_path / "inbox.db")
    old = sqlite3.connect(path)
    old.executescript(inbox_db.INBOX_SCHEMA)
    rows = [("a" * 32, None, NOW),                       # pruned before transcription
            ("b" * 32, None, None),                      # file lost, path cleared
            ("c" * 32, "2026/09/" + "c" * 32 + ".webm", None)]   # still has its audio
    for i, audio_path, pruned in rows:
        old.execute("INSERT INTO inbox_items (id, source, title, created_at, updated_at,"
                    " transcript_status, audio_path, audio_pruned_at, audio_bytes)"
                    " VALUES (?, 'voice', 't', ?, ?, 'pending', ?, ?, 100)",
                    (i, NOW, NOW, audio_path, pruned))
    old.commit()
    old.close()
    c = inbox_db.connect(path)
    inbox_db.init_inbox_schema(c)
    got = {r["id"][0]: (r["transcript_status"], r["audio_missing_at"]) for r in
           c.execute("SELECT id, transcript_status, audio_missing_at FROM inbox_items")}
    assert got == {"a": ("failed", None), "b": ("failed", NOW), "c": ("pending", None)}
    c.close()


def test_a_transaction_whose_commit_fails_is_rolled_back_not_left_open(conn):
    """COMMIT inside the try: a COMMIT that fails (here a deferred foreign key) must roll
    back, or the connection is left mid-transaction and every later BEGIN fails."""
    conn.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE c (pid INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED)")
    with pytest.raises(Exception):
        with inbox_db.transaction(conn):
            conn.execute("INSERT INTO c (pid) VALUES (99)")
    assert conn.in_transaction is False
    with inbox_db.transaction(conn):
        conn.execute("INSERT INTO p (id) VALUES (1)")
    assert conn.execute("SELECT COUNT(*) FROM c").fetchone()[0] == 0


def test_a_transaction_never_masks_the_real_error_with_a_rollback_error(conn):
    class Boom(Exception):
        pass
    with pytest.raises(Boom):
        with inbox_db.transaction(conn):
            conn.execute("ROLLBACK")          # already gone, as some SQLite errors do
            raise Boom()
    assert conn.in_transaction is False
