"""Read side: password gate, bearer token, security headers, JSON contract,
HTML rendering of every state."""

from dashboard import db
from dashboard.services import COOLDOWN_FLOOR_S, ok_dwell_s
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
    settings.app_env = "dev"   # prod refuses to start without a password (see test_misc)
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
    assert "default-src 'self'" in csp and "https://" not in csp
    # Exactly one inline script is allowed, by a per-request nonce; no 'self', no unsafe-inline.
    import re
    m = re.search(r"script-src 'nonce-([A-Za-z0-9_-]{16,})'", csp)
    assert m, csp
    assert "'unsafe-inline'" not in csp and "script-src 'self'" not in csp
    html = r.data.decode()
    assert html.count("<script") == 1 and f'<script nonce="{m.group(1)}">' in html
    assert "src=" not in html.split("<script", 1)[1].split(">", 1)[0]   # inline, no external src
    # The nonce is fresh per request.
    r2 = authed.get("/")
    assert re.search(r"nonce-([A-Za-z0-9_-]+)", r2.headers["Content-Security-Policy"]).group(1) != m.group(1)


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
    core.record_ping(registry.get("disk"), {"status": "metric", "metrics": {
        "disk_free_bytes": 100 * 1024 ** 3, "disk_total_bytes": 400 * 1024 ** 3}}, now=NOW)
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
    # disk: a capacity gauge, OK at 75% used, and the sub-object every other kind reports as null.
    assert by_id["disk"]["state"] == "OK" and by_id["disk"]["lag"] is None
    assert by_id["disk"]["disk"] == {
        "measured_at": db.to_iso(NOW), "free_bytes": 100 * 1024 ** 3,
        "total_bytes": 400 * 1024 ** 3, "used_bytes": 300 * 1024 ** 3, "used_pct": 75.0,
        "min_free_bytes": 25 * 1024 ** 3, "max_used_pct": 90, "low": False, "low_on": []}
    assert by_id["disk"]["last_run"] is None and by_id["disk"]["cadence_s"] is None
    assert all(j["disk"] is None for j in d["jobs"] if j["id"] != "disk")
    s = d["summary"]
    assert (s["ok"], s["late"], s["fail"], s["stale_dest"], s["behind"], s["unknown"]) == (2, 1, 1, 1, 2, 2)
    assert s["total"] == 9 and s["computed_at"]


def test_alert_policy_is_exposed_additively(read, core, registry):
    """The /api/v1 contract is frozen, so the alert block is additive: every
    contract key still present, plus `alert` on both endpoints. `source` is what
    makes the resolution inspectable — DEPLOY.md §1d's verification step reads it
    to prove the box's jobs.yml edit landed."""
    core.record_ping(registry.get("tree"), {"status": "fail"}, now=NOW)
    for endpoint, pick in (("/api/v1/status", lambda d: {j["id"]: j for j in d["jobs"]}["tree"]),
                           ("/api/v1/jobs/tree", lambda d: d["job"])):
        j = pick(read.get(endpoint, headers=bearer()).get_json())
        assert CONTRACT_KEYS <= set(j)
        assert set(j["alert"]) == {"after_s", "never", "source", "bad_since",
                                   "alerted_at", "cooldown_s", "last_paged_at"}
        assert j["alert"]["never"] is False and j["alert"]["after_s"] == 0
        assert j["alert"]["source"] == "alert_after_s"
        assert j["alert"]["bad_since"] == db.to_iso(NOW)     # episode is running
        assert j["alert"]["alerted_at"] == db.to_iso(NOW)    # ...and was paged (threshold 0)
        # The per-job cooldown, so "quiet phone, unhappy board" is answerable
        # from the API: this job paged just now and cannot page again for 6 h.
        assert j["alert"]["cooldown_s"] == COOLDOWN_FLOOR_S
        assert j["alert"]["last_paged_at"] == db.to_iso(NOW)
    # `info` declares nothing and is informational: it never pages, and says why.
    j = {x["id"]: x for x in read.get("/api/v1/status", headers=bearer()).get_json()["jobs"]}["info"]
    assert j["alert"] == {"after_s": None, "never": True, "source": "informational",
                          "bad_since": None, "alerted_at": None,
                          "cooldown_s": None, "last_paged_at": None}
    core.record_ping(registry.get("tree"), {"status": "ok"}, now=NOW + 5)
    core.recompute_all(now=NOW + 5 + ok_dwell_s(registry.get("tree")))
    j = {x["id"]: x for x in read.get("/api/v1/status", headers=bearer()).get_json()["jobs"]}["tree"]
    assert j["alert"]["bad_since"] is None and j["alert"]["alerted_at"] is None
    # ...but `last_paged_at` SURVIVES the episode it belongs to — that is the
    # whole point of it: the cooldown spans episodes, `alerted_at` does not.
    assert j["alert"]["last_paged_at"] == db.to_iso(NOW)


