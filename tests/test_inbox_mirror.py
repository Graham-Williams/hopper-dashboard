"""The GitHub issue mirror. `fetch` is injected, so the suite stays
network-free — and the three rules that make it correct are asserted directly:
a partial answer closes nothing, an issue Hopper filed is never mirrored twice,
and an item closes only when every one of its issues is closed."""

from __future__ import annotations

import pytest

from dashboard import github_mirror, inbox_db

NOW = 1_800_000_000.0
REPO = "Graham-Williams/km-tracker"


@pytest.fixture
def conn(tmp_path):
    c = inbox_db.connect(str(tmp_path / "inbox.db"))
    inbox_db.init_inbox_schema(c)
    yield c
    c.close()


def issue(number, title="An issue", body="", **extra):
    out = {"number": number, "title": title, "body": body,
           "html_url": f"https://github.com/{REPO}/issues/{number}"}
    out.update(extra)
    return out


class FakeGitHub:
    """Records every call and answers from a scripted list of responses."""

    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        if not self.pages:
            return github_mirror.MirrorResponse(status=200, body=[])
        return self.pages.pop(0)


def ok(items, etag='W/"etag-1"', **headers):
    return github_mirror.MirrorResponse(
        status=200, headers={"ETag": etag, **headers}, body=items)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #

def test_a_clean_scan_mirrors_open_issues_and_skips_pull_requests(conn):
    fetch = FakeGitHub([ok([
        issue(1, "Wheel spins twice", "on iOS only"),
        issue(2, "A pull request", pull_request={"url": "…"}),
        issue(3, "Backups are quiet"),
    ])])
    out = github_mirror.sync(conn, [REPO], now=NOW, fetch=fetch)
    assert out["ok"] == 1 and out["failed"] == 0
    rows = inbox_db.list_items(conn)
    titles = {r["title"] for r in rows}
    assert titles == {"Wheel spins twice", "Backups are quiet"}
    assert "A pull request" not in titles          # a PR is not triage work
    assert all(r["source"] == "github" for r in rows)
    assert all(r["project"] == "km-tracker" for r in rows)
    assert all(r["mirror_url"].startswith(f"https://github.com/{REPO}/issues/")
               for r in rows)
    # The repo name is the only thing interpolated into the URL, and it is
    # already forced to start with an alphanumeric in both halves.
    assert fetch.calls[0][0].startswith(
        f"https://api.github.com/repos/{REPO}/issues?")
    assert "state=open" in fetch.calls[0][0]


def test_a_resync_is_idempotent(conn):
    fetch = FakeGitHub([ok([issue(1, "Wheel spins twice")]),
                        ok([issue(1, "Wheel spins twice (retitled)")])])
    github_mirror.sync(conn, [REPO], now=NOW, fetch=fetch)
    github_mirror.sync(conn, [REPO], now=NOW + 900, fetch=fetch)
    rows = inbox_db.list_items(conn)
    assert len(rows) == 1
    assert rows[0]["title"] == "Wheel spins twice (retitled)"


def test_a_304_is_a_no_op_and_closes_nothing(conn):
    fetch = FakeGitHub([ok([issue(1, "Wheel spins twice")]),
                        github_mirror.MirrorResponse(status=304)])
    github_mirror.sync(conn, [REPO], now=NOW, fetch=fetch)
    out = github_mirror.sync(conn, [REPO], now=NOW + 900, fetch=fetch)
    assert out["results"][0]["status"] == "unchanged"
    rows = inbox_db.list_items(conn)
    assert len(rows) == 1 and rows[0]["state"] == "open"
    # The second call sent the ETag the first one returned.
    assert fetch.calls[1][1]["If-None-Match"] == 'W/"etag-1"'


def test_pagination_follows_full_pages(conn):
    first = [issue(n, f"issue {n}") for n in range(1, github_mirror.PER_PAGE + 1)]
    fetch = FakeGitHub([ok(first), ok([issue(999, "last one")])])
    out = github_mirror.sync(conn, [REPO], now=NOW, fetch=fetch)
    assert out["issues"] == github_mirror.PER_PAGE + 1
    assert len(fetch.calls) == 2
    assert "page=2" in fetch.calls[1][0]
    # The ETag is only conditional on page 1 — a page-2 request with page 1's
    # ETag would 304 the wrong resource.
    assert "If-None-Match" not in fetch.calls[1][1]


# --------------------------------------------------------------------------- #
# Close detection — only on a COMPLETE, error-free scan
# --------------------------------------------------------------------------- #

def test_an_issue_that_leaves_the_open_list_is_closed_not_deleted(conn):
    fetch = FakeGitHub([ok([issue(1, "Wheel"), issue(2, "Backups")]),
                        ok([issue(1, "Wheel")], etag='W/"etag-2"')])
    github_mirror.sync(conn, [REPO], now=NOW, fetch=fetch)
    github_mirror.sync(conn, [REPO], now=NOW + 900, fetch=fetch)
    by_title = {r["title"]: r for r in inbox_db.list_items(conn)}
    assert by_title["Backups"]["state"] == "closed"        # still on the board
    assert by_title["Backups"]["archived_at"] is None      # never destroyed
    assert by_title["Wheel"]["state"] == "open"


