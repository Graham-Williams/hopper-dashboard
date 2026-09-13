"""Core write-side behaviour: transitions persisted + notified, metric pings,
probe cycle recording, scheduler resilience."""

from dashboard import db, probes
from dashboard.probes import ProbeResult
from dashboard.scheduler import Scheduler
from dashboard.services import ok_dwell_s
from tests.conftest import RecordingNotifier

NOW = 1_800_000_000.0


def ok(**metrics):
    return {"status": "ok", "metrics": metrics}


def test_ping_transitions_unknown_to_ok_without_alert(core, notifier, registry):
    state = core.record_ping(registry.get("snap"), ok(), now=NOW)
    assert state == "OK"
    conn = core.connect()
    changes = db.recent_state_changes(conn, "snap")
    assert [(c["from_state"], c["to_state"]) for c in changes] == [("UNKNOWN", "OK")]
    assert db.job_row(conn, "snap")["since"] == db.to_iso(NOW)
    assert notifier.sent == []  # first sighting is not a recovery


def test_fail_transition_alerts_high_priority(core, notifier, registry):
    job = registry.get("snap")
    core.record_ping(job, ok(), now=NOW)
    core.record_ping(job, {"status": "fail", "reason": "timeout"}, now=NOW + 10)
    assert len(notifier.sent) == 1
    title, body, priority = notifier.sent[0]
    assert title == "[dashboard] Snap DB → FAIL" and priority == "high"
    # Body is the job id + the state the episode is about: the free-text reason
    # never goes to ntfy.sh. (These fixtures use alert_after_s: 0, so there is no
    # "for over …" suffix — see tests/test_alert_thresholds.py for that.)
    assert body == "snap: FAIL" and "timeout" not in body


def test_recovery_alerts_default_priority(core, notifier, registry):
    job = registry.get("snap")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    core.record_ping(job, ok(), now=NOW + 10)
    assert [p for _, _, p in notifier.sent] == ["high"]   # the OK is not yet held
    core.recompute_all(now=NOW + 10 + ok_dwell_s(job))    # episode closes -> recovery
    priorities = [p for _, _, p in notifier.sent]
    assert priorities == ["high", "default"]
    assert notifier.sent[-1][0].endswith("→ OK")


def test_steady_state_does_not_alert_or_record(core, notifier, registry):
    job = registry.get("snap")
    for i in range(5):
        core.record_ping(job, ok(), now=NOW + i)
    assert len(db.recent_state_changes(core.connect(), "snap")) == 1
    assert notifier.sent == []


def test_ticker_fires_late_without_traffic(core, notifier, registry):
    job = registry.get("snap")  # deadline 600
    core.record_ping(job, ok(), now=NOW)
    states = core.recompute_all(now=NOW + 599)
    assert states["snap"] == "OK"
    states = core.recompute_all(now=NOW + 601)
    assert states["snap"] == "LATE"
    assert notifier.sent[-1][0] == "[dashboard] Snap DB → LATE"
    assert notifier.sent[-1][2] == "default"
    # Heartbeat resumes -> recovery alert, once the OK has been HELD for the
    # job's own dwell (two of its 300 s cadences).
    assert core.record_ping(job, ok(), now=NOW + 700) == "OK"
    core.recompute_all(now=NOW + 700 + ok_dwell_s(job))
    assert notifier.sent[-1][0].endswith("→ OK")


def test_stale_dest_alert_is_high_priority(core, notifier, registry):
    job = registry.get("snap")
    core.record_ping(job, ok(db_sha256="cd" * 32), now=NOW)
    conn = core.connect()
    with conn:
        db.insert_probe(conn, "snap", probed_at=db.to_iso(NOW), ok=True,
                        newest_iso=db.to_iso(NOW - 5 * 86400), count=3,
                        state_sha="ab" * 32)
    states = core.recompute_all(now=NOW + 1)
    assert states["snap"] == "STALE_DEST"
    assert notifier.sent[-1][2] == "high"


def test_metric_ping_never_counts_as_run_or_success(core, notifier, registry):
    job = registry.get("snap")
    core.record_ping(job, ok(), now=NOW)
    # Job goes LATE; a metric-only update must NOT rescue it.
    core.recompute_all(now=NOW + 1000)
    state = core.record_ping(job, {"status": "metric", "metrics": {"bytes": 1}},
                             now=NOW + 1001)
    assert state == "LATE"
    conn = core.connect()
    assert len(db.recent_runs(conn, "snap")) == 1
    assert db.job_row(conn, "snap")["last_metrics"] == {"bytes": 1}


def test_metric_ping_can_flip_manual_job_to_behind(core, notifier, registry):
    job = registry.get("offload")
    assert core.record_ping(job, {"status": "metric", "metrics": {"lag_bytes": 10}},
                            now=NOW) == "OK"
    assert core.record_ping(job, {"status": "metric", "metrics": {"lag_bytes": 99999}},
                            now=NOW + 1) == "BEHIND"
    assert notifier.sent[-1][0] == "[dashboard] Offload → BEHIND"


