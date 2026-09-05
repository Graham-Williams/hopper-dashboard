"""hopper-dashboard: jobs & backups dashboard.

``create_app(role)`` builds one of two Flask apps that share a SQLite file:

- ``read``   — HTML board + JSON API behind the password gate (port 8080,
               exposed through the Cloudflare tunnel).
- ``ingest`` — ``POST /api/v1/ping/<job>`` + the background scheduler (port
               8081, published on the box's Tailscale IP only). Run it with a
               single gunicorn worker so exactly one scheduler exists.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from flask import Flask

from .config import Settings
from .humanize import absolute, human_bytes, human_duration, relative
from .notify import Notifier
from .ratelimit import LoginRateLimiter, SlidingWindowLimiter
from .registry import Registry, load_registry
from .services import Core

__version__ = "0.1.0"
ROLES = ("read", "ingest")

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Refusing to start with a configuration that would expose the read side."""


def _check_read_config(settings: Settings) -> None:
    """Fail FAST (the process exits, compose restarts it loudly) rather than
    serve an unprotected board or a login that can never stick.

    - ``APP_ENV=prod`` with no ``APP_PASSWORD`` → the board would be public
      through the tunnel.
    - ``APP_PASSWORD`` set but no ``SESSION_SECRET`` → each gunicorn worker
      would mint its own ephemeral signing key, so a login on one worker is
      an invalid cookie on the next: an endless redirect loop.
    """
    if settings.app_env == "prod" and not settings.app_password:
        raise ConfigError("APP_ENV=prod but APP_PASSWORD is empty — refusing to "
                          "serve the read side without the password gate "
                          "(set APP_PASSWORD, or APP_ENV=dev for local work)")
    if settings.app_password and not settings.session_secret:
        raise ConfigError("APP_PASSWORD is set but SESSION_SECRET is empty — "
                          "multi-worker logins would loop (set SESSION_SECRET="
                          "$(openssl rand -hex 32))")


def create_app(role: str = "read", settings: Settings | None = None,
               registry: Registry | None = None,
               notifier: Notifier | None = None) -> Flask:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s",
                            datefmt="%H:%M:%S")
    settings = settings or Settings.from_env()
    registry = registry or load_registry(settings.jobs_file)

    # The ingest role serves no static files at all: static_folder=None means
    # there is no /static/<path> route to probe on the Tailscale port.
    app = Flask(__name__,
                static_folder=None if role == "ingest" else "static",
                template_folder="templates")
    app.config["SETTINGS"] = settings
    app.config["ROLE"] = role
    app.config["MAX_CONTENT_LENGTH"] = settings.max_body_bytes
    app.config["JSON_SORT_KEYS"] = False
    app.jinja_env.autoescape = True
    app.extensions["registry"] = registry

    core = Core(settings, registry, notifier)
    core.init_store()
    app.extensions["core"] = core

    if role == "ingest":
        from . import ingest
        from .scheduler import Scheduler
        app.register_blueprint(ingest.bp)
        app.register_error_handler(404, ingest.unknown_route)
        app.extensions["ping_limiter"] = SlidingWindowLimiter(
            settings.ping_rate_max, settings.ping_rate_window_s)
        if not settings.ingest_token:
            log.warning("INGEST_TOKEN unset — every ping will be rejected (401).")
        scheduler = Scheduler(core, settings.tick_interval_s,
                              settings.probe_interval_s)
        app.extensions["scheduler"] = scheduler
        if settings.start_scheduler:
            scheduler.start()
        return app

    # -- read role -------------------------------------------------------- #
    from . import web
    _check_read_config(settings)
    app.secret_key = settings.session_secret or secrets.token_hex(32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_NAME="dashboard_session",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    app.extensions["login_limiter"] = LoginRateLimiter(
        settings.login_rate_max, settings.login_rate_window_s)
    # One shared bucket for every client: caps total password guessing even
    # when the per-IP limiter is being evaded with many source addresses.
    app.extensions["login_global_limiter"] = LoginRateLimiter(
        settings.login_global_max, settings.login_rate_window_s, max_tracked_ips=2)
    if settings.app_password:
        if not settings.session_secret:
            log.warning("APP_PASSWORD set but SESSION_SECRET unset — using an "
                        "ephemeral signing key; sessions won't survive a restart.")
        log.info("APP_PASSWORD set — shared-password gate ENABLED.")
    else:
        log.warning("APP_PASSWORD unset — gate OFF (local dev only).")
    if not settings.trusted_proxy_cidrs:
        log.info("TRUSTED_PROXY_CIDR unset — CF-Connecting-IP is ignored; "
                 "login rate-limits key on the TCP peer (the tunnel container).")
    if not settings.app_host:
        log.warning("APP_HOST unset — Host/Origin pinning disabled (local dev).")

    app.jinja_env.filters["human_bytes"] = human_bytes
    app.jinja_env.filters["human_duration"] = human_duration
    app.jinja_env.filters["absolute"] = absolute
    app.jinja_env.filters["relative"] = relative
    app.jinja_env.globals["app_version"] = __version__
    app.jinja_env.globals["app_env"] = settings.app_env
    app.jinja_env.globals["csp_nonce"] = web.csp_nonce
    app.register_blueprint(web.bp)
    return app
