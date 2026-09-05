"""Read side: password gate, bearer token, security headers, JSON contract,
HTML rendering of every state."""

from dashboard import db
from tests.conftest import PASSWORD, READ_TOKEN

CONTRACT_KEYS = {"id", "name", "machine", "kind", "state", "since", "last_run",
                 "last_success", "cadence_s", "grace_s", "destination", "protects",
                 "method", "lag", "dest", "last_metrics"}
import time

NOW = float(int(time.time())) - 120  # recent, so view-side freshness math is realistic


def bearer(token=READ_TOKEN):
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #

def test_healthz_read_exempt(read):
    r = read.get("/healthz")
    assert r.status_code == 200 and r.get_json()["role"] == "read"


def test_board_redirects_to_login_with_next(read):
    r = read.get("/jobs/snap?x=1")
    assert r.status_code == 302
    assert r.headers["Location"].startswith("/login?next=")
    assert "jobs/snap" in r.headers["Location"] and "x%3D1" in r.headers["Location"]


def test_static_exempt(read):
    assert read.get("/static/app.css").status_code == 200


def test_login_flow_sets_secure_cookie_and_redirects_next(read):
    r = read.post("/login", data={"password": PASSWORD, "next": "/jobs/snap"})
    assert r.status_code == 302 and r.headers["Location"] == "/jobs/snap"
    cookie = r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie
    assert PASSWORD not in cookie
    assert read.get("/").status_code == 200


def test_wrong_password_401_then_rate_limited(read, settings):
    for _ in range(settings.login_rate_max):
        r = read.post("/login", data={"password": "nope"})
        assert r.status_code == 401
    r = read.post("/login", data={"password": "nope"})
    assert r.status_code == 429
    # Even the right password is blocked while limited.
    assert read.post("/login", data={"password": PASSWORD}).status_code == 429


def test_open_redirect_rejected(read):
    for bad in ("https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)"):
        r = read.post("/login", data={"password": PASSWORD, "next": bad})
        assert r.status_code == 302 and r.headers["Location"] == "/", bad


def test_logout_clears_session(authed):
    assert authed.get("/").status_code == 200
    r = authed.get("/logout")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert authed.get("/").status_code == 302


def test_login_page_renders_and_redirects_when_authed(read_app, authed):
    r = read_app.test_client().get("/login")
    assert r.status_code == 200 and b'type="password"' in r.data
    assert authed.get("/login").status_code == 302


def test_gate_off_when_no_password(settings, registry, notifier):
    from dashboard import create_app
    settings.app_password = ""
    app = create_app("read", settings, registry, notifier)
    c = app.test_client()
    assert c.get("/").status_code == 200
    assert c.get("/login").status_code == 302


# --------------------------------------------------------------------------- #
# Bearer token (Hopper)
# --------------------------------------------------------------------------- #

def test_api_without_auth_is_401_json_not_redirect(read):
    r = read.get("/api/v1/status")
    assert r.status_code == 401 and r.get_json() == {"error": "unauthorized"}


def test_bearer_read_token_works_on_api(read):
    assert read.get("/api/v1/status", headers=bearer()).status_code == 200
    assert read.get("/api/v1/jobs/snap", headers=bearer()).status_code == 200


def test_bearer_read_token_does_not_open_html(read):
    r = read.get("/", headers=bearer())
    assert r.status_code == 302


def test_wrong_or_ingest_token_rejected_on_api(read):
    assert read.get("/api/v1/status", headers=bearer("wrong")).status_code == 401
    assert read.get("/api/v1/status", headers=bearer("test-ingest-token")).status_code == 401


def test_empty_read_token_never_matches(settings, registry, notifier):
    from dashboard import create_app
    settings.read_token = ""
    app = create_app("read", settings, registry, notifier)
    assert app.test_client().get("/api/v1/status", headers=bearer("")).status_code == 401