def test_metric_ping_flips_disk_job_to_behind_and_alerts(core, notifier, registry):
    job = registry.get("disk")
    gib = 1024 ** 3
    assert core.record_ping(job, {"status": "metric", "metrics": {
        "disk_free_bytes": 200 * gib, "disk_total_bytes": 400 * gib}}, now=NOW) == "OK"
    state = core.record_ping(job, {"status": "metric", "metrics": {
        "disk_free_bytes": 10 * gib}}, now=NOW + 10)       # shallow merge keeps the total
    assert state == "BEHIND"
    title, body, priority = notifier.sent[-1]
    assert body == "disk: BEHIND" and priority == "default"   # not urgent, but actionable
    assert db.job_row(core.connect(), "disk")["state_reason"].startswith("only 10.0 GiB free")


def test_disk_job_has_no_cadence_deadline_but_does_go_stale(core, registry, notifier):
    gib = 1024 ** 3
    core.record_ping(registry.get("disk"), {"status": "metric", "metrics": {
        "disk_free_bytes": 200 * gib, "disk_total_bytes": 400 * gib}}, now=NOW)
    # Hours of silence: a scheduled job would be LATE long ago; a gauge keeps its
    # reading (mac-probe / box-containers own the machine's liveness signal).
    assert core.recompute_all(now=NOW + 6 * 3600)["disk"] == "OK"
    # Two days of it is a different thing: nothing is feeding the gauge any more.
    assert core.recompute_all(now=NOW + 49 * 3600)["disk"] == "LATE"
    assert notifier.sent[-1][1] == "disk: LATE"


def test_disk_fail_ping_changes_the_board_and_alerts(core, registry, notifier):
    """A `fail` ping used to leave the card OK for ever: the disk branch computed
    straight from last_metrics and never looked at the run. End-to-end, because
    the board and /api/v1/status are the path that is actually read."""
    job = registry.get("disk")
    gib = 1024 ** 3
    assert core.record_ping(job, {"status": "metric", "metrics": {
        "disk_free_bytes": 200 * gib, "disk_total_bytes": 400 * gib}}, now=NOW) == "OK"
    state = core.record_ping(job, {"status": "fail", "reason": "error",
                                   "note": "statvfs /data: [Errno 2] No such file"},
                             now=NOW + 300)
    assert state == "FAIL"
    assert notifier.sent[-1][1] == "disk: FAIL"
    assert "statvfs" in db.job_row(core.connect(), "disk")["state_reason"]
    # …and a later good reading clears it (the failed run stays the newest run row).
    assert core.record_ping(job, {"status": "metric", "metrics": {
        "disk_free_bytes": 199 * gib}}, now=NOW + 600) == "OK"


def test_ping_recomputes_other_jobs_too(core, registry):
    core.record_ping(registry.get("snap"), ok(), now=NOW)
    core.record_ping(registry.get("tree"), ok(), now=NOW + 5000)
    assert db.job_row(core.connect(), "snap")["state"] == "LATE"


def test_notifier_failure_never_breaks_ingest(settings, registry):
    from dashboard.services import Core
    core = Core(settings, registry, RecordingNotifier(fail=True))
    core.init_store()
    job = registry.get("snap")
    core.record_ping(job, ok(), now=NOW)
    assert core.record_ping(job, {"status": "fail"}, now=NOW + 1) == "FAIL"
    assert db.job_row(core.connect(), "snap")["state"] == "FAIL"


def test_notifier_disabled_when_env_empty():
    from dashboard.notify import Notifier
    n = Notifier("", "")
    assert not n.enabled
    assert n.notify_alert("x", "x", "FAIL", 86400) is False
    assert n.notify_recovery("x", "x", "FAIL") is False
    n2 = Notifier("https://ntfy.sh", "")
    assert not n2.enabled


def test_probe_cycle_records_self_job_and_probe_rows(core, registry, monkeypatch):
    monkeypatch.setattr(probes, "probe_job",
                        lambda job, **kw: ProbeResult(ok=True, newest_iso=db.to_iso(NOW - 10),
                                                      count=4, state_sha="ab" * 32))
    results = core.run_probe_cycle(now=NOW)
    assert set(results) == {"snap"}
    conn = core.connect()
    p = db.last_probe(conn, "snap")
    assert p["ok"] and p["count"] == 4 and p["state_sha"] == "ab" * 32
    self_run = db.last_run(conn, "dashboard-probes")
    assert self_run["status"] == "ok" and self_run["source"] == "scheduler"
    assert db.job_row(conn, "dashboard-probes")["state"] == "OK"


def test_probe_failure_becomes_self_job_failure_with_note(core, registry, monkeypatch):
    """Two consecutive failed cycles (the default threshold) trip FAIL, and the
    per-job errors ride along in the note."""
    monkeypatch.setattr(probes, "probe_job",
                        lambda job, **kw: ProbeResult(ok=False, error="rclone exit 3: not found"))
    core.run_probe_cycle(now=NOW)
    core.run_probe_cycle(now=NOW + 300)
    conn = core.connect()
    self_run = db.last_run(conn, "dashboard-probes")
    assert self_run["status"] == "fail"
    assert "snap: rclone exit 3: not found" in self_run["note"]
    assert db.job_row(conn, "dashboard-probes")["state"] == "FAIL"
    assert db.last_probe(conn, "snap")["ok"] is False


def test_probe_that_raises_is_caught(core, registry, monkeypatch):
    def boom(job, **kw):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(probes, "probe_job", boom)
    results = core.run_probe_cycle(now=NOW)
    assert results["snap"].ok is False and "kaboom" in results["snap"].error
    core.run_probe_cycle(now=NOW + 300)
    assert db.job_row(core.connect(), "dashboard-probes")["state"] == "FAIL"


