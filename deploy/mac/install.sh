#!/bin/bash
# Install the hopper-dashboard Mac launchd agents. Idempotent.
#
#   deploy/mac/install.sh [--url http://<box-tailscale-ip>:8081] [--inbox]
#
# Two agents:
#   com.hopper.dashboard-probe     hourly  — the metrics probe (always installed)
#   com.hopper.inbox-transcribe    5 min   — the Inbox transcription worker (--inbox only)
#
# --inbox additionally prompts for INBOX_URL (the PUBLIC dashboard host) and INBOX_TOKEN
# (hidden, same rule as every other token here) and appends them to the env file if absent.
# It is opt-in because the transcription worker needs mlx-whisper in a venv on this Mac; the
# probe needs nothing but the stock python3.
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
INBOX_LABEL="com.hopper.inbox-transcribe"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CONF_DIR="$HOME/.config/hopper-dashboard"
ENV_FILE="$CONF_DIR/env"
PLIST_SRC="$HERE/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
URL=""; TOKEN=""; WANT_INBOX=""; INBOX_URL=""; INBOX_TOKEN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)   URL="$2"; shift 2 ;;
    --inbox) WANT_INBOX=1; shift ;;
    --inbox-url) INBOX_URL="$2"; WANT_INBOX=1; shift 2 ;;
    --token|--inbox-token) echo "ERROR: $1 is not accepted (it would leak into shell history / ps); the script prompts with a hidden read" >&2; exit 2 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
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

