"""Read role: HTML board + JSON API behind the shared-password gate.

Gate semantics (ported from jjho / km / taste-twin / todoist-points):
``APP_PASSWORD`` set → every route except ``/login``, ``/logout``, ``/healthz``
and ``/static/*`` requires the signed session cookie. Additionally
``/api/v1/*`` accepts ``Authorization: Bearer <READ_TOKEN>`` so Hopper can read
the board without a browser session. ``APP_HOST`` (optional) pins Host and,
for POSTs, Origin/Referer as a CSRF defence.

This role also enforces HTTPS at the origin (``X-Forwarded-Proto: http`` → 307
to ``https://<APP_HOST>…``, plus HSTS). The INGEST role does not, and must not:
see ``_https_redirect`` below.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from urllib.parse import quote, urlsplit

from flask import (Blueprint, Response, abort, current_app, g, jsonify,
                   redirect, render_template, request, session, url_for)

from . import db
from .password_gate import client_ip as _client_ip
from .password_gate import safe_next
from .views import build_job_detail, build_status

log = logging.getLogger(__name__)

bp = Blueprint("web", __name__)

SESSION_KEY = "dashboard_authed"
GATE_EXEMPT = {"/login", "/logout", "/healthz"}

# script-src allows exactly ONE inline script — the per-request nonce'd
# timestamp localizer in base.html. No 'self', no 'unsafe-inline', no hosts:
# nothing else can execute. Everything else stays locked.
_CSP_TEMPLATE = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                 "script-src 'nonce-{nonce}'; object-src 'none'; base-uri 'self'; "
                 "form-action 'self'; frame-ancestors 'none'")
GLOBAL_LOGIN_KEY = "*"
#: Methods the Origin/Referer CSRF pin applies to.
MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# One year, and deliberately NO includeSubDomains / preload: every hostname on
# graham-williams.com owns its own policy, and a preload entry is effectively
# irreversible. Matches the apex landing page's snippets/security-headers.conf,
# which is the reference implementation for all six apps.
HSTS_VALUE = "max-age=31536000"
# Printable ASCII, no spaces and no control bytes: what may be pasted into a
# Location header. Makes CR/LF header injection structurally impossible rather
# than merely unlikely (Werkzeug would also refuse, but not from here).
#
# ⚠️ THIS PATTERN IS SAFE ONLY UNDER `.fullmatch()`. EVERY call site MUST use
# `fullmatch` — never `.match()`, never `.search()`. The pattern is unanchored,
# so `_SAFE_TARGET_RE.match('/x\n')` SUCCEEDS (it matches the safe prefix and
# stops), and a single `fullmatch`->`match` slip would therefore let a newline
# through into a response header: header injection. Anchoring with `^...$`
# would NOT save it either — in Python "$" also matches immediately before a
# TRAILING newline. `tests/test_web.py` pins both facts.
_SAFE_TARGET_RE = re.compile(r"[\x21-\x7e]*")
_MAX_TARGET_LEN = 2000


def _settings():
    return current_app.config["SETTINGS"]


def client_ip() -> str:
    return _client_ip(_settings().trusted_proxy_cidrs)


def csp_nonce() -> str:
    nonce = getattr(g, "csp_nonce", None)
    if nonce is None:
        nonce = g.csp_nonce = secrets.token_urlsafe(16)
    return nonce


def _gate_enabled() -> bool:
    return bool(_settings().app_password)


def _presented_bearer() -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _token_ok(expected: str) -> bool:
    """Constant-time bearer compare. An empty expected token fails CLOSED —
    the same rule INGEST_TOKEN follows, so an un-provisioned INBOX_TOKEN means
    "every machine call is 401", never "every machine call is allowed"."""
    supplied = _presented_bearer()
    if not expected or not supplied:
        # Burn a comparison anyway so timing does not distinguish the cases.
        hmac.compare_digest(supplied or "x", expected or "y")
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())


def _bearer_ok() -> bool:
    return _token_ok(_settings().read_token)


def _machine_endpoints() -> frozenset:
    """Endpoints the Inbox's MACHINE token may authenticate (registered by
    ``create_app``). Scoped to an endpoint set, not a path prefix: INBOX_TOKEN
    must never become a second read token for the whole API."""
    return current_app.extensions.get("inbox_machine_endpoints", frozenset())


def auth_kind() -> str:
    """Which credential this request actually presented.

    Three are distinguishable and the difference matters downstream: a session
    cookie is AMBIENT (so it needs the CSRF origin pin), while both bearer
    tokens have to be attached deliberately by a non-browser client and
    therefore cannot be replayed by a cross-site form post.
    """
    if session.get(SESSION_KEY) is True:
        return "session"
    if request.endpoint in _machine_endpoints() and _token_ok(_settings().inbox_token):
        return "inbox"
    if request.path.startswith("/api/v1/") and _bearer_ok():
        return "read"
    return "none"


def is_authed() -> bool:
    return auth_kind() != "none"


def require_session():
    """Guard for a MUTATING browser route: a READ_TOKEN bearer must never write.

    ``READ_TOKEN`` is Hopper's read credential, handed to a watch that polls
    ``/api/v1/status``; it is deliberately not a write credential, and the
    generic gate above would otherwise let it through on any ``/api/v1/`` path.
    Returns a 401 response to return from the view, or None to carry on.
    """
    if not _gate_enabled():
        return None            # local dev with no gate at all
    if session.get(SESSION_KEY) is True:
        return None
    return jsonify({"error": "this endpoint needs a browser session"}), 401


def _request_target() -> str:
    """The path+query to re-issue over https, preserving percent-encoding.

    ``request.path`` is already URL-decoded, so ``/a%2Fb`` and ``/a/b`` are
    indistinguishable there — rebuilding from it would silently rewrite the URL
    the visitor asked for. The raw request target is in ``RAW_URI`` (gunicorn,
    and Werkzeug's test client) or ``REQUEST_URI``; both are used verbatim when
    they are an origin-form target that is safe to emit. Otherwise fall back to
    a conservatively re-quoted path plus the byte-exact query string, and to
    ``/`` if even that would not be safe.

    Only origin-form (``/…``) is accepted: an absolute-form request target
    carries its own host, and trusting that would be host reflection.
    """
    for key in ("RAW_URI", "REQUEST_URI"):
        raw = request.environ.get(key) or ""
        if (raw.startswith("/") and len(raw) <= _MAX_TARGET_LEN
                and _SAFE_TARGET_RE.fullmatch(raw)):
            return raw
    target = quote(request.path, safe="/")
    qs = request.query_string.decode("latin-1")
    if qs and _SAFE_TARGET_RE.fullmatch(qs):
        target = f"{target}?{qs}"
    if len(target) > _MAX_TARGET_LEN or not _SAFE_TARGET_RE.fullmatch(target):
        return "/"
    return target


@bp.before_app_request
def _https_redirect():
    """Enforce HTTPS at the origin — defence in depth behind the edge.

    Cloudflare's zone-wide "Always Use HTTPS" already redirects http→https, but
    is one dashboard toggle away from regressing, so the origin enforces it too.
    Only the tunnel reaches this role, and cloudflared forwards the visitor's
    scheme in ``X-Forwarded-Proto``.

    Three rules, all load-bearing:

    - Redirect ONLY when the header is present and exactly ``http``. An ABSENT
      header is never redirected: the container HEALTHCHECK and Hopper's
      in-network ``/api/v1/status`` read (``curl -H 'Host: <APP_HOST>' http://…``)
      send none, and redirecting them would break monitoring, not protect it.
    - The target is built from the configured ``APP_HOST`` pin, never from the
      request's own Host/URL — host reflection here would be an open redirect.
      It is used through ``Settings.https_redirect_host``, which only yields it
      when it is a bare hostname: a value like ``host@evil.example`` would
      otherwise emit a Location the browser resolves to ``evil.example``, and
      one containing a CRLF would 500 every request instead of failing open.
    - No (or malformed) ``APP_HOST`` → no redirect (fail open). That keeps
      local dev, the documented local visual-QA path and the test suite
      working; in production ``APP_HOST`` is always set (it is also what the
      Host pin needs).

    **307, and never cacheable.** The ``Location`` is byte-identical to the URL
    that was requested, so a cacheable answer is self-referential: RFC 9111
    makes a 301 with no ``Cache-Control`` heuristically cacheable
    *indefinitely*, which would let one misdeployed ``APP_HOST`` stick in every
    visitor's browser with no way to recall it, and would let a shared cache
    hand an https visitor a redirect to itself. 307 also preserves the method,
    so a plain-http POST is re-sent over https rather than silently downgraded
    to a bodiless GET. HSTS is what provides the durable upgrade; the redirect
    does not need to be permanent. ``Cache-Control: no-store`` comes from
    ``_security_headers`` (every read-side response gets it) and
    ``Vary: X-Forwarded-Proto`` is added here, since this answer depends
    entirely on that header.

    This hook lives on the read blueprint, which is registered ONLY for the read
    role — so the Tailscale-only ingest listener on :8081 is structurally exempt
    and its plain-HTTP heartbeat POSTs are untouched. That is deliberate: those
    pings never traverse the tunnel and have no TLS to upgrade to; redirecting
    them would silently stop every heartbeat and make the board lie.

    Runs before the password gate so an http request is upgraded rather than
    first answered with a redirect to a plain-http ``/login``.
    """
    app_host = _settings().https_redirect_host
    if not app_host:
        return None
    if (request.headers.get("X-Forwarded-Proto") or "").strip().lower() != "http":
        return None
    resp = redirect(f"https://{app_host}{_request_target()}", code=307)
    resp.vary.add("X-Forwarded-Proto")
    return resp


@bp.before_app_request
def _password_gate():
    if not _gate_enabled():
        return None
    path = request.path
    if path in GATE_EXEMPT:
        return None
    static_prefix = (current_app.static_url_path or "/static").rstrip("/") + "/"
    if request.endpoint == "static" or path.startswith(static_prefix):
        return None
    if is_authed():
        return None
    if path.startswith("/api/v1/"):
        return jsonify({"error": "unauthorized"}), 401
    nxt = path
    if request.query_string:
        nxt = f"{path}?{request.query_string.decode('latin-1')}"
    return redirect(url_for("web.login", next=nxt))


@bp.before_app_request
def _host_origin_pin():
    app_host = _settings().app_host
    if not app_host or request.path == "/healthz":
        return None
    if request.host.split(":", 1)[0].lower() != app_host:
        abort(403)
    # EVERY mutating method, not just POST. This used to fire on POST alone,
    # which was fine while POST was the only way to change anything — the
    # Inbox's PATCH would have arrived with no CSRF pin at all, so the check is
    # widened BEFORE the route exists rather than after.
    if request.method in MUTATING_METHODS:
        # A bearer token is not an ambient credential: a cross-site form post
        # cannot set an Authorization header, so there is nothing for an origin
        # pin to defend on the Inbox's machine endpoints — and requiring one
        # would make them unreachable from the Mac worker's curl, which sends
        # neither Origin nor Referer.
        if auth_kind() == "inbox":
            return None
        origin = request.headers.get("Origin", "")
        referer = request.headers.get("Referer", "")
        if origin:
            if (urlsplit(origin).hostname or "").lower() != app_host:
                abort(403)
        elif referer:
            if (urlsplit(referer).hostname or "").lower() != app_host:
                abort(403)
        else:
            abort(403)
    return None


@bp.after_app_request
def _security_headers(resp: Response) -> Response:
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    # same-origin, NOT no-referrer: under no-referrer browsers send Origin: null
    # on the app's own form POST and the CSRF pin would reject the login.
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    # Sent on every read-side response (including the 307 above). A browser
    # ignores it on a plain-http response per RFC 6797, so it costs nothing
    # there; over the tunnel it pins this hostname to https for a year.
    resp.headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
    resp.headers.setdefault("Content-Security-Policy",
                            _CSP_TEMPLATE.format(nonce=csp_nonce()))
    resp.headers.setdefault("Cache-Control", "no-store")
    # Vary on EVERY response, not just the 307. The redirect decision keys
    # entirely off X-Forwarded-Proto, so the 200/302 bodies it gates are
    # equally scheme-dependent: without this a shared cache could store an
    # https-served 200 and later hand it to a plain-http request. Theoretical
    # behind Cloudflare today, but "the edge is one dashboard toggle from
    # regressing" is this whole feature's threat model, so the
    # cache-correctness argument is carried through.
    # .vary.add() APPENDS — Flask adds "Cookie" to Vary when the session is
    # touched, and ``headers["Vary"] = ...`` would silently clobber it.
    resp.vary.add("X-Forwarded-Proto")
    return resp


# --------------------------------------------------------------------------- #
# Auth routes
# --------------------------------------------------------------------------- #

@bp.get("/healthz")
def healthz():
    return {"status": "ok", "role": "read"}


@bp.get("/login")
def login():
    if not _gate_enabled():
        return redirect(url_for("web.index"))
    if session.get(SESSION_KEY) is True:
        return redirect(safe_next(request.args.get("next")))
    return render_template("login.html", next=request.args.get("next", ""),
                           error=None)


@bp.post("/login")
def login_post():
    if not _gate_enabled():
        return redirect(url_for("web.index"))
    next_target = request.form.get("next", "")
    ip = client_ip()
    limiter = current_app.extensions["login_limiter"]
    global_limiter = current_app.extensions["login_global_limiter"]
    if limiter.is_blocked(ip) or global_limiter.is_blocked(GLOBAL_LOGIN_KEY):
        log.warning("login blocked (rate limit) for %s", ip)
        return render_template(
            "login.html", next=next_target,
            error="Too many failed attempts. Try again in a few minutes."), 429
    supplied = request.form.get("password", "")
    if hmac.compare_digest(supplied.encode(), _settings().app_password.encode()):
        session.clear()
        session[SESSION_KEY] = True
        session.permanent = True
        limiter.reset(ip)
        return redirect(safe_next(next_target))
    limiter.record_failure(ip)
    global_limiter.record_failure(GLOBAL_LOGIN_KEY)
    log.warning("failed login attempt from %s", ip)  # never log the password
    return render_template("login.html", next=next_target,
                           error="Incorrect password."), 401


@bp.get("/logout")
def logout():
    session.clear()
    if _gate_enabled():
        return redirect(url_for("web.login"))
    return redirect(url_for("web.index"))


# --------------------------------------------------------------------------- #
# Board
# --------------------------------------------------------------------------- #

def _conn():
    """Every request-path connection to ``dashboard.db`` from the read role is
    ``query_only``. The board has never written this file; now it cannot.

    (``Core.init_store`` still opens a writable connection at app construction —
    before any request, once per process — to run the additive migration.)
    """
    return db.connect_query_only(_settings().db_path)


@bp.get("/")
def index():
    registry = current_app.extensions["registry"]
    now = time.time()
    conn = _conn()
    try:
        status = build_status(conn, registry, now, with_history=True)
    finally:
        conn.close()
    groups = [(m, [j for j in status["jobs"] if j["machine"] == m])
              for m in ("box", "mac")]
    computed = db.from_iso(status["summary"].get("computed_at"))
    scheduler_stale = computed is None or (now - computed) > 5 * 60
    return render_template("index.html", status=status, groups=groups,
                           now=now, scheduler_stale=scheduler_stale)


@bp.get("/jobs/<job_id>")
def job_page(job_id: str):
    registry = current_app.extensions["registry"]
    now = time.time()
    conn = _conn()
    try:
        detail = build_job_detail(conn, registry, job_id, limit=100, now=now)
    finally:
        conn.close()
    if detail is None:
        abort(404)
    return render_template("job.html", d=detail, job=detail["job"], now=now)


@bp.get("/api/v1/status")
def api_status():
    registry = current_app.extensions["registry"]
    conn = _conn()
    try:
        status = build_status(conn, registry)
    finally:
        conn.close()
    return jsonify(status)


@bp.get("/api/v1/jobs/<job_id>")
def api_job(job_id: str):
    registry = current_app.extensions["registry"]
    try:
        limit = int(request.args.get("limit", 100))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 1000))
    conn = _conn()
    try:
        detail = build_job_detail(conn, registry, job_id, limit=limit)
    finally:
        conn.close()
    if detail is None:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(detail)


@bp.app_errorhandler(404)
def _not_found(_exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return render_template("error.html", code=404,
                           message="No such page."), 404


@bp.app_errorhandler(403)
def _forbidden(_exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": "forbidden"}), 403
    return render_template("error.html", code=403,
                           message="Forbidden."), 403