def test_scheduler_step_probes_then_ticks(core, registry, monkeypatch):
    calls = []
    monkeypatch.setattr(core, "run_probe_cycle", lambda now=None: calls.append(("probe", now)))
    monkeypatch.setattr(core, "recompute_all", lambda now=None: calls.append(("tick", now)))
    s = Scheduler(core, tick_interval_s=60, probe_interval_s=300)
    s.step(NOW)
    s.step(NOW + 60)
    s.step(NOW + 300)
    assert [c[0] for c in calls] == ["probe", "tick", "probe"]


def test_scheduler_step_survives_exceptions(core, monkeypatch):
    def boom(now=None):
        raise RuntimeError("no")
    monkeypatch.setattr(core, "run_probe_cycle", boom)
    s = Scheduler(core)
    s.step(NOW)  # must not raise
    assert s.last_tick == NOW


def test_prune_bounds_rows(core, registry):
    conn = core.connect()
    with conn:
        for i in range(30):
            db.insert_run(conn, "snap", received_at=db.to_iso(NOW + i), status="ok")
        db.prune(conn, keep_runs=10, keep_probes=10)
    assert len(db.recent_runs(conn, "snap", 100)) == 10
    assert db.last_run(conn, "snap")["received_at"] == db.to_iso(NOW + 29)


def test_prune_is_per_job_and_per_table(core, registry):
    """The cap applies to each job independently (a chatty job must not evict a quiet job's history) and
    to runs and probes independently; a job under the cap is untouched; the newest rows are the ones kept."""
    conn = core.connect()
    with conn:
        for i in range(30):
            db.insert_run(conn, "snap", received_at=db.to_iso(NOW + i), status="ok")
            db.insert_run(conn, "tree", received_at=db.to_iso(NOW + i), status="fail" if i == 29 else "ok")
            db.insert_probe(conn, "snap", probed_at=db.to_iso(NOW + i), ok=True, count=i)
        for i in range(3):
            db.insert_run(conn, "mirror", received_at=db.to_iso(NOW + i), status="ok")
            db.insert_probe(conn, "tree", probed_at=db.to_iso(NOW + i), ok=True, count=i)
        db.prune(conn, keep_runs=10, keep_probes=5)
    assert len(db.recent_runs(conn, "snap", 100)) == 10
    assert len(db.recent_runs(conn, "tree", 100)) == 10
    assert len(db.recent_runs(conn, "mirror", 100)) == 3          # under the cap: untouched
    assert db.last_run(conn, "tree")["status"] == "fail"           # newest kept, oldest dropped
    assert [r["received_at"] for r in db.recent_runs(conn, "tree", 100)][-1] == db.to_iso(NOW + 20)
    assert conn.execute("SELECT COUNT(*) FROM probes WHERE job_id='snap'").fetchone()[0] == 5
    assert conn.execute("SELECT COUNT(*) FROM probes WHERE job_id='tree'").fetchone()[0] == 3
    assert db.last_probe(conn, "snap")["count"] == 29
    # Idempotent: a second prune deletes nothing.
    before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    with conn:
        db.prune(conn, keep_runs=10, keep_probes=5)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == before


# --------------------------------------------------------------------------- #
# Flap damping: the self-job must not page on one transient rate limit
# --------------------------------------------------------------------------- #

def _probe_results(monkeypatch, *, ok: bool, error: str = "", transient: bool = False):
    monkeypatch.setattr(
        probes, "probe_job",
        lambda job, **kw: ProbeResult(ok=ok, count=1 if ok else None,
                                      error=error or None, transient=transient))


def _streak(core, job_id: str = "snap") -> int:
    """The damping state, read where it now lives: derived from the job's own
    probe rows, not from a metric on the self job."""
    return db.probe_fail_streak(core.connect(), job_id)


def test_one_failed_cycle_does_not_trip_fail(core, notifier, monkeypatch):
    """The production defect: a single rateLimitExceeded used to record a fail
    run, flip the card to FAIL and push an alert."""
    _probe_results(monkeypatch, ok=True)
    core.run_probe_cycle(now=NOW)
    notifier.sent.clear()
    _probe_results(monkeypatch, ok=False,
                   error="transient (Drive quota/timeout): rclone exit 7: "
                         "rateLimitExceeded", transient=True)
    core.run_probe_cycle(now=NOW + 300)
    conn = core.connect()
    run = db.last_run(conn, "dashboard-probes")
    assert run["status"] == "ok"                      # damped, not a failure yet
    assert db.job_row(conn, "dashboard-probes")["state"] == "OK"
    assert notifier.sent == []                       # and therefore no push
    # The error is still recorded and still legible.
    assert "rateLimitExceeded" in run["note"]
    assert "damped" in run["reason"] and "1 of 2" in run["reason"]
    assert db.last_probe(conn, "snap")["ok"] is False
    assert _streak(core) == 1
    # The damping state is no longer a metric on the self job — see
    # test_damping_state_is_not_writable_through_ingest.
    assert "fail_streak" not in db.job_row(conn, "dashboard-probes")["last_metrics"]


