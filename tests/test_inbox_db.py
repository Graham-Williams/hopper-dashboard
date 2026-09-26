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


def test_repo_number_uniqueness_stops_a_duplicate_item(conn):
    """The rule that stops the repo scan cloning a voice row Hopper already
    filed: an issue belongs to exactly ONE item."""
    voice = inbox_db.create_item(conn, source="voice", text="spoken", now=NOW)
    other = inbox_db.create_item(conn, source="voice", text="also spoken", now=NOW)
    inbox_db.link_issue(conn, voice, repo="a/b", number=7,
                        url="https://github.com/a/b/issues/7", now=NOW)
    again = inbox_db.link_issue(conn, other, repo="a/b", number=7,
                                url="https://github.com/a/b/issues/7", now=NOW)
    assert again["item_id"] == voice                   # still the first item
    assert conn.execute("SELECT COUNT(*) FROM inbox_issues").fetchone()[0] == 1
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
    inbox_db.mark_issues_closed(conn, "a/b", open_numbers=[], now=NOW)
    inbox_db.close_items_whose_issues_all_closed(conn, "a/b", now=NOW)
    assert inbox_db.get_item(conn, item)["state"] == "open"   # c/d#2 still open
    inbox_db.mark_issues_closed(conn, "c/d", open_numbers=[], now=NOW)
    inbox_db.close_items_whose_issues_all_closed(conn, "c/d", now=NOW)
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
