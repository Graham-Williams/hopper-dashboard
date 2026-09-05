"""App-level shared-password gate helpers (env-gated by ``APP_PASSWORD``).

Ported from jjho-fan-almanac's ``password_gate.py`` — the same pattern as
km-tracker / todoist-points / taste-twin. When ``APP_PASSWORD`` is set, every
read-side request that isn't ``/login``, ``/logout``, ``/healthz`` or a static
asset is redirected to ``/login`` until the visitor presents the password. A
correct password stores only a *marker* in Flask's signed session cookie; the
password itself is never stored or logged. Unset ``APP_PASSWORD`` = gate OFF
(local dev only — never expose the read side with the gate off).

The stateless helpers live here; the routes and ``before_request`` hook live in
:mod:`.web`.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from flask import request, url_for

from .ratelimit import LoginRateLimiter  # re-exported for callers

__all__ = ["LoginRateLimiter", "client_ip", "safe_next"]


def client_ip() -> str:
    """Best-effort client IP. Behind the Cloudflare tunnel the real client is
    in ``CF-Connecting-IP``; ``remote_addr`` is the tunnel hop."""
    return (request.headers.get("CF-Connecting-IP")
            or request.remote_addr or "").strip()


def safe_next(target: str | None, fallback_endpoint: str = "web.index") -> str:
    """Return ``target`` only if it is a safe *local* path, else the fallback.

    Open-redirect defense: a valid ``next`` must be a single-slash, same-site
    path with no scheme and no network location. Rejects absolute URLs,
    protocol-relative URLs (``//evil``) and the backslash trick (``/\\evil``).
    """
    fallback = url_for(fallback_endpoint)
    if not target:
        return fallback
    if (not target.startswith("/")
            or target.startswith("//")
            or target[:2] == "/\\"):
        return fallback
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return fallback
    return target
