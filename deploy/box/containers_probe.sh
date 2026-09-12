#!/bin/bash
# Entry point for dashboard-containers.service: run `docker ps` and post the box-containers
# heartbeat, then post the box's disk capacity (box-disk) off the same 5-minute timer — a gauge
# needs no schedule of its own, and one timer means one thing to verify. All parsing/posting lives
# in probes/containers_probe.py and probes/disk_probe.py (stdlib Python, unit-tested); this wrapper
# only pins the interpreter and the repo path. Pass --dry-run to print instead of send.
#
# Both probes run even if the first one fails (a broken docker must not hide a full disk) and the
# service's exit status is non-zero if either failed.
#
# Requires DASHBOARD_URL + INGEST_TOKEN in the environment (systemd supplies them from
# /etc/hopper-dashboard/ingest.env). Optional PROBE_DISK_PATH (default /). For a manual run:
#   set -a; . /etc/hopper-dashboard/ingest.env; set +a; deploy/box/containers_probe.sh --dry-run
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-/usr/bin/python3}"
[[ -x "$PY" ]] || PY="$(command -v python3)"

# Only --dry-run is meaningful to both; containers-only flags (--stdin, --docker) stay behind.
DRY=""
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then DRY="--dry-run"; fi
done

rc=0
"$PY" "$REPO/probes/containers_probe.py" "$@" || rc=$?
"$PY" "$REPO/probes/disk_probe.py" ${DRY:+"$DRY"} || rc=$?
exit "$rc"