def test_two_consecutive_failed_cycles_trip_fail_once(core, notifier, monkeypatch):
    _probe_results(monkeypatch, ok=True)
    core.run_probe_cycle(now=NOW)
    notifier.sent.clear()
    _probe_results(monkeypatch, ok=False, error="rclone exit 7: rateLimitExceeded",
                   transient=True)
    core.run_probe_cycle(now=NOW + 300)
    core.run_probe_cycle(now=NOW + 600)
    conn = core.connect()
    assert db.last_run(conn, "dashboard-probes")["status"] == "fail"
    assert db.job_row(conn, "dashboard-probes")["state"] == "FAIL"
    assert [t for t, _, _ in notifier.sent] == ["[dashboard] Probe cycle → FAIL"]
    assert _streak(core) == 2
    # A third failing cycle is the same fact: no second alert.
    core.run_probe_cycle(now=NOW + 900)
    assert len(notifier.sent) == 1
    assert _streak(core) == 3


def test_intervening_success_resets_the_streak(core, notifier, monkeypatch):
    _probe_results(monkeypatch, ok=False, error="rclone exit 7: rateLimitExceeded",
                   transient=True)
    core.run_probe_cycle(now=NOW)
    assert _streak(core) == 1
    _probe_results(monkeypatch, ok=True)
    core.run_probe_cycle(now=NOW + 300)
    assert _streak(core) == 0
    # …so the next single failure is damped again rather than tripping FAIL.
    _probe_results(monkeypatch, ok=False, error="rclone exit 7: rateLimitExceeded",
                   transient=True)
    core.run_probe_cycle(now=NOW + 600)
    conn = core.connect()
    assert db.last_run(conn, "dashboard-probes")["status"] == "ok"
    assert db.job_row(conn, "dashboard-probes")["state"] == "OK"
    assert [t for t, _, _ in notifier.sent] == []


def test_one_good_cycle_clears_fail_immediately(core, notifier, monkeypatch):
    """Damping delays a FAIL; it must never delay the clearing of one. The
    STATE goes back to OK on the first clean cycle - what waits is the recovery
    PUSH, which needs the job's dwell (two probe cycles) of unbroken OK before
    the episode is believed to be over."""
    _probe_results(monkeypatch, ok=False, error="rclone exit 7: rateLimitExceeded",
                   transient=True)
    core.run_probe_cycle(now=NOW)
    core.run_probe_cycle(now=NOW + 300)
    assert db.job_row(core.connect(), "dashboard-probes")["state"] == "FAIL"
    _probe_results(monkeypatch, ok=True)
    core.run_probe_cycle(now=NOW + 600)          # ONE good cycle clears the state
    assert db.job_row(core.connect(), "dashboard-probes")["state"] == "OK"
    assert notifier.sent[-1][0] == "[dashboard] Probe cycle → FAIL"
    job = core.registry.get("dashboard-probes")
    core.run_probe_cycle(now=NOW + 600 + ok_dwell_s(job))
    assert notifier.sent[-1][0] == "[dashboard] Probe cycle → OK"


def test_fail_streak_survives_a_restart(settings, registry, monkeypatch):
    """The streak is derived from the persisted probe rows, so a container
    restart mid-outage cannot reset the damping and hide a persistent
    failure forever."""
    from dashboard.services import Core
    _probe_results(monkeypatch, ok=False, error="rclone exit 7: rateLimitExceeded",
                   transient=True)
    first = Core(settings, registry, RecordingNotifier())
    first.init_store()
    first.run_probe_cycle(now=NOW)
    assert _streak(first) == 1
    fresh = Core(settings, registry, RecordingNotifier())   # new process, same DB
    fresh.init_store()
    fresh.run_probe_cycle(now=NOW + 300)
    assert _streak(fresh) == 2
    assert db.job_row(fresh.connect(), "dashboard-probes")["state"] == "FAIL"


def test_threshold_is_configurable(settings, registry, monkeypatch):
    from dashboard.services import Core
    settings.probe_fail_threshold = 1            # the pre-fix behaviour
    core = Core(settings, registry, RecordingNotifier())
    core.init_store()
    _probe_results(monkeypatch, ok=False, error="rclone exit 7: rateLimitExceeded",
                   transient=True)
    core.run_probe_cycle(now=NOW)
    assert db.job_row(core.connect(), "dashboard-probes")["state"] == "FAIL"


def test_transient_and_hard_failures_read_differently(core, monkeypatch):
    """Requirement: a quota/timeout must not look like "the destination is
    missing files" in the reason text."""
    _probe_results(monkeypatch, ok=False, error="timed out after 240s",
                   transient=True)
    core.run_probe_cycle(now=NOW)
    reason = db.last_run(core.connect(), "dashboard-probes")["reason"]
    assert "transient quota/timeout" in reason
    _probe_results(monkeypatch, ok=False, error="rclone exit 3: directory not found")
    core.run_probe_cycle(now=NOW + 300)
    row = db.last_run(core.connect(), "dashboard-probes")
    assert "transient" not in row["reason"] and "probe error" in row["reason"]
    assert row["metrics"]["failed_transient"] == 0


def test_probe_cycle_passes_the_configured_timeout(core, settings, monkeypatch):
    seen = {}

    def fake(job, timeout=None):
        seen["timeout"] = timeout
        return ProbeResult(ok=True, count=1)
    settings.rclone_timeout_s = 300
    monkeypatch.setattr(probes, "probe_job", fake)
    core.run_probe_cycle(now=NOW)
    assert seen["timeout"] == 300


