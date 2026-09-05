#!/bin/bash
# ping.sh — post a one-off heartbeat to hopper-dashboard from any script or from Hopper by hand.
#
#   probes/ping.sh <job_id> <ok|fail|skipped> [note]
#
# Optional environment:
#   REASON=pushed|skipped-unchanged|error|…   EXIT_CODE=<int>   STARTED_AT=<iso8601>
#   METRICS_JSON='{"files": 3}'  (must already be a JSON object)   DRY_RUN=1 (print, don't send)
#   HOPPER_DASHBOARD_ENV=<path>  (default ~/.config/hopper-dashboard/env; on the box:
#                                 /etc/hopper-dashboard/ingest.env — pass it explicitly)
#
# Intended for the manual jobs with nothing to compute on a timer — taste-twin-publish,
# jjho-refresh, baby-pool-sync — call it as the LAST step of the manual run, e.g.
#   python scripts/publish.py mhgaillo && ~/code/hopper-dashboard/probes/ping.sh taste-twin-publish ok "mhgaillo"
#   ... || ~/code/hopper-dashboard/probes/ping.sh taste-twin-publish fail "publish.py exited $?"
#
# Reads DASHBOARD_URL + INGEST_TOKEN from the env file (KEY=VALUE, chmod 600). Never echoes the token.
# Exit codes: 0 sent, 2 usage/config error, 3 curl/HTTP failure.
set -u

ENV_FILE="${HOPPER_DASHBOARD_ENV:-$HOME/.config/hopper-dashboard/env}"
JOB_ID="${1:-}"; STATUS="${2:-}"; NOTE="${3:-}"

usage() { echo "usage: $0 <job_id> <ok|fail|skipped> [note]" >&2; exit 2; }
[[ -n "$JOB_ID" && -n "$STATUS" ]] || usage
[[ "$JOB_ID" =~ ^[a-z0-9-]+$ ]] || { echo "ERROR: job_id must match [a-z0-9-]+" >&2; exit 2; }
case "$STATUS" in ok|fail|skipped) ;; *) echo "ERROR: status must be ok|fail|skipped" >&2; exit 2 ;; esac

if [[ -r "$ENV_FILE" ]]; then
  # plain KEY=VALUE parsing — no `source`, so a stray command in the file can't execute
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    line="${line#export }"
    key="${line%%=*}"; val="${line#*=}"
    val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
    case "$key" in DASHBOARD_URL|INGEST_TOKEN) [[ -z "${!key:-}" ]] && printf -v "$key" '%s' "$val" ;; esac
  done < "$ENV_FILE"
fi
DASHBOARD_URL="${DASHBOARD_URL:-http://100.101.1.28:8081}"
INGEST_TOKEN="${INGEST_TOKEN:-}"
[[ -n "$INGEST_TOKEN" || -n "${DRY_RUN:-}" ]] || { echo "ERROR: INGEST_TOKEN not set (env file: $ENV_FILE)" >&2; exit 2; }

# JSON-escape a string: backslash, double quote, control chars → spaces. Truncate note to 500.
json_str() {
  local s="$1"
  s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
  s="$(printf '%s' "$s" | tr '\n\r\t' '   ')"
  printf '%s' "$s"
}
NOTE="${NOTE:0:500}"
FINISHED_AT="$(date +%Y-%m-%dT%H:%M:%S%z | sed -E 's/([0-9]{2})([0-9]{2})$/\1:\2/')"

BODY="{\"status\":\"$STATUS\",\"finished_at\":\"$FINISHED_AT\""
[[ -n "${STARTED_AT:-}" ]] && BODY+=",\"started_at\":\"$(json_str "$STARTED_AT")\""
[[ -n "${REASON:-}" ]]     && BODY+=",\"reason\":\"$(json_str "$REASON")\""
[[ -n "${EXIT_CODE:-}" && "${EXIT_CODE}" =~ ^-?[0-9]+$ ]] && BODY+=",\"exit_code\":${EXIT_CODE}"
[[ -n "$NOTE" ]]           && BODY+=",\"note\":\"$(json_str "$NOTE")\""
[[ -n "${METRICS_JSON:-}" ]] && BODY+=",\"metrics\":${METRICS_JSON}"
BODY+="}"

URL="${DASHBOARD_URL%/}/api/v1/ping/$JOB_ID"
if [[ -n "${DRY_RUN:-}" ]]; then
  echo "DRY-RUN POST $URL"; echo "  $BODY"; exit 0
fi

# -f: non-2xx → exit 22; -sS: quiet but show errors; -m 10 + --retry 2 bound the wait.
if out="$(/usr/bin/curl -fsS -m 10 --retry 2 -X POST \
      -H "Authorization: Bearer ${INGEST_TOKEN}" -H 'Content-Type: application/json' \
      --data "$BODY" "$URL" 2>&1)"; then
  echo "sent $JOB_ID $STATUS → $out"
else
  echo "ERROR: ping $JOB_ID failed: $out" >&2
  exit 3
fi
