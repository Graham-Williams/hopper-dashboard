"""Regression tests for the pre-deploy review fixes (2026-09-05): never-pinged
jobs go LATE, missing-vs-differ for copy trees, the machine-offline alert
rule, prod fail-fast config, proxy-aware client IP + global login cap, limiter
key cap, ISO clamping, ingest parsing bounds, ntfy body, probe argv."""

from __future__ import annotations

import copy
import json
import re
import sqlite3

import pytest

from dashboard import create_app, db, probes
from dashboard.db import from_iso, to_iso
from dashboard.humanize import absolute
from dashboard.ratelimit import KEY_MAX_LEN, SlidingWindowLimiter
from dashboard.state import Facts, compute_state, dest_info, lag_info
from tests.conftest import (INGEST_TOKEN, JOBS_DOC, PASSWORD, READ_TOKEN, auth,
                            pin_created_at)
from dashboard.registry import parse_registry
from dashboard.services import Core, ok_dwell_s

NOW = 1_800_000_000.0
REG = parse_registry(JOBS_DOC)


def _example_dwell(reg, job_id) -> float:
    """Same idea for a core built over the shipped jobs.example.yml."""
    return ok_dwell_s(reg.get(job_id))


def _dwell(job_id) -> float:
    """The job's own OK dwell — derived, never a flat constant, because it is
    cadence-aware (services.ok_dwell_s) and a wait that is too short fails as
    SILENCE, which is what these tests are here to tell apart."""
    return ok_dwell_s(REG.get(job_id))


POISON_ISO = "0001-01-01T00:00:00+14:00"   # datetime.timestamp() raises OverflowError on this


def run(status="ok", ago=0, **extra):
    return {"status": status, "received_at": to_iso(NOW - ago), **extra}


def bearer(token=READ_TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# 14. never-pinged scheduled jobs: UNKNOWN → LATE after cadence+grace
# --------------------------------------------------------------------------- #

def test_never_pinged_is_unknown_within_deadline_then_late():
    job = REG.get("containers")  # deadline 600
    f = Facts(created_at=to_iso(NOW - 599))
    assert compute_state(job, f, NOW)[0] == "UNKNOWN"
    f = Facts(created_at=to_iso(NOW - 601))
    state, reason = compute_state(job, f, NOW)
    assert state == "LATE" and "never pinged" in reason and "heartbeat installed" in reason


def test_never_pinged_late_uses_late_means_and_applies_with_metrics_only():
    job = REG.get("macprobe")  # 3600 + 7200, late_means set
    assert compute_state(job, Facts(created_at=to_iso(NOW - 20_000)), NOW) == ("LATE", "Mac offline or asleep")
    # metrics but no run: still LATE once the deadline has passed
    mirror = REG.get("mirror")
    f = Facts(last_metrics={"pending": 0}, created_at=to_iso(NOW - 10_000))
    assert compute_state(mirror, f, NOW)[0] == "LATE"


def test_never_pinged_manual_and_missing_created_at_stay_unknown():
    assert compute_state(REG.get("offload"), Facts(created_at=to_iso(NOW - 10 ** 8)), NOW)[0] == "UNKNOWN"
    assert compute_state(REG.get("info"), Facts(created_at=to_iso(NOW - 10 ** 8)), NOW)[0] == "UNKNOWN"
    assert compute_state(REG.get("snap"), Facts(created_at=None), NOW)[0] == "UNKNOWN"


def test_ticker_flips_never_pinged_job_to_late_and_alerts(core, notifier, settings):
    """A mis-installed drop-in must not be silent forever: with no ping ever,
    the ticker alone takes box jobs UNKNOWN → LATE and pages."""
    pin_created_at(settings, to_iso(NOW))
    states = core.recompute_all(now=NOW + 599)
    assert states["containers"] == "UNKNOWN" and notifier.sent == []
    states = core.recompute_all(now=NOW + 601)
    assert states["containers"] == "LATE" and states["snap"] == "LATE"
    assert states["offload"] == "UNKNOWN" and states["info"] == "UNKNOWN"   # manual: never LATE
    titles = [t for t, _, _ in notifier.sent]
    assert "[dashboard] Containers → LATE" in titles and "[dashboard] Snap DB → LATE" in titles
    conn = core.connect()
    assert db.job_row(conn, "containers")["created_at"] == to_iso(NOW)
    assert [c["to_state"] for c in db.recent_state_changes(conn, "containers")] == ["LATE"]
    # first real heartbeat → recovery
    assert core.record_ping(REG.get("containers"), {"status": "ok"}, now=NOW + 700) == "OK"


def test_schema_migration_backfills_created_at(tmp_path):
    path = str(tmp_path / "old.db")
    conn = db.connect(path)
    conn.executescript("""
        CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'UNKNOWN', since TEXT,
            last_metrics TEXT, last_metrics_at TEXT, updated_at TEXT);
        INSERT INTO jobs (id, state, since, updated_at) VALUES ('old', 'UNKNOWN', '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z');
    """)
    db.init_schema(conn)
    row = db.job_row(conn, "old")
    assert row["created_at"] == "2026-01-01T00:00:00Z" and row["state_reason"] is None


# --------------------------------------------------------------------------- #
# 8b. Schema migration: the alert-episode columns, and the race that adds them
# --------------------------------------------------------------------------- #

LEGACY_JOBS = """
CREATE TABLE jobs (
    id              TEXT PRIMARY KEY,
    state           TEXT NOT NULL DEFAULT 'UNKNOWN',
    since           TEXT,
    state_reason    TEXT,
    last_metrics    TEXT,
    last_metrics_at TEXT,
    updated_at      TEXT,
    created_at      TEXT
);
"""


def _legacy_db(tmp_path, name="legacy.db"):
    """A live 0.1 database: no `bad_since`, no `alerted_at`, one real row."""
    path = str(tmp_path / name)
    conn = db.connect(path)
    conn.executescript(LEGACY_JOBS)
    conn.execute("INSERT INTO jobs (id, state, since, updated_at, created_at, last_metrics) "
                 "VALUES ('km-backup','FAIL',?,?,?,'{\"bytes\": 7}')",
                 (to_iso(NOW), to_iso(NOW), to_iso(NOW)))
    conn.close()
    return path


def test_schema_migration_adds_the_alert_episode_columns(tmp_path):
    """The live box DB predates bad_since/alerted_at. Migrating must add them
    without touching a row: NULL means "not currently in an episode", so a job
    that is already broken starts a fresh clock and pages one threshold later —
    late, never silent."""
    conn = db.connect(_legacy_db(tmp_path))
    try:
        db.init_schema(conn)
        db.init_schema(conn)                               # idempotent: a redeploy re-runs it
        row = db.job_row(conn, "km-backup")
        assert row["bad_since"] is None and row["alerted_at"] is None
        # ...and the two columns added since: the cooldown stamp and the state a
        # page was about. NULL for both, which is why no job starts life inside a
        # cooldown it never earned, and why an already-paged episode is HEALED
        # rather than ranked (services.Core._page).
        assert row["last_paged_at"] is None and row["alerted_state"] is None
        assert row["state"] == "FAIL" and row["last_metrics"] == {"bytes": 7}   # no data lost
        with conn:
            db.set_alert_episode(conn, "km-backup", to_iso(NOW + 60), None)
        row = db.job_row(conn, "km-backup")
        assert row["bad_since"] == to_iso(NOW + 60)
        assert row["updated_at"] == to_iso(NOW)            # the liveness column is untouched
        with conn:
            db.set_alert_episode(conn, "km-backup", to_iso(NOW + 60),
                                 to_iso(NOW + 120), "FAIL")
        assert db.job_row(conn, "km-backup")["alerted_state"] == "FAIL"
        # The accumulator's index comes with the same migration, on a table that
        # already exists in a 0.1 database.
        idx = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "sc_job_seq" in idx
    finally:
        conn.close()


def test_concurrent_init_schema_never_raises_duplicate_column(tmp_path):
    """`create_app` calls init_store() for BOTH roles and entrypoint.sh starts
    ingest (1 worker) + the read workers together, so on the very deploy that
    adds a column those processes race PRAGMA table_info → ALTER TABLE. The
    losers used to raise `OperationalError: duplicate column name` out of
    create_app, killing a worker; the entrypoint then stops the other gunicorn
    and compose restarts the container. It self-heals on the second boot, but
    "the dashboard is down" is the loudest silence there is."""
    import threading
    path = _legacy_db(tmp_path)
    workers = 6
    ready = threading.Barrier(workers, timeout=10)
    errors: list[BaseException] = []

    def migrate():
        conn = db.connect(path)
        try:
            ready.wait()                       # everyone reads the old schema at once
            db.init_schema(conn)
        except BaseException as exc:           # noqa: BLE001 - the point of the test
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=migrate) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    conn = db.connect(path)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert {"bad_since", "alerted_at", "created_at"} <= cols
        assert conn.execute("SELECT created_at FROM jobs WHERE id='km-backup'"
                            ).fetchone()["created_at"] == to_iso(NOW)   # backfilled once
    finally:
        conn.close()