# --------------------------------------------------------------------------- #
# Per-job probe interval
# --------------------------------------------------------------------------- #

def _reg_with_intervals():
    import copy
    from dashboard.registry import parse_registry
    from tests.conftest import JOBS_DOC
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][1]["probe"] = {"rclone_path": "gdrive-ro:Backups",
                               "interval_s": 1800}       # tree: big, slow
    doc["jobs"][4]["probe"] = {"rclone_path": "gdrive-ro:Gremlins",
                               "interval_s": 1800}       # offload: big, slow
    return parse_registry(doc)


def test_big_trees_are_probed_on_their_own_cadence(settings, monkeypatch):
    """snap has no interval_s → probed every cycle; the two 1800 s jobs are
    probed once and then skipped until their interval elapses."""
    from dashboard.services import Core
    core = Core(settings, _reg_with_intervals(), RecordingNotifier())
    core.init_store()
    _probe_results(monkeypatch, ok=True)
    assert set(core.run_probe_cycle(now=NOW)) == {"snap", "tree", "offload"}
    assert set(core.run_probe_cycle(now=NOW + 300)) == {"snap"}
    assert set(core.run_probe_cycle(now=NOW + 1799)) == {"snap"}
    assert set(core.run_probe_cycle(now=NOW + 1800)) == {"snap", "tree", "offload"}


def test_skipping_a_job_keeps_its_last_probe_row(settings, monkeypatch):
    """A skipped cycle must not look like a missing destination: the previous
    listing stands, so the card keeps its newest/count."""
    from dashboard.services import Core
    core = Core(settings, _reg_with_intervals(), RecordingNotifier())
    core.init_store()
    monkeypatch.setattr(probes, "probe_job",
                        lambda job, **kw: ProbeResult(ok=True, count=7,
                                                      newest_iso=db.to_iso(NOW - 10)))
    core.run_probe_cycle(now=NOW)
    core.run_probe_cycle(now=NOW + 300)
    conn = core.connect()
    assert db.last_probe(conn, "tree")["count"] == 7
    assert conn.execute("SELECT COUNT(*) FROM probes WHERE job_id='tree'"
                        ).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM probes WHERE job_id='snap'"
                        ).fetchone()[0] == 2


def test_self_heartbeat_is_recorded_even_when_nothing_is_due(settings, monkeypatch):
    """Otherwise a cycle in which every job was skipped would starve the
    self-job's dead-man's switch and page for LATE."""
    import copy
    from dashboard.registry import parse_registry
    from dashboard.services import Core
    from tests.conftest import JOBS_DOC
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][0]["probe"]["interval_s"] = 1800
    core = Core(settings, parse_registry(doc), RecordingNotifier())
    core.init_store()
    _probe_results(monkeypatch, ok=True)
    core.run_probe_cycle(now=NOW)
    assert core.run_probe_cycle(now=NOW + 300) == {}
    conn = core.connect()
    run = db.last_run(conn, "dashboard-probes")
    assert run["status"] == "ok" and run["metrics"]["probed"] == 0
    assert db.job_row(conn, "dashboard-probes")["state"] == "OK"


# --------------------------------------------------------------------------- #
# Damping must delay an alert, never cancel one
# --------------------------------------------------------------------------- #

def _core_with(doc, settings, **overrides):
    """A Core over a modified JOBS_DOC, with created_at pinned so nothing goes
    LATE for being new."""
    from dashboard.registry import parse_registry
    from dashboard.services import Core
    from tests.conftest import pin_created_at
    for key, value in overrides.items():
        setattr(settings, key, value)
    core = Core(settings, parse_registry(doc), RecordingNotifier())
    core.init_store()
    pin_created_at(settings)
    return core


def _intervald_doc(**probe):
    """JOBS_DOC with `tree` probed on its own interval (the shape the runbook
    prescribes for a big tree) — and nothing else probed, so a cycle in which
    `tree` is not due is a cycle that probes nothing."""
    import copy
    from tests.conftest import JOBS_DOC
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][1]["probe"] = {"rclone_path": "gdrive-ro:Backups", **probe}
    doc["jobs"][0]["probe"]["interval_s"] = 1800
    return doc


def test_persistent_hard_failure_on_an_intervald_job_reaches_fail(settings,
                                                                  monkeypatch):
    """THE regression. `tree` is probed every 1800 s (6th cycle at the 300 s
    default) and its destination is genuinely gone. The streak used to count
    CYCLES, so the five cycles in which `tree` was not due reset it and the
    failure could never reach the threshold — a permanently broken destination
    alerted exactly never. A hard error is not damped at all now."""
    core = _core_with(_intervald_doc(interval_s=1800), settings)
    monkeypatch.setattr(
        probes, "probe_job",
        lambda job, **kw: (ProbeResult(ok=False,
                                       error="rclone exit 3: directory not found")
                           if job.id == "tree"
                           else ProbeResult(ok=True, count=3)))
    core.run_probe_cycle(now=NOW)
    conn = core.connect()
    assert db.job_row(conn, "dashboard-probes")["state"] == "FAIL"
    assert ("[dashboard] Probe cycle → FAIL"
            in [t for t, _, _ in core.notifier.sent])
    assert "not damped" in db.last_run(conn, "dashboard-probes")["reason"]
    # …and the five cycles in which `tree` is not due keep it FAIL instead of
    # silently recording success (the old code reported `ok`/"probed").
    for i in range(1, 6):
        core.run_probe_cycle(now=NOW + i * 300)
    conn = core.connect()
    assert db.job_row(conn, "dashboard-probes")["state"] == "FAIL"
    assert db.last_run(conn, "dashboard-probes")["status"] == "fail"
    assert _streak(core, "tree") == 1
    assert len([t for t, _, _ in core.notifier.sent
                if t.startswith("[dashboard] Probe cycle")]) == 1


