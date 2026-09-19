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

Three properties of :func:`default_fetch` are load-bearing and easy to lose in
a refactor, which is why each is pinned by a test that drives a real HTTP
server on loopback (``tests/test_inbox_mirror.py``):

* **The response is read with a hard byte cap.** Anyone can open an issue on a
  public repo, bodies run to 65 kB each, and up to 1000 of them accumulate in
  memory before a single row is written — in a container with no ``mem_limit``.
  Each issue is also projected down to the four fields this module reads before
  it is kept, so a 20-field GitHub payload does not sit in the list either.
* **Redirects are NOT followed off ``api.github.com``.** ``urlopen``'s default
  opener follows up to ten redirects to any host and scheme, and
  ``HTTPRedirectHandler`` does not strip ``Authorization`` cross-origin — so a
  301 (which GitHub serves legitimately, for a renamed repo) could hand
  ``INBOX_GITHUB_TOKEN`` to ``http://127.0.0.1:8081/`` or to a sibling
  container on the shared docker network. :data:`_OPENER` refuses any redirect
  whose target is not https + exactly ``api.github.com``.
* **The token never reaches a log or the database.** ``http.client.putheader``
  raises ``ValueError('Invalid header value %r' % value)`` with the whole
  ``Bearer <token>`` inside it, and that string would become
  ``MirrorResponse.error`` → ``log.warning`` → ``inbox_mirror_state.last_error``.
  Two independent defences: ``config`` validates the token at startup so an
  illegal header value cannot be built, and :func:`_redact` scrubs the token
  out of every error string on the way back regardless.
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
from .config import GITHUB_REPO_RE, GITHUB_TOKEN_RE

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
#: Hard cap on ONE response body. A page of 100 issues with 65 kB bodies is
#: ~6.5 MB, so this is generous for anything real and still bounded — which an
#: unlimited ``resp.read()`` in a container with no ``mem_limit`` was not.
MAX_BODY_BYTES = 8 * 1024 * 1024
#: The only host this module may end up talking to, after any redirect.
API_HOST = "api.github.com"
#: The fields ``_sync_repo`` actually reads. Everything else GitHub sends
#: (reactions, labels, the whole `user` object, two dozen URLs) is dropped
#: before the issue joins the in-memory list.
ISSUE_FIELDS = ("number", "title", "body", "pull_request")


class _GitHubOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect ONLY when it stays on ``https://api.github.com``.

    Returning ``None`` makes urllib raise the original ``HTTPError`` instead of
    following, which lands on the ordinary error path here: the sync is PARTIAL
    and therefore closes nothing. That is the right outcome — a repo whose
    redirect we refuse is a repo we did not read.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or parts.netloc.lower() != API_HOST:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


#: Built once. `urlopen` uses a module-global default opener whose redirect
#: handler follows anywhere; this one does not, and nothing else in the process
#: is affected because it is used explicitly rather than installed.
_OPENER = urllib.request.build_opener(_GitHubOnlyRedirects)


def _redact(text: str, headers: dict | None) -> str:
    """Strip the bearer token out of an error string.

    ``http.client`` puts the offending header value verbatim into its
    ``ValueError``, and that string is logged AND stored in
    ``inbox_mirror_state.last_error``. Startup validation should make that
    unreachable; this makes it harmless if it ever is not.
    """
    auth = str((headers or {}).get("Authorization") or "")
    token = auth.partition(" ")[2].strip()
    for secret in (auth, token):
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _project_issue(raw: dict) -> dict:
    """Keep the four fields this module reads and drop the rest."""
    return {k: raw[k] for k in ISSUE_FIELDS if k in raw}


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
    """The real HTTP call. Injected in tests so the suite stays network-free.

    Three things here are deliberate and tested against a live loopback server
    rather than a fake, because a fake is exactly what let them go unnoticed:
    the byte cap on ``read``, the redirect-refusing opener, and the timeout.
    """
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _OPENER.open(request, timeout=HTTP_TIMEOUT_S) as resp:
            # One byte over the cap is enough to know it is over the cap, and
            # the rest is never pulled into memory.
            raw = resp.read(MAX_BODY_BYTES + 1)
            resp_headers = dict(resp.headers.items())
            if len(raw) > MAX_BODY_BYTES:
                # status 0, not 200: the caller's PARTIAL logic then refuses to
                # upsert or close anything off a body we did not fully read.
                return MirrorResponse(
                    status=0, headers=resp_headers,
                    error=f"response body over {MAX_BODY_BYTES} bytes")
            return MirrorResponse(status=resp.status, headers=resp_headers,
                                  body=json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        # 304 and 403 arrive here; both carry headers we need. A refused
        # redirect arrives here too (redirect_request returning None re-raises
        # the 3xx), which is why this path must not be a success.
        body = None
        try:
            raw = exc.read(MAX_BODY_BYTES + 1)
            if len(raw) <= MAX_BODY_BYTES:
                body = json.loads(raw) if raw else None
        except Exception:                                 # noqa: BLE001
            body = None
        return MirrorResponse(status=exc.code, headers=dict(exc.headers.items()),
                              body=body, error=f"HTTP {exc.code}")
    except Exception as exc:                              # noqa: BLE001
        return MirrorResponse(
            status=0,
            error=_redact(f"{type(exc).__name__}: {exc}", headers))


def _headers(token: str, etag: str | None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        # `config` already refuses a token like this at startup. Checking again
        # here is what keeps `http.client.putheader` — which embeds the whole
        # header VALUE in its ValueError — from ever being handed one. The
        # message below deliberately contains no part of the token.
        if not GITHUB_TOKEN_RE.match(token):
            raise ValueError("INBOX_GITHUB_TOKEN contains characters that "
                             "cannot appear in an HTTP header value")
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
            reason = _redact(resp.error or f"HTTP {resp.status}",
                             {"Authorization": f"Bearer {token}"} if token else None)[:200]
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
            # Projected HERE, not later: `issues` holds up to MAX_PAGES *
            # PER_PAGE entries before anything is written, and keeping the
            # whole GitHub payload for each of them is hundreds of MB resident
            # for no reason. Four fields is everything the loop below reads.
            issues.append(_project_issue(raw))
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
            # Redacted BEFORE anything is logged: an exception raised while
            # building the request can carry the header value with it, and
            # `log.exception` would have written the raw message (the tail of
            # the traceback) out before this line ever ran — the exact leak
            # `_redact` exists to prevent. The traceback is given up on purpose;
            # the redacted `type: message` is what identifies the fault anyway.
            reason = _redact(f"{type(exc).__name__}: {exc}",
                             {"Authorization": f"Bearer {token}"} if token else None)
            log.error("inbox github mirror: %s raised: %s", repo, reason[:200])
            results.append({"repo": repo, "status": "error",
                            "error": reason[:200]})
    ok = sum(1 for r in results if r["status"] in ("ok", "unchanged"))
    return {
        "repos": len(results),
        "ok": ok,
        "failed": len(results) - ok,
        "issues": sum(int(r.get("issues") or 0) for r in results),
        "results": results,
    }
