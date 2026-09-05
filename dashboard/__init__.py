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

    app = Flask(__name__, static_folder="static", template_folder="templates")
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
    if settings.app_password:
        if not settings.session_secret:
            log.warning("APP_PASSWORD set but SESSION_SECRET unset — using an "
                        "ephemeral signing key; sessions won't survive a restart.")
        log.info("APP_PASSWORD set — shared-password gate ENABLED.")
    else:
        log.warning("APP_PASSWORD unset — gate OFF (local dev only).")
    if not settings.app_host:
        log.warning("APP_HOST unset — Host/Origin pinning disabled (local dev).")

    app.jinja_env.filters["human_bytes"] = human_bytes
    app.jinja_env.filters["human_duration"] = human_duration
    app.jinja_env.filters["absolute"] = absolute
    app.jinja_env.filters["relative"] = relative
    app.jinja_env.globals["app_version"] = __version__
    app.jinja_env.globals["app_env"] = settings.app_env
    app.register_blueprint(web.bp)
    return app
