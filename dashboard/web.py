"""Read role: HTML board + JSON API behind the shared-password gate.

Gate semantics (ported from jjho / km / taste-twin / todoist-points):
``APP_PASSWORD`` set → every route except ``/login``, ``/logout``, ``/healthz``
and ``/static/*`` requires the signed session cookie. Additionally
``/api/v1/*`` accepts ``Authorization: Bearer <READ_TOKEN>`` so Hopper can read
the board without a browser session. ``APP_HOST`` (optional) pins Host and,
for POSTs, Origin/Referer as a CSRF defence.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from urllib.parse import urlsplit

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


def _bearer_ok() -> bool:
    expected = _settings().read_token
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or not expected:
        return False
    return hmac.compare_digest(token.encode(), expected.encode())


def is_authed() -> bool:
    if session.get(SESSION_KEY) is True:
        return True
    if request.path.startswith("/api/v1/") and _bearer_ok():
        return True
    return False


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
    if request.method == "POST":
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
    resp.headers.setdefault("Content-Security-Policy",
                            _CSP_TEMPLATE.format(nonce=csp_nonce()))
    resp.headers.setdefault("Cache-Control", "no-store")
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
    return db.connect(_settings().db_path)


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
