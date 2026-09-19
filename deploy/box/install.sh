#!/bin/bash
# Install the box-side heartbeats for hopper-dashboard. Run ON THE BOX with sudo. Idempotent.
#
#   sudo deploy/box/install.sh --url http://<box-tailscale-ip>:8081 [--token-file ~/hopper-dashboard/.env]
#                              [--user <login>]
#
# Does:
#   1. /etc/hopper-dashboard/ingest.env  (root:root 0600) — DASHBOARD_URL + INGEST_TOKEN. Read by
#      systemd (PID 1, as root) via EnvironmentFile=, so the user-owned units never need to read it
#      themselves; 0600 root is therefore correct even though the units run as User=<login>.
#      The token is read from --token-file (the compose .env: its INGEST_TOKEN= line) or, if that
#      is omitted, prompted with a HIDDEN read. It is never accepted on the command line (shell
#      history / `ps`). An existing env file is kept unless a token is supplied (then rewritten).
#   2. drop-ins  /etc/systemd/system/{km-backup,todoist-points-backup,hopper-dashboard-backup}.service.d/heartbeat.conf
#   3. dashboard-containers.service + .timer AND hopper-dashboard-backup.service + .timer
#      (templates: @@REPO@@ → this checkout, @@USER@@ → --user, default $SUDO_USER, i.e. whoever
#      ran sudo; must be in the docker group)
#   4. systemctl daemon-reload; enable --now both timers
#   5. prints verification: systemctl cat of each unit, list-timers, and a dry-run of the container probe
#
# The backup timer and its heartbeat drop-in are installed TOGETHER, always. An unmonitored
# backup fails silently and the board keeps showing the job as it last was — the exact failure
# this repo exists to catch.
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
URL=""; TOKEN=""; TOKEN_FILE=""
RUN_USER="${SUDO_USER:-$(id -un)}"
UNITS=(km-backup todoist-points-backup)          # foreign units we only drop a heartbeat into
OWN_UNITS=(hopper-dashboard-backup)              # units this repo ships AND heartbeats

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)        URL="$2"; shift 2 ;;
    --token-file) TOKEN_FILE="$2"; shift 2 ;;
    --user)       RUN_USER="$2"; shift 2 ;;
    --token) echo "ERROR: --token is not accepted (it would leak into shell history / ps); use --token-file or the hidden prompt" >&2; exit 2 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# No default URL: the box's Tailscale IP is deployment-specific and stays out of the repo. Reuse the
# one already in the env file when re-running without --url.
if [[ -z "$URL" && -f "$ENV_FILE" ]]; then
  URL="$(grep -m1 '^DASHBOARD_URL=' "$ENV_FILE" | cut -d= -f2- || true)"
