#!/bin/bash
# Remove the hopper-dashboard Mac probe launchd agent. Idempotent.
#   deploy/mac/uninstall.sh [--purge]    # --purge also deletes ~/.config/hopper-dashboard (token + state)
# Logs in ~/Library/Logs are left in place either way.
set -euo pipefail
LABEL="com.hopper.dashboard-probe"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
PURGE=""; [[ "${1:-}" == "--purge" ]] && PURGE=1

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "unloaded $LABEL" || echo "$LABEL was not loaded"
if [[ -f "$PLIST_DST" ]]; then rm -f "$PLIST_DST"; echo "removed $PLIST_DST"; fi
if [[ -n "$PURGE" ]]; then
  rm -rf "$HOME/.config/hopper-dashboard"; echo "removed ~/.config/hopper-dashboard (env + state)"
else
  echo "kept ~/.config/hopper-dashboard (pass --purge to remove the token + state)"
fi