class _RacingConn:
    """A connection whose `PRAGMA table_info` answers a moment before the column
    appears — the exact window between the read and the ALTER."""

    def __init__(self, conn, column="bad_since"):
        self._conn = conn
        self._column = column
        self._fired = False

    def execute(self, sql, *args):
        cur = self._conn.execute(sql, *args)
        if sql.startswith("PRAGMA table_info") and not self._fired:
            self._fired = True
            stale = cur.fetchall()                     # what WE saw...
            self._conn.execute(                        # ...and what somebody else did
                f"ALTER TABLE jobs ADD COLUMN {self._column} TEXT")
            return stale
        return cur

    def executescript(self, sql):
        return self._conn.executescript(sql)


def test_migration_survives_a_column_added_between_the_read_and_the_alter(tmp_path):
    """The deterministic form of the race above: we decide to add `bad_since`,
    another process adds it first, and our ALTER then raises
    `OperationalError: duplicate column name` out of create_app — killing the
    worker and, via entrypoint.sh, the whole container."""
    conn = db.connect(_legacy_db(tmp_path))
    try:
        db.init_schema(_RacingConn(conn))              # must not raise
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert {"bad_since", "alerted_at"} <= cols
    finally:
        conn.close()


def test_add_column_tolerates_having_lost_the_race_but_not_a_real_error(tmp_path):
    conn = db.connect(_legacy_db(tmp_path))
    try:
        db._add_column(conn, "jobs", "bad_since", "TEXT")
        db._add_column(conn, "jobs", "bad_since", "TEXT")     # lost the race
        with pytest.raises(sqlite3.OperationalError):         # real errors still raise
            db._add_column(conn, "nope_not_a_table", "x", "TEXT")
    finally:
        conn.close()


def test_init_schema_is_idempotent_on_a_fresh_db(tmp_path):
    conn = db.connect(str(tmp_path / "fresh.db"))
    try:
        for _ in range(3):
            db.init_schema(conn)
        db.ensure_jobs(conn, ["km-backup"])
        db.init_schema(conn)
        assert db.job_row(conn, "km-backup")["bad_since"] is None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 9. rclone_copy_tree: missing (stale) vs differ (informational)
# --------------------------------------------------------------------------- #

def test_copy_tree_differ_only_is_ok_not_stale():
    job = REG.get("tree")
    f = Facts(last_run=run(), last_success=run(),
              last_metrics={"missing_files": 0, "missing_bytes": 0, "differ_files": 3, "differ_bytes": 4096})
    state, reason = compute_state(job, f, NOW)
    assert state == "OK" and "3 file(s) edited since" in reason
    assert dest_info(job, f, NOW)["fresh"] is True
    lag = lag_info(job, f, NOW)
    assert lag["bytes"] == 0 and lag["files"] == 0
    assert lag["differ_files"] == 3 and lag["differ_bytes"] == 4096


def test_copy_tree_missing_is_stale_even_with_zero_differ():
    job = REG.get("tree")
    f = Facts(last_run=run(), last_success=run(),
              last_metrics={"missing_files": 1, "missing_bytes": 50, "differ_files": 0})
    state, reason = compute_state(job, f, NOW)
    assert state == "STALE_DEST" and "never reached" in reason and "50 bytes" in reason
    # missing_files alone (bytes unknown) is enough
    f = Facts(last_run=run(), last_success=run(), last_metrics={"missing_files": 2})
    assert compute_state(job, f, NOW)[0] == "STALE_DEST"


def test_copy_tree_ignores_legacy_lag_bytes_for_staleness():
    """The old combined lag_bytes (missing+differ) flapped the card; it no longer drives STALE_DEST."""
    job = REG.get("tree")
    f = Facts(last_run=run(), last_success=run(), last_metrics={"lag_bytes": 999, "lag_files": 4})
    assert compute_state(job, f, NOW)[0] == "OK"
    assert dest_info(job, f, NOW)["fresh"] is None      # cannot judge without missing_*


