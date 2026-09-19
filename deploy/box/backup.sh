#!/usr/bin/env bash
#
# backup.sh — off-box backups for hopper-dashboard. Driven by hopper-dashboard-backup.timer.
#
# WHY THIS EXISTS AT ALL. Until the Inbox, this app held nothing irreplaceable: every row in
# dashboard.db is a heartbeat or a probe reading that the next tick reproduces. The Inbox
# changed that — inbox.db holds Graham's own voice notes' transcripts and his triage state
# (what he has reviewed, what became which GitHub issue), and /app/data/inbox/audio holds the
# recordings themselves. There is no other copy of any of it. The dashboard exists to catch
# unbacked-up data; it must not be the last thing on the box without a backup.
#
# It is a deliberate near-copy of ~/km-tracker/scripts/backup.sh: same shape, same failure
# discipline, same restore procedure. Two things are worth keeping identical on purpose —
#
#   1. THE SNAPSHOT RUNS INSIDE THE CONTAINER. The live DBs are WAL-mode and their -wal/-shm
#      sidecars are owned by the container user (UID 10001). SQLite's online backup API must
#      WRITE those sidecars to take its read lock, so running it host-side fails with
#      "attempt to write a readonly database" — even opening mode=ro. So: snapshot + integrity
#      check via `docker exec`, then `docker cp` the finished file out.
#   2. rclone COPY, NEVER SYNC. copy only adds to the remote, so nothing that happens on the
#      box — a prune bug, a wiped volume, a bad restore — can delete the off-box copy.
#
# RESTORING IS NOT A `cp`. The stale -wal/-shm sidecars must be deleted and the file re-owned
# to 10001 first, or SQLite replays the old WAL over the restored image and silently hands
# back the PRE-restore data. See DEPLOY.md → "Restore from a snapshot".
#
# Config: deploy/box/.env.backup beside this script, or the environment. All optional.
set -euo pipefail

