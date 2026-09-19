"""The Inbox's two scheduler cadences: they are independent of the probe cycle,
independent of each other, and each re-arms from its own END clock."""

from __future__ import annotations

import time

import pytest

from dashboard import db, github_mirror, inbox_audio, inbox_db
from dashboard.scheduler import Scheduler
from dashboard.services import INBOX_GITHUB_JOB_ID, Core
from tests.conftest import JOBS_DOC, RecordingNotifier, pin_created_at

NOW = 1_800_000_000.0
REPO = "Graham-Williams/km-tracker"


@pytest.fixture
def settings(settings):
    settings.inbox_github_repos = (REPO,)
    return settings


@pytest.fixture
def core(settings, notifier):
    """Overrides the shared fixture to declare the REAL heartbeat job id — the
    one the scheduler posts to. The shared JOBS_DOC carries a generic `worker`
    for kind coverage; this suite needs `inbox-github-sync` by name."""
    import copy

    from dashboard.registry import parse_registry
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"].append({"id": INBOX_GITHUB_JOB_ID, "name": "Inbox GitHub mirror",
                        "machine": "box", "kind": "worker",
                        "protects": "the inbox's view of open issues",
                        "method": "in-container scheduler", "cadence_s": 900,
                        "grace_s": 900, "alert_after_s": 0})
    c = Core(settings, parse_registry(doc), notifier)
    c.init_store()
    pin_created_at(settings)
    return c


@pytest.fixture
def sched(core):
    return Scheduler(core, tick_interval_s=60, probe_interval_s=300)


def ok(items, etag='W/"e1"'):
    return github_mirror.MirrorResponse(status=200, headers={"ETag": etag},
                                        body=items)


def issue(number, title="An issue"):
    return {"number": number, "title": title, "body": ""}


# --------------------------------------------------------------------------- #
# Cadence + isolation
# --------------------------------------------------------------------------- #

