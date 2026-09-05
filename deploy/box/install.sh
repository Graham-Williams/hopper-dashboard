#!/bin/bash
# Install the box-side heartbeats for hopper-dashboard. Run ON THE BOX with sudo. Idempotent.
#
#   sudo deploy/box/install.sh [--url http://100.101.1.28:8081] [--token <INGEST_TOKEN>]
#
# Does:
#   1. /etc/hopper-dashboard/ingest.env  (root:root 0600) — DASHBOARD_URL + INGEST_TOKEN. Read by
#      systemd (PID 1, as root) via EnvironmentFile=, so the graham-owned units never need to read it
#      themselves; 0600 root is therefore correct even though the units run as User=graham.
#      Existing file is kept unless --token is passed (then it is rewritten).
#   2. drop-ins  /etc/systemd/system/{km-backup,todoist-points-backup}.service.d/heartbeat.conf
#   3. dashboard-containers.service + .timer (template @@REPO@@ → this checkout)
#   4. systemctl daemon-reload; enable --now dashboard-containers.timer
#   5. prints verification: systemctl cat of each unit, list-timers, and a dry-run of the container probe
#
# Does NOT: restart or touch any container, restart the backup units, or modify ~/km-tracker or
# ~/todoist-points. daemon-reload only re-reads unit files; the oneshot backup units simply pick
# up the drop-in on their next timer tick.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ENV_DIR=/etc/hopper-dashboard
ENV_FILE="$ENV_DIR/ingest.env"
SYSD=/etc/systemd/system
URL="http://100.101.1.28:8081"; TOKEN=""
UNITS=(km-backup todoist-points-backup)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)   URL="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# The service runs as graham; make sure the checkout is where the rendered unit will point.
RUN_USER="$(grep -m1 '^User=' "$HERE/dashboard-containers.service" | cut -d= -f2)"
id "$RUN_USER" >/dev/null 2>&1 || { echo "ERROR: user $RUN_USER does not exist"; exit 1; }
id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx docker || echo "WARN: $RUN_USER is not in the docker group — docker ps will fail"
for u in "${UNITS[@]}"; do
  [[ -f "$SYSD/$u.service" ]] || echo "WARN: $SYSD/$u.service not found — its drop-in will be inert until the unit exists"
done

# --- 1. env file ---------------------------------------------------------------
install -d -m 0755 -o root -g root "$ENV_DIR"
if [[ -f "$ENV_FILE" && -z "$TOKEN" ]]; then
  echo "kept existing $ENV_FILE"
else
  if [[ -z "$TOKEN" ]]; then
    read -r -s -p "INGEST_TOKEN (same value as INGEST_TOKEN in ~/hopper-dashboard/.env; hidden): " TOKEN; echo
  fi
  [[ -n "$TOKEN" ]] || { echo "ERROR: empty token"; exit 2; }
  umask 077
  printf '# hopper-dashboard ingest credentials. Read by systemd via EnvironmentFile=. root:root 0600.\nDASHBOARD_URL=%s\nINGEST_TOKEN=%s\n' "$URL" "$TOKEN" > "$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  umask 022
  echo "wrote $ENV_FILE"
fi
chown root:root "$ENV_FILE"; chmod 0600 "$ENV_FILE"

# --- 2. drop-ins ----------------------------------------------------------------
for u in "${UNITS[@]}"; do
  install -d -m 0755 "$SYSD/$u.service.d"
  install -m 0644 "$HERE/$u.service.d/heartbeat.conf" "$SYSD/$u.service.d/heartbeat.conf"
  echo "installed $SYSD/$u.service.d/heartbeat.conf"
done

# --- 3. containers timer --------------------------------------------------------
sed "s|@@REPO@@|$REPO|g" "$HERE/dashboard-containers.service" > "$SYSD/dashboard-containers.service.tmp"
mv "$SYSD/dashboard-containers.service.tmp" "$SYSD/dashboard-containers.service"
chmod 0644 "$SYSD/dashboard-containers.service"
install -m 0644 "$HERE/dashboard-containers.timer" "$SYSD/dashboard-containers.timer"
chmod +x "$HERE/containers_probe.sh" "$REPO/probes/containers_probe.py" 2>/dev/null || true
echo "installed dashboard-containers.{service,timer}"

# --- 4. reload + enable ---------------------------------------------------------
systemctl daemon-reload
systemctl enable --now dashboard-containers.timer
echo "enabled dashboard-containers.timer"

# --- 5. verify ------------------------------------------------------------------
echo; echo "=== systemd-analyze verify ==="
systemd-analyze verify --man=no "$SYSD/dashboard-containers.service" "${UNITS[@]/%/.service}" && echo "ok"
echo; echo "=== drop-ins as systemd sees them ==="
for u in "${UNITS[@]}"; do systemctl cat "$u.service" | grep -A3 'heartbeat.conf' || echo "!! $u.service has no heartbeat drop-in"; done
echo; echo "=== timers ==="
systemctl list-timers --no-pager | grep -E 'NEXT|km-backup|todoist-points-backup|dashboard-containers' || true
echo; echo "=== container probe dry run (as $RUN_USER) ==="
sudo -u "$RUN_USER" env "$(grep '^DASHBOARD_URL=' "$ENV_FILE")" "$REPO/deploy/box/containers_probe.sh" --dry-run || echo "dry run failed (rc=$?)"
echo
echo "First heartbeats: box-containers within 5 min (or now: systemctl start dashboard-containers.service);"
echo "km-backup / todoist-points-backup on their next timer tick (≤5 min). Check with:"
echo "  journalctl -u dashboard-containers.service -n 5 --no-pager"
echo "  curl -sS -H \"Authorization: Bearer \$READ_TOKEN\" http://localhost:8080/api/v1/status   (from inside the compose network)"
