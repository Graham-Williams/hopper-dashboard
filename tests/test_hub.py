"""The Hub (``/``): card order, counts, the health strip, and the page rules
it shares with the board — one script, no style= attributes, no JSON link."""

from __future__ import annotations

import re

from dashboard import inbox_db
from dashboard.hub import health_strip

NOW = 1_900_000_000


def _inbox(settings):
    conn = inbox_db.connect(settings.inbox_db_path)
    inbox_db.init_inbox_schema(conn)
    return conn


def test_inbox_card_comes_first_and_links_to_capture(authed):
    html = authed.get("/").data.decode()
    assert html.index('class="hub-card hub-inbox"') < html.index('class="hub-card hub-dashboard"')
    # Record is a REAL link (works with JS off), and it is inside the Inbox card.
    inbox_card = html.split('class="hub-card hub-inbox"', 1)[1].split("</section>", 1)[0]
    assert re.search(r'<a class="hub-record" href="/inbox#capture">', inbox_card)
    assert 'href="/inbox?awaiting=review"' in inbox_card
    assert 'href="/inbox"' in inbox_card
    dash_card = html.split('class="hub-card hub-dashboard"', 1)[1].split("</section>", 1)[0]
    assert 'href="/dashboard"' in dash_card


def test_inbox_counts_are_shown(authed, settings):
    conn = _inbox(settings)
    with conn:
        for i in range(3):
            inbox_db.create_item(conn, source="typed", text=f"note {i}")
        closed = inbox_db.create_item(conn, source="typed", text="done")
        inbox_db.update_item(conn, closed, {"state": "closed"})
    conn.close()
    html = authed.get("/").data.decode()
    assert "3 open items" in html


def test_health_strip_folds_states_and_labels_every_problem(authed, core, registry):
    core.record_ping(registry.get("snap"), {"status": "fail"}, now=NOW)
    html = authed.get("/").data.decode()
    strip = html.split('class="health"', 1)[1].split("</p>", 1)[0]
    # Never colour alone: every bucket carries a glyph AND a word.
    assert re.search(r"●</span> \d+ OK</span>", strip)
    assert re.search(r"▲</span> 0 behind</span>", strip)
    assert re.search(r"✕</span> 1 failing</span>", strip)
    assert 'class="hs hs-failing"' in strip          # coloured: non-zero
    assert 'class="hs hs-behind zero"' in strip       # muted: zero


def test_health_strip_helper():
    assert health_strip({"ok": 12, "late": 1, "behind": 2, "fail": 1,
                         "stale_dest": 1, "unknown": 3, "total": 20}) == {
        "ok": 12, "behind": 3, "failing": 2, "unknown": 3, "total": 20}
    assert health_strip({})["ok"] == 0


def test_stale_scheduler_is_flagged_on_the_hub(authed, core):
    import time
    assert "Scheduler may be stale" not in authed.get("/").data.decode()
    core.recompute_all(now=time.time() - 3600)
    assert "Scheduler may be stale" in authed.get("/").data.decode()


def test_hub_has_exactly_one_script_and_no_style_attributes(authed):
    html = authed.get("/").data.decode()
    assert html.count("<script") == 1
    assert "style=" not in html


def test_nav_has_equal_tabs_with_aria_current_and_no_json_link(authed):
    def nav(path):
        html = authed.get(path).data.decode()
        return html.split('<nav class="top"', 1)[1].split("</nav>", 1)[0]
    hub = nav("/")
    assert 'href="/api/v1/status"' not in hub and ">JSON<" not in hub
    assert re.search(r'class="brand" href="/" aria-current="page"', hub)
    assert 'class="tab" href="/dashboard">Dashboard<' in hub
    assert 'class="tab" href="/inbox">Inbox<' in hub
    assert 'class="signout" href="/logout">Sign out<' in hub
    assert 'class="tab" href="/dashboard" aria-current="page"' in nav("/dashboard")
    assert 'class="tab" href="/dashboard" aria-current="page"' in nav("/jobs/snap")
    assert 'class="tab" href="/inbox" aria-current="page"' in nav("/inbox")


def test_the_hub_is_behind_the_gate(read):
    r = read.get("/")
    assert r.status_code == 302 and r.headers["Location"].startswith("/login")


def test_inbox_capture_panel_is_the_record_anchor(authed):
    assert 'id="capture"' in authed.get("/inbox").data.decode()


def test_hub_css_rules_that_keep_it_usable_on_a_phone():
    """Pinned because a visual regression here passes every server test: the
    Record button's height, equal-width cards at 640px, equal tabs."""
    import os
    css = open(os.path.join(os.path.dirname(__file__), "..", "dashboard",
                            "static", "app.css")).read()
    record = css.split("a.hub-record {", 1)[1].split("}", 1)[0]
    assert "min-height: 56px" in record and "width: 100%" in record
    assert "@media (min-width: 640px) { .hub { grid-template-columns: 1fr 1fr;" in css
    tabs = css.split("nav.top .tabs {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns: 1fr 1fr" in tabs
    nav_a = css.split("nav.top a {", 1)[1].split("}", 1)[0]
    assert "min-height: 44px" in nav_a

