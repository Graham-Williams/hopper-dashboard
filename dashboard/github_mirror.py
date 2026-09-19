"""Mirror open GitHub issues into the Inbox, unauthenticated by default.

Graham's ten public repos are readable without a token, which is why the
default needs no credential at all: the repo list lives in ``.env``
(``INBOX_GITHUB_REPOS``, validated at startup by ``config``), and an optional
``INBOX_GITHUB_TOKEN`` only lifts the 60 requests/hour anonymous limit.

Three rules decide whether this module is correct, and all three are about not
losing information:

1. **Close-detection runs ONLY after a complete, error-free scan of a repo.**
   The API is asked for ``state=open``, so "this key is not in the answer" means
   closed — but only if the answer is the whole answer. A timeout on page 3, a
   403 from the rate limiter, or a listing truncated at ``MAX_PAGES`` all mean
   the answer is partial, and a partial answer must close nothing.
2. **An issue Hopper already filed is never mirrored again.** The
   ``UNIQUE(repo, number)`` index on ``inbox_issues`` is the store-level half;
   this module's :func:`_sync_repo` is the other, checking for a linked issue
   before creating a mirrored row. Without it, filing an issue for a voice note
   would put the same piece of work on the board twice, one row nobody reviewed.
3. **A voice item closes only when ALL of its linked issues are closed.** One
   spoken note can spawn issues in two repos, and one of them being done is not
   the note being done.

Everything that comes back is untrusted text written by whoever opened the
issue. It is stored verbatim and escaped at render; nothing here interpolates
it anywhere. The only value interpolated into a URL is the repo name, which
``config.GITHUB_REPO_RE`` has already forced to start with an alphanumeric in
both halves — so a repo of ``..`` cannot walk out of ``/repos/``.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import inbox_db
from .config import GITHUB_REPO_RE

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
PER_PAGE = 100
#: 1000 open issues in one repo is far past anything real here; stopping is
#: better than paging for ever, and a truncated listing is treated as PARTIAL
#: so it can never close anything.
MAX_PAGES = 10
USER_AGENT = "hopper-dashboard-inbox"
HTTP_TIMEOUT_S = 20
#: How long to stay away after the API says no. Overridden by an explicit
#: ``Retry-After`` or ``X-RateLimit-Reset``.
DEFAULT_BACKOFF_S = 900


@dataclass
class MirrorResponse:
    status: int
    headers: dict = field(default_factory=dict)
    body: object = None
    error: str | None = None

    def header(self, name: str, default=None):
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return default


def default_fetch(url: str, headers: dict) -> MirrorResponse:
    """The real HTTP call. Injected in tests so the suite stays network-free."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as resp:
            raw = resp.read()
            return MirrorResponse(status=resp.status,
                                  headers=dict(resp.headers.items()),
                                  body=json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        # 304 and 403 arrive here; both carry headers we need.
        body = None
        try:
            raw = exc.read()
            body = json.loads(raw) if raw else None
        except Exception:                                 # noqa: BLE001
            body = None
        return MirrorResponse(status=exc.code, headers=dict(exc.headers.items()),
                              body=body, error=f"HTTP {exc.code}")
    except Exception as exc:                              # noqa: BLE001
        return MirrorResponse(status=0, error=f"{type(exc).__name__}: {exc}")


def _headers(token: str, etag: str | None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if etag:
        headers["If-None-Match"] = etag
    return headers


def _issues_url(repo: str, page: int) -> str:
    """The one place a repo name becomes a URL. Quoted anyway — belt and braces
    over the startup-time regex."""
    query = urllib.parse.urlencode({"state": "open", "per_page": PER_PAGE,
                                    "page": page, "sort": "created",
                                    "direction": "desc"})
    return f"{API_ROOT}/repos/{urllib.parse.quote(repo)}/issues?{query}"


def _backoff_from(resp: MirrorResponse, now: float) -> float | None:
    """Seconds-since-epoch to stay away until, from whatever the API said.

    ``Retry-After`` wins (it is an instruction), then an exhausted
    ``X-RateLimit-Remaining`` with its ``X-RateLimit-Reset``. Being told to wait
    and then asking again is how an anonymous client gets a longer ban.
    """
    retry_after = resp.header("Retry-After")
    if retry_after:
        try:
            return now + max(0.0, float(str(retry_after).strip()))
        except ValueError:
            pass
    remaining = resp.header("X-RateLimit-Remaining")
    try:
        if remaining is not None and int(str(remaining).strip()) <= 0:
            reset = resp.header("X-RateLimit-Reset")
            if reset is not None:
                return max(now, float(str(reset).strip()))
            return now + DEFAULT_BACKOFF_S
    except ValueError:
        pass
    if resp.status in (403, 429):
        return now + DEFAULT_BACKOFF_S
    return None


def _project_for(repo: str) -> str | None:
    name = repo.split("/", 1)[-1]
    return name if inbox_db.PROJECT_RE.match(name) and len(name) <= inbox_db.MAX_PROJECT else None


def _issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{int(number)}"


def _sync_repo(conn, repo: str, *, now: float, fetch, token: str) -> dict:
    """One repo. Returns a result dict; never raises."""
    key = f"{inbox_db.MIRROR_GITHUB}:{repo}"
    now_text = inbox_db.to_iso(now)
    state = inbox_db.get_mirror_state(conn, key)

    blocked_until = inbox_db.from_iso_or_none(state.get("backoff_until"))
    if blocked_until is not None and blocked_until > now:
        return {"repo": repo, "status": "backoff", "issues": 0,
                "until": state.get("backoff_until")}

    issues: list[dict] = []
    etag = state.get("etag")
    new_etag = None
    for page in range(1, MAX_PAGES + 1):
        resp = fetch(_issues_url(repo, page),
                     _headers(token, etag if page == 1 else None))
        if resp.status == 304:
            with conn:
                inbox_db.set_mirror_state(conn, key, last_sync_at=now_text,
                                          last_status="unchanged", last_error=None,
                                          backoff_until=None)
            # Nothing changed upstream, so the open set we already recorded is
            # still the open set. No close pass is needed and none is run.
            return {"repo": repo, "status": "unchanged", "issues": 0}
        if resp.status != 200 or not isinstance(resp.body, list):
            backoff = _backoff_from(resp, now)
            reason = (resp.error or f"HTTP {resp.status}")[:200]
            with conn:
                inbox_db.set_mirror_state(
                    conn, key, last_sync_at=now_text, last_status="error",
                    last_error=reason,
                    backoff_until=inbox_db.to_iso(backoff) if backoff else None,
                    rate_remaining=_int_or_none(resp.header("X-RateLimit-Remaining")))
            log.warning("inbox github mirror: %s page %d failed: %s",
                        repo, page, reason)
            # PARTIAL: whatever we already read is not the whole answer, so
            # nothing is upserted and above all nothing is closed.
            return {"repo": repo, "status": "error", "issues": 0, "error": reason}
        if page == 1:
            new_etag = resp.header("ETag")
        for raw in resp.body:
            if not isinstance(raw, dict):
                continue
            # A pull request is served by the issues endpoint and is NOT a piece
            # of work waiting to be triaged. Skipping it is the whole reason
            # this loop reads the raw dict rather than a count.
            if "pull_request" in raw:
                continue
            issues.append(raw)
        if len(resp.body) < PER_PAGE:
            break
    else:
        # Ran out of pages without the listing ending: treat as PARTIAL.
        with conn:
            inbox_db.set_mirror_state(
                conn, key, last_sync_at=now_text, last_status="truncated",
                last_error=f"more than {MAX_PAGES * PER_PAGE} open issues")
        return {"repo": repo, "status": "truncated", "issues": len(issues)}

    # -- complete, error-free: safe to upsert AND to close ------------------ #
    project = _project_for(repo)
    seen_keys: list[str] = []
    open_numbers: list[int] = []
    mirrored = linked = 0
    with conn:
        for raw in issues:
            number = raw.get("number")
            if not isinstance(number, int) or number <= 0:
                continue
            open_numbers.append(number)
            title = inbox_db.clean_text(raw.get("title"), inbox_db.MAX_TITLE)
            url = _issue_url(repo, number)
            if inbox_db.linked_issue(conn, repo, number) is not None:
                # Hopper filed this one FOR an item that is already on the
                # board. Refresh its label; do not mirror it as a second row.
                inbox_db.refresh_issue(conn, repo, number, title=title or None,
                                       url=url, state="open", now=now_text)
                linked += 1
                continue
            item_key = inbox_db.github_key(repo, number)
            inbox_db.upsert_mirror_item(
                conn, mirror_key=item_key, source=inbox_db.MIRROR_GITHUB,
                title=title or f"{repo}#{number}",
                body=inbox_db.clean_text(raw.get("body"), inbox_db.MAX_TEXT),
                project=project, url=url, now=now_text)
            inbox_db.reopen_mirror_item(conn, item_key, now=now_text)
            seen_keys.append(item_key)
            mirrored += 1
        closed_rows = inbox_db.close_missing_mirror_items(
            conn, prefix=f"{inbox_db.MIRROR_GITHUB}:{repo}#",
            seen_keys=seen_keys, now=now_text)
        closed_issues = inbox_db.mark_issues_closed(conn, repo, open_numbers,
                                                    now=now_text)
        closed_items = inbox_db.close_items_whose_issues_all_closed(
            conn, repo, now=now_text)
        inbox_db.set_mirror_state(
            conn, key, etag=new_etag or etag, last_sync_at=now_text,
            last_status="ok", last_error=None, backoff_until=None,
            rate_remaining=None)
    return {"repo": repo, "status": "ok", "issues": len(issues),
            "mirrored": mirrored, "already_linked": linked,
            "closed_mirrored": closed_rows, "closed_issues": closed_issues,
            "closed_items": closed_items}


def _int_or_none(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def sync(conn, repos, *, now: float | None = None, fetch=None,
         token: str = "") -> dict:
    """Sync every repo. One repo's failure never stops the others."""
    now = time.time() if now is None else now
    fetch = fetch or default_fetch
    results = []
    for repo in repos:
        if not GITHUB_REPO_RE.match(str(repo or "")):
            results.append({"repo": str(repo), "status": "invalid"})
            continue
        try:
            results.append(_sync_repo(conn, repo, now=now, fetch=fetch,
                                      token=token))
        except Exception as exc:                          # noqa: BLE001
            log.exception("inbox github mirror: %s raised", repo)
            results.append({"repo": repo, "status": "error",
                            "error": f"{type(exc).__name__}: {exc}"[:200]})
    ok = sum(1 for r in results if r["status"] in ("ok", "unchanged"))
    return {
        "repos": len(results),
        "ok": ok,
        "failed": len(results) - ok,
        "issues": sum(int(r.get("issues") or 0) for r in results),
        "results": results,
    }
