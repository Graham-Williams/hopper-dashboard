"""The Hub: ``/``, the landing page on the READ role.

Two equal cards and nothing else: the Inbox (what needs Graham's review, and
a big Record button) first, because that is what he opens the site on his
phone to do; then a one-row health strip of the jobs board. Each card links to
its full page (``/inbox``, ``/dashboard``).

Read-only. ``dashboard.db`` is read through the same ``query_only``
connection the board uses, and ``inbox.db`` only for its counts. The page
carries exactly one ``<script>`` (base.html's timestamp localizer): the Record
button is a plain link to ``/inbox#capture``, so it works with JS off and no
second script is needed here.
"""

from __future__ import annotations

import time

from flask import Blueprint, current_app, render_template

from . import inbox_db
from .views import build_status
from .web import _conn as _dashboard_conn
from .web import scheduler_stale

bp = Blueprint("hub", __name__)


def health_strip(summary: dict) -> dict:
    """The board's six state counts folded into the three a phone glance
    needs. ``behind`` is "late or lagging" (a heartbeat or copy that is
    overdue), ``failing`` is "a run failed or the destination is stale" —
    the two that mean something is actually wrong."""
    return {
        "ok": int(summary.get("ok") or 0),
        "behind": int(summary.get("late") or 0) + int(summary.get("behind") or 0),
        "failing": (int(summary.get("fail") or 0)
                    + int(summary.get("stale_dest") or 0)),
        "unknown": int(summary.get("unknown") or 0),
        "total": int(summary.get("total") or 0),
    }


@bp.get("/")
def index():
    settings = current_app.config["SETTINGS"]
    registry = current_app.extensions["registry"]
    now = time.time()
    conn = _dashboard_conn()
    try:
        status = build_status(conn, registry, now, with_history=False)
    finally:
        conn.close()
    iconn = inbox_db.connect(settings.inbox_db_path)
    try:
        counts = inbox_db.counts(iconn)
    finally:
        iconn.close()
    return render_template("hub.html", now=now, counts=counts,
                           health=health_strip(status["summary"]),
                           scheduler_stale=scheduler_stale(status, now))
