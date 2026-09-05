"""Ingest role: ``POST /api/v1/ping/<job_id>`` + ``GET /healthz``.

Published only on the box's Tailscale IP. Auth is one shared bearer token
(``INGEST_TOKEN``) compared in constant time; a missing token config fails
closed (every ping → 401). Undeclared job ids → 404 so a typo cannot create a
phantom job. Bodies are capped at 64 KB, ``note`` at 500 chars, metrics to flat
scalars. Form-encoded ``result=`` / ``exit=`` is accepted for systemd
``ExecStopPost`` curls.
"""

from __future__ import annotations

import hmac
import logging
import math
import re

from flask import Blueprint, Response, abort, current_app, jsonify, request

from .db import from_iso
from .password_gate import client_ip

log = logging.getLogger(__name__)

bp = Blueprint("ingest", __name__)

STATUSES = ("ok", "fail", "skipped", "metric")
NOTE_MAX = 500
REASON_MAX = 100
METRIC_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
METRIC_MAX_KEYS = 50
METRIC_STR_MAX = 1000
METRIC_LIST_MAX = 100


class PayloadError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Payload parsing (pure; unit-tested directly)
# --------------------------------------------------------------------------- #

def _scalar(value, where: str):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PayloadError(f"{where}: non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > METRIC_STR_MAX:
            raise PayloadError(f"{where}: string longer than {METRIC_STR_MAX}")
        return value
    raise PayloadError(f"{where}: values must be numbers or short strings")


def parse_metrics(raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PayloadError("metrics must be an object")
    if len(raw) > METRIC_MAX_KEYS:
        raise PayloadError(f"metrics: more than {METRIC_MAX_KEYS} keys")
    out: dict = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not METRIC_KEY_RE.match(key):
            raise PayloadError("metrics: bad key (use [A-Za-z0-9_.-], ≤64 chars)")
        if isinstance(value, dict):
            raise PayloadError(f"metrics.{key}: nested objects are not allowed")
        if isinstance(value, (list, tuple)):
            if len(value) > METRIC_LIST_MAX:
                raise PayloadError(f"metrics.{key}: list longer than {METRIC_LIST_MAX}")
            out[key] = [_scalar(v, f"metrics.{key}[]") for v in value]
            if any(isinstance(v, (list, tuple, dict)) for v in value):
                raise PayloadError(f"metrics.{key}: nested lists are not allowed")
            continue
        out[key] = _scalar(value, f"metrics.{key}")
    return out


def _iso_or_none(value, name: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 64 or from_iso(value) is None:
        raise PayloadError(f"{name} must be an ISO-8601 timestamp")
    return value


def _short_str(value, name: str, max_len: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PayloadError(f"{name} must be a string")
    return value[:max_len]


def parse_json_payload(doc) -> dict:
    if not isinstance(doc, dict):
        raise PayloadError("body must be a JSON object")
    status = doc.get("status")
    if not isinstance(status, str) or status.lower() not in STATUSES:
        raise PayloadError("status must be one of ok|fail|skipped|metric")
    exit_code = doc.get("exit_code")
    if exit_code is not None:
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise PayloadError("exit_code must be an integer")
        if not -32768 <= exit_code <= 32767:
            raise PayloadError("exit_code out of range")
    return {
        "status": status.lower(),
        "started_at": _iso_or_none(doc.get("started_at"), "started_at"),
        "finished_at": _iso_or_none(doc.get("finished_at"), "finished_at"),
        "reason": _short_str(doc.get("reason"), "reason", REASON_MAX),
        "exit_code": exit_code,
        "note": _short_str(doc.get("note"), "note", NOTE_MAX),
        "metrics": parse_metrics(doc.get("metrics")),
    }


def parse_form_payload(form) -> dict:
    """systemd ``ExecStopPost`` shape: ``result=$SERVICE_RESULT&exit=$EXIT_STATUS``.
    ``result=success`` → ok; anything else → fail with ``reason=<result>``."""
    result = (form.get("result") or "").strip()
    if not result:
        raise PayloadError("form body needs result=")
    exit_raw = (form.get("exit") or "").strip()
    exit_code: int | None = None
    if exit_raw:
        try:
            exit_code = int(exit_raw)
        except ValueError:
            # systemd may report a signal name (e.g. KILL) rather than a number.
            exit_code = None
    ok = result.lower() == "success"
    note = _short_str(form.get("note"), "note", NOTE_MAX)
    extras = []
    if not ok and exit_raw and exit_code is None:
        extras.append(f"exit={exit_raw}")
    # systemd also has $EXIT_CODE (exited|killed|dumped) — how the process ended.
    how = (form.get("exit_code") or "").strip()
    if how and how.lower() != "exited":
        extras.append(f"exit_code={how[:32]}")
    if extras:
        note = (note + " " if note else "") + " ".join(extras)
    return {
        "status": "ok" if ok else "fail",
        "started_at": None,
        "finished_at": None,
        "reason": None if ok else result[:REASON_MAX],
        "exit_code": exit_code,
        "note": note[:NOTE_MAX] if note else None,
        "metrics": {},
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

def _bearer() -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def _authorized() -> bool:
    expected = current_app.config["SETTINGS"].ingest_token
    supplied = _bearer()
    if not expected or not supplied:
        # Still burn a comparison so timing is uniform.
        hmac.compare_digest(supplied or "x", expected or "y")
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())


@bp.get("/healthz")
def healthz():
    return {"status": "ok", "role": "ingest"}


@bp.post("/api/v1/ping/<job_id>")
def ping(job_id: str):
    settings = current_app.config["SETTINGS"]
    limiter = current_app.extensions["ping_limiter"]
    ip = client_ip()
    if not limiter.hit(ip):
        log.warning("ping rate-limited for %s", ip)
        return jsonify({"ok": False, "error": "rate limited"}), 429
    if not _authorized():
        # No detail: don't reveal whether the job exists or what was wrong.
        return Response(status=401)
    core = current_app.extensions["core"]
    job = core.registry.get(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "unknown job"}), 404
    if (request.content_length or 0) > settings.max_body_bytes:
        return jsonify({"ok": False, "error": "body too large"}), 413
    try:
        if request.mimetype == "application/x-www-form-urlencoded":
            payload = parse_form_payload(request.form)
            source = "form"
        else:
            doc = request.get_json(force=True, silent=True)
            if doc is None:
                raise PayloadError("body must be valid JSON")
            payload = parse_json_payload(doc)
            source = "ping"
    except PayloadError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    state = core.record_ping(job, payload, source=source)
    return jsonify({"ok": True, "state": state})


@bp.errorhandler(413)
def _too_large(_exc):
    return jsonify({"ok": False, "error": "body too large"}), 413


def unknown_route(_exc):
    return jsonify({"ok": False, "error": "not found"}), 404


def _abort_unused():  # pragma: no cover - keeps `abort` import meaningful for linters
    abort(404)
