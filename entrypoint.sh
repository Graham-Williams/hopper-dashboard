#!/bin/sh
# Container entrypoint: stage the rclone config, drop to the unprivileged
# `dashboard` user, then run the two gunicorn processes (read :8080, ingest
# :8081) and exit if either dies so compose's restart policy brings the whole
# container back.
#
# Privilege model: the image has no USER directive, so PID 1 starts as root
# ONLY long enough to copy the host's rclone.conf. The host file is 0600 and
# owned by the host login (uid 1000 on the box); the app runs as uid 10001, so
# a non-root copy would fail with EACCES and crash-loop (seen on the real box).
# Root copies it into a 0700 dir under /tmp (tmpfs), chowns it to `dashboard`,
# then re-execs THIS script via setpriv as dashboard:dashboard with the
# supplementary groups reset. Nothing else ever runs as root, and the host
# file is never written to.
set -eu

RCLONE_CONFIG="${RCLONE_CONFIG:-/tmp/rclone/rclone.conf}"
export RCLONE_CONFIG
RCLONE_SRC="${RCLONE_SRC:-/rclone/rclone.conf}"
APP_USER="${APP_USER:-dashboard}"

stage_rclone_conf() {
  # rclone rewrites its config on token refresh, so the host's rclone.conf is
  # bind-mounted :ro and copied to a writable tmpfs path. Never written back.
  dir="$(dirname "$RCLONE_CONFIG")"
  mkdir -p -m 0700 "$dir"
  cp "$RCLONE_SRC" "$RCLONE_CONFIG"
  chmod 0600 "$RCLONE_CONFIG"
}

if [ "$(id -u)" = "0" ]; then
  if [ -f "$RCLONE_SRC" ]; then
    stage_rclone_conf
    chown -R "$APP_USER:$APP_USER" "$(dirname "$RCLONE_CONFIG")"
  else
    echo "entrypoint: no $RCLONE_SRC mounted — destination probes will fail" >&2
  fi
  # Drop privileges for good: new uid/gid, supplementary groups from the passwd
  # entry (none for `dashboard`), no way back to root. setpriv ships in the
  # base image (util-linux).
  exec setpriv --reuid="$APP_USER" --regid="$APP_USER" --init-groups \
       --no-new-privs "$0" "$@"
fi

# ---- from here on we are the unprivileged app user --------------------------
if [ -r "$RCLONE_CONFIG" ]; then
  : # staged by the root phase above (or by a previous run)
elif [ -r "$RCLONE_SRC" ]; then
  stage_rclone_conf   # started non-root with a readable mount (local dev / CI --user)
else
  echo "entrypoint: no readable rclone config at $RCLONE_CONFIG or $RCLONE_SRC — destination probes will fail" >&2
fi

READ_WORKERS="${READ_WORKERS:-2}"
READ_THREADS="${READ_THREADS:-4}"

# Ingest MUST be a single worker: it owns the scheduler thread and the writes.
gunicorn --workers 1 --threads 4 --bind 0.0.0.0:8081 --timeout 60 \
  --worker-tmp-dir /tmp --access-logfile - --access-logformat '%(h)s "%(r)s" %(s)s' \
  "dashboard:create_app(role='ingest')" &
INGEST_PID=$!

gunicorn --workers "$READ_WORKERS" --threads "$READ_THREADS" --bind 0.0.0.0:8080 --timeout 60 \
  --worker-tmp-dir /tmp --access-logfile - --access-logformat '%(h)s "%(r)s" %(s)s' \
  "dashboard:create_app(role='read')" &
READ_PID=$!

term() {
  kill -TERM "$INGEST_PID" "$READ_PID" 2>/dev/null || true
  wait "$INGEST_PID" "$READ_PID" 2>/dev/null || true
  exit 0
}
trap term TERM INT

# POSIX sh has no `wait -n`; poll until either process exits, then take the
# other down so the container restarts as a unit.
while kill -0 "$INGEST_PID" 2>/dev/null && kill -0 "$READ_PID" 2>/dev/null; do
  sleep 2 &
  wait $! || true
done
echo "entrypoint: a gunicorn process exited; stopping the other" >&2
kill -TERM "$INGEST_PID" "$READ_PID" 2>/dev/null || true
wait "$INGEST_PID" "$READ_PID" 2>/dev/null || true
exit 1
