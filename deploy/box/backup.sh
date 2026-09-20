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
#   2. THE DATABASES ARE ADDITIVE. Their snapshots go up with `rclone copy`, so nothing that
#      happens on the box — a prune bug, a wiped volume, a bad restore — can delete an
#      off-box DB snapshot. (The ring and the daily/ tier DO delete, deliberately and by
#      retention count, via prune_remote; "copy never deletes" describes the upload, not the
#      whole script.)
#
# ⚠️ THE AUDIO TREE IS THE EXCEPTION, AND IT IS DELIBERATE. Graham's decision 2026-09-19: the
# recordings MIRROR the container, deletions included, because an additive audio backup
# silently defeats the Inbox's own Delete control and its 180-day privacy ceiling — the point
# of Delete is that a password read aloud stops existing. Guarded by three independent
# refuse-a-mass-deletion brakes (proportional, absolute, windowed) measured against what the
# REMOTE actually holds; see push_audio. The two DATABASES are untouched and stay additive.
#
# RESTORING IS NOT A `cp`. The stale -wal/-shm sidecars must be deleted and the file re-owned
# to 10001 first, or SQLite replays the old WAL over the restored image and silently hands
# back the PRE-restore data. See DEPLOY.md → "Restore from a snapshot".
#
# Config: deploy/box/.env.backup beside this script, or the environment. All optional.
set -euo pipefail
# Pin the locale to C so every sort/glob/comparison below is byte-ordered and deterministic
# regardless of the caller's environment. LOAD-BEARING for snapshot filename ordering:
# systemd's manager environment on the box carries LANG=en_US.UTF-8 and the unit sets no
# locale of its own, and UTF-8 collation IGNORES PUNCTUATION, which inverts the "_N"
# collision-suffix ordering the prunes below rely on (measured: C sorts "...Z.db" before
# "...Z_1.db"; en_US.UTF-8 sorts them the other way round). Unpinned, two runs landing in the
# same second can make the prune delete the NEWEST snapshot instead of the oldest. Everything
# here is ASCII — timestamps are numeric `date` formats, all messages are English — so
# nothing else is affected. Same rule, same reason, as ~/km-tracker/scripts/backup.sh.
export LC_ALL=C

log() { printf '%s backup.sh: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

# Octal permission bits of a file (Linux `stat -c` primary; macOS `stat -f` fallback so the
# script stays testable off-box). Echoes e.g. "600"; non-zero rc if neither form works.
perms_of() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null
}

# True if the file is writable by group or other — a tamper risk for a file we `source`.
# Returns 2 if the mode could not be determined.
is_group_or_other_writable() {
  local mode perms group_digit other_digit
  mode="$(perms_of "$1")" || return 2
  perms="${mode: -3}"                 # last 3 octal digits (owner/group/other)
  group_digit="${perms:1:1}"
  other_digit="${perms:2:1}"
  (( (group_digit & 2) || (other_digit & 2) ))
}