def test_alert_policy_shown_on_the_job_page(settings, notifier):
    import copy
    from dashboard import create_app
    from dashboard.registry import parse_registry
    from tests.conftest import JOBS_DOC
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][1]["alert_after_s"] = 108000                 # tree: 1d 6h
    doc["jobs"][2].pop("alert_after_s")                      # mirror: never
    doc["jobs"][2]["alert"] = "never"
    reg = parse_registry(doc)
    app = create_app("read", settings, reg, notifier)
    app.extensions["core"].record_ping(reg.get("tree"), {"status": "fail"}, now=NOW)
    c = app.test_client()
    c.post("/login", data={"password": PASSWORD})
    html = c.get("/jobs/tree").data.decode()
    assert "Pages after 1d 6h continuously not OK." in html
    assert "not OK since" in html and "not paged yet" in html
    assert "Never pages" in c.get("/jobs/mirror").data.decode()
    assert html.count("<script") == 1                        # CSP: still exactly one inline script


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
    assert html.count("<script") == 1                # only the nonce'd timestamp localizer
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


# --------------------------------------------------------------------------- #
# Copy / layout fixes from browser QA
# --------------------------------------------------------------------------- #

def test_never_run_hint_is_well_formed_with_and_without_clause(authed):
    html = authed.get("/").data.decode()
    # 'info' has no max_age → sentence ends right after "yet"; 'offload' has one → the em-dash clause.
    assert "No completion has been pinged yet.</p>" in html
    assert "No completion has been pinged yet — the 14d max-age target is inert until the first" in html
    assert "yet .</p>" not in html and " .</p>" not in html


def test_dest_row_omits_unknown_newest_and_copy_tree_pill(authed, core, registry, read):
    # Mac probe shape for a copy tree: a count but no newest-object time, with bytes missing.
    core.record_ping(registry.get("tree"), {"status": "ok",
                     "metrics": {"missing_bytes": 4096, "missing_files": 1, "dest_count": 1032}}, now=NOW)
    for html in (authed.get("/").data.decode(), authed.get("/jobs/tree").data.decode()):
        card = html[html.index('state-stale_dest'):]
        row = card[card.index("1032 object"):card.index("</dd>", card.index("1032 object"))]
        assert "newest" not in card[card.index("Dest") if "Dest" in card else 0:card.index("1032 object")]
        assert "never" not in row and "1032 objects" in row
        assert 'pill-fail">stale' not in row                   # STALE_DEST chip + Missing row already say it
    j = {x["id"]: x for x in read.get("/api/v1/status", headers=bearer()).get_json()["jobs"]}["tree"]
    assert j["dest"] == {"newest": None, "count": 1032, "fresh": False, "probed_at": None, "probe_error": None}


def test_non_copy_tree_dest_row_keeps_pill_and_newest(authed, core, registry):
    core.record_ping(registry.get("offload"), {"status": "metric", "metrics": {
        "lag_bytes": 0, "dest_newest_iso": db.to_iso(NOW - 3600), "dest_count": 140}}, now=NOW)
    html = authed.get("/").data.decode()
    assert "newest <time" in html and "140 objects" in html and 'pill-ok">fresh' in html


def test_login_page_has_no_nav_links_but_board_does(read_app, authed):
    login = read_app.test_client().get("/login").data.decode()   # fresh, signed-out client
    assert "Sign out" not in login and 'href="/api/v1/status"' not in login
    assert "hopper-dashboard v" in login                       # footer stays
    board = authed.get("/").data.decode()
    assert "Sign out" in board and 'href="/api/v1/status"' in board


def test_summary_timestamps_are_localizable_time_elements(authed, core):
    core.recompute_all(now=time.time())
    html = authed.get("/").data.decode()
    assert "states computed <time datetime=" in html and "just now" in html
    assert "generated <time datetime=" in html


# --------------------------------------------------------------------------- #
# Disk capacity gauge (kind: disk)
# --------------------------------------------------------------------------- #

def _disk_ping(core, registry, free_gib, total_gib=400, now=NOW):
    core.record_ping(registry.get("disk"), {"status": "metric", "metrics": {
        "disk_free_bytes": free_gib * 1024 ** 3,
        "disk_total_bytes": total_gib * 1024 ** 3}}, now=now)