def test_the_inbox_jobs_run_on_their_own_cadences(core, sched, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run_probe_cycle", lambda now=None: None)
    monkeypatch.setattr(core, "recompute_all", lambda now=None: None)
    monkeypatch.setattr(core, "sync_inbox_github",
                        lambda now=None: calls.append(("github", now)))
    monkeypatch.setattr(core, "prune_inbox_audio",
                        lambda now=None: calls.append(("prune", now)))
    sched.github_interval_s, sched.prune_interval_s = 900, 3600
    sched.step(NOW)                                   # first pass: both run
    assert [c[0] for c in calls] == ["github", "prune"]
    sched.step(NOW + 60)                              # neither is due
    assert len(calls) == 2
    # A hair past the interval, not exactly on it: re-arming from the END clock
    # means `last_github` is a few microseconds past NOW, which is the whole
    # point (see Scheduler._run_due) and makes an exact-boundary step a miss.
    sched.step(NOW + 901)                             # github only
    assert [c[0] for c in calls] == ["github", "prune", "github"]
    sched.step(NOW + 3601)
    assert [c[0] for c in calls] == ["github", "prune", "github", "github", "prune"]


def test_a_failing_github_sync_kills_neither_the_probe_cycle_nor_the_tick(
        core, sched, monkeypatch):
    """Four cadences, four independent try/excepts. The board's own probing must
    not be able to die because GitHub 500'd."""
    seen = []
    monkeypatch.setattr(core, "run_probe_cycle", lambda now=None: seen.append("probe"))
    monkeypatch.setattr(core, "recompute_all", lambda now=None: seen.append("tick"))

    def boom(now=None):
        raise RuntimeError("github is down")
    monkeypatch.setattr(core, "sync_inbox_github", boom)
    monkeypatch.setattr(core, "prune_inbox_audio",
                        lambda now=None: seen.append("prune"))
    sched.step(NOW)                                   # must not raise
    sched.step(NOW + 60)
    sched.step(NOW + 300)
    assert seen == ["probe", "prune", "tick", "probe"]
    assert sched.last_tick == NOW + 300
    assert sched.last_probe is not None


def test_a_failing_prune_does_not_stop_the_github_sync(core, sched, monkeypatch):
    seen = []
    monkeypatch.setattr(core, "run_probe_cycle", lambda now=None: None)
    monkeypatch.setattr(core, "recompute_all", lambda now=None: None)
    monkeypatch.setattr(core, "sync_inbox_github",
                        lambda now=None: seen.append("github"))
    monkeypatch.setattr(core, "prune_inbox_audio",
                        lambda now=None: (_ for _ in ()).throw(OSError("disk")))
    sched.github_interval_s = sched.prune_interval_s = 900
    sched.step(NOW)
    sched.step(NOW + 901)
    assert seen == ["github", "github"]


def test_each_job_rearms_from_its_own_end_clock(core, sched, monkeypatch):
    """Re-arming from the START would make a sync that spent five minutes being
    rate-limited instantly due again on return — which is how an anonymous
    client talks itself into a longer ban."""
    monkeypatch.setattr(core, "run_probe_cycle", lambda now=None: None)
    monkeypatch.setattr(core, "recompute_all", lambda now=None: None)
    monkeypatch.setattr(core, "prune_inbox_audio", lambda now=None: None)

    def slow(now=None):
        time.sleep(0.05)
    monkeypatch.setattr(core, "sync_inbox_github", slow)
    sched.step(NOW)
    assert sched.last_github >= NOW + 0.05
    assert sched.last_prune == pytest.approx(NOW, abs=0.05)


def test_the_intervals_have_floors(core):
    core.settings.inbox_github_interval_s = 1
    core.settings.inbox_prune_interval_s = 1
    s = Scheduler(core)
    assert s.github_interval_s == 60 and s.prune_interval_s == 300


# --------------------------------------------------------------------------- #
# The sync itself writes inbox.db and heartbeats dashboard.db
# --------------------------------------------------------------------------- #

def test_the_sync_writes_inbox_db_and_posts_a_heartbeat(core, settings,
                                                        monkeypatch):
    pages = [ok([issue(1, "Wheel spins twice")])]
    monkeypatch.setattr(github_mirror, "default_fetch",
                        lambda url, headers: pages.pop(0))
    summary = core.sync_inbox_github(now=NOW)
    assert summary["ok"] == 1 and summary["issues"] == 1
    conn = inbox_db.connect(settings.inbox_db_path)
    try:
        assert [r["title"] for r in inbox_db.list_items(conn)] == ["Wheel spins twice"]
    finally:
        conn.close()
    row = db.last_run(core.connect(), INBOX_GITHUB_JOB_ID)
    assert row["status"] == "ok" and row["source"] == "scheduler"
    assert "1 open issue" in row["reason"]


def test_a_failed_sync_heartbeats_fail(core, monkeypatch):
    monkeypatch.setattr(
        github_mirror, "default_fetch",
        lambda url, headers: github_mirror.MirrorResponse(status=500, error="boom"))
    core.sync_inbox_github(now=NOW)
    row = db.last_run(core.connect(), INBOX_GITHUB_JOB_ID)
    assert row["status"] == "fail" and "1 of 1 repo(s) failed" in row["reason"]


def test_no_repos_configured_still_heartbeats_ok(core):
    core.settings.inbox_github_repos = ()
    summary = core.sync_inbox_github(now=NOW)
    assert summary == {"repos": 0, "ok": 0, "failed": 0, "issues": 0, "results": []}
    assert db.last_run(core.connect(), INBOX_GITHUB_JOB_ID)["status"] == "ok"


def test_a_missing_job_in_jobs_yml_warns_once_and_never_crashes(settings, caplog):
    """jobs.yml is gitignored, so merging this feature ships the schema and
    never the values: on a box whose file has not been edited yet, the mirror
    must still run — UNMONITORED, and said so, rather than crashing."""
    import copy

    from dashboard.registry import parse_registry
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"] = [j for j in doc["jobs"] if j["id"] != "worker"]
    core = Core(settings, parse_registry(doc), RecordingNotifier())
    core.init_store()
    pin_created_at(settings)
    core.settings.inbox_github_repos = ()
    with caplog.at_level("WARNING"):
        core.sync_inbox_github(now=NOW)
        core.sync_inbox_github(now=NOW + 900)
    warnings = [r for r in caplog.records if "UNMONITORED" in r.getMessage()]
    assert len(warnings) == 1
    assert INBOX_GITHUB_JOB_ID in warnings[0].getMessage()


# --------------------------------------------------------------------------- #
# Prune + reconcile
# --------------------------------------------------------------------------- #

def _note(conn, audio_dir, *, status, reviewed, created, item_id):
    stored = inbox_audio.save(audio_dir, item_id=item_id,
                              data=b"\x1a\x45\xdf\xa3" + b"\x00" * 200,
                              declared_mime="audio/webm", max_bytes=10 ** 6,
                              created_at=created)
    with conn:
        inbox_db.create_item(conn, source="voice", text="spoken", now=created,
                             item_id=item_id, transcript_status=status,
                             audio=stored.as_row())
        if reviewed:
            inbox_db.update_item(conn, item_id, {"reviewed": True}, now=created)
    return stored.path


def test_the_prune_needs_all_three_conditions(core, settings):
    """All three, never two: the transcript is Whisper-quality, Graham has
    reviewed it, and it is past the retention window. Only then are the words
    safely in the DB (and so in the DB backup) before the only recording of
    them is destroyed.

    Every age here sits BETWEEN the retention window and the privacy ceiling,
    so this test measures the convenience prune alone — the ceiling is the next
    test and it deliberately overrides all three of these conditions."""
    days = settings.inbox_audio_retention_days
    old = inbox_db.to_iso(NOW - (days + 5) * 86400)
    new = inbox_db.to_iso(NOW)
    conn = inbox_db.connect(settings.inbox_db_path)
    audio_dir = settings.inbox_audio_dir
    try:
        good = _note(conn, audio_dir, status="whisper", reviewed=True,
                     created=old, item_id="a" * 32)
        not_whisper = _note(conn, audio_dir, status="live", reviewed=True,
                            created=old, item_id="b" * 32)
        not_reviewed = _note(conn, audio_dir, status="whisper", reviewed=False,
                             created=old, item_id="c" * 32)
        too_new = _note(conn, audio_dir, status="whisper", reviewed=True,
                        created=new, item_id="d" * 32)
    finally:
        conn.close()
    out = core.prune_inbox_audio(now=NOW)
    assert out["pruned"] == 1
    assert inbox_audio.open_path(audio_dir, good) is None
    for path in (not_whisper, not_reviewed, too_new):
        assert inbox_audio.open_path(audio_dir, path) is not None


def test_the_privacy_ceiling_deletes_old_audio_whatever_its_state(core, settings):
    """INBOX_AUDIO_RETENTION_DAYS reads like a maximum. Without a backstop it
    behaves like a MINIMUM, because all three convenience conditions are things
    that can simply never happen: Graham never ticks Reviewed, Whisper failed
    (`failed` is deliberately outside PRUNABLE_TRANSCRIPT_STATUSES), or the Mac
    worker never ran. Each of those keeps a recording of his voice for ever.

    Past twice the retention window the audio goes regardless — and the row
    keeps its metadata plus an `audio_pruned_at` stamp, so the board says
    honestly that there WAS a recording and it is gone."""
    days = settings.inbox_audio_retention_days
    ancient = inbox_db.to_iso(NOW - (2 * days + 5) * 86400)
    conn = inbox_db.connect(settings.inbox_db_path)
    audio_dir = settings.inbox_audio_dir
    try:
        never_reviewed = _note(conn, audio_dir, status="whisper", reviewed=False,
                               created=ancient, item_id="a" * 32)
        gave_up = _note(conn, audio_dir, status="failed", reviewed=False,
                        created=ancient, item_id="b" * 32)
        never_transcribed = _note(conn, audio_dir, status="pending",
                                  reviewed=False, created=ancient,
                                  item_id="c" * 32)
    finally:
        conn.close()
    out = core.prune_inbox_audio(now=NOW)
    assert out["pruned"] == 3
    for path in (never_reviewed, gave_up, never_transcribed):
        assert inbox_audio.open_path(audio_dir, path) is None
    conn = inbox_db.connect(settings.inbox_db_path)
    try:
        row = inbox_db.get_item(conn, "b" * 32)
    finally:
        conn.close()
    # Honest about what happened: no path, a prune stamp, and the size/duration
    # metadata kept so the row does not look as if it never had audio.
    assert row["audio_path"] is None and row["audio_pruned_at"]
    assert row["audio_bytes"] and row["transcript_status"] == "failed"


def test_the_reconcile_clears_a_dangling_audio_path(core, settings):
    """The price of storing audio as files rather than BLOBs. Without this the
    one control Graham taps to check a transcript 404s."""
    conn = inbox_db.connect(settings.inbox_db_path)
    audio_dir = settings.inbox_audio_dir
    try:
        path = _note(conn, audio_dir, status="pending", reviewed=False,
                     created=inbox_db.to_iso(NOW), item_id="e" * 32)
    finally:
        conn.close()
    import os
    os.remove(os.path.join(audio_dir, path))
    out = core.prune_inbox_audio(now=NOW)
    assert out["cleared"] == 1 and out["pruned"] == 0
    conn = inbox_db.connect(settings.inbox_db_path)
    try:
        assert inbox_db.get_item(conn, "e" * 32)["audio_path"] is None
    finally:
        conn.close()


def test_the_reconcile_removes_an_orphan_file_but_leaves_strangers_alone(
        core, settings):
    audio_dir = settings.inbox_audio_dir
    orphan = inbox_audio.save(audio_dir, item_id="f" * 32,
                              data=b"\x1a\x45\xdf\xa3" + b"\x00" * 200,
                              declared_mime="audio/webm", max_bytes=10 ** 6,
                              created_at=inbox_db.to_iso(NOW))
    import os
    stranger = os.path.join(audio_dir, "2026", "09", "README.txt")
    os.makedirs(os.path.dirname(stranger), exist_ok=True)
    with open(stranger, "w", encoding="utf-8") as fh:
        fh.write("not ours")
    out = core.prune_inbox_audio(now=NOW)
    assert out["orphans"] == 1
    assert inbox_audio.open_path(audio_dir, orphan.path) is None
    assert os.path.exists(stranger)


def test_neither_inbox_job_writes_dashboard_db_rows_of_its_own(core, settings,
                                                               monkeypatch):
    """They write inbox.db. The ONLY thing they put in dashboard.db is the
    heartbeat run, through the same record_ping path every other job uses."""
    monkeypatch.setattr(
        github_mirror, "default_fetch",
        lambda url, headers: github_mirror.MirrorResponse(status=200, body=[]))
    conn = core.connect()
    before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    core.prune_inbox_audio(now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == before
    core.sync_inbox_github(now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == before + 1
    conn.close()