# --------------------------------------------------------------------------- #
# Headers / host pin
# --------------------------------------------------------------------------- #

def test_security_headers(authed):
    r = authed.get("/")
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Referrer-Policy"] == "same-origin"
    assert h["X-Frame-Options"] == "DENY"
    csp = h["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "script-src 'none'" in csp
    assert "https://" not in csp


def test_host_pin_and_csrf(settings, registry, notifier):
    from dashboard import create_app
    settings.app_host = "dash.example.com"
    app = create_app("read", settings, registry, notifier)
    c = app.test_client()
    assert c.get("/healthz").status_code == 200
    assert c.get("/login", base_url="http://other.example.com").status_code == 403
    assert c.get("/login", base_url="https://dash.example.com").status_code == 200
    # POST without Origin/Referer → rejected; with matching Origin → accepted.
    assert c.post("/login", data={"password": PASSWORD},
                  base_url="https://dash.example.com").status_code == 403
    assert c.post("/login", data={"password": PASSWORD}, base_url="https://dash.example.com",
                  headers={"Origin": "https://evil.example"}).status_code == 403
    r = c.post("/login", data={"password": PASSWORD}, base_url="https://dash.example.com",
               headers={"Origin": "https://dash.example.com"})
    assert r.status_code == 302


# --------------------------------------------------------------------------- #
# JSON contract
# --------------------------------------------------------------------------- #

def _seed_all_states(core, registry):
    """Drive the store so that every state appears on the board."""
    conn = core.connect()
    core.record_ping(registry.get("snap"), {"status": "ok", "metrics": {"db_sha256": "cd" * 32}}, now=NOW)
    with conn:
        db.insert_probe(conn, "snap", probed_at=db.to_iso(NOW), ok=True,
                        newest_iso=db.to_iso(NOW - 5 * 86400), count=3, state_sha="ab" * 32)
    core.record_ping(registry.get("tree"), {"status": "fail", "reason": "timeout", "note": "killed"}, now=NOW)
    core.record_ping(registry.get("mirror"), {"status": "ok", "metrics": {"pending": 2, "mismatch": 0}}, now=NOW)
    core.record_ping(registry.get("containers"), {"status": "ok", "metrics": {"running": "app-1,tunnel-1"}}, now=NOW)
    core.record_ping(registry.get("offload"), {"status": "metric",
                     "metrics": {"lag_bytes": 5 * 10 ** 9, "lag_files": 12,
                                 "dest_newest_iso": db.to_iso(NOW - 3600), "dest_count": 140}}, now=NOW)
    core.record_ping(registry.get("macprobe"), {"status": "ok"}, now=NOW - 20_000)
    core.recompute_all(now=NOW + 1)  # snap → STALE_DEST, macprobe → LATE, info → UNKNOWN


def test_status_shape_matches_contract(read, core, registry):
    _seed_all_states(core, registry)
    r = read.get("/api/v1/status", headers=bearer())
    d = r.get_json()
    assert set(d) >= {"generated_at", "summary", "jobs"}
    assert set(d["summary"]) >= {"ok", "late", "fail", "stale_dest", "behind", "unknown"}
    assert len(d["jobs"]) == len(registry)
    for j in d["jobs"]:
        assert CONTRACT_KEYS <= set(j), j["id"]
        assert set(j["dest"]) >= {"newest", "count", "fresh"}
        assert j["lag"] is None or isinstance(j["lag"], dict)
        assert isinstance(j["last_metrics"], dict)
    by_id = {j["id"]: j for j in d["jobs"]}
    assert by_id["snap"]["state"] == "STALE_DEST" and by_id["snap"]["dest"]["fresh"] is False
    assert by_id["tree"]["state"] == "FAIL"
    assert by_id["mirror"]["state"] == "BEHIND"
    assert by_id["containers"]["state"] == "OK"
    assert by_id["offload"]["state"] == "BEHIND" and by_id["offload"]["lag"]["bytes"] == 5 * 10 ** 9
    assert by_id["offload"]["last_run"] is None and by_id["offload"]["dest"]["count"] == 140
    assert by_id["macprobe"]["state"] == "LATE"
    assert by_id["info"]["state"] == "UNKNOWN" and by_id["info"]["lag"]["behind"] is False
    s = d["summary"]
    assert (s["ok"], s["late"], s["fail"], s["stale_dest"], s["behind"], s["unknown"]) == (1, 1, 1, 1, 2, 2)
    assert s["total"] == 8 and s["computed_at"]


def test_job_detail_json(read, core, registry):
    _seed_all_states(core, registry)
    r = read.get("/api/v1/jobs/snap?limit=5", headers=bearer())
    d = r.get_json()
    assert d["job"]["id"] == "snap" and CONTRACT_KEYS <= set(d["job"])
    assert len(d["runs"]) == 1 and d["runs"][0]["status"] == "ok"
    assert [c["to_state"] for c in d["state_changes"]] == ["STALE_DEST", "OK"]
    assert d["probes"][0]["count"] == 3
    assert read.get("/api/v1/jobs/nope", headers=bearer()).status_code == 404
    assert read.get("/api/v1/jobs/snap?limit=abc", headers=bearer()).status_code == 200


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

def test_board_renders_every_state(authed, core, registry):
    _seed_all_states(core, registry)
    r = authed.get("/")
    assert r.status_code == 200
    html = r.data.decode()
    for state in ("OK", "LATE", "FAIL", "STALE DEST", "BEHIND", "UNKNOWN"):
        assert state in html, state
    assert html.count("<article") == len(registry)
    assert "Mac offline or asleep" in html          # late_means hint
    assert "5.0 GB" in html                          # human bytes for manual lag
    assert 'class="strip"' in html                   # history sparkline
    assert "informational" in html
    assert "<script" not in html                     # no JS at all
    assert 'href="/static/app.css"' in html
    assert "cdn" not in html.lower()


def test_board_empty_state(authed):
    r = authed.get("/")
    assert r.status_code == 200 and r.data.count(b"UNKNOWN") >= 8


def test_board_flags_stale_scheduler(authed, core, registry):
    core.recompute_all(now=NOW - 3600)  # an hour ago relative to the real clock
    assert b"Scheduler may be stale" in authed.get("/").data


def test_job_page_renders_tables(authed, core, registry):
    _seed_all_states(core, registry)
    r = authed.get("/jobs/snap")
    html = r.data.decode()
    assert r.status_code == 200
    assert "State changes" in html and "Destination probes" in html and "db_sha256" in html
    r = authed.get("/jobs/tree")
    assert b"killed" in r.data  # note shown
    assert authed.get("/jobs/nope").status_code == 404


def test_html_escapes_untrusted_note(authed, core, registry):
    core.record_ping(registry.get("tree"), {"status": "fail", "note": "<img src=x onerror=alert(1)>"}, now=NOW)
    html = authed.get("/jobs/tree").data.decode()
    assert "<img src=x" not in html and "&lt;img" in html


def test_container_card_marks_missing_names(authed, core, registry):
    core.record_ping(registry.get("containers"), {"status": "ok", "metrics": {"running": "app-1"}}, now=NOW)
    html = authed.get("/").data.decode()
    assert 'class="missing">tunnel-1' in html


def test_state_reason_exposed_and_shown(authed, read, core, registry):
    core.record_ping(registry.get("containers"), {"status": "ok", "metrics": {"running": "app-1"}}, now=NOW)
    j = {x["id"]: x for x in read.get("/api/v1/status", headers=bearer()).get_json()["jobs"]}["containers"]
    assert j["state"] == "FAIL" and "tunnel-1" in j["state_reason"]
    html = authed.get("/").data.decode()
    assert "not running: tunnel-1" in html
