#!/usr/bin/env python3
"""box-disk heartbeat — runs ON THE BOX from the same systemd timer as box-containers
(deploy/box/containers_probe.sh, every 5 min).

``statvfs`` on the root filesystem → POST /api/v1/ping/box-disk with ``status=metric`` and
``metrics.disk_free_bytes`` / ``disk_total_bytes``. A ``disk`` job is a gauge, not a scheduled
job: it is never LATE, so the numbers ride in as metrics rather than as a run, and the box's
liveness is still owned by box-containers on this very timer. If ``statvfs`` itself fails the
ping is ``fail`` with the error in the note, so a vanished mount point is visible rather than a
gauge frozen at yesterday's figure.

Env: DASHBOARD_URL, INGEST_TOKEN (from /etc/hopper-dashboard/ingest.env via EnvironmentFile=),
optional PROBE_DISK_PATH (default ``/``). Stdlib only; Python 3.9+. ``--dry-run`` prints the ping.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes.common import ProbeError, build_ping, disk_free, now_iso, require_dashboard_url, send_ping  # noqa: E402

JOB_ID = "box-disk"
DEFAULT_PATH = "/"


def build_disk_ping(path: str) -> dict:
    """A ``metric`` ping with the capacity figures, or a ``fail`` run if the path is unreadable."""
    try:
        metrics = disk_free(path)
    except OSError as exc:
        return build_ping("fail", started_at=now_iso(), finished_at=now_iso(), reason="error",
                          note="statvfs %s: %s" % (path, exc))
    metrics["disk_path"] = path
    return build_ping("metric", metrics=metrics)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--job", default=JOB_ID, help="job id to ping (default %s)" % JOB_ID)
    ap.add_argument("--path", default=os.environ.get("PROBE_DISK_PATH", DEFAULT_PATH),
                    help="filesystem to measure (default %s)" % DEFAULT_PATH)
    args = ap.parse_args(argv)

    try:
        url = require_dashboard_url(dict(os.environ))
    except ProbeError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 2
    body = build_disk_ping(args.path)

    if args.dry_run:
        print("DRY-RUN POST %s/api/v1/ping/%s\n  %s" % (url, args.job, json.dumps(body, sort_keys=True)))
        return 0
    token = os.environ.get("INGEST_TOKEN", "")
    if not token:
        print("ERROR: INGEST_TOKEN not set", file=sys.stderr)
        return 2
    try:
        code, resp = send_ping(url, token, args.job, body)
    except ProbeError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    print("sent %s %s → HTTP %d %s" % (args.job, body["status"], code, resp.strip()[:80]))
    return 0 if body["status"] != "fail" else 1


if __name__ == "__main__":
    sys.exit(main())