def test_copy_tree_board_shows_missing_and_differ_separately(authed, core):
    core.record_ping(REG.get("tree"), {"status": "ok", "metrics": {
        "missing_files": 0, "missing_bytes": 0, "differ_files": 2, "differ_bytes": 2048}}, now=NOW)
    html = authed.get("/").data.decode()
    assert "2 files / 2.0 KB edited since last copy (normal lag)" in html
    assert "never uploaded" in html


# --------------------------------------------------------------------------- #
# 10. machine-offline rule: one alert for the Mac, not one per Mac job
# --------------------------------------------------------------------------- #

def test_mac_offline_suppresses_sibling_late_alerts_but_records_state(core, notifier):
    mac = REG.get("macprobe")    # deadline 10800
    tree = REG.get("tree")       # deadline 90000
    mirror = REG.get("mirror")   # deadline 4200
    snap = REG.get("snap")       # box, deadline 600
    for j in (mac, tree, mirror, snap):
        core.record_ping(j, {"status": "ok", "metrics": {"missing_files": 0}}, now=NOW)
    notifier.sent.clear()
    states = core.recompute_all(now=NOW + 100_000)   # everything is LATE now
    assert states == {**states, "macprobe": "LATE", "tree": "LATE", "mirror": "LATE", "snap": "LATE"}
    titles = sorted(t for t, _, _ in notifier.sent)
    # Box job alerts normally; the Mac produces exactly ONE alert (its probe job).
    assert "[dashboard] Snap DB → LATE" in titles
    assert "[dashboard] Mac probe → LATE" in titles
    assert "[dashboard] Tree copy → LATE" not in titles and "[dashboard] Drive mirror → LATE" not in titles
    # ...but the sibling transitions are still persisted and visible.
    conn = core.connect()
    assert db.job_row(conn, "tree")["state"] == "LATE"
    assert [c["to_state"] for c in db.recent_state_changes(conn, "tree")][0] == "LATE"


def test_mac_offline_suppression_covers_late_transitions_after_probe_already_late(core, notifier):
    mac, mirror = REG.get("macprobe"), REG.get("mirror")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(mirror, {"status": "ok"}, now=NOW + 7000)
    core.recompute_all(now=NOW + 11_000)        # mac-probe LATE (mirror still OK: 4000 < 4200)
    assert [t for t, _, _ in notifier.sent] == ["[dashboard] Mac probe → LATE"]
    core.recompute_all(now=NOW + 12_000)        # mirror LATE while mac-probe already LATE
    assert [t for t, _, _ in notifier.sent] == ["[dashboard] Mac probe → LATE"]
    assert db.job_row(core.connect(), "mirror")["state"] == "LATE"


def test_mac_comes_back_one_recovery_alert(core, notifier):
    """Real wake order: mac_probe.py posts pa-backup, then drive-mirror, then its own heartbeat — three
    HTTP requests, three recomputes. The siblings therefore recover while the probe is STILL LATE; those
    plain LATE→OK recoveries are muted and the probe's own recovery is the one alert."""
    mac, tree, mirror = REG.get("macprobe"), REG.get("tree"), REG.get("mirror")
    for j in (mac, tree, mirror):
        core.record_ping(j, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 100_000)          # everything on the Mac is LATE
    assert [t for t, _, _ in notifier.sent] == ["[dashboard] Mac probe → LATE"]
    notifier.sent.clear()
    core.record_ping(tree, {"status": "ok"}, now=NOW + 100_100)     # recovers while probe still LATE
    core.record_ping(mirror, {"status": "ok"}, now=NOW + 100_101)   # recovers while probe still LATE
    assert notifier.sent == []
    core.record_ping(mac, {"status": "ok"}, now=NOW + 100_102)
    assert notifier.sent == []                     # OK, but not yet HELD: no episode has closed
    # One pass past the probe's own dwell closes all three episodes at once. The
    # siblings recovered a couple of seconds EARLIER, so their episodes close in
    # this same pass — still muted, because the probe's episode is only now
    # ending and `_returning_probes` reads the pre-recompute rows.
    core.recompute_all(now=NOW + 100_102 + _dwell("macprobe"))
    assert [t for t, _, _ in notifier.sent] == ["[dashboard] Mac probe → OK"]
    conn = core.connect()   # the muted recoveries are still persisted and visible
    assert db.job_row(conn, "tree")["state"] == "OK" and db.job_row(conn, "mirror")["state"] == "OK"
    assert [c["to_state"] for c in db.recent_state_changes(conn, "mirror")][:2] == ["OK", "LATE"]


def test_mac_sibling_waking_into_fail_still_alerts(core, notifier):
    """Only plain LATE→OK recoveries ride on the probe's alert; a sibling that comes back FAILED is news."""
    mac, tree = REG.get("macprobe"), REG.get("tree")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(tree, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 100_000)
    notifier.sent.clear()
    core.record_ping(tree, {"status": "fail", "reason": "rclone exit 1"}, now=NOW + 100_100)
    assert [t for t, _, _ in notifier.sent] == ["[dashboard] Tree copy → FAIL"]
    core.record_ping(mac, {"status": "ok"}, now=NOW + 100_101)
    core.recompute_all(now=NOW + 100_101 + _dwell("macprobe"))
    assert [t for t, _, _ in notifier.sent][-1] == "[dashboard] Mac probe → OK"


def test_mac_offline_rule_is_a_pure_function_of_probe_state(core, notifier):
    """Sibling LATE→OK with the probe already OK (not LATE, no probe transition) alerts normally — the
    muting is tied to the machine being offline, not to the sibling having been LATE."""
    mac, mirror = REG.get("macprobe"), REG.get("mirror")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(mirror, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 5000)             # mirror LATE (4200), probe still OK (10800)
    assert [t for t, _, _ in notifier.sent] == ["[dashboard] Drive mirror → LATE"]
    core.record_ping(mirror, {"status": "ok"}, now=NOW + 5001)
    core.recompute_all(now=NOW + 5001 + _dwell("mirror"))
    assert [t for t, _, _ in notifier.sent][-1] == "[dashboard] Drive mirror → OK"


