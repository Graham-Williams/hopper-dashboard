#!/bin/bash
# Entry point for dashboard-containers.service: post the box's disk capacity (box-disk), then run
# `docker ps` and post the box-containers heartbeat, both off the same 5-minute timer — a gauge
# needs no schedule of its own, and one timer means one thing to verify. All parsing/posting lives
# in probes/disk_probe.py and probes/containers_probe.py (stdlib Python, unit-tested); this wrapper
# only pins the interpreter and the repo path. Pass --dry-run to print instead of send.
#
# Order: the disk probe is the cheap one (a statvfs + one POST, ~34 s worst case), `docker ps`
# is the slow one (30 s timeout + its own retries). Cheap first, so the unit's TimeoutStartSec
# can never turn docker trouble into a MISSING disk reading — which is the one this timer is least
# able to report any other way (see the arithmetic in dashboard-containers.service).
#
# Both probes run even if the other fails (a broken docker must not hide a full disk) and the
# service's exit status is non-zero if either failed — when BOTH fail it carries the containers
# code, because a disk failure is already on the board and a containers one may not be. A
# non-zero exit is only visible in the
# journal, so when the disk probe fails WITHOUT having posted anything itself (rc != 3: it could
# not start at all — moved, renamed, interpreter gone) this wrapper posts the `fail` ping for it.
# Otherwise box-containers would keep reporting ok while box-disk silently froze at its last
# reading. disk_probe.py's exit codes: 0 = metrics posted, 1 = nothing landed (transport),
# 2 = nothing landed (config), 3 = a `fail` run WAS posted (statvfs error — already on the board).
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

# Report a disk probe that never reached the dashboard itself. Same form-encoded shape as the
# backup units' ExecStopPost drop-ins (result= / exit= / note=), so nothing new to understand:
# result != success → a `fail` run on box-disk, whose note names this unit.
post_disk_probe_failure() {
  local rc="$1"
  local note="disk probe exited rc=${rc} without posting; see journalctl -u dashboard-containers.service"
  if [[ -n "$DRY" ]]; then
    echo "DRY-RUN POST ${DASHBOARD_URL:-<DASHBOARD_URL unset>}/api/v1/ping/box-disk  result=probe-failed exit=${rc}"
    return 0
  fi
  if [[ -z "${DASHBOARD_URL:-}" || -z "${INGEST_TOKEN:-}" ]]; then
    echo "cannot report the disk probe failure: DASHBOARD_URL/INGEST_TOKEN not set" >&2
    return 0
  fi
  command -v curl >/dev/null 2>&1 || { echo "cannot report the disk probe failure: curl not found" >&2; return 0; }
  # `-H @-` reads the header from stdin (curl >= 7.55) so the token never appears in this
  # process's argv, i.e. in /proc/<pid>/cmdline, which any local user can read.
  curl -fsS -m 10 --retry 2 -X POST -H @- <<<"Authorization: Bearer ${INGEST_TOKEN}" \
    --data-urlencode "result=probe-failed" \
    --data-urlencode "exit=${rc}" \
    --data-urlencode "note=${note}" \
    "${DASHBOARD_URL}/api/v1/ping/box-disk" >/dev/null \
    || echo "box-disk failure ping did not land (dashboard unreachable?)" >&2
}

disk_rc=0
containers_rc=0
"$PY" "$REPO/probes/disk_probe.py" ${DRY:+"$DRY"} || disk_rc=$?
"$PY" "$REPO/probes/containers_probe.py" "$@" || containers_rc=$?
if [[ "$disk_rc" -ne 0 && "$disk_rc" -ne 3 ]]; then
  post_disk_probe_failure "$disk_rc"
fi
# Surface the CONTAINERS failure in preference to the disk one when both fail: a disk
# failure is already a `fail` ping on the board (posted by the probe itself at rc 3, or
# by the fallback above), whereas a containers failure may exist nowhere but here.
# Neither code is lost — both probes log their own error — but the exit status can only
# carry one, so it carries the one the board cannot tell you about.
if [[ "$containers_rc" -ne 0 ]]; then
  rc="$containers_rc"
else
  rc="$disk_rc"
fi
exit "$rc"
