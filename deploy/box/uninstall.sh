#!/bin/bash
# Remove the box-side hopper-dashboard heartbeats. Run ON THE BOX with sudo. Idempotent.
#   sudo deploy/box/uninstall.sh [--purge]    # --purge also deletes /etc/hopper-dashboard
# Never touches containers, the km-tracker / todoist-points backup units, or their repos — only
# their heartbeat drop-ins. It DOES remove this repo's own hopper-dashboard-backup unit+timer,
# which install.sh put there; the snapshots it already took are left alone, on the box and on
# Drive (the push is `rclone copy`, which never deletes).
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 2; }
SYSD=/etc/systemd/system
PURGE=""; [[ "${1:-}" == "--purge" ]] && PURGE=1

for t in dashboard-containers hopper-dashboard-backup; do
  systemctl disable --now "$t.timer" 2>/dev/null && echo "disabled $t.timer" || true
  rm -f "$SYSD/$t.timer" "$SYSD/$t.service"
done
# The backup's own heartbeat drop-in goes with its unit; the two foreign units keep theirs
# only until this loop removes it (their units and repos are never touched either way).
for u in km-backup todoist-points-backup hopper-dashboard-backup; do
  if [[ -f "$SYSD/$u.service.d/heartbeat.conf" ]]; then
    rm -f "$SYSD/$u.service.d/heartbeat.conf"; echo "removed $u heartbeat drop-in"
    rmdir "$SYSD/$u.service.d" 2>/dev/null || true   # only if we were the sole drop-in
  fi
done
systemctl daemon-reload
systemctl reset-failed dashboard-containers.service hopper-dashboard-backup.service 2>/dev/null || true
if [[ -n "$PURGE" ]]; then rm -rf /etc/hopper-dashboard; echo "removed /etc/hopper-dashboard"; else echo "kept /etc/hopper-dashboard/ingest.env (pass --purge to remove)"; fi
echo "done — km-tracker/todoist-points backups keep running exactly as before, just without"
echo "the heartbeat. The hopper-dashboard backup TIMER is gone, but every snapshot it already"
echo "took is still in ~/hopper-dashboard-backups and on Drive (rclone copy never deletes)."
