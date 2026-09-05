#!/bin/bash
# Remove the box-side hopper-dashboard heartbeats. Run ON THE BOX with sudo. Idempotent.
#   sudo deploy/box/uninstall.sh [--purge]    # --purge also deletes /etc/hopper-dashboard
# Never touches containers, the backup units themselves, or their repos.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 2; }
SYSD=/etc/systemd/system
PURGE=""; [[ "${1:-}" == "--purge" ]] && PURGE=1

systemctl disable --now dashboard-containers.timer 2>/dev/null && echo "disabled dashboard-containers.timer" || true
rm -f "$SYSD/dashboard-containers.timer" "$SYSD/dashboard-containers.service"
for u in km-backup todoist-points-backup; do
  if [[ -f "$SYSD/$u.service.d/heartbeat.conf" ]]; then
    rm -f "$SYSD/$u.service.d/heartbeat.conf"; echo "removed $u heartbeat drop-in"
    rmdir "$SYSD/$u.service.d" 2>/dev/null || true   # only if we were the sole drop-in
  fi
done
systemctl daemon-reload
systemctl reset-failed dashboard-containers.service 2>/dev/null || true
if [[ -n "$PURGE" ]]; then rm -rf /etc/hopper-dashboard; echo "removed /etc/hopper-dashboard"; else echo "kept /etc/hopper-dashboard/ingest.env (pass --purge to remove)"; fi
echo "done — backup units keep running exactly as before, just without the heartbeat"