def test_disk_gauge_renders_on_board_and_job_page(authed, core, registry):
    _disk_ping(core, registry, 100)
    for html in (authed.get("/").data.decode(), authed.get("/jobs/disk").data.decode()):
        card = html[html.index("Capacity"):]
        assert "75.0% used" in card
        assert "100.0 GiB free of 400.0 GiB" in card          # GiB, not human_bytes' decimal GB
        assert 'class="gaugebar"' in card and 'class="fill" x="0" y="0" width="75.0"' in card
        assert 'class="mark" x="90"' in card                  # the 90% ceiling marker
        # The capacity thresholds and the alert policy are stated SEPARATELY: one
        # says what turns the gauge BEHIND, the other whether BEHIND ever reaches
        # the phone. Running them together is how a gauge with thresholds AND
        # `alert: never` came to claim it alerts.
        assert "BEHIND below 25.0 GiB free or over 90% used" in card
        assert "pages after 0s not OK" in card
    # The bar must not rely on an inline style attribute: style-src is 'self' with no
    # 'unsafe-inline', so a style="width:…" bar would silently render empty.
    assert "style=" not in authed.get("/").data.decode()


def test_disk_gauge_marks_a_low_disk(authed, core, registry):
    _disk_ping(core, registry, 10)
    html = authed.get("/").data.decode()
    assert "gauge-low" in html and "BEHIND" in html
    assert "only 10.0 GiB free, below the 25.0 GiB floor" in html   # state_reason hint


def test_disk_card_without_metrics_says_so(authed):
    html = authed.get("/").data.decode()
    assert "no disk metrics reported yet" in html and "Capacity" in html


def test_disk_card_survives_a_zero_total(authed, core, registry):
    """A 0-byte total (bind mount, vanished path) must not 500 the board."""
    _disk_ping(core, registry, 10, total_gib=0)
    html = authed.get("/").data.decode()
    assert "capacity unknown" in html and "10.0 GiB free" in html
    assert "gaugebar" not in html[html.index("Capacity"):html.index("Capacity") + 600]


def test_gauge_bar_width_is_clamped_at_both_ends(read_app):
    """The width is data from a probe, and an SVG width="-40.0" is not a valid length.
    Unreachable through ingest today (the percent is derived from clamped metrics), so
    drive the macro directly rather than pretending the path exists."""
    disk_gauge = read_app.jinja_env.get_template("_macros.html").module.disk_gauge

    def render(used_pct):
        return disk_gauge({"free_bytes": 1, "total_bytes": 2, "used_pct": used_pct,
                           "min_free_bytes": None, "max_used_pct": None,
                           "low": False, "measured_at": None}, NOW,
                          {"never": True, "after_s": None})

    assert 'class="fill" x="0" y="0" width="0"' in render(-40.0)
    assert 'class="fill" x="0" y="0" width="100"' in render(120.0)
    assert 'class="fill" x="0" y="0" width="62.5"' in render(62.5)


# --------------------------------------------------------------------------- #
# Board captions: "will this job ever page", not `informational`
#
# They are different questions, and the difference is load-bearing for exactly
# the job that matters. `minecraft-offload` is `alert: never` AND carries
# `manual:` thresholds, so it is NOT `informational` — the caption keyed off
# `informational` skipped it entirely, and it sits on the board reading BEHIND
# (its normal resting state between offloads) with nothing saying it will never
# page. That is the same informational-vs-alert_never confusion this branch
# fixed in the code, left in the one place Graham actually looks.
# --------------------------------------------------------------------------- #

def _board(settings, notifier, doc):
    from dashboard import create_app
    from dashboard.registry import parse_registry
    reg = parse_registry(doc)
    app = create_app("read", settings, reg, notifier)
    c = app.test_client()
    c.post("/login", data={"password": PASSWORD})
    return app, reg, c


def test_a_never_alerting_job_with_lag_thresholds_says_so_on_the_board(settings, notifier):
    """The `minecraft-offload` shape: opted out by `alert: never`, but with
    `manual:` thresholds, so `informational` is False."""
    import copy
    from tests.conftest import JOBS_DOC
    doc = copy.deepcopy(JOBS_DOC)
    offload = [j for j in doc["jobs"] if j["id"] == "offload"][0]
    offload.pop("alert_after_s")
    offload["alert"] = "never"
    assert offload["manual"]["max_lag_bytes"]            # not informational
    app, reg, c = _board(settings, notifier, doc)
    app.extensions["core"].record_ping(
        reg.get("offload"), {"status": "ok", "metrics": {"lag_bytes": 99999}}, now=NOW)
    html = c.get("/").data.decode()
    card = html[html.index('id="job-offload"'):]
    card = card[:card.index("</article>")]
    assert "never alerts" in card
    assert "alert: never" in card                        # ...and WHICH reason
    assert "informational" not in card