# Assert a config value is a positive integer (>= 1). A non-numeric or zero retention
# arithmetic-evaluates to 0, which makes the prune slices below cover the WHOLE array and
# delete EVERY snapshot — locally and, via prune_remote, on Drive too. `${VAR:-60}` is no
# defence: "0" and " " are both non-empty. Fail loudly instead.
require_positive_int() {
  local name="$1" val="$2"
  [[ "${val}" =~ ^[0-9]{1,9}$ ]] || die "${name}='${val}' is not an integer (must be 1..999999999)"
  (( 10#${val} >= 1 )) || die "${name}='${val}' must be >= 1"
}

# A PERCENTAGE is not "a positive integer". require_positive_int happily accepts 100 (which
# makes the drop test `count * 100 < prev * 0` — never true, so the brake is silently OFF on
# every run), 200 (which makes the right-hand side NEGATIVE — the brake is off AND inverted),
# and a 20-digit number (which overflows the arithmetic). It also lets "050" through, and
# bash reads a leading zero as OCTAL, so "050" would quietly become 40.
#
# So: 1..99 only, base 10 forced, and the value normalised in place by the caller. 100 is
# refused loudly rather than treated as "no brake" — a config that disables the one guard
# standing between this script and Graham's only copy of his voice must be a typo until
# proven otherwise, and BACKUP_AUDIO=0 is the honest way to opt out of the mirror entirely.
require_percent() {
  local name="$1" val="$2" v
  [[ "${val}" =~ ^[0-9]{1,3}$ ]] || die "${name}='${val}' is not a 1-3 digit percentage"
  v=$((10#${val}))
  (( v >= 1 && v <= 99 )) || die "${name}='${val}' must be between 1 and 99 (100 would disable the drop guard entirely — use BACKUP_AUDIO=0 if you really want no audio mirror; 0 would refuse every run)"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env.backup"
if [[ -f "${ENV_FILE}" ]]; then
  # `source` EXECUTES this file as shell, every five minutes, as a user in the `docker` group
  # (root-equivalent on this box). If anyone but the owner can write it, they can run commands
  # as that user — refuse rather than inherit that. Same guard as km-tracker's backup.sh.
  if is_group_or_other_writable "${ENV_FILE}"; then
    die "${ENV_FILE} is group/other-writable — refusing to source it (run: chmod 600 ${ENV_FILE})"
  elif (( $? == 2 )); then
    log "WARN: could not determine the permissions of ${ENV_FILE}; sourcing anyway"
  fi
  set -a
  # ENV_FILE is runtime config, so its contents are not knowable statically.
  # shellcheck source=/dev/null
  source "${ENV_FILE}"
  set +a
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
# --- the audio brakes. THREE of them, and each can refuse on its own -------------------
#
# The audio tree MIRRORS deletions (see push_audio), so these are the only thing standing
# between a bug and the sole copy of Graham's recordings. They are deliberately independent:
#
#   PROPORTIONAL (AUDIO_MAX_DROP_PCT) — refuse when the count falls by more than this share.
#     Catches the wiped volume, the wrong container, the wrong path.
#   ABSOLUTE (AUDIO_MAX_DROP_FILES) — refuse when more than this many files would be deleted
#     in ONE run, whatever the proportion. A percentage alone cannot see a large tree losing
#     a sub-threshold slice: 45% per run against a 50% brake never trips, and 1024 files walk
#     down to 1 in ten runs — fifty minutes at the five-minute cadence.
#   WINDOWED (AUDIO_DROP_WINDOW_MIN) — the percentage is measured not only against the
#     PREVIOUS run but against the highest count seen in this window (a high-water mark), so
#     cumulative loss is visible even when no single step is large enough to trip anything.
AUDIO_MAX_DROP_PCT="${AUDIO_MAX_DROP_PCT:-50}"
AUDIO_MAX_DROP_FILES="${AUDIO_MAX_DROP_FILES:-25}"
AUDIO_DROP_WINDOW_MIN="${AUDIO_DROP_WINDOW_MIN:-1440}"
# The deliberate-purge override, and it is ONE-SHOT BY CONSTRUCTION. It is an ordinary env
# var read from .env.backup, so "re-run once with it set" is advice a file cannot enforce —
# left behind, a boolean would disable every brake for ever with nothing but a WARN line.
# It therefore carries the EXACT number of recordings the purge should leave behind
# (AUDIO_ALLOW_MASS_DELETE=<count>) and is honoured only when that number matches what this
# run actually mirrored. A stale value authorises a state that has already happened, which
# is to say: nothing. Empty (the default) is off.
AUDIO_ALLOW_MASS_DELETE="${AUDIO_ALLOW_MASS_DELETE:-}"

# BEFORE any mkdir or prune: a bad retention deletes data, and the prune slices below cannot
# tell "0" from "unset".
require_positive_int LOCAL_RETENTION "${LOCAL_RETENTION}"
require_positive_int DRIVE_RETENTION "${DRIVE_RETENTION}"
require_positive_int DAILY_RETENTION "${DAILY_RETENTION}"
require_percent AUDIO_MAX_DROP_PCT "${AUDIO_MAX_DROP_PCT}"
require_positive_int AUDIO_MAX_DROP_FILES "${AUDIO_MAX_DROP_FILES}"
require_positive_int AUDIO_DROP_WINDOW_MIN "${AUDIO_DROP_WINDOW_MIN}"
# Normalise to base 10 NOW, once, so no later `(( ))` can read a leading zero as octal.
AUDIO_MAX_DROP_PCT=$((10#${AUDIO_MAX_DROP_PCT}))
AUDIO_MAX_DROP_FILES=$((10#${AUDIO_MAX_DROP_FILES}))
AUDIO_DROP_WINDOW_MIN=$((10#${AUDIO_DROP_WINDOW_MIN}))

# 0700, not the deploy umask's 0755: these directories hold snapshots of inbox.db and copies
# of Graham's voice recordings. The repo argues those deserve a stricter gate than the house
# password; the on-box mirror should not be readable by every account on the box.
install -d -m 0700 "${BACKUP_ROOT}"
install -d -m 0700 "${LOCAL_BACKUP_DIR}" "${STATE_DIR}"
command -v docker >/dev/null 2>&1 || die "docker not on PATH (the snapshot runs inside the container)"
[[ "$(docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || echo false)" == "true" ]] \
  || die "container ${CONTAINER} is not running — cannot snapshot a WAL DB from the host (see the header)"

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

# --- one DB: snapshot inside the container, verify, dedupe, keep ---------------------
# Sets SNAPSHOT_PATH / SNAPSHOT_CKSUM for the caller.
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
    SNAPSHOT_PATH="${prev}"
  else
    local ts dest n=1
    ts="$(date -u +%Y%m%dT%H%M%SZ)"
    dest="${LOCAL_BACKUP_DIR}/${name}_${ts}.db"
    # 1-second resolution: the timer and a manual run in the same second would collide and
    # `mv` would destroy the first. "_N" still sorts after the bare name ("_" > ".").
    while [[ -e "${dest}" ]]; do dest="${LOCAL_BACKUP_DIR}/${name}_${ts}_${n}.db"; n=$((n+1)); done
    mv "${tmp}" "${dest}"
    printf '%s\n' "${SNAPSHOT_CKSUM}" > "${ck_file}"
    SNAPSHOT_PATH="${dest}"
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

# --- the audio tree: a MIRROR, deletions included ------------------------------------
#
# The audio tree is NOT in the DB (files on disk, by design — blobs would make every snapshot
# byte-unique and defeat the sha256 dedupe above), so it needs its own path off-box.
#
# ⚠️ IT IS THE ONE THING HERE THAT PROPAGATES DELETIONS, DELIBERATELY. Graham's decision,
# 2026-09-19: an additive audio backup quietly defeats both the Inbox's Delete control and its
# 180-day privacy ceiling — a recording he deletes (DESIGN.md sells Delete as the retraction
# for "a password read aloud") would sit on Drive for ever. So Delete means deleted
# everywhere, for AUDIO ONLY. The two DATABASES stay additive (`rclone copy` + the ring +
# the daily/ tier) — a prune bug there can still never reach the off-box copy.
#
# Both hops must mirror or the property does not hold:
#   1. `docker cp` only ADDS and overwrites, so copying into a PERSISTENT directory would
#      keep deleted recordings alive on the host for ever and Drive would never see the
#      deletion. Each run therefore copies into a FRESH temp dir and swaps it in.
#   2. Upload = `rclone copy` (adds/updates) + an explicit delete pass for remote files the
#      container no longer has. Deliberately NOT rclone's own whole-tree mirroring verb: same
#      end state, but the deletes are ours — one file at a time, logged, and only after the
#      guard below — and no blunt mirror-everything command exists anywhere in this script for
#      a later edit to point at the DB ring by accident. (tests/test_deploy_backup.py pins
#      that absence.)
#
# THE GUARDS, in the spirit of the refuse-an-empty-snapshot rule: a mass deletion is far more
# likely to be a wiped volume, a bad prune or a mis-set path than an intentional purge. If the
# container's audio directory is missing entirely, or the count has fallen too far (see the
# three brakes above), NOTHING is deleted off-box: the upload still happens, the deletion pass
# is skipped, the remembered count is NOT advanced, and the run says so.
#
# ⚠️ WHAT THE BRAKE MEASURES AGAINST IS ITSELF SAFETY-CRITICAL. The baseline used to come from
# a state file, falling back to the HOST mirror directory — a directory nothing ever created,
# so the baseline was 0 on exactly the runs that needed one most: the first run after deploy,
# any run after the state dir was cleared, after a BACKUP_ROOT/HOME change, and after a
# rebuild-from-Drive. A baseline of 0 opens the brake completely (nothing is "a drop from 0").
# Measured before the fix: a container holding 1 file against a remote holding 20 deleted 19
# of them, silently, and logged "mirrored (deletions included)". The baseline now comes from
# the REMOTE LISTING — what Drive actually holds, which is the thing being protected — and a
# run that cannot obtain that listing does not delete at all. A run that does not know what
# the remote holds has no business deleting from it.

# Echo the number of files under <remote dir>. rc 0 means the number is TRUSTWORTHY; rc 1
# means the listing could not be obtained and the caller must not infer anything from it.
# `rclone mkdir` first so a not-yet-created directory reads as empty (rc 0, count 0) rather
# than as an error — that is the one "missing" case that genuinely is "holds nothing".
remote_audio_count() {  # <remote dir>
  local out rc=0
  rclone mkdir "$1" >/dev/null 2>&1 || true
  out="$(rclone lsf "$1" --recursive --files-only 2>/dev/null)" || rc=$?
  (( rc == 0 )) || return 1
  printf '%s' "${out}" | awk 'END{print NR}'
}

# The deliberate-purge override. True only when AUDIO_ALLOW_MASS_DELETE names the EXACT count
# this run would leave behind, so one value authorises one specific purge and can never
# silently authorise a different, later one.
purge_authorised() {  # <resulting count>
  [[ "${AUDIO_ALLOW_MASS_DELETE}" =~ ^[0-9]{1,9}$ ]] || return 1
  (( 10#${AUDIO_ALLOW_MASS_DELETE} == $1 ))
}

# rc 0 = this <from> -> <to> transition may be mirrored; rc 1 = refuse (and it has said why).
# Both brakes are checked, and either can refuse on its own.
audio_drop_allowed() {  # <from> <to> <what is being measured>
  local from="$1" to="$2" what="$3" dropped=0
  if (( from > to )); then dropped=$(( from - to )); fi
  (( dropped > 0 )) || return 0
  if (( to * 100 < from * (100 - AUDIO_MAX_DROP_PCT) )); then
    log "ERROR: audio: ${what} holds ${to} recording(s), down from ${from} — a drop of more than ${AUDIO_MAX_DROP_PCT}% (AUDIO_MAX_DROP_PCT)"
    return 1
  fi
  if (( dropped > AUDIO_MAX_DROP_FILES )); then
    log "ERROR: audio: ${what} holds ${to} recording(s), down from ${from} — ${dropped} file(s) in a single run, more than AUDIO_MAX_DROP_FILES=${AUDIO_MAX_DROP_FILES}"
    return 1
  fi
  return 0
}

push_audio() {
  [[ "${BACKUP_AUDIO}" == "1" ]] || return 0
  docker exec "${CONTAINER}" test -d "${CONTAINER_AUDIO_DIR}" 2>/dev/null || {
    log "WARN: audio: ${CONTAINER_AUDIO_DIR} is not present in ${CONTAINER} — SKIPPING the audio sync entirely. A missing tree is never propagated as a deletion (fresh volume? wrong CONTAINER_AUDIO_DIR? wrong container?)"
    return 0
  }

  local remote="${RCLONE_DEST}/audio"
  local count prev="" deletions_allowed=1 rc=0 count_file="${STATE_DIR}/last_audio_count"
  count="$(docker exec -e DIR="${CONTAINER_AUDIO_DIR}" "${CONTAINER}" python3 -c \
    'import os,sys; print(sum(len(f) for _,_,f in os.walk(os.environ["DIR"])))' 2>/dev/null)" \
    || { log "ERROR: could not count the audio files inside ${CONTAINER}"; return 1; }
  [[ "${count}" =~ ^[0-9]{1,9}$ ]] || { log "ERROR: unreadable audio file count '${count}'"; return 1; }
  count=$((10#${count}))

  # --- the baseline the brakes measure against ---------------------------------------
  if [[ -f "${count_file}" ]]; then
    prev="$(cat "${count_file}" 2>/dev/null || true)"
    [[ "${prev}" =~ ^[0-9]{1,9}$ ]] || prev=""
  fi
  if [[ -z "${prev}" ]]; then
    # No remembered count: first run after deploy, a cleared state dir, a changed
    # BACKUP_ROOT/HOME, or a rebuild-from-Drive. Ask the REMOTE what it holds.
    if prev="$(remote_audio_count "${remote}")" && [[ "${prev}" =~ ^[0-9]{1,9}$ ]]; then
      log "audio: no remembered count — baseline taken from ${remote}, which holds ${prev} file(s)"
    else
      prev=""
      deletions_allowed=0
      # Loud, not quiet. A backup that cannot read its own destination is the exact failure
      # this dashboard exists to surface; returning 0 here would leave the job green while
      # the mirror silently stopped mirroring.
      rc=1
      # Note what is deliberately NOT done here: the count is not written either. Recording
      # ${mirrored} as the baseline would claim the remote holds that many when this run just
      # failed to find out — and if it actually held twenty more, the NEXT run would see no
      # drop and delete them. So the state file stays empty, the next run asks the remote
      # again, and deletion begins the first time that question gets an answer. If it never
      # does, rclone is broken and nothing should be being deleted anyway.
      log "WARN: audio: no remembered count AND ${remote} could not be listed — uploading only. NOTHING is deleted off-box this run and no baseline is recorded; deletion resumes on the first run that can read the remote."
    fi
  fi
  prev=$((10#${prev:-0}))

  # --- the windowed high-water mark ----------------------------------------------------
  # Without this, only the single previous run is visible and a steady sub-threshold drip is
  # invisible for ever. With it, the percentage is measured against the largest count seen in
  # the last AUDIO_DROP_WINDOW_MIN minutes, so cumulative loss trips the same brake.
  local hw=0 hw_epoch=0 hw_file="${STATE_DIR}/audio_high_water" now base
  now="$(date +%s)"
  if [[ -f "${hw_file}" ]]; then
    read -r hw hw_epoch < "${hw_file}" 2>/dev/null || true
  fi
  [[ "${hw}" =~ ^[0-9]{1,9}$ ]] || hw=0
  [[ "${hw_epoch}" =~ ^[0-9]{1,12}$ ]] || hw_epoch=0
  if (( hw_epoch == 0 || now - hw_epoch > AUDIO_DROP_WINDOW_MIN * 60 )); then
    hw=0; hw_epoch=0                       # the mark has aged out; this run sets a fresh one
  fi
  base="${prev}"
  if (( hw > base )); then base="${hw}"; fi

  # Pre-flight, before the copy out: the cheapest place to refuse.
  if ! audio_drop_allowed "${base}" "${count}" "${CONTAINER}:${CONTAINER_AUDIO_DIR}"; then
    if ! purge_authorised "${count}"; then
      log "ERROR: audio: REFUSING to mirror that to ${remote}; the off-box copies are untouched. If the purge was deliberate, re-run ONCE with AUDIO_ALLOW_MASS_DELETE=${count} — the exact count it should leave behind, so the authorisation cannot outlive this one purge"
      return 1
    fi
    log "WARN: audio: mirroring the deletion anyway — AUDIO_ALLOW_MASS_DELETE=${count} matches what this run would leave behind"
  fi

  local staged
  staged="$(mktemp -d "${BACKUP_ROOT}/.audio.XXXXXX")" \
    || { log "ERROR: audio: could not create a staging directory under ${BACKUP_ROOT}"; return 1; }
  # Checked explicitly, NOT left to `set -e`: this function is invoked as `push_audio || ...`,
  # which suppresses errexit for its whole body. An unchecked mktemp failure would leave
  # staged="" and turn the docker cp below into a copy onto "/".
  [[ -n "${staged}" && -d "${staged}" ]] \
    || { log "ERROR: audio: staging directory is not usable"; return 1; }
  cleanup_audio() { [[ -z "${staged}" ]] || rm -rf "${staged}"; return 0; }
  trap cleanup_audio RETURN

  # A fresh directory every run: this is what makes a deletion visible at all.
  docker cp "${CONTAINER}:${CONTAINER_AUDIO_DIR}/." "${staged}/" \
    || { log "ERROR: docker cp of the audio tree failed"; return 1; }
  # `docker cp` brings the container's modes with it (~0644). These are recordings of
  # Graham's voice; owner-only, like everything else under BACKUP_ROOT.
  chmod -R go-rwx "${staged}" || log "WARN: audio: could not tighten the staged tree's modes"

  # --- re-measure THE SET THAT IS ACTUALLY MIRRORED -----------------------------------
  # The brake used to be applied to the in-container count while the delete list came from
  # the staged host tree: two measurements of two different sets at two different moments,
  # so the brake could pass on one set while the deletion ran against another. It needs no
  # injected fault to diverge — `os.walk` counts symlinks and `find -type f` does not, so a
  # container with 4 real files and 6 symlinks reported 10, the guard stayed silent, and 6 of
  # 10 remote recordings were deleted under a log line that said "10 recording(s) mirrored".
  # An interrupted `docker cp` that still exits 0 does the same thing with real files.
  #
  # From here on ${mirrored} — the staged tree, the exact set uploaded and diffed against the
  # remote — is the ONLY number used: for the brakes, for the override, for the remembered
  # count, and for the log line.
  local mirrored
  mirrored="$(find "${staged}" -type f 2>/dev/null | wc -l | tr -d ' ')"
  [[ "${mirrored}" =~ ^[0-9]{1,9}$ ]] \
    || { log "ERROR: audio: could not count the staged tree"; return 1; }
  mirrored=$((10#${mirrored}))

  if (( mirrored < count )); then
    log "ERROR: audio: ${CONTAINER} reported ${count} recording(s) but only ${mirrored} landed in the staging tree — the two do not describe the same set, so the deletion list cannot be trusted. Uploading what staged; NOTHING is deleted off-box and the remembered count is not advanced."
    deletions_allowed=0
    rc=1
  fi

  if (( deletions_allowed )) && ! audio_drop_allowed "${base}" "${mirrored}" "the staged tree"; then
    if purge_authorised "${mirrored}"; then
      log "WARN: audio: mirroring the deletion anyway — AUDIO_ALLOW_MASS_DELETE=${mirrored} matches what this run mirrored"
    else
      log "ERROR: audio: REFUSING to delete anything from ${remote} this run; the off-box copies are untouched. If the purge was deliberate, re-run ONCE with AUDIO_ALLOW_MASS_DELETE=${mirrored}"
      deletions_allowed=0
      rc=1
    fi
  fi

  rclone copy "${staged}" "${remote}" \
    || { log "ERROR: rclone copy of the audio tree failed"; return 1; }
  if (( deletions_allowed )); then
    delete_remote_extras "${staged}" "${remote}" "${mirrored}" || rc=1
  else
    log "WARN: audio: the deletion pass was SKIPPED this run — ${remote} may still hold recordings the container no longer has"
  fi

  # Swap the staged tree in as the host mirror, so it reflects deletions too. Both `mv`s are
  # checked for the same reason the mktemp above is: errexit is off inside this function.
  rm -rf "${AUDIO_MIRROR_DIR}.old"
  if [[ -e "${AUDIO_MIRROR_DIR}" ]]; then
    mv "${AUDIO_MIRROR_DIR}" "${AUDIO_MIRROR_DIR}.old" \
      || { log "ERROR: audio: could not rotate the host mirror aside; leaving it as it was"; return 1; }
  fi
  mv "${staged}" "${AUDIO_MIRROR_DIR}" \
    || { log "ERROR: audio: could not swap the staged tree in as the host mirror (Drive is unaffected)"; return 1; }
  staged=""                       # adopted; nothing left for the RETURN trap to remove
  rm -rf "${AUDIO_MIRROR_DIR}.old"

  if (( rc == 0 )) && (( deletions_allowed )); then
    # Only a CLEAN mirror advances the remembered count, so the drop guard above always
    # compares against the last state Drive actually reflects.
    printf '%s\n' "${mirrored}" > "${count_file}"
    if (( mirrored >= hw )) || purge_authorised "${mirrored}"; then
      printf '%s %s\n' "${mirrored}" "${now}" > "${hw_file}"
    fi
    log "audio: ${mirrored} recording(s) mirrored to ${remote} (deletions included)"
  else
    log "WARN: audio: ${mirrored} recording(s) uploaded, but this run was not a clean mirror — ${remote} may still hold recordings the container no longer has, and the remembered count is unchanged"
  fi
  return "${rc}"
}

# Delete files under <remote dir> that <local dir> no longer has. The mirroring half of
# push_audio, kept separate so the delete list is visible and logged one file at a time.
delete_remote_extras() {  # <local dir> <remote dir> <mirrored count>
  local local_dir="$1" remote_dir="$2" mirrored="$3" rc=0 f listing ls_rc=0
  local want=() have=() extras=()
  while IFS= read -r f; do [[ -n "$f" ]] && want+=("$f"); done \
    < <(cd "${local_dir}" && find . -type f 2>/dev/null | sed 's|^\./||' | sort || true)
  # ⚠️ THE LISTING'S EXIT STATUS IS LOAD-BEARING. This used to be `$(rclone lsf … 2>/dev/null
  # | sort || true)`, so a failed listing produced an EMPTY `have` and the function returned 0
  # — "could not list the remote" was indistinguishable from "listed it, nothing to delete".
  # Measured: the run logged "4 recording(s) mirrored … (deletions included)" while the remote
  # still held 5 untouched, and advanced the remembered count to 4 — which is precisely the
  # corrupted baseline that lets the NEXT run mirror a wipe.
  rclone mkdir "${remote_dir}" >/dev/null 2>&1 || true
  listing="$(rclone lsf "${remote_dir}" --recursive --files-only 2>/dev/null)" || ls_rc=$?
  if (( ls_rc != 0 )); then
    log "ERROR: audio: could not list ${remote_dir} (rclone exited ${ls_rc}) — what it holds is UNKNOWN, so nothing is deleted and the remembered count stays put. An unreadable remote is not an empty one."
    return 1
  fi
  while IFS= read -r f; do [[ -n "$f" ]] && have+=("$f"); done \
    < <(printf '%s\n' "${listing}" | sort)
  (( ${#have[@]} )) || return 0
  # "the container has NO recordings at all, the remote has some" is never propagated on its
  # own say-so: it is the wiped-volume shape. Deleting everything always needs the explicit
  # override, naming 0 as the count it should leave behind.
  if (( ${#want[@]} == 0 )) && ! purge_authorised 0; then
    log "ERROR: audio: the container has no recordings while ${remote_dir} holds ${#have[@]} — REFUSING to delete them all (set AUDIO_ALLOW_MASS_DELETE=0 if that is really intended)"
    return 1
  fi
  while IFS= read -r f; do [[ -n "$f" ]] && extras+=("$f"); done \
    < <(comm -13 <(printf '%s\n' ${want[@]+"${want[@]}"}) <(printf '%s\n' "${have[@]}"))
  (( ${#extras[@]} )) || return 0
  # The ABSOLUTE brake, applied to the real delete list rather than to a projection of it.
  # This is the last gate before an irreversible `rclone deletefile`, and it is the one that
  # sees a sub-threshold proportional drip for what it is: a lot of files going away at once.
  if (( ${#extras[@]} > AUDIO_MAX_DROP_FILES )) && ! purge_authorised "${mirrored}"; then
    log "ERROR: audio: ${#extras[@]} file(s) under ${remote_dir} are no longer in the container — more than AUDIO_MAX_DROP_FILES=${AUDIO_MAX_DROP_FILES} for a single run. REFUSING to delete ANY of them; the off-box copies are untouched. If it was deliberate, re-run ONCE with AUDIO_ALLOW_MASS_DELETE=${mirrored}"
    return 1
  fi
  for f in "${extras[@]}"; do
    if rclone deletefile "${remote_dir}/${f}"; then
      log "audio: deleted ${f} from ${remote_dir} (gone from the container)"
    else
      log "WARN: could not delete ${f} from ${remote_dir}"; rc=1
    fi
  done
  rclone rmdirs "${remote_dir}" --leave-root >/dev/null 2>&1 || true
  return "${rc}"
}

# --- run -----------------------------------------------------------------------------
declare -a PUSHABLE=()
SNAPSHOTS=0
for pair in ${CONTAINER_DBS}; do
  name="${pair%%:*}"; src="${pair#*:}"
  SNAPSHOT_PATH=""; SNAPSHOT_CKSUM=""
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
