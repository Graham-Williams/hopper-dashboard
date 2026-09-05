"""Regression tests for the pre-deploy review fixes (2026-09-05): never-pinged
jobs go LATE, missing-vs-differ for copy trees, the machine-offline alert
rule, prod fail-fast config, proxy-aware client IP + global login cap, limiter
key cap, ISO clamping, ingest parsing bounds, ntfy body, probe argv."""

from __future__ import annotations

import json
import re

import pytest

from dashboard import create_app, db, probes
from dashboard.db import from_iso, to_iso
from dashboard.humanize import absolute
from dashboard.ratelimit import KEY_MAX_LEN, SlidingWindowLimiter
from dashboard.state import Facts, compute_state, dest_info, lag_info
from tests.conftest import (INGEST_TOKEN, JOBS_DOC, PASSWORD, READ_TOKEN, auth,
                            pin_created_at)
from dashboard.registry import parse_registry

NOW = 1_800_000_000.0
REG = parse_registry(JOBS_DOC)
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
    mac, mirror = REG.get("macprobe"), REG.get("mirror")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(mirror, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 20_000)
    notifier.sent.clear()
    # The Mac wakes: its probe posts, and in the same batch the mirror posts too.
    core.record_ping(mirror, {"status": "ok"}, now=NOW + 20_100)   # mirror recovers while probe still LATE
    core.record_ping(mac, {"status": "ok"}, now=NOW + 20_101)
    titles = [t for t, _, _ in notifier.sent]
    # mirror's recovery came in its own batch, before the probe recovered → a normal recovery alert;
    # the probe's own recovery alerts; nothing is duplicated.
    assert titles == ["[dashboard] Drive mirror → OK", "[dashboard] Mac probe → OK"]


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
    assert body == "containers: OK → FAIL"
    assert "tunnel-1" not in body and "not running" not in body


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