def test_persistent_quota_failure_on_an_intervald_job_reaches_fail(settings,
                                                                   monkeypatch):
    """DESIGN.md's promise — "a persistent quota failure must still reach FAIL"
    — proven for a job that is NOT probed every cycle. Damping counts that
    job's own probes, so the threshold is reached on its second probe (cycle 6),
    not never."""
    core = _core_with(_intervald_doc(interval_s=1800), settings)
    monkeypatch.setattr(
        probes, "probe_job",
        lambda job, **kw: (ProbeResult(
            ok=False, transient=True,
            error="transient (Drive quota/timeout): rclone exit 7: "
                  "rateLimitExceeded")
            if job.id == "tree" else ProbeResult(ok=True, count=3)))
    states = []
    for i in range(7):
        core.run_probe_cycle(now=NOW + i * 300)
        states.append(db.job_row(core.connect(), "dashboard-probes")["state"])
    # Damped on the first failure, still damped while not due, FAIL on the
    # second actual probe of `tree`.
    assert states == ["OK"] * 6 + ["FAIL"]
    assert _streak(core, "tree") == 2
    assert [t for t, _, _ in core.notifier.sent
            if "Probe cycle" in t] == ["[dashboard] Probe cycle → FAIL"]
    # One successful probe of the offending job clears it immediately.
    monkeypatch.setattr(probes, "probe_job",
                        lambda job, **kw: ProbeResult(ok=True, count=3))
    core.run_probe_cycle(now=NOW + 1800 + 1800)
    assert db.job_row(core.connect(), "dashboard-probes")["state"] == "OK"


def test_a_cycle_with_nothing_due_does_not_reset_a_pending_streak(settings,
                                                                 monkeypatch):
    """An empty cycle used to count as an all-clear: no results → no failures →
    streak 0, status ok, reason "probed". It must now report that it probed
    nothing, and leave the pending failure pending."""
    core = _core_with(_intervald_doc(interval_s=1800), settings)
    monkeypatch.setattr(
        probes, "probe_job",
        lambda job, **kw: ProbeResult(ok=False, transient=True,
                                      error="rclone exit 7: rateLimitExceeded"))
    core.run_probe_cycle(now=NOW)                 # both snap and tree fail
    assert _streak(core, "tree") == 1 and _streak(core, "snap") == 1
    assert core.run_probe_cycle(now=NOW + 300) == {}      # nothing due
    conn = core.connect()
    run = db.last_run(conn, "dashboard-probes")
    assert run["metrics"]["probed"] == 0
    assert run["status"] == "ok"                  # still damped, not a success
    assert "damped" in run["reason"]              # NOT "probed"
    assert _streak(core, "tree") == 1             # the streak survived
    assert db.job_row(conn, "dashboard-probes")["state"] == "OK"


def test_nothing_due_reads_differently_from_probed_clean(settings, monkeypatch):
    core = _core_with(_intervald_doc(interval_s=1800), settings)
    monkeypatch.setattr(probes, "probe_job",
                        lambda job, **kw: ProbeResult(ok=True, count=1))
    core.run_probe_cycle(now=NOW)
    assert db.last_run(core.connect(), "dashboard-probes")["reason"] == "probed"
    core.run_probe_cycle(now=NOW + 300)
    assert (db.last_run(core.connect(), "dashboard-probes")["reason"]
            == "no probes due")


def test_a_registry_with_no_probed_jobs_is_not_reassuring(settings, monkeypatch):
    """jobs.example.yml ships with both probe blocks commented out. In that
    state the self job used to report OK "probed" forever while probing
    nothing at all."""
    import copy
    from tests.conftest import JOBS_DOC
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"] = [j for j in doc["jobs"] if "probe" not in j]
    assert [j["id"] for j in doc["jobs"] if j["id"] == "dashboard-probes"]
    core = _core_with(doc, settings)
    called = []
    monkeypatch.setattr(probes, "probe_job",
                        lambda job, **kw: called.append(job.id))
    assert core.run_probe_cycle(now=NOW) == {} and called == []
    run = db.last_run(core.connect(), "dashboard-probes")
    assert run["reason"] == "no probes due" and run["metrics"]["probed"] == 0
    # The heartbeat is still recorded: the dead-man's switch must keep being fed
    # whether or not there is anything to probe.
    assert run["status"] == "ok"
    assert db.job_row(core.connect(), "dashboard-probes")["state"] == "OK"


