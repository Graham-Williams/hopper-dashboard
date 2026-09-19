#!/bin/bash
# Remove the hopper-dashboard Mac launchd agents. Idempotent.
#   deploy/mac/uninstall.sh [--purge]    # --purge also deletes ~/.config/hopper-dashboard (tokens + state)
#   deploy/mac/uninstall.sh --inbox-only # remove ONLY the transcription worker, keep the probe
# Logs in ~/Library/Logs are left in place either way.
#
# Removing the transcription worker does NOT lose anything: the audio stays on the box and the
# items simply stay untranscribed until a worker runs again. It DOES mean the `inbox-transcribe`
# job stops heartbeating and goes LATE on the board after ~14 h, which is correct — nobody is
# running it.
set -euo pipefail
LABELS=(com.hopper.dashboard-probe com.hopper.inbox-transcribe)
PURGE=""
case "${1:-}" in
  --purge)      PURGE=1 ;;
  --inbox-only) LABELS=(com.hopper.inbox-transcribe) ;;
  "")           ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac

for label in "${LABELS[@]}"; do
  plist="$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null && echo "unloaded $label" || echo "$label was not loaded"
  if [[ -f "$plist" ]]; then rm -f "$plist"; echo "removed $plist"; fi
done

if [[ -n "$PURGE" ]]; then
  rm -rf "$HOME/.config/hopper-dashboard"; echo "removed ~/.config/hopper-dashboard (env + state)"
else
  echo "kept ~/.config/hopper-dashboard (pass --purge to remove the tokens + state)"
fi