fi
[[ "$URL" =~ ^https?:// ]] || { echo "ERROR: --url http://<box-tailscale-ip>:8081 is required (no default)"; exit 2; }

if [[ -n "$TOKEN_FILE" ]]; then
  [[ -r "$TOKEN_FILE" ]] || { echo "ERROR: cannot read $TOKEN_FILE"; exit 2; }
  TOKEN="$(grep -m1 '^INGEST_TOKEN=' "$TOKEN_FILE" | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' || true)"
  [[ -n "$TOKEN" ]] || { echo "ERROR: no INGEST_TOKEN= line in $TOKEN_FILE"; exit 2; }
fi

# The container-heartbeat service runs as $RUN_USER (rendered into User=); it must be a real login
# in the docker group, and $RUN_USER must not be root (docker ps needs no root; least privilege).
[[ "$RUN_USER" != "root" ]] || { echo "ERROR: refusing User=root — pass --user <login in the docker group>"; exit 2; }
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
    read -r -s -p "INGEST_TOKEN (same value as INGEST_TOKEN in the compose .env; hidden): " TOKEN; echo
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
sed -e "s|@@REPO@@|$REPO|g" -e "s|@@USER@@|$RUN_USER|g" "$HERE/dashboard-containers.service" > "$SYSD/dashboard-containers.service.tmp"
grep -q '@@' "$SYSD/dashboard-containers.service.tmp" && { echo "ERROR: unrendered placeholder in unit"; rm -f "$SYSD/dashboard-containers.service.tmp"; exit 1; }
mv "$SYSD/dashboard-containers.service.tmp" "$SYSD/dashboard-containers.service"
chmod 0644 "$SYSD/dashboard-containers.service"
install -m 0644 "$HERE/dashboard-containers.timer" "$SYSD/dashboard-containers.timer"
chmod +x "$HERE/containers_probe.sh" "$REPO/probes/containers_probe.py" "$REPO/probes/disk_probe.py" 2>/dev/null || true
echo "installed dashboard-containers.{service,timer}"

# --- 3b. backup timer + its heartbeat drop-in (installed together, never apart) ---
for u in "${OWN_UNITS[@]}"; do
  sed -e "s|@@REPO@@|$REPO|g" -e "s|@@USER@@|$RUN_USER|g" "$HERE/$u.service" > "$SYSD/$u.service.tmp"
  grep -q '@@' "$SYSD/$u.service.tmp" && { echo "ERROR: unrendered placeholder in $u.service"; rm -f "$SYSD/$u.service.tmp"; exit 1; }
  mv "$SYSD/$u.service.tmp" "$SYSD/$u.service"
  chmod 0644 "$SYSD/$u.service"
  install -m 0644 "$HERE/$u.timer" "$SYSD/$u.timer"
  install -d -m 0755 "$SYSD/$u.service.d"
  install -m 0644 "$HERE/$u.service.d/heartbeat.conf" "$SYSD/$u.service.d/heartbeat.conf"
  echo "installed $u.{service,timer} + its heartbeat drop-in"
done
chmod +x "$HERE/backup.sh" "$HERE/verify_snapshot.py" 2>/dev/null || true

# --- 4. reload + enable ---------------------------------------------------------
systemctl daemon-reload
systemctl enable --now dashboard-containers.timer
echo "enabled dashboard-containers.timer"
for u in "${OWN_UNITS[@]}"; do systemctl enable --now "$u.timer"; echo "enabled $u.timer"; done

# --- 5. verify ------------------------------------------------------------------
echo; echo "=== systemd-analyze verify ==="
systemd-analyze verify --man=no "$SYSD/dashboard-containers.service" "${OWN_UNITS[@]/%/.service}" "${UNITS[@]/%/.service}" && echo "ok"
echo; echo "=== drop-ins as systemd sees them ==="
for u in "${UNITS[@]}" "${OWN_UNITS[@]}"; do systemctl cat "$u.service" | grep -E 'heartbeat.conf|ExecStopPost' || echo "!! $u.service has no heartbeat drop-in"; done
echo; echo "=== timers ==="
systemctl list-timers --no-pager | grep -E 'NEXT|km-backup|todoist-points-backup|dashboard-containers|hopper-dashboard-backup' || true
echo; echo "=== box probe dry run (as $RUN_USER): box-containers + box-disk ==="
sudo -u "$RUN_USER" env "$(grep '^DASHBOARD_URL=' "$ENV_FILE")" "$REPO/deploy/box/containers_probe.sh" --dry-run || echo "dry run failed (rc=$?)"
echo
echo "First heartbeats: box-containers + box-disk within 5 min (or now: systemctl start dashboard-containers.service);"
echo "km-backup / todoist-points-backup / hopper-dashboard-backup on their next timer tick (≤5 min). Check with:"
echo "  journalctl -u hopper-dashboard-backup.service -n 20 --no-pager   # the backup's own log lines"
echo "  journalctl -u dashboard-containers.service -n 5 --no-pager"
echo "  # read side (curl is not in the image; use python inside the container). Host MUST equal APP_HOST —"
echo "  # the read role pins Host on everything but /healthz, so without the header this is a 403:"
echo "  docker exec -e RT=\"\$(grep '^READ_TOKEN=' ~/hopper-dashboard/.env | cut -d= -f2-)\" -e AH=\"\$(grep '^APP_HOST=' ~/hopper-dashboard/.env | cut -d= -f2-)\" hopper-dashboard python -c \"import os,json,urllib.request as u; r=u.Request('http://127.0.0.1:8080/api/v1/status', headers={'Authorization':'Bearer '+os.environ['RT'],'Host':os.environ['AH']}); print(json.loads(u.urlopen(r, timeout=5).read())['summary'])\""
echo "  # (once Cloudflare is wired, read via the public hostname instead: https://<APP_HOST>/api/v1/status)"