def test_an_informational_job_still_says_why_it_never_alerts(settings, notifier):
    """The other reason, still captioned, and told apart from the first: no
    thresholds at all, so the policy resolved to `never` on its own."""
    import copy
    from tests.conftest import JOBS_DOC
    doc = copy.deepcopy(JOBS_DOC)
    offload = [j for j in doc["jobs"] if j["id"] == "offload"][0]
    offload.pop("alert_after_s")
    offload.pop("manual")                                # no thresholds -> informational
    app, reg, c = _board(settings, notifier, doc)
    assert reg.get("offload").informational and reg.get("offload").alert_never
    app.extensions["core"].record_ping(
        reg.get("offload"), {"status": "ok", "metrics": {"lag_bytes": 5}}, now=NOW)
    html = c.get("/").data.decode()
    card = html[html.index('id="job-offload"'):]
    card = card[:card.index("</article>")]
    assert "never alerts" in card and "informational: no thresholds set" in card


def test_a_job_that_can_page_gets_no_never_alerts_caption(settings, notifier):
    """The caption must not be printed for a job that WILL page — the opposite
    lie. `offload` with a real threshold is not opted out of anything."""
    import copy
    from tests.conftest import JOBS_DOC
    doc = copy.deepcopy(JOBS_DOC)
    app, reg, c = _board(settings, notifier, doc)        # offload: alert_after_s 0
    app.extensions["core"].record_ping(
        reg.get("offload"), {"status": "ok", "metrics": {"lag_bytes": 99999}}, now=NOW)
    html = c.get("/").data.decode()
    card = html[html.index('id="job-offload"'):]
    card = card[:card.index("</article>")]
    assert "never alerts" not in card


def test_a_disk_gauge_with_thresholds_but_alert_never_does_not_claim_it_alerts(
        settings, notifier):
    """The `_macros.html` half of the same bug. The gauge foot read its caption
    off the CAPACITY thresholds — "alerts below 25.0 GiB free" — which is a flat
    lie on a gauge that carries thresholds AND `alert: never`. The thresholds
    say what turns the gauge BEHIND; the alert policy says whether BEHIND ever
    reaches a phone. Two facts, two sources."""
    import copy
    from tests.conftest import JOBS_DOC
    doc = copy.deepcopy(JOBS_DOC)
    disk = [j for j in doc["jobs"] if j["id"] == "disk"][0]
    disk.pop("alert_after_s")
    disk["alert"] = "never"
    app, reg, c = _board(settings, notifier, doc)
    assert reg.get("disk").min_free_bytes and reg.get("disk").alert_never
    app.extensions["core"].record_ping(
        reg.get("disk"), {"status": "metric",
                          "metrics": {"disk_free_bytes": 100 * 1024 ** 3,
                                      "disk_total_bytes": 400 * 1024 ** 3}}, now=NOW)
    html = c.get("/").data.decode()
    card = html[html.index("Capacity"):]
    assert "BEHIND below 25.0 GiB free" in card          # the thresholds, stated
    assert "never pages" in card                         # ...and the policy, stated
    assert "alerts below" not in card                    # never the old conflation


def test_a_threshold_less_disk_gauge_that_pages_says_so(settings, notifier):
    """And the mirror image, which the old caption also got backwards: a gauge
    with NO capacity thresholds but an explicit `alert_after_s` used to read
    "informational — no thresholds set, never alerts" while being perfectly able
    to page for a reading that went stale."""
    import copy
    from tests.conftest import JOBS_DOC
    doc = copy.deepcopy(JOBS_DOC)
    disk = [j for j in doc["jobs"] if j["id"] == "disk"][0]
    disk.pop("disk")                                     # no capacity thresholds
    disk["alert_after_s"] = 3600
    app, reg, c = _board(settings, notifier, doc)
    assert reg.get("disk").informational and not reg.get("disk").alert_never
    app.extensions["core"].record_ping(
        reg.get("disk"), {"status": "metric",
                          "metrics": {"disk_free_bytes": 100 * 1024 ** 3,
                                      "disk_total_bytes": 400 * 1024 ** 3}}, now=NOW)
    card = c.get("/").data.decode()
    card = card[card.index("Capacity"):]
    assert "no capacity thresholds set" in card
    assert "pages after 1h not OK" in card
    assert "never alerts" not in card
