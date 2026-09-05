#!/bin/bash
# Entry point for dashboard-containers.service: run `docker ps` and post the box-containers
# heartbeat. All parsing/posting lives in probes/containers_probe.py (stdlib Python, unit-tested);
# this wrapper only pins the interpreter and the repo path. Pass --dry-run to print instead of send.
#
# Requires DASHBOARD_URL + INGEST_TOKEN in the environment (systemd supplies them from
# /etc/hopper-dashboard/ingest.env). For a manual run:
#   set -a; . /etc/hopper-dashboard/ingest.env; set +a; deploy/box/containers_probe.sh --dry-run
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-/usr/bin/python3}"
[[ -x "$PY" ]] || PY="$(command -v python3)"
exec "$PY" "$REPO/probes/containers_probe.py" "$@"