def test_no_success_backstop_trips_under_a_high_threshold(settings, monkeypatch):
    """The rate-based backstop: whatever the threshold says, a destination that
    has not been listed successfully for PROBE_NO_SUCCESS_S is reported. Damping
    may delay an alert; it may not cancel one."""
    import copy
    from tests.conftest import JOBS_DOC
    core = _core_with(copy.deepcopy(JOBS_DOC), settings,
                      probe_fail_threshold=10, probe_no_success_s=600)
    monkeypatch.setattr(probes, "probe_job",
                        lambda job, **kw: ProbeResult(ok=True, count=1))
    core.run_probe_cycle(now=NOW)                          # a success to age out
    monkeypatch.setattr(
        probes, "probe_job",
        lambda job, **kw: ProbeResult(ok=False, transient=True,
                                      error="rclone exit 7: rateLimitExceeded"))
    core.run_probe_cycle(now=NOW + 300)                    # age 300 <= 600
    assert db.job_row(core.connect(), "dashboard-probes")["state"] == "OK"
    core.run_probe_cycle(now=NOW + 900)                    # age 900 > 600
    conn = core.connect()
    assert db.job_row(conn, "dashboard-probes")["state"] == "FAIL"
    reason = db.last_run(conn, "dashboard-probes")["reason"]
    assert "no successful probe of snap" in reason
    assert _streak(core) < 10                              # not the streak rule


def test_no_success_backstop_can_be_disabled(settings, monkeypatch):
    import copy
    from tests.conftest import JOBS_DOC
    core = _core_with(copy.deepcopy(JOBS_DOC), settings,
                      probe_fail_threshold=10, probe_no_success_s=0)
    monkeypatch.setattr(
        probes, "probe_job",
        lambda job, **kw: ProbeResult(ok=False, transient=True,
                                      error="rclone exit 7: rateLimitExceeded"))
    for i in range(5):
        core.run_probe_cycle(now=NOW + i * 300)
    assert db.job_row(core.connect(), "dashboard-probes")["state"] == "OK"


def test_damping_state_is_not_writable_through_ingest(core, registry,
                                                      monkeypatch):
    """`fail_streak` was damping state living in an ingest-writable metric:
    posting it as 0 before every cycle held the self job at "failure 1 of 2"
    for good. The streak is now derived from the probes table, which ingest
    cannot write."""
    forge = {"status": "metric", "metrics": {"fail_streak": 0}}
    self_job = registry.get("dashboard-probes")
    _probe_results(monkeypatch, ok=False, error="rclone exit 7: rateLimitExceeded",
                   transient=True)
    core.record_ping(self_job, dict(forge), now=NOW - 1)
    core.run_probe_cycle(now=NOW)
    core.record_ping(self_job, dict(forge), now=NOW + 299)
    core.run_probe_cycle(now=NOW + 300)
    conn = core.connect()
    assert db.job_row(conn, "dashboard-probes")["state"] == "FAIL"
    # And the forged value is not left on the board as a number nothing reads.
    assert "fail_streak" not in db.job_row(conn, "dashboard-probes")["last_metrics"]


# --------------------------------------------------------------------------- #
# Cycle cost: fresh clock, post-cycle scheduling, wall-clock budget
# --------------------------------------------------------------------------- #

class _FakeMonotonic:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _slow_probes(monkeypatch, seconds: float, ok: bool = True):
    """Make each probe "take" `seconds` of wall clock."""
    from dashboard import services
    clock = _FakeMonotonic()
    monkeypatch.setattr(services, "_monotonic", clock)

    def probe(job, **kw):
        clock.t += seconds
        return ProbeResult(ok=ok, count=1, error=None if ok else "boom")
    monkeypatch.setattr(probes, "probe_job", probe)
    return clock


def test_recompute_uses_the_post_cycle_clock(core, registry, monkeypatch):
    """A cycle is judged against a clock from AFTER the listings, not from
    before them: with serial probes the start-of-cycle clock could be a full
    cycle stale, so LATE lagged by up to ~2x the cycle duration."""
    core.record_ping(registry.get("snap"), ok(), now=NOW)   # deadline 300+300
    _slow_probes(monkeypatch, 400)
    core.run_probe_cycle(now=NOW + 500)                     # ends at NOW + 900
    conn = core.connect()
    run = db.last_run(conn, "dashboard-probes")
    assert run["started_at"] == db.to_iso(NOW + 500)
    assert run["received_at"] == db.to_iso(NOW + 900)
    assert db.last_probe(conn, "snap")["probed_at"] == db.to_iso(NOW + 900)
    # 900s of silence on a 600s deadline: LATE, not OK-because-the-clock-is-old.
    assert db.job_row(conn, "snap")["state"] == "LATE"


def test_scheduler_schedules_from_the_end_of_the_cycle(core, monkeypatch):
    """An overrunning cycle must not make the next one instantly due — that
    would hammer the remote that just rate-limited us."""
    _slow_probes(monkeypatch, 400)
    cycles = []
    real = core.run_probe_cycle
    monkeypatch.setattr(core, "run_probe_cycle",
                        lambda now=None: (cycles.append(now), real(now))[1])
    sched = Scheduler(core, probe_interval_s=300)
    sched.step(NOW)
    assert sched.last_probe == NOW + 400          # end of cycle, not NOW
    sched.step(NOW + 500)                         # only 100s since the END
    assert cycles == [NOW]                        # …so no second cycle yet
    sched.step(NOW + 700)                         # 300s since the END
    assert cycles == [NOW, NOW + 700]