def _example_core(settings, notifier):
    """A Core over the SHIPPED jobs.example.yml, woken once (pa-backup, drive-mirror, then the probe's
    own heartbeat — the real HTTP order, seconds apart) with the notifier cleared."""
    from dashboard.registry import load_registry
    from dashboard.services import Core
    from tests.conftest import EXAMPLE_JOBS
    reg = load_registry(EXAMPLE_JOBS)
    core = Core(settings, reg, notifier)
    core.init_store()
    pin_created_at(settings)                       # never-pinged box jobs must not go LATE during the test
    mac, pa, mirror = reg.get("mac-probe"), reg.get("pa-backup"), reg.get("drive-mirror")
    assert pa.grace_s >= mac.grace_s + 120 and mirror.grace_s >= mac.grace_s + 120
    core.record_ping(pa, {"status": "ok", "metrics": {"missing_files": 0}}, now=NOW)
    core.record_ping(mirror, {"status": "ok", "metrics": {"pending": 0, "mismatch": 0}}, now=NOW + 3)
    core.record_ping(mac, {"status": "ok"}, now=NOW + 5)
    notifier.sent.clear()
    return core, reg, (mac, pa, mirror)


def _tick(core, start, stop, step=60):
    t = start
    while t < stop:
        core.recompute_all(now=t)
        t += step
    return t


def test_example_jobs_a_night_or_a_weekend_asleep_pages_nobody(settings, notifier):
    """End-to-end against the shipped jobs.example.yml: an ordinary sleep pages NOBODY. Not because
    `mac-probe` is muted — it carries a 72 h threshold (see the multi-day test below) — but because every
    Mac threshold outlasts a weekend, and the machine-offline rule mutes the siblings behind the probe
    while it is LATE. Also exercises the tick-straddle the graces exist for: the ticker runs every 60 s,
    so with equal graces a tick could land between drive-mirror's deadline (pinged a few seconds before
    the probe) and mac-probe's; the example file gives siblings grace_s >= probe + 120 so drive-mirror's
    deadline is always strictly AFTER the probe's."""
    core, _reg, (mac, pa, mirror) = _example_core(settings, notifier)
    assert not mac.alert_never                     # the machine must be ABLE to page (see below)
    # Sleep through every Mac deadline: the probe's (3600+50400 after its ping), the siblings' (later by
    # construction), and pa-backup's (a day later still). Then keep sleeping to a full weekend.
    t = _tick(core, NOW, NOW + 86400 + 50520 + 120)
    conn = core.connect()
    assert {jid: db.job_row(conn, jid)["state"] for jid in ("mac-probe", "pa-backup", "drive-mirror")} \
        == {"mac-probe": "LATE", "pa-backup": "LATE", "drive-mirror": "LATE"}
    assert notifier.sent == []                     # the board shows it; the phone stays quiet
    t = _tick(core, t, NOW + 60 * 3600, step=300)  # 60 h: a Friday-night-to-Monday-morning lid-shut
    assert notifier.sent == []
    # ...and the episodes ARE running, so a problem that outlives the threshold would still page.
    assert db.job_row(conn, "drive-mirror")["bad_since"] is not None
    assert db.job_row(conn, "drive-mirror")["alerted_at"] is None
    # Mac wakes: three sequential pings, each its own recompute.
    wake = t
    core.record_ping(pa, {"status": "ok", "metrics": {"missing_files": 0}}, now=wake)
    core.record_ping(mirror, {"status": "ok", "metrics": {"pending": 0, "mismatch": 0}}, now=wake + 3)
    core.record_ping(mac, {"status": "ok"}, now=wake + 5)
    assert notifier.sent == []                     # nothing was paged, so nothing "recovers"
    assert {jid: db.job_row(conn, jid)["state"] for jid in ("mac-probe", "pa-backup", "drive-mirror")} \
        == {"mac-probe": "OK", "pa-backup": "OK", "drive-mirror": "OK"}
    # Each job turned OK when ITS ping landed (wake, wake+3, wake+5), so the
    # last of them closes one dwell after that — 900 s here, two of mac-probe's
    # hourly cadences, not the old flat 300.
    core.recompute_all(now=wake + 5 + _example_dwell(_reg, "drive-mirror"))
    assert db.job_row(conn, "drive-mirror")["bad_since"] is None


def test_example_jobs_a_mac_gone_for_days_pages_exactly_once(settings, notifier):
    """Silence is only correct while it is temporary. A Mac that is simply GONE — dead, stolen, launchd
    probe unloaded — must not page nothing at all, which is what `alert: never` on `mac-probe` would buy:
    it is the job whose alert the machine-offline rule borrows, so on the strength of its LATE it mutes
    every sibling too. Now the machine itself pages, ONCE, at 15 h LATE + 72 h — and still only once,
    because the siblings are (correctly) suppressed behind it."""
    core, _reg, (mac, pa, mirror) = _example_core(settings, notifier)
    deadline = mac.cadence_s + mac.grace_s
    _tick(core, NOW, NOW + deadline + mac.alert_after_s - 600, step=300)
    assert notifier.sent == []                     # 87 h minus ten minutes: still nothing
    _tick(core, NOW + deadline + mac.alert_after_s - 600,
          NOW + 10 * 86400, step=300)              # ...and then ten days of gone
    assert [t for t, _, _ in notifier.sent] == ["[dashboard] Mac probe → LATE"]
    assert notifier.sent[0][1] == "mac-probe: LATE for over 3d"
    conn = core.connect()
    assert db.job_row(conn, "mac-probe")["alerted_at"] is not None
    for jid in ("pa-backup", "drive-mirror"):      # siblings: episode running, page unspent
        assert db.job_row(conn, jid)["bad_since"] is not None
        assert db.job_row(conn, jid)["alerted_at"] is None


def test_mac_sibling_recovery_in_same_batch_as_probe_is_muted(core, notifier, monkeypatch):
    """Recomputed in one batch (the ticker), probe LATE→OK and sibling LATE→OK together = one alert."""
    mac, mirror = REG.get("macprobe"), REG.get("mirror")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(mirror, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 20_000)
    notifier.sent.clear()
    conn = core.connect()
    with conn:   # simulate both heartbeats having landed, then one recompute
        db.insert_run(conn, "macprobe", received_at=to_iso(NOW + 20_100), status="ok")
        db.insert_run(conn, "mirror", received_at=to_iso(NOW + 20_100), status="ok")
    core.recompute_all(now=NOW + 20_101)
    assert notifier.sent == []                     # both OK; neither episode has closed yet
    core.recompute_all(now=NOW + 20_101 + _dwell("macprobe"))
    assert [t for t, _, _ in notifier.sent] == ["[dashboard] Mac probe → OK"]


