#!/bin/bash
# Install the hourly hopper-dashboard Mac probe as a launchd user agent. Idempotent.
#
#   deploy/mac/install.sh [--url http://100.101.1.28:8081] [--token <INGEST_TOKEN>]
#
# 1. writes ~/.config/hopper-dashboard/env (chmod 600) if it doesn't exist — prompts for the token
#    (silent read) unless --token is given; an existing file is never overwritten
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
URL="http://100.101.1.28:8081"; TOKEN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)   URL="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
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
  if [[ -z "$TOKEN" ]]; then
    read -r -s -p "INGEST_TOKEN (from the box's ~/hopper-dashboard/.env; input hidden): " TOKEN; echo
  fi
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