def test_cycle_budget_defers_the_rest_and_catches_up_next_cycle(settings,
                                                                monkeypatch):
    """The budget bounds one cycle's wall clock (probes are serial, so
    n x timeout could otherwise overrun the self job's own deadline). Deferred
    jobs stay due and are probed first next time, so none is starved."""
    import copy
    from tests.conftest import JOBS_DOC
    doc = copy.deepcopy(JOBS_DOC)                 # three probed jobs, no intervals
    doc["jobs"][1]["probe"] = {"rclone_path": "gdrive-ro:Backups"}
    doc["jobs"][4]["probe"] = {"rclone_path": "gdrive-ro:Gremlins"}
    core = _core_with(doc, settings, probe_interval_s=300)
    clock = _slow_probes(monkeypatch, 200)        # 2 fit in a 300s budget
    first = core.run_probe_cycle(now=NOW)
    assert len(first) == 2                        # 3 probed jobs, 1 deferred
    run = db.last_run(core.connect(), "dashboard-probes")
    assert run["metrics"] == {"probed": 2, "failed": 0, "failed_transient": 0,
                              "deferred": 1, "probe_fail_jobs": 0,
                              "probe_fail_streak": 0}
    deferred = ({"snap", "tree", "offload"} - set(first)).pop()
    clock.t = 0
    second = core.run_probe_cycle(now=NOW + 300)
    assert deferred in second                     # least-recently-probed first


# --------------------------------------------------------------------------- #
# The real classifier, through a real probe_job, through a full cycle
# --------------------------------------------------------------------------- #

REAL_QUOTA_STDERR = (
    "rclone exit 1: Failed to lsjson with 2 errors: last error was: couldn't "
    "list directory: googleapi: Error 403: User Rate Limit Exceeded. Rate of "
    "requests for user exceed configured project quota, userRateLimitExceeded "
    "(dir Backups/km-tracker/daily)")


def _rclone_raises(monkeypatch, message: str):
    """Fail inside `run_rclone_lsjson`, so the cycle runs through the real
    `probe_job` → `_failed` → `is_transient_error` path instead of being handed
    a fabricated ProbeResult with `transient=` already decided."""
    def boom(path, timeout=None):
        raise probes.ProbeError(message)
    monkeypatch.setattr(probes, "run_rclone_lsjson", boom)


def test_real_quota_error_is_classified_and_damped_end_to_end(core, notifier,
                                                              monkeypatch):
    _rclone_raises(monkeypatch, REAL_QUOTA_STDERR)
    core.run_probe_cycle(now=NOW)
    conn = core.connect()
    probe = db.last_probe(conn, "snap")
    assert probe["ok"] is False
    assert probe["error"].startswith("transient (Drive quota/timeout): ")
    run = db.last_run(conn, "dashboard-probes")
    assert run["status"] == "ok" and "damped" in run["reason"]
    assert "transient quota/timeout" in run["reason"]
    assert run["metrics"]["failed_transient"] == 1
    assert db.job_row(conn, "dashboard-probes")["state"] == "OK"
    assert notifier.sent == []
    # Second consecutive failure: the damping expires and it pages.
    core.run_probe_cycle(now=NOW + 300)
    assert db.job_row(core.connect(), "dashboard-probes")["state"] == "FAIL"


def test_real_hard_error_pages_on_the_first_cycle_end_to_end(core, notifier,
                                                             monkeypatch):
    _rclone_raises(monkeypatch, "rclone exit 3: directory not found")
    core.run_probe_cycle(now=NOW)
    conn = core.connect()
    assert db.last_probe(conn, "snap")["error"] == "rclone exit 3: directory not found"
    run = db.last_run(conn, "dashboard-probes")
    assert run["status"] == "fail" and run["metrics"]["failed_transient"] == 0
    assert db.job_row(conn, "dashboard-probes")["state"] == "FAIL"
    assert [t for t, _, _ in notifier.sent] == ["[dashboard] Probe cycle → FAIL"]


def test_an_object_name_cannot_disguise_a_hard_error_as_transient(core,
                                                                 monkeypatch):
    """rclone echoes the object it was listing, and those names come from Drive
    (including a shared folder). A file called `503 timeout` must not buy a hard
    failure two cycles of damping."""
    _rclone_raises(monkeypatch,
                   'rclone exit 3: directory not found (dir "Backups/503 timeout")')
    core.run_probe_cycle(now=NOW)
    conn = core.connect()
    assert "transient" not in (db.last_probe(conn, "snap")["error"] or "")
    assert db.job_row(conn, "dashboard-probes")["state"] == "FAIL"


def test_a_bare_path_in_real_rclone_stderr_cannot_disguise_a_hard_error(
        core, notifier, monkeypatch):
    """Real rclone prints the failing path BOTH quoted and bare, and the backup
    trees are dated — `km_tracker-20260503-0312.db` contains `503`. A permission
    failure on an ordinary nightly snapshot path must page on the first cycle,
    not buy itself a cycle of damping."""
    _rclone_raises(monkeypatch,
                   'rclone exit 1: Failed to lsjson: failed to open directory '
                   '"km_tracker-20260503-0312.db": open '
                   '/srv/backups/km_tracker-20260503-0312.db: permission denied')
    core.run_probe_cycle(now=NOW)
    conn = core.connect()
    assert "transient" not in (db.last_probe(conn, "snap")["error"] or "")
    assert db.last_run(conn, "dashboard-probes")["metrics"]["failed_transient"] == 0
    assert db.job_row(conn, "dashboard-probes")["state"] == "FAIL"
    assert [t for t, _, _ in notifier.sent] == ["[dashboard] Probe cycle → FAIL"]