def test_box_jobs_never_suppressed_by_dashboard_probes_job(core, notifier):
    """The dashboard's own probe job is not a 'machine reachable' signal."""
    snap = REG.get("snap")
    core.record_ping(snap, {"status": "ok"}, now=NOW)
    core.record_ping(REG.get("dashboard-probes"), {"status": "ok"}, now=NOW)
    notifier.sent.clear()
    core.recompute_all(now=NOW + 5000)     # both LATE
    titles = {t for t, _, _ in notifier.sent}
    assert "[dashboard] Snap DB → LATE" in titles and "[dashboard] Probe cycle → LATE" in titles


# --------------------------------------------------------------------------- #
# 25. ntfy body carries only the transition
# --------------------------------------------------------------------------- #

def test_ntfy_body_never_carries_reason_text(core, notifier):
    job = REG.get("containers")
    core.record_ping(job, {"status": "ok", "metrics": {"running": "app-1,tunnel-1"}}, now=NOW)
    core.record_ping(job, {"status": "ok", "metrics": {"running": "app-1"}}, now=NOW + 1)
    title, body, _ = notifier.sent[-1]
    assert title == "[dashboard] Containers → FAIL"
    assert body == "containers: FAIL"
    assert "tunnel-1" not in body and "not running" not in body


LEAK = "ZZLEAK"          # one marker prefix, so the assertion can be total


def _hostile_doc():
    """The shared registry with every free-text field loaded with a marker.

    `name` deliberately keeps its innocuous value: the job NAME is the one piece
    of free text that is *supposed* to reach ntfy (it is what makes a push
    readable on a phone, it is byte-identical to pre-0.2 behaviour, and it is
    documented as such). Everything else here — what the job protects, how, the
    destination path, what LATE means, the expected container names — is local
    detail that must never leave the box."""
    doc = copy.deepcopy(JOBS_DOC)
    for raw in doc["jobs"]:
        raw["protects"] = f"{LEAK}-PROTECTS /Users/someone/secret.db"
        raw["method"] = f"{LEAK}-METHOD rclone --config /etc/rclone.conf"
        raw["alert_after_s"] = 0                      # page on the first not-OK pass
        if raw.get("destination") is not None:
            raw["destination"] = f"gdrive:{LEAK}-DRIVE-FOLDER-ID"
        if raw.get("late_means") is not None:
            raw["late_means"] = f"{LEAK}-LATE-MEANS the machine is asleep"
        if raw.get("expect"):
            raw["expect"] = [f"{LEAK}-container-1", f"{LEAK}-container-2"]
        if raw.get("probe"):
            raw["probe"] = {"rclone_path": f"gdrive:{LEAK}-PROBE-PATH",
                            "state_dir": f"/state/{LEAK}-STATE-DIR"}
    return doc


def test_no_free_text_from_a_ping_or_the_registry_can_reach_ntfy(settings, notifier):
    """THE privacy invariant of the alerting layer, asserted rather than
    promised: ntfy.sh is a third party, and the ONLY things that may reach it
    are the job's name, its id, its state and its own configured threshold.

    Three docstrings said so and nothing checked it. The existing body test
    looks at ONE push for TWO specific substrings, and the three tests that pass
    a `reason` never inspect the payload for it — so a future signature change
    (`notify_alert(..., reason=reason)`, a "helpful" reason in the body, a
    metric echoed into the title) would ship green. This drives real episodes
    through `Core` with markers in every free-text field a human or a probe can
    write — reason, note, metric keys AND values, and the registry's own
    protects/method/destination/late_means/expect — and asserts that not one
    push, attempted or delivered, contains any of them."""
    core = Core(settings, parse_registry(_hostile_doc()), notifier)
    core.init_store()
    pin_created_at(settings, "2020-01-01T00:00:00Z")     # never pinged → LATE
    reason = (f"{LEAK}-REASON km-tracker-app-1 exited; rclone stderr: directory "
              f"not found gdrive:{LEAK}/Backups")
    note = f"{LEAK}-NOTE box is 100.64.0.1, password is hunter2"
    hostile_metrics = {f"{LEAK}_metric_key": f"{LEAK}_metric_value",
                       "running": f"{LEAK}-container-1", "note": note}

    core.recompute_all(now=NOW)                          # every job LATE → pages
    tree = core.registry.get("tree")
    core.record_ping(tree, {"status": "fail", "reason": reason, "note": note,
                            "exit_code": 1, "metrics": hostile_metrics},
                     now=NOW + 1)                        # LATE → FAIL
    core.record_ping(tree, {"status": "ok", "reason": reason, "note": note},
                     now=NOW + 2)                        # → OK; the recovery waits out the dwell
    core.record_ping(core.registry.get("containers"),
                     {"status": "ok", "reason": reason, "note": note,
                      "metrics": {"running": f"{LEAK}-container-1"}}, now=NOW + 3)
    core.record_ping(core.registry.get("disk"),
                     {"status": "metric",
                      "metrics": {"disk_free_bytes": 1, "disk_total_bytes": 100,
                                  "path": f"/Users/someone/{LEAK}-PATH"}},
                     now=NOW + 4)
    core.recompute_all(now=NOW + 5)
    # Past `tree`'s dwell, so the recovery push is in the sample too — it is its
    # own free-text opportunity (it names the state the episode was about).
    core.recompute_all(now=NOW + 2 + _dwell("tree"))

    assert len(notifier.attempts) >= 6                   # not vacuous: it really paged
    assert notifier.attempts == notifier.sent            # nothing swallowed en route
    for title, body, priority in notifier.attempts:
        assert LEAK not in title, title
        assert LEAK not in body, body
        assert LEAK not in priority, priority
    # Positively: the push says who and what, and nothing else. The name is the
    # one deliberate disclosure; the id, state and threshold are the rest.
    titles = {t for t, _, _ in notifier.sent}
    assert "[dashboard] Tree copy → FAIL" in titles
    assert "[dashboard] Tree copy → OK" in titles
    assert {b for _, b, _ in notifier.sent if b.startswith("tree:")} == {
        "tree: FAIL", "tree: FAIL → OK"}      # its LATE is the Mac's, and muted


# --------------------------------------------------------------------------- #
# 18. read role fails fast on an exposing config
# --------------------------------------------------------------------------- #