# --- 1b. Inbox keys (opt-in) ----------------------------------------------------
# Appended to the SAME env file, never a second one: the worker and the probe read one
# config. Existing keys are left alone, so a re-run with --inbox is a no-op.
if [[ -n "$WANT_INBOX" ]] && ! grep -q '^INBOX_TOKEN=' "$ENV_FILE"; then
  if [[ -z "$INBOX_URL" ]]; then
    read -r -p "INBOX_URL (the PUBLIC dashboard host, e.g. https://dashboard.example.com): " INBOX_URL
  fi
  [[ "$INBOX_URL" =~ ^https?:// ]] || { echo "ERROR: INBOX_URL must start with http:// or https://"; exit 2; }
  # Hidden read, same rule as INGEST_TOKEN: a token on the command line lands in shell
  # history and in `ps`, and this one grants write access to the Inbox.
  read -r -s -p "INBOX_TOKEN (from the box's ~/hopper-dashboard/.env; input hidden): " INBOX_TOKEN; echo
  [[ -n "$INBOX_TOKEN" ]] || { echo "ERROR: empty token"; exit 2; }
  umask 077
  cat >> "$ENV_FILE" <<EOF

# --- Inbox (com.hopper.inbox-transcribe + the inbox-backlog sub-probe) ---
# INBOX_URL is the PUBLIC host and INBOX_TOKEN a DIFFERENT credential from INGEST_TOKEN
# above, which is for the Tailscale-only ingest port. Both are needed: the worker pulls
# and posts over the public host, and heartbeats over Tailscale.
INBOX_URL=$INBOX_URL
INBOX_TOKEN=$INBOX_TOKEN
# mlx-whisper is not installable under /usr/bin/python3; point this at a venv that has it.
# (Today it borrows another repo's venv — a dedicated one is the cleaner long-term answer:
#  python3 -m venv ~/.local/venvs/whisper && ~/.local/venvs/whisper/bin/pip install mlx-whisper)
INBOX_WHISPER_PYTHON=$HOME/code/jjho-fan-almanac/.venv/bin/python
INBOX_WHISPER_MODEL=mlx-community/whisper-large-v3-turbo
INBOX_BACKLOG_FILE=$HOME/personal-assistant/backlog.txt
EOF
  umask 022
  chmod 600 "$ENV_FILE"
  echo "appended the Inbox keys to $ENV_FILE"
elif [[ -n "$WANT_INBOX" ]]; then
  echo "Inbox keys already present in $ENV_FILE, leaving them alone"
fi

# --- 2. plists ------------------------------------------------------------------
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s|@@REPO@@|$REPO|g" -e "s|@@HOME@@|$HOME|g" "$PLIST_SRC" > "$PLIST_DST.tmp"
# ⚠️ plutil is NOT this check. A literal @@FOO@@ is a perfectly VALID plist string, so
# `plutil -lint` passes and launchd only fails later, at exec — silently, every hour. That is
# exactly how the box container heartbeat died on 2026-09-12. Same guard as deploy/box/install.sh.
# (Written as if/then, not `grep -q && {...}`, so a NON-match cannot trip `set -e`.)
if grep -q '@@' "$PLIST_DST.tmp"; then
  echo "ERROR: unrendered placeholder in $PLIST_DST.tmp — launchd would fail at exec, not at load"
  grep -o '@@[A-Z_]*@@' "$PLIST_DST.tmp" | sort -u
  rm -f "$PLIST_DST.tmp"; exit 1
fi
/usr/bin/plutil -lint "$PLIST_DST.tmp" >/dev/null
mv "$PLIST_DST.tmp" "$PLIST_DST"
echo "installed $PLIST_DST"

if [[ -n "$WANT_INBOX" ]]; then
  INBOX_PLIST_DST="$HOME/Library/LaunchAgents/$INBOX_LABEL.plist"
  sed -e "s|@@REPO@@|$REPO|g" -e "s|@@HOME@@|$HOME|g" "$HERE/$INBOX_LABEL.plist" > "$INBOX_PLIST_DST.tmp"
  if grep -q '@@' "$INBOX_PLIST_DST.tmp"; then           # see the note above: plutil misses this
    echo "ERROR: unrendered placeholder in $INBOX_PLIST_DST.tmp — the agent would load and then fail at exec"
    grep -o '@@[A-Z_]*@@' "$INBOX_PLIST_DST.tmp" | sort -u
    rm -f "$INBOX_PLIST_DST.tmp"; exit 1
  fi
  /usr/bin/plutil -lint "$INBOX_PLIST_DST.tmp" >/dev/null
  # The PATH injection is what makes mlx-whisper able to find ffmpeg under launchd. Without
  # it every transcription fails inside load_audio in a way that reads like bad audio.
  grep -q '/opt/homebrew/bin' "$INBOX_PLIST_DST.tmp" || { echo "ERROR: the transcribe plist lost its PATH injection — mlx-whisper will not find ffmpeg under launchd"; rm -f "$INBOX_PLIST_DST.tmp"; exit 1; }
  mv "$INBOX_PLIST_DST.tmp" "$INBOX_PLIST_DST"
  echo "installed $INBOX_PLIST_DST"
  WP="$(grep -m1 '^INBOX_WHISPER_PYTHON=' "$ENV_FILE" | cut -d= -f2- || true)"
  [[ -x "$WP" ]] || echo "WARN: INBOX_WHISPER_PYTHON ($WP) is not executable — transcription will report a clear environment error until it is"
  [[ -x /opt/homebrew/bin/ffmpeg || -x /usr/local/bin/ffmpeg ]] || echo "WARN: ffmpeg not found — mlx-whisper cannot decode audio without it (brew install ffmpeg)"
fi

# --- 3. (re)load ----------------------------------------------------------------
UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"
launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -E 'state|program|run interval' | sed 's/^/  /' || true
echo "loaded $LABEL (hourly + RunAtLoad — first real run is happening now in the background)"
if [[ -n "$WANT_INBOX" ]]; then
  launchctl bootout "gui/$UID_NUM/$INBOX_LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$HOME/Library/LaunchAgents/$INBOX_LABEL.plist"
  echo "loaded $INBOX_LABEL (every 5 min + RunAtLoad)"
fi

# --- 4. dry run -----------------------------------------------------------------
echo; echo "=== dry run (nothing sent) ==="
HOPPER_DASHBOARD_ENV="$ENV_FILE" /usr/bin/python3 "$REPO/probes/mac_probe.py" --dry-run || echo "dry run exited $? — see $HOME/Library/Logs/hopper-dashboard-probe.log"
if [[ -n "$WANT_INBOX" ]]; then
  echo; echo "=== transcription worker dry run (fetches the queue, posts nothing) ==="
  HOPPER_DASHBOARD_ENV="$ENV_FILE" /usr/bin/python3 "$REPO/probes/inbox_transcribe.py" --dry-run \
    || echo "dry run exited $? — see $HOME/Library/Logs/hopper-inbox-transcribe.log"
fi
echo
echo "log:   $HOME/Library/Logs/hopper-dashboard-probe.log"
echo "check: launchctl list | grep $LABEL   (second column = last exit code, 0 is good)"
if [[ -n "$WANT_INBOX" ]]; then
  echo "log:   $HOME/Library/Logs/hopper-inbox-transcribe.log"
  echo "check: launchctl list | grep $INBOX_LABEL"
fi