def test_an_issue_that_reopens_upstream_reopens_here(conn):
    fetch = FakeGitHub([ok([issue(1, "Wheel")]),
                        ok([], etag='W/"etag-2"'),
                        ok([issue(1, "Wheel")], etag='W/"etag-3"')])
    github_mirror.sync(conn, [REPO], now=NOW, fetch=fetch)
    github_mirror.sync(conn, [REPO], now=NOW + 900, fetch=fetch)
    assert inbox_db.list_items(conn)[0]["state"] == "closed"
    github_mirror.sync(conn, [REPO], now=NOW + 1800, fetch=fetch)
    assert inbox_db.list_items(conn)[0]["state"] == "open"


@pytest.mark.parametrize("bad", [
    github_mirror.MirrorResponse(status=500, error="HTTP 500"),
    github_mirror.MirrorResponse(status=0, error="URLError: timed out"),
    github_mirror.MirrorResponse(status=403, headers={"X-RateLimit-Remaining": "0",
                                                      "X-RateLimit-Reset": "1800003600"}),
    github_mirror.MirrorResponse(status=200, body={"message": "not a list"}),
])
def test_a_failed_scan_never_closes_anything(conn, bad):
    """THE rule. The API is asked for `state=open`, so "not in the answer" only
    means closed when the answer is the whole answer — and a timeout, a rate
    limit or a garbage body all mean it is not."""
    fetch = FakeGitHub([ok([issue(1, "Wheel"), issue(2, "Backups")]), bad])
    github_mirror.sync(conn, [REPO], now=NOW, fetch=fetch)
    out = github_mirror.sync(conn, [REPO], now=NOW + 900, fetch=fetch)
    assert out["results"][0]["status"] == "error"
    assert out["failed"] == 1
    assert {r["state"] for r in inbox_db.list_items(conn)} == {"open"}


def test_a_failure_on_a_later_page_closes_nothing_either(conn):
    """A partial listing is the sharpest version of the same trap: page 1 came
    back fine, so the naive reading is "these are the open ones"."""
    full = [issue(n, f"issue {n}") for n in range(1, github_mirror.PER_PAGE + 1)]
    fetch = FakeGitHub([ok(full), ok([issue(999, "last one")]),
                        ok(full[:2], etag='W/"etag-2"'),
                        github_mirror.MirrorResponse(status=500, error="HTTP 500")])
    github_mirror.sync(conn, [REPO], now=NOW, fetch=fetch)
    assert len(inbox_db.list_items(conn, limit=500)) == github_mirror.PER_PAGE + 1
    # Page 1 is short this time, so it does not page again — reset the script.
    fetch.pages = [ok(full), github_mirror.MirrorResponse(status=500, error="boom")]
    out = github_mirror.sync(conn, [REPO], now=NOW + 900, fetch=fetch)
    assert out["results"][0]["status"] == "error"
    assert {r["state"] for r in inbox_db.list_items(conn, limit=500)} == {"open"}


def test_a_truncated_listing_closes_nothing(conn):
    full = [issue(n, f"issue {n}") for n in range(1, github_mirror.PER_PAGE + 1)]
    fetch = FakeGitHub([ok(full) for _ in range(github_mirror.MAX_PAGES)])
    out = github_mirror.sync(conn, [REPO], now=NOW, fetch=fetch)
    assert out["results"][0]["status"] == "truncated"
    assert inbox_db.list_items(conn) == []        # nothing upserted either


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #

def test_a_rate_limit_sets_a_backoff_and_the_next_run_stays_away(conn):
    limited = github_mirror.MirrorResponse(
        status=403, headers={"X-RateLimit-Remaining": "0",
                             "X-RateLimit-Reset": str(int(NOW + 600))})
    fetch = FakeGitHub([limited])
    github_mirror.sync(conn, [REPO], now=NOW, fetch=fetch)
    state = inbox_db.get_mirror_state(conn, f"github:{REPO}")
    assert state["backoff_until"] == inbox_db.to_iso(NOW + 600)
    before = len(fetch.calls)
    out = github_mirror.sync(conn, [REPO], now=NOW + 60, fetch=fetch)
    assert out["results"][0]["status"] == "backoff"
    assert len(fetch.calls) == before            # no request was made at all
    # Once it expires, it tries again.
    fetch.pages = [ok([issue(1, "Wheel")], etag='W/"etag-9"')]
    out = github_mirror.sync(conn, [REPO], now=NOW + 700, fetch=fetch)
    assert out["results"][0]["status"] == "ok"
    assert inbox_db.get_mirror_state(conn, f"github:{REPO}")["backoff_until"] is None


