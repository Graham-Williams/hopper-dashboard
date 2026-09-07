#!/bin/bash
# Install the hourly hopper-dashboard Mac probe as a launchd user agent. Idempotent.
#
#   deploy/mac/install.sh [--url http://<box-tailscale-ip>:8081]
#
# 1. writes ~/.config/hopper-dashboard/env (chmod 600) if it doesn't exist — prompts for the URL
#    (unless --url is given) and for the token with a HIDDEN read (never pass the token on the
#    command line: it would land in shell history and `ps`); an existing file is never overwritten.
#    There is deliberately no default URL: the box's Tailscale IP stays out of the repo.
# 2. renders deploy/mac/com.hopper.dashboard-probe.plist (absolute paths) into ~/Library/LaunchAgents
# 3. launchctl bootout (ignore "not loaded") + bootstrap, so a re-run picks up plist changes
# 4. runs the probe once with --dry-run and prints the pings it WOULD send
#
# Nothing here touches the box, uploads anything, or edits files outside ~/.config/hopper-dashboard,
# ~/Library/LaunchAgents and ~/Library/Logs.
set -euo pipefail

LABEL="com.hopper.dashboard-probe"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CONF_DIR="$HOME/.config/hopper-dashboard"
ENV_FILE="$CONF_DIR/env"
PLIST_SRC="$HERE/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
URL=""; TOKEN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)   URL="$2"; shift 2 ;;
    --token) echo "ERROR: --token is not accepted (it would leak into shell history / ps); the script prompts with a hidden read" >&2; exit 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -x /usr/bin/python3 ]] || { echo "ERROR: /usr/bin/python3 missing (install Xcode Command Line Tools)"; exit 1; }
[[ -x /opt/homebrew/bin/rclone || -x /usr/local/bin/rclone ]] || echo "WARN: rclone not at /opt/homebrew/bin or /usr/local/bin — the rclone sub-probes will report fail"

# --- 1. env file ---------------------------------------------------------------
mkdir -p "$CONF_DIR"; chmod 700 "$CONF_DIR"
if [[ -f "$ENV_FILE" ]]; then
  echo "env file exists, leaving it alone: $ENV_FILE"
else
  if [[ -z "$URL" ]]; then
    read -r -p "DASHBOARD_URL (http://<box-tailscale-ip>:8081): " URL
  fi
  [[ "$URL" =~ ^https?:// ]] || { echo "ERROR: DASHBOARD_URL must start with http:// or https://"; exit 2; }
  read -r -s -p "INGEST_TOKEN (from the box's ~/hopper-dashboard/.env; input hidden): " TOKEN; echo
  [[ -n "$TOKEN" ]] || { echo "ERROR: empty token"; exit 2; }
  umask 077
  cat > "$ENV_FILE" <<EOF
# hopper-dashboard Mac probe config (chmod 600). Read by probes/mac_probe.py and probes/ping.sh.
DASHBOARD_URL=$URL
INGEST_TOKEN=$TOKEN
# Optional overrides (defaults shown in probes/mac_probe.py load_settings): PROBE_RCLONE=,
# PROBE_PA_LOG=, PROBE_PA_SRC=, PROBE_PA_REMOTE=, PROBE_MC_BASE=, PROBE_MC_REMOTE=, PROBE_MC_SCRIPT=,
# PROBE_MC_MIN_AGE=, PROBE_DISK_PATH=, PROBE_DRIVEFS_DIR=, PROBE_RCLONE_TIMEOUT=, PROBE_HTTP_TIMEOUT=
EOF
  umask 022
  echo "wrote $ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# --- 2. plist -------------------------------------------------------------------
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s|@@REPO@@|$REPO|g" -e "s|@@HOME@@|$HOME|g" "$PLIST_SRC" > "$PLIST_DST.tmp"
/usr/bin/plutil -lint "$PLIST_DST.tmp" >/dev/null
mv "$PLIST_DST.tmp" "$PLIST_DST"
echo "installed $PLIST_DST"

# --- 3. (re)load ----------------------------------------------------------------
UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"
launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -E 'state|program|run interval' | sed 's/^/  /' || true
echo "loaded $LABEL (hourly + RunAtLoad — first real run is happening now in the background)"

# --- 4. dry run -----------------------------------------------------------------
echo; echo "=== dry run (nothing sent) ==="
HOPPER_DASHBOARD_ENV="$ENV_FILE" /usr/bin/python3 "$REPO/probes/mac_probe.py" --dry-run || echo "dry run exited $? — see $HOME/Library/Logs/hopper-dashboard-probe.log"
echo
echo "log:   $HOME/Library/Logs/hopper-dashboard-probe.log"
echo "check: launchctl list | grep $LABEL   (second column = last exit code, 0 is good)"