log() { printf '%s backup.sh: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env.backup"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a; source "${ENV_FILE}"; set +a
fi

CONTAINER="${BACKUP_CONTAINER:-hopper-dashboard}"
# Space-separated <name>:<path-inside-container> pairs. dashboard.db is the board's own
# history (re-derivable, but its state_changes are not); inbox.db is the irreplaceable half.
CONTAINER_DBS="${CONTAINER_DBS:-dashboard:/app/data/dashboard.db inbox:/app/data/inbox.db}"
CONTAINER_AUDIO_DIR="${CONTAINER_AUDIO_DIR:-/app/data/inbox/audio}"
BACKUP_ROOT="${BACKUP_ROOT:-${HOME}/hopper-dashboard-backups}"
LOCAL_BACKUP_DIR="${LOCAL_BACKUP_DIR:-${BACKUP_ROOT}/snapshots}"
AUDIO_MIRROR_DIR="${AUDIO_MIRROR_DIR:-${BACKUP_ROOT}/audio}"
STATE_DIR="${STATE_DIR:-${BACKUP_ROOT}/state}"
RCLONE_DEST="${RCLONE_DEST:-}"                        # e.g. gdrive:hopper-dashboard-backups
LOCAL_RETENTION="${LOCAL_RETENTION:-60}"
DRIVE_RETENTION="${DRIVE_RETENTION:-30}"
DAILY_RETENTION="${DAILY_RETENTION:-30}"
DRIVE_PUSH_INTERVAL_MIN="${DRIVE_PUSH_INTERVAL_MIN:-15}"
ALLOW_EMPTY_SNAPSHOT="${ALLOW_EMPTY_SNAPSHOT:-0}"
BACKUP_AUDIO="${BACKUP_AUDIO:-1}"

mkdir -p "${LOCAL_BACKUP_DIR}" "${STATE_DIR}"
command -v docker >/dev/null 2>&1 || die "docker not on PATH (the snapshot runs inside the container)"
[[ "$(docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || echo false)" == "true" ]] \
  || die "container ${CONTAINER} is not running — cannot snapshot a WAL DB from the host (see the header)"

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

# --- one DB: snapshot inside the container, verify, dedupe, keep ---------------------
# Sets SNAPSHOT_PATH / SNAPSHOT_CKSUM / SNAPSHOT_CHANGED for the caller.
snapshot_db() {
  local name="$1" src="$2" tmp ctmp prev rc=0
  tmp="$(mktemp "${LOCAL_BACKUP_DIR}/.snapshot.XXXXXX.db")"
  ctmp=""
  # Armed BEFORE the in-container mktemp so an early failure still reaps the host temp —
  # it is a dotfile, which the <name>_*.db prune glob never sees. The suffix glob catches
  # the snapshot's own -wal/-shm, created when the verification step below opens it.
  cleanup_db() {
    rm -f "${tmp}" "${tmp}"-*
    [[ -n "${ctmp}" ]] && docker exec "${CONTAINER}" rm -f "${ctmp}" >/dev/null 2>&1 || true
  }
  trap cleanup_db RETURN

  ctmp="$(docker exec "${CONTAINER}" mktemp "/tmp/${name}_snap.XXXXXX.db")" \
    || die "could not create a temp path inside ${CONTAINER}"
  # Paths go in via -e, never interpolated into the python source.
  docker exec -i -e SRC="${src}" -e DST="${ctmp}" "${CONTAINER}" python3 - <<'PY' || return 2
import os, sqlite3, sys
src, dst = os.environ["SRC"], os.environ["DST"]
# The host-side precondition checks a HOST path; this is the path actually read. Without
# this guard sqlite3.connect() would CREATE the missing file and .backup() would faithfully
# copy an empty DB — a snapshot that passes integrity_check and rotates every good copy out.
if not os.path.isfile(src):
    sys.stderr.write("source DB not found inside the container at %s\n" % src)
    sys.exit(1)
s = sqlite3.connect(src)
try:
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
finally:
    s.close()
c = sqlite3.connect(dst)
try:
    ok = c.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    c.close()
if ok != "ok":
    sys.stderr.write("integrity_check failed: %s\n" % ok)
    sys.exit(1)
PY
  docker cp "${CONTAINER}:${ctmp}" "${tmp}" || die "docker cp of the ${name} snapshot failed"
  docker exec "${CONTAINER}" rm -f "${ctmp}" >/dev/null 2>&1 && ctmp="" || true

  # Re-verify the HOST copy — the file we actually keep and push. A truncated `docker cp`
  # would otherwise ship to Drive undetected, since everything downstream only sha256s this.
  prev="$(ls -1 "${LOCAL_BACKUP_DIR}/${name}"_*.db 2>/dev/null | sort | tail -n1 || true)"
  python3 "${SCRIPT_DIR}/verify_snapshot.py" "${tmp}" "${prev}" "${ALLOW_EMPTY_SNAPSHOT}" || rc=$?
  if (( rc == 3 )); then
    die "refusing the ${name} snapshot: every table is empty while ${prev##*/} has data — the live DB looks wiped. Investigate before the good copies rotate out; set ALLOW_EMPTY_SNAPSHOT=1 if it really was emptied on purpose"
  elif (( rc != 0 )); then
    die "${name} snapshot failed verification after copy out of the container"
  fi

  SNAPSHOT_CKSUM="$(sha256_of "${tmp}")"
  local last="" ck_file="${STATE_DIR}/last_local_${name}.sha256"
  [[ -f "${ck_file}" ]] && last="$(cat "${ck_file}")"
  if [[ "${SNAPSHOT_CKSUM}" == "${last}" && -n "${prev}" && -f "${prev}" ]]; then
    log "${name}: unchanged since ${prev##*/} (sha ${SNAPSHOT_CKSUM:0:12}); keeping one copy"
    SNAPSHOT_PATH="${prev}"; SNAPSHOT_CHANGED=0
  else
    local ts dest n=1
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    dest="${LOCAL_BACKUP_DIR}/${name}_${ts}.db"
    # 1-second resolution: the timer and a manual run in the same second would collide and
    # `mv` would destroy the first. "_N" still sorts after the bare name ("_" > ".").
    while [[ -e "${dest}" ]]; do dest="${LOCAL_BACKUP_DIR}/${name}_${ts}_${n}.db"; n=$((n+1)); done
    mv "${tmp}" "${dest}"
    printf '%s\n' "${SNAPSHOT_CKSUM}" > "${ck_file}"
    SNAPSHOT_PATH="${dest}"; SNAPSHOT_CHANGED=1
    log "${name}: saved ${dest##*/} (sha ${SNAPSHOT_CKSUM:0:12})"
  fi

  local snaps=()
  while IFS= read -r f; do snaps+=("$f"); done \
    < <(ls -1 "${LOCAL_BACKUP_DIR}/${name}"_*.db 2>/dev/null | sort -r || true)
  if (( ${#snaps[@]} > LOCAL_RETENTION )); then
    for old in "${snaps[@]:LOCAL_RETENTION}"; do rm -f "${old}"; log "${name}: pruned ${old##*/}"; done
  fi
}

# --- Drive (throttled, decoupled from the local snapshot) ---------------------------
# rc 0 = pushed or intentionally skipped; rc 1 = a CONFIGURED remote actually errored.
# An unconfigured rclone is a WARN, not a failure: the local snapshot is already safe and
# failing the unit every 5 minutes during setup teaches everyone to ignore it.
rclone_ready() {
  [[ -n "${RCLONE_DEST}" ]] || { log "WARN: RCLONE_DEST not set — local snapshots only"; return 1; }
  command -v rclone >/dev/null 2>&1 || { log "WARN: rclone not installed — local snapshots only"; return 1; }
  rclone listremotes 2>/dev/null | grep -qx "${RCLONE_DEST%%:*}:" \
    || { log "WARN: rclone remote '${RCLONE_DEST%%:*}:' not configured — local snapshots only"; return 1; }
}

prune_remote() {  # <dir> <glob> <keep>
  local files=() f
  while IFS= read -r f; do files+=("$f"); done \
    < <(rclone lsf "$1" --files-only --include "$2" 2>/dev/null | sort -r || true)
  # --files-only is load-bearing: without it `lsf` also lists the daily/ SUBDIR, which
  # reverse-sorts last and lands in the delete slice on every run once the listing exceeds
  # the retention count (harmless, but it logs an rclone ERROR on every push for ever).
  if (( ${#files[@]} > $3 )); then
    for old in "${files[@]:$3}"; do
      rclone deletefile "$1/${old}" && log "pruned ${1##*/}/${old}" || log "WARN: could not prune ${old}"
    done
  fi
}

push_db() {  # <name> <snapshot path> <checksum>
  local name="$1" path="$2" cksum="$3" ck_file="${STATE_DIR}/last_drive_${1}.sha256" last=""
  [[ -f "${ck_file}" ]] && last="$(cat "${ck_file}")"
  if [[ "${cksum}" == "${last}" ]]; then
    log "${name}: Drive already has this DB (sha ${cksum:0:12})"; return 0
  fi
  log "${name}: pushing ${path##*/} to ${RCLONE_DEST}"
  rclone copy "${path}" "${RCLONE_DEST}" || { log "ERROR: rclone copy failed for ${name}"; return 1; }
  prune_remote "${RCLONE_DEST}" "${name}_*.db" "${DRIVE_RETENTION}"
  # Daily long-tail tier: the ring above can rotate out within hours, so a logical corruption
  # noticed a day later would have no clean copy left. At most one file per UTC day.
  local today; today="$(date -u +%Y%m%d)"
  if [[ -z "$(rclone lsf "${RCLONE_DEST}/daily" --files-only --include "${name}_${today}T*.db" 2>/dev/null | head -n1 || true)" ]]; then
    rclone copy "${path}" "${RCLONE_DEST}/daily" || { log "ERROR: rclone copy to daily/ failed"; return 1; }
    prune_remote "${RCLONE_DEST}/daily" "${name}_*.db" "${DAILY_RETENTION}"
    log "${name}: added today's daily snapshot"
  fi
  printf '%s\n' "${cksum}" > "${ck_file}"
}

# The audio tree is NOT in the DB (files on disk, by design — blobs would make every snapshot
# byte-unique and defeat the sha256 dedupe above). It gets its own additive copy in TWO hops,
# and both hops are additive on purpose: `docker cp` of the directory CONTENTS only adds and
# overwrites, and `rclone copy` only adds to the remote. So the app's 90-day audio prune —
# or a bug in it — can never remove a recording from either the host mirror or Drive.
push_audio() {
  [[ "${BACKUP_AUDIO}" == "1" ]] || return 0
  docker exec "${CONTAINER}" test -d "${CONTAINER_AUDIO_DIR}" 2>/dev/null \
    || { log "audio: ${CONTAINER_AUDIO_DIR} not present yet — nothing to copy"; return 0; }
  mkdir -p "${AUDIO_MIRROR_DIR}"
  docker cp "${CONTAINER}:${CONTAINER_AUDIO_DIR}/." "${AUDIO_MIRROR_DIR}/" \
    || { log "ERROR: docker cp of the audio tree failed"; return 1; }
  local n; n="$(find "${AUDIO_MIRROR_DIR}" -type f | wc -l | tr -d ' ')"
  rclone copy "${AUDIO_MIRROR_DIR}" "${RCLONE_DEST}/audio" \
    || { log "ERROR: rclone copy of the audio tree failed"; return 1; }
  log "audio: ${n} file(s) mirrored to ${RCLONE_DEST}/audio"
}

# --- run -----------------------------------------------------------------------------
declare -a PUSHABLE=()
SNAPSHOTS=0
for pair in ${CONTAINER_DBS}; do
  name="${pair%%:*}"; src="${pair#*:}"
  SNAPSHOT_PATH=""; SNAPSHOT_CKSUM=""; SNAPSHOT_CHANGED=0
  rc=0; snapshot_db "${name}" "${src}" || rc=$?
  if (( rc == 2 )); then
    # A DB that is not there yet (the Inbox has never been opened on a fresh volume) must not
    # fail the unit — but a run where NONE of them produced a snapshot must.
    log "WARN: ${name} (${src}) could not be snapshotted — skipping it this run"
    continue
  fi
  SNAPSHOTS=$((SNAPSHOTS+1))
  PUSHABLE+=("${name}|${SNAPSHOT_PATH}|${SNAPSHOT_CKSUM}")
done
(( SNAPSHOTS > 0 )) || die "no DB could be snapshotted (checked: ${CONTAINER_DBS})"

PUSH_RC=0
if rclone_ready; then
  NOW="$(date +%s)"; LAST=0
  [[ -f "${STATE_DIR}/last_drive_push.epoch" ]] && LAST="$(cat "${STATE_DIR}/last_drive_push.epoch")"
  if (( (NOW - LAST) / 60 < DRIVE_PUSH_INTERVAL_MIN )); then
    log "last Drive push was $(( (NOW - LAST) / 60 ))min ago (< ${DRIVE_PUSH_INTERVAL_MIN}min); skipping"
  else
    for entry in "${PUSHABLE[@]}"; do
      IFS='|' read -r name path cksum <<<"${entry}"
      push_db "${name}" "${path}" "${cksum}" || PUSH_RC=1
    done
    push_audio || PUSH_RC=1
    (( PUSH_RC == 0 )) && printf '%s\n' "${NOW}" > "${STATE_DIR}/last_drive_push.epoch"
  fi
fi

if (( PUSH_RC != 0 )); then
  log "Drive push did not complete (rc=${PUSH_RC}); local snapshots are unaffected"
  exit "${PUSH_RC}"
fi
log "done (${SNAPSHOTS} DB snapshot(s))"