def test_retry_after_wins_over_the_rate_limit_headers(conn):
    resp = github_mirror.MirrorResponse(
        status=429, headers={"Retry-After": "120", "X-RateLimit-Remaining": "0",
                             "X-RateLimit-Reset": str(int(NOW + 9999))})
    github_mirror.sync(conn, [REPO], now=NOW, fetch=FakeGitHub([resp]))
    state = inbox_db.get_mirror_state(conn, f"github:{REPO}")
    assert state["backoff_until"] == inbox_db.to_iso(NOW + 120)


def test_one_repos_failure_does_not_stop_the_others(conn):
    other = "Graham-Williams/taste-twin"
    fetch = FakeGitHub([github_mirror.MirrorResponse(status=500, error="boom"),
                        ok([issue(4, "Second repo issue")])])
    out = github_mirror.sync(conn, [REPO, other], now=NOW, fetch=fetch)
    assert out["ok"] == 1 and out["failed"] == 1
    assert [r["title"] for r in inbox_db.list_items(conn)] == ["Second repo issue"]


def test_an_invalid_repo_is_refused_without_a_request(conn):
    fetch = FakeGitHub([ok([issue(1)])])
    out = github_mirror.sync(conn, ["../x", "not-a-repo"], now=NOW, fetch=fetch)
    assert [r["status"] for r in out["results"]] == ["invalid", "invalid"]
    assert fetch.calls == []


# --------------------------------------------------------------------------- #
# The no-duplicates rule
# --------------------------------------------------------------------------- #

def test_a_scan_never_clones_an_item_hopper_already_filed(conn):
    """Hopper files issue #95 for a spoken note. The next repo scan sees #95
    open — and must NOT put the same piece of work on the board a second time,
    as a row nobody reviewed."""
    spoken = inbox_db.create_item(conn, source="voice",
                                  text="the wheel spins twice", now="2026-09-01T00:00:00Z")
    with conn:
        inbox_db.link_issue(conn, spoken, repo=REPO, number=95,
                            url=f"https://github.com/{REPO}/issues/95",
                            title="Wheel spins twice")
    fetch = FakeGitHub([ok([issue(95, "Wheel spins twice (edited on GitHub)"),
                            issue(96, "Something else")])])
    out = github_mirror.sync(conn, [REPO], now=NOW, fetch=fetch)
    assert out["results"][0]["already_linked"] == 1
    rows = inbox_db.list_items(conn)
    assert len(rows) == 2
    assert {r["source"] for r in rows} == {"voice", "github"}
    assert [r["id"] for r in rows if r["source"] == "voice"] == [spoken]
    # The linked issue's own label is refreshed from the scan.
    issues = inbox_db.issues_for(conn, [spoken])[spoken]
    assert issues[0]["title"] == "Wheel spins twice (edited on GitHub)"
    assert issues[0]["state"] == "open"


def test_a_voice_item_closes_only_when_every_linked_issue_is_closed(conn):
    other = "Graham-Williams/taste-twin"
    spoken = inbox_db.create_item(conn, source="voice", text="two repos worth",
                                  now="2026-09-01T00:00:00Z")
    with conn:
        inbox_db.link_issue(conn, spoken, repo=REPO, number=1, url="u1")
        inbox_db.link_issue(conn, spoken, repo=other, number=2, url="u2")
    # km's #1 closes; taste-twin's #2 is still open.
    fetch = FakeGitHub([ok([]), ok([issue(2, "still open")])])
    github_mirror.sync(conn, [REPO, other], now=NOW, fetch=fetch)
    assert inbox_db.get_item(conn, spoken)["state"] == "open"
    # Now #2 closes too.
    fetch = FakeGitHub([ok([], etag='W/"e2"'), ok([], etag='W/"e3"')])
    github_mirror.sync(conn, [REPO, other], now=NOW + 900, fetch=fetch)
    assert inbox_db.get_item(conn, spoken)["state"] == "closed"


# --------------------------------------------------------------------------- #
# Untrusted text
# --------------------------------------------------------------------------- #

def test_a_hostile_issue_title_survives_to_render_escaped(conn, authed, settings):
    """An issue title is written by whoever opened it. It is stored verbatim and
    escaped at render — never interpolated, never turned into markup."""
    nasty = '</script><img src=x onerror="alert(1)">{{ 7*7 }}'
    live = inbox_db.connect(settings.inbox_db_path)
    try:
        github_mirror.sync(live, [REPO], now=NOW,
                           fetch=FakeGitHub([ok([issue(1, nasty, nasty)])]))
    finally:
        live.close()
    html = authed.get("/inbox").data.decode()
    assert "<img" not in html and 'onerror="alert(1)"' not in html
    assert "&lt;/script&gt;" in html and "&lt;img" in html
    assert html.count("</script>") == html.count("<script")
    assert "{{ 7*7 }}" in html and "49" not in html.split('<ul class="items"', 1)[1]
