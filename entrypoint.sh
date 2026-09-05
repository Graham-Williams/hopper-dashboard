#!/bin/sh
# Container entrypoint: stage the rclone config, then run the two gunicorn
# processes (read :8080, ingest :8081) and exit if either dies so compose's
# restart policy brings the whole container back.
set -eu

# rclone rewrites its config on token refresh, so the host's rclone.conf is
# bind-mounted :ro at /rclone/rclone.conf and copied to a writable tmpfs path.
# It is never written back to the host.
RCLONE_CONFIG="${RCLONE_CONFIG:-/tmp/rclone/rclone.conf}"
export RCLONE_CONFIG
if [ -f /rclone/rclone.conf ]; then
  mkdir -p "$(dirname "$RCLONE_CONFIG")"
  cp /rclone/rclone.conf "$RCLONE_CONFIG"
  chmod 0600 "$RCLONE_CONFIG"
else
  echo "entrypoint: no /rclone/rclone.conf mounted — destination probes will fail" >&2
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
