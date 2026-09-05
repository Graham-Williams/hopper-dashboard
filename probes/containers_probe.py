#!/usr/bin/env python3
"""box-containers heartbeat — runs ON THE BOX from a systemd timer (deploy/box/dashboard-containers.*).

``docker ps --format '{{.Names}} {{.Status}}'`` → POST /api/v1/ping/box-containers with
status=ok and metrics.running / metrics.unhealthy (comma-separated names). If docker itself
can't be queried the ping is status=fail with the error in the note, so the dashboard shows
"docker unreadable" rather than silently going LATE.

Env: DASHBOARD_URL, INGEST_TOKEN (from /etc/hopper-dashboard/ingest.env via EnvironmentFile=).
Stdlib only; Python 3.9+. ``--dry-run`` prints the ping. ``--stdin`` reads docker ps output from
stdin instead of running docker (used by the shell wrapper and by hand for debugging).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import containers  # noqa: E402
from probes.common import DEFAULT_DASHBOARD_URL, ProbeError, build_ping, now_iso, run_cmd, send_ping  # noqa: E402

JOB_ID = "box-containers"


def gather(docker_bin: str = "docker", timeout: float = 30.0) -> str:
    rc, out, err = run_cmd([docker_bin, "ps", "--format", "{{.Names}} {{.Status}}"], timeout=timeout)
    if rc != 0:
        raise ProbeError("docker ps failed (rc=%s): %s" % (rc, err.strip()[-300:] or out.strip()[-300:]))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stdin", action="store_true", help="read `docker ps` output from stdin")
    ap.add_argument("--docker", default=os.environ.get("DOCKER_BIN", "docker"))
    args = ap.parse_args(argv)

    url = os.environ.get("DASHBOARD_URL", DEFAULT_DASHBOARD_URL)
    token = os.environ.get("INGEST_TOKEN", "")
    started = now_iso()

    try:
        text = sys.stdin.read() if args.stdin else gather(args.docker)
        summary = containers.parse_docker_ps(text)
        body = build_ping("ok", started_at=started, finished_at=now_iso(), reason="pushed",
                          note=containers.summary_note(summary), metrics=summary.metrics())
    except ProbeError as e:
        body = build_ping("fail", started_at=started, finished_at=now_iso(), reason="error", note=str(e))

    if args.dry_run:
        print("DRY-RUN POST %s/api/v1/ping/%s\n  %s" % (url, JOB_ID, json.dumps(body, sort_keys=True)))
        return 0
    if not token:
        print("ERROR: INGEST_TOKEN not set", file=sys.stderr)
        return 2
    try:
        code, resp = send_ping(url, token, JOB_ID, body)
    except ProbeError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    print("sent %s %s → HTTP %d %s" % (JOB_ID, body["status"], code, resp.strip()[:80]))
    return 0 if body["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
