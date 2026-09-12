"""Core write-side behaviour: transitions persisted + notified, metric pings,
probe cycle recording, scheduler resilience."""

from dashboard import db, probes
from dashboard.probes import ProbeResult
from dashboard.scheduler import Scheduler
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
    # Body is the bare transition: the free-text reason never goes to ntfy.sh.
    assert body == "snap: OK → FAIL" and "timeout" not in body


def test_recovery_alerts_default_priority(core, notifier, registry):
    job = registry.get("snap")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    core.record_ping(job, ok(), now=NOW + 10)
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
    # Heartbeat resumes → recovery alert.
    assert core.record_ping(job, ok(), now=NOW + 700) == "OK"
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
    assert n.notify_transition("x", "x", "OK", "FAIL", None) is False
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


def _streak(core) -> int:
    return db.job_row(core.connect(), "dashboard-probes")["last_metrics"]["fail_streak"]


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


def test_recovery_from_fail_is_immediate(core, notifier, monkeypatch):
    _probe_results(monkeypatch, ok=False, error="rclone exit 7: rateLimitExceeded",
                   transient=True)
    core.run_probe_cycle(now=NOW)
    core.run_probe_cycle(now=NOW + 300)
    assert db.job_row(core.connect(), "dashboard-probes")["state"] == "FAIL"
    _probe_results(monkeypatch, ok=True)
    core.run_probe_cycle(now=NOW + 600)          # ONE good cycle is enough
    assert db.job_row(core.connect(), "dashboard-probes")["state"] == "OK"
    assert notifier.sent[-1][0] == "[dashboard] Probe cycle → OK"


def test_fail_streak_survives_a_restart(settings, registry, monkeypatch):
    """The streak lives in the jobs table, so a container restart mid-outage
    cannot reset the damping and hide a persistent failure forever."""
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