def test_prod_without_password_refuses_to_start(settings, registry, notifier):
    from dashboard import ConfigError
    settings.app_password = ""
    settings.app_env = "prod"
    with pytest.raises(ConfigError, match="APP_PASSWORD"):
        create_app("read", settings, registry, notifier)
    # the ingest role has no gate and is unaffected
    create_app("ingest", settings, registry, notifier)


def test_password_without_session_secret_refuses_to_start(settings, registry, notifier):
    from dashboard import ConfigError
    settings.session_secret = ""
    with pytest.raises(ConfigError, match="SESSION_SECRET"):
        create_app("read", settings, registry, notifier)
    settings.app_env = "dev"   # dev is not exempt: the loop would happen there too
    with pytest.raises(ConfigError, match="SESSION_SECRET"):
        create_app("read", settings, registry, notifier)


def test_dev_without_password_is_allowed(settings, registry, notifier):
    settings.app_password = ""
    settings.session_secret = ""
    settings.app_env = "dev"
    assert create_app("read", settings, registry, notifier).test_client().get("/").status_code == 200


def test_app_env_is_normalised_so_prod_cannot_be_dodged_by_case_or_whitespace(monkeypatch, tmp_path):
    """`APP_ENV=" PROD"` (a stray space / capital in .env) must still be prod: the read role has to refuse
    to start without a password, not quietly come up ungated because ' PROD' != 'prod'."""
    from dashboard import ConfigError
    from dashboard.config import Settings
    monkeypatch.setenv("DASHBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("JOBS_FILE", "jobs.example.yml")
    monkeypatch.setenv("DASHBOARD_NO_SCHEDULER", "1")
    monkeypatch.setenv("APP_ENV", " PROD")
    for var in ("APP_PASSWORD", "SESSION_SECRET", "SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert Settings.from_env().app_env == "prod"
    with pytest.raises(ConfigError, match="APP_PASSWORD"):
        create_app("read")
    monkeypatch.setenv("APP_ENV", "   ")          # blank collapses to the default, which is prod
    assert Settings.from_env().app_env == "prod"
    with pytest.raises(ConfigError, match="APP_PASSWORD"):
        create_app("read")


# --------------------------------------------------------------------------- #
# 19. non-finite metric values must not poison the board
# --------------------------------------------------------------------------- #

def test_num_treats_non_finite_as_absent():
    from dashboard.state import _num
    assert _num("nan") is None and _num("inf") is None and _num("-inf") is None
    assert _num("1e999") is None and _num(float("inf")) is None and _num(float("nan")) is None
    assert _num(" 42 ") == 42.0 and _num(7) == 7.0 and _num(True) is None and _num(None) is None


def test_num_treats_an_absurd_magnitude_as_absent():
    """ingest rejects a numeric 1e308, but "1e308" is a legal short STRING metric and
    every probe may send numbers as text — so the cap lives here as well, or int() puts
    a 309-digit figure in /api/v1/status and ~310 characters in the gauge heading."""
    from dashboard.state import METRIC_ABS_MAX, _num, disk_info
    from dashboard.registry import parse_job
    from dashboard.state import Facts
    assert _num("1e308") is None and _num(-1e308) is None
    assert _num(METRIC_ABS_MAX) == METRIC_ABS_MAX      # exactly on the ceiling is fine
    job = parse_job({"id": "d", "name": "D", "machine": "box", "kind": "disk",
                     "protects": "space", "method": "statvfs"}, 0)
    d = disk_info(job, Facts(last_metrics={"disk_free_bytes": "1e308",
                                           "disk_total_bytes": "1e308"}))
    assert d["free_bytes"] is None and d["used_pct"] is None


@pytest.mark.parametrize("metrics", [
    {"dest_count": "nan"},
    {"missing_bytes": "inf"},
    {"missing_bytes": "1e999", "missing_files": "-inf"},
    {"dest_count": "NaN", "differ_files": "Infinity"},
])
def test_non_finite_string_metrics_never_500_the_readers(ingest, authed, core, registry, metrics):
    """JSON can't carry NaN, but a probe may send numbers as strings; "nan"/"inf"/"1e999" parse as floats
    and used to blow up int() in the state computation, 500ing every reader until the metric was
    overwritten. The ping may be accepted (200) or rejected (400) — but the board must keep rendering."""
    tree = registry.get("tree")                     # unprobed rclone_copy_tree: reads dest_count/missing_* from metrics
    core.record_ping(tree, {"status": "ok", "metrics": {"missing_files": 0}}, now=NOW)
    r = ingest.post("/api/v1/ping/tree", json={"status": "metric", "metrics": metrics}, headers=auth())
    assert r.status_code in (200, 400), r.data
    assert authed.get("/").status_code == 200
    assert authed.get("/api/v1/status").status_code == 200
    assert authed.get("/jobs/tree").status_code == 200
    assert authed.get("/api/v1/jobs/tree").status_code == 200
    # A run ping carrying the same metrics must not poison the row either.
    r = ingest.post("/api/v1/ping/tree", json={"status": "ok", "metrics": metrics}, headers=auth())
    assert r.status_code in (200, 400), r.data
    assert authed.get("/").status_code == 200 and authed.get("/jobs/tree").status_code == 200
    body = authed.get("/api/v1/status").get_json()
    tree_row = next(j for j in body["jobs"] if j["id"] == "tree")
    assert tree_row["state"] in ("OK", "STALE_DEST")   # computed, not crashed


# --------------------------------------------------------------------------- #
# 21. client IP: header trusted only from TRUSTED_PROXY_CIDR; global login cap
# --------------------------------------------------------------------------- #

def test_cf_header_ignored_without_trusted_proxy(read, settings):
    """Spoofing CF-Connecting-IP must not give a direct client fresh rate-limit buckets."""
    for i in range(settings.login_rate_max):
        r = read.post("/login", data={"password": "nope"},
                      headers={"CF-Connecting-IP": f"203.0.113.{i}"})
        assert r.status_code == 401
    r = read.post("/login", data={"password": "nope"}, headers={"CF-Connecting-IP": "203.0.113.99"})
    assert r.status_code == 429


def test_cf_header_honoured_from_trusted_proxy(settings, registry, notifier):
    settings.trusted_proxy_cidrs = ("172.18.0.0/16",)
    settings.login_global_max = 1000
    c = create_app("read", settings, registry, notifier).test_client()
    env = {"REMOTE_ADDR": "172.18.0.5"}
    for i in range(settings.login_rate_max):
        assert c.post("/login", data={"password": "nope"}, environ_base=env,
                      headers={"CF-Connecting-IP": "198.51.100.7"}).status_code == 401
    # same real client → blocked; a different real client behind the same proxy → not blocked
    assert c.post("/login", data={"password": "nope"}, environ_base=env,
                  headers={"CF-Connecting-IP": "198.51.100.7"}).status_code == 429
    assert c.post("/login", data={"password": "nope"}, environ_base=env,
                  headers={"CF-Connecting-IP": "198.51.100.8"}).status_code == 401
    # the header from an UNtrusted peer is still ignored (buckets by peer)
    assert c.post("/login", data={"password": "nope"}, environ_base={"REMOTE_ADDR": "10.0.0.1"},
                  headers={"CF-Connecting-IP": "198.51.100.7"}).status_code == 401
    # a garbage header value falls back to the peer rather than becoming a bucket
    assert c.post("/login", data={"password": "nope"}, environ_base=env,
                  headers={"CF-Connecting-IP": "not-an-ip"}).status_code == 401


def test_global_failed_login_cap_across_ips(settings, registry, notifier):
    settings.login_rate_max = 3
    settings.login_global_max = 5
    c = create_app("read", settings, registry, notifier).test_client()
    ips = [f"10.1.1.{i}" for i in range(5)]
    for ip in ips:   # 5 failures from 5 different peers: each under its per-IP cap
        assert c.post("/login", data={"password": "nope"}, environ_base={"REMOTE_ADDR": ip}).status_code == 401
    # the 6th client is blocked by the global cap — even with the right password
    assert c.post("/login", data={"password": "nope"}, environ_base={"REMOTE_ADDR": "10.9.9.9"}).status_code == 429
    assert c.post("/login", data={"password": PASSWORD}, environ_base={"REMOTE_ADDR": "10.9.9.9"}).status_code == 429


def test_trusted_proxy_cidr_env_parsing(monkeypatch, tmp_path):
    from dashboard.config import Settings
    monkeypatch.setenv("DASHBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("TRUSTED_PROXY_CIDR", " 172.18.0.0/16, 10.0.0.1 ,")
    assert Settings.from_env().trusted_proxy_cidrs == ("172.18.0.0/16", "10.0.0.1")
    monkeypatch.setenv("TRUSTED_PROXY_CIDR", "")
    assert Settings.from_env().trusted_proxy_cidrs == ()
    monkeypatch.setenv("TRUSTED_PROXY_CIDR", "cloudflare")
    with pytest.raises(ValueError, match="TRUSTED_PROXY_CIDR"):
        Settings.from_env()


def test_ingest_rate_limit_keys_on_peer_not_header(ingest, settings):
    for i in range(settings.ping_rate_max):
        r = ingest.post("/api/v1/ping/snap", json={"status": "ok"}, headers={**auth(), "CF-Connecting-IP": f"203.0.113.{i}"})
        assert r.status_code == 200
    r = ingest.post("/api/v1/ping/snap", json={"status": "ok"}, headers={**auth(), "CF-Connecting-IP": "203.0.113.250"})
    assert r.status_code == 429


# --------------------------------------------------------------------------- #
# 22. limiter: hard key cap + key truncation
# --------------------------------------------------------------------------- #

def test_limiter_enforces_key_cap_by_evicting_stalest():
    lim = SlidingWindowLimiter(max_events=5, window_seconds=1000, max_tracked_keys=3)
    lim.hit("a", now=1); lim.hit("b", now=2); lim.hit("c", now=3)
    assert lim.tracked() == 3
    lim.hit("d", now=4)                       # nothing expired → evict the stalest ("a")
    assert lim.tracked() == 3 and set(lim._events) == {"b", "c", "d"}
    lim.hit("b", now=5)                       # existing key: no eviction needed
    assert set(lim._events) == {"b", "c", "d"}
    for i in range(50):                       # a flood never grows past the cap
        lim.record(f"flood{i}", now=10 + i)
    assert lim.tracked() == 3


def test_limiter_truncates_long_keys():
    lim = SlidingWindowLimiter(max_events=2, window_seconds=10)
    long_key = "x" * 500
    assert lim.hit(long_key, now=0) and lim.hit(long_key + "tail-differs", now=1)
    assert not lim.hit(long_key, now=2)       # same 64-char bucket
    assert all(len(k) <= KEY_MAX_LEN for k in lim._events)
    lim.reset(long_key + "other-tail")
    assert lim.tracked() == 0


# --------------------------------------------------------------------------- #
# 23. ISO clamping — a poisoned timestamp must not 500 the board
# --------------------------------------------------------------------------- #

def test_from_iso_clamps_and_never_raises():
    assert from_iso(POISON_ISO) is None
    assert from_iso("9999-12-31T23:59:59Z") == 253402300799.0
    assert from_iso("9999-12-31T23:59:59+00:00") == 253402300799.0
    assert from_iso("9999-12-31T23:59:59-01:00") is None       # past the max
    assert from_iso("1969-12-31T23:59:59Z") is None
    assert from_iso("1970-01-01T00:00:00Z") == 0
    assert from_iso("x" * 100) is None and from_iso(12345) is None  # type: ignore[arg-type]
    assert absolute(POISON_ISO) == "" and absolute("garbage") == ""


def test_poisoned_dest_newest_iso_does_not_500(authed, read, core):
    """dest_newest_iso is client-supplied (a probe metric). 0001-01-01T00:00:00+14:00 used to
    raise OverflowError inside from_iso/absolute → HTTP 500 on the board, the job page and the API."""
    core.record_ping(REG.get("offload"), {"status": "metric", "metrics": {
        "dest_newest_iso": POISON_ISO, "dest_count": 3, "lag_bytes": 0}}, now=NOW)
    core.record_ping(REG.get("offload"), {"status": "ok", "finished_at": "9999-12-31T23:59:59Z"}, now=NOW + 1)
    assert authed.get("/").status_code == 200
    assert authed.get("/jobs/offload").status_code == 200
    r = read.get("/api/v1/status", headers=bearer())
    assert r.status_code == 200
    j = {x["id"]: x for x in r.get_json()["jobs"]}["offload"]
    assert j["dest"]["newest"] is None and j["dest"]["count"] == 3 and j["state"] == "OK"


def test_poisoned_probe_row_does_not_500(authed, core):
    conn = core.connect()
    with conn:
        db.insert_probe(conn, "snap", probed_at=POISON_ISO, ok=True, newest_iso=POISON_ISO, count=1)
        db.insert_run(conn, "snap", received_at=to_iso(NOW), status="ok", started_at=POISON_ISO,
                      finished_at="not a date")
    assert authed.get("/jobs/snap").status_code == 200
    assert authed.get("/").status_code == 200


# --------------------------------------------------------------------------- #
# 24. ingest parsing bounds
# --------------------------------------------------------------------------- #

def test_deeply_nested_json_is_400_not_500(ingest):
    body = "[" * 100_000 + "]" * 100_000
    r = ingest.post("/api/v1/ping/snap", data=body, headers=auth(), content_type="application/json")
    assert r.status_code in (400, 413)
    r = ingest.post("/api/v1/ping/snap", data='{"status":"ok","metrics":' + "[" * 20_000 + "]" * 20_000 + "}",
                    headers=auth(), content_type="application/json")
    assert r.status_code in (400, 413)


@pytest.mark.parametrize("body", [
    {"status": "ok", "metrics": {"big": 2 ** 63 + 1}},
    {"status": "ok", "metrics": {"big": -(2 ** 63) - 1}},
    {"status": "ok", "metrics": {"l": [2 ** 70]}},
    {"status": "ok", "metrics": {"k\n": 1}},          # fullmatch: '$' would accept a trailing newline
])
def test_int_bounds_and_key_fullmatch(ingest, body):
    r = ingest.post("/api/v1/ping/snap", data=json.dumps(body), headers=auth(), content_type="application/json")
    assert r.status_code == 400, r.get_json()


def test_int_at_bound_accepted(ingest):
    r = ingest.post("/api/v1/ping/snap", json={"status": "ok", "metrics": {"big": 2 ** 63}}, headers=auth())
    assert r.status_code == 200


def test_ingest_app_has_no_static_route(ingest, ingest_app):
    assert ingest_app.static_folder is None and "static" not in ingest_app.view_functions
    assert ingest.get("/static/app.css").status_code == 404
    assert ingest.get("/static/../jobs.yml").status_code == 404


def test_rclone_argv_terminates_options(monkeypatch):
    seen = {}

    class P:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return P()

    monkeypatch.setattr(probes.subprocess, "run", fake_run)
    probes.run_rclone_lsjson("--config=/etc/passwd")
    a = seen["argv"]
    assert a[-2] == "--" and a[-1] == "--config=/etc/passwd" and a[0] == "rclone"


# --------------------------------------------------------------------------- #
# 15. manual job that has never run says so
# --------------------------------------------------------------------------- #

def _card(html: str, job_id: str) -> str:
    start = html.index(f'id="job-{job_id}"')
    return html[start:html.index("</article>", start)]


def test_manual_never_run_is_explicit_on_board_and_api(authed, read, core):
    core.record_ping(REG.get("offload"), {"status": "metric", "metrics": {"lag_bytes": 0}}, now=NOW)
    html = authed.get("/").data.decode()
    card = _card(html, "offload")
    assert "Never run." in card and "ping.sh offload ok" in card and "inert until the first" in card
    assert "Never run." in _card(html, "info")          # informational manual job: same hint, no target text
    assert "inert until" not in _card(html, "info")
    j = {x["id"]: x for x in read.get("/api/v1/status", headers=bearer()).get_json()["jobs"]}
    assert j["offload"]["never_run"] is True and j["offload"]["state"] == "OK"
    core.record_ping(REG.get("offload"), {"status": "ok", "note": "seeded"}, now=NOW + 1)
    assert "Never run." not in _card(authed.get("/").data.decode(), "offload")
    assert {x["id"]: x for x in read.get("/api/v1/status", headers=bearer()).get_json()["jobs"]}["offload"]["never_run"] is False


# --------------------------------------------------------------------------- #
# 17. <time datetime> localizer
# --------------------------------------------------------------------------- #

def test_time_elements_carry_machine_datetime(authed, core):
    core.record_ping(REG.get("snap"), {"status": "ok"}, now=NOW)
    html = authed.get("/jobs/snap").data.decode()
    assert re.search(r'<time datetime="2027-01-15T08:00:00Z" title="2027-01-15 08:00:00 UTC">', html)
    assert "querySelectorAll('time[datetime]')" in html


# --------------------------------------------------------------------------- #
# The read role cannot write dashboard.db (enforced, not documented)
# --------------------------------------------------------------------------- #

def test_the_read_roles_request_path_connection_refuses_to_write(read_app, settings):
    """"Only ingest writes" was a convention, and the Inbox makes the read role
    a writer for the first time — of inbox.db. So the rule is now enforced on
    THIS file: a stray write from a read worker raises SQLITE_READONLY instead
    of racing the scheduler's transactions."""
    import sqlite3

    import pytest as _pytest

    from dashboard import db
    with read_app.test_request_context("/"):
        from dashboard.web import _conn
        conn = _conn()
        try:
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
            # Reads still work — the board is built from them.
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] > 0
            for sql, args in (
                    ("INSERT INTO jobs (id, state) VALUES ('evil','OK')", ()),
                    ("UPDATE jobs SET state='OK'", ()),
                    ("DELETE FROM probes", ()),
                    ("INSERT INTO runs (job_id, received_at, status)"
                     " VALUES ('snap','2026-01-01T00:00:00Z','ok')", ())):
                with _pytest.raises(sqlite3.OperationalError,
                                    match="readonly|read-only"):
                    conn.execute(sql, args)
        finally:
            conn.close()
    # The ingest side is untouched: it is the writer.
    w = db.connect(settings.db_path)
    try:
        assert w.execute("PRAGMA query_only").fetchone()[0] == 0
    finally:
        w.close()


def test_query_only_works_on_a_cold_wal_database(tmp_path):
    """Why PRAGMA query_only and not a `mode=ro` URI: a read-only handle to a
    WAL db fails SQLITE_CANTOPEN when the -shm sidecar does not exist yet, which
    is exactly the first request after a fresh data volume."""
    import os

    from dashboard import db
    path = str(tmp_path / "cold.db")
    conn = db.connect(path)
    db.init_schema(conn)
    conn.close()
    for sidecar in ("-wal", "-shm"):
        if os.path.exists(path + sidecar):
            os.remove(path + sidecar)
    ro = db.connect_query_only(path)
    try:
        assert ro.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    finally:
        ro.close()
