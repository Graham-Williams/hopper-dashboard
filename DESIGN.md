# hopper-dashboard — design (v1, 2026-09-04)

## Goal
One page (and one JSON endpoint) that answers, for every automated or on-demand job Graham relies on:
**what does it protect, how, when did it last run, when did it last SUCCEED, and does the destination
actually hold fresh bytes?** Plus, for manual jobs, **how far behind are they?** Hopper must be able to read
the same answer machine-readably. v1 is read-only observe; actions stay with Hopper over SSH.

## Non-goals (v1)
- No trigger buttons / no docker socket / no systemd control from the web app.
- No per-user accounts: same shared `APP_PASSWORD` gate as km/todoist/taste-twin/jjho.
- No replacement of the existing schedulers (launchd, systemd timers, compose restart policies).

## Architecture
Three inputs, one store, two outputs.

Inputs:
1. **Heartbeats (push).** For the two systemd backup units, a drop-in
   `/etc/systemd/system/<unit>.service.d/heartbeat.conf` adds
   `ExecStopPost=curl … -d result=$SERVICE_RESULT -d exit=$EXIT_STATUS …` — runs on success, non-zero exit AND
   the 300 s timeout kill, without touching either repo's script (survives `git pull`). The backup scripts
   themselves may ALSO post a richer heartbeat (push reason: pushed / skipped-unchanged / skipped-throttle,
   db sha) as a nice-to-have. Mac jobs (launchd) post from the script. Each POSTs `/api/v1/ping/<job_id>` at end of run with
   `{status: ok|fail, started_at, finished_at, bytes?, files?, note?, metrics?: {...}}`. Auth: per-job token
   (`Authorization: Bearer <token>`), tokens live only in the box `.env` / Mac launchd env. Ingest is published
   ONLY on the box's Tailscale IP `100.101.1.28:<port>` (precedent: synapse on :8008) — reachable from the box
   itself and the Mac over the tailnet, never through the Cloudflare tunnel. ufw is INACTIVE on the box, so a
   `0.0.0.0` publish would be LAN-exposed: always bind explicitly.
2. **Destination probes (pull).** Run INSIDE the dashboard container by a scheduler thread (every 5–10 min;
   `rclone lsjson --recursive` on a backup folder is ~1 s). The host's `~/.config/rclone/rclone.conf` is
   bind-mounted `:ro` and an entrypoint copies it to a writable `RCLONE_CONFIG` path (rclone rewrites the conf
   on token refresh, so a bare `:ro` mount fails). Host state dirs `~/km-backups/state` and
   `~/todoist-points/data/.backup-state` are bind-mounted `:ro` so `last_drive.sha256` /
   `last_drive_push.epoch` can be cross-checked against the newest Drive object. Container probes for
   `container` type use `docker ps` output pushed by a tiny host-side heartbeat instead of a socket mount
   (NO docker socket in the container). ⚠️ SCOPE: the box's `gdrive:` remote sees ONLY `km-tracker-backups/`
   and `todoist-points-backups/` — `Backups/` (Hopper docs) and `Gremlins/` (Minecraft) are only visible from
   the Mac's remote, so those destination probes run in the MAC probe (input 3) and arrive as metrics.
   Per job type:
   - `rclone_copy_tree`: `rclone check --one-way <src> <dst>` semantics → missing/differ counts. For Mac-sourced
     trees (recordings, personal-assistant) the box can't see the source, so the MAC probe reports
     `missing_bytes`/`missing_files` via heartbeat metrics instead, and the box probe only records newest
     object time + object count at the destination.
   - `db_snapshot`: newest object + count in `gdrive:<bucket>/` and `daily/`; healthy if newest snapshot's
     content hash == current DB hash (dedup-aware) OR newest object age < cadence*N. Needs the DB hash from the
     backup script's heartbeat (`metrics.db_sha256`) so "old newest file" is explainable.
   - `container`: `docker ps` health/status for each expected container; restarts count.
   - `drive_mirror`: RESOLVED (spike 2026-09-04) — Mac probe copies
     `~/Library/Application Support/Google/DriveFS/<account-id>/mirror_sqlite.db{,-wal,-shm}` to a temp dir
     (WAL is live; never open in place) and reads: `root_config` (which folders are mirrored),
     `pending_uploads` + `queued_uploads` + `pending_deletes` row counts, and
     `count(*) FROM mirror_item WHERE local_size!=cloud_size OR local_md5_checksum!=cloud_md5_checksum`.
     Caught up == all zero. Metrics: `pending`, `mismatch`, `roots`. Independent cloud-side verification
     (weekly, from either machine): `rclone size gdrive: --drive-root-folder-id <root-id> --json` per root vs
     local byte count (skip symlinks, `.DS_Store`, 0-byte). Discovered ids on this account: computer root
     `My Mac` = `1Jk8gfibxo0F5nXo7zm7KlFkEdIn9wrWF`; Documents `1ld9DSCQZHK7G_-_aEUDd8KcvrSqVGKLZ`, Desktop
     `1HTekdf0YxeumbWS06c10q2wnr1bHlEwt`, minecraft-channel `1inrtdCnN5ED0NR4QjQCBdVxvgFqbqmzi` (put these in
     the gitignored jobs.yml, not the repo). Discovery command for a fresh setup:
     `rclone backend query gdrive: "mimeType='application/vnd.google-apps.folder' and 'me' in owners and trashed=false"`
     → keep entries with no `parents`. Bytes matched exactly local↔cloud on 2026-09-04 for all three roots;
     the only recurring log errors are 3 symlinks in a venv that Drive can't upload (benign).
   - `manual`: no schedule; freshness target expressed as max age and/or max lag (bytes/files behind), lag
     supplied by whichever machine can compute it.
3. **Mac probe (push).** A launchd job (`com.hopper.dashboard-probe`, hourly, plus RunAtLoad so it fires after
   wake) that computes: recordings/world-backups/replays not yet on Drive (rclone check --one-way, byte total),
   nightly backup last outcome (parse `~/Library/Logs/hopper-backup.log` last line), disk free, DriveFS roots
   state; POSTs each as a heartbeat/metrics to the box over Tailscale. If the Mac is asleep the box simply marks
   the Mac probe "not heard from" after its grace period — that itself is a signal (Mac offline).

Store: SQLite (`jobs`, `runs`, `probes`, `state_changes`). Job registry seeded from a gitignored `jobs.yml`
(committed `jobs.example.yml`) — names, type, cadence, grace, expectations, description, destination.

Outputs:
- **HTML** `/` — one card per job grouped by machine; state chip (OK / LATE / FAIL / STALE-DEST / UNKNOWN),
  last run, last success, destination freshness, lag for manual jobs, sparkline of recent runs. Theme-aware,
  mobile-friendly, same visual language as the other apps. Password gate.
- **JSON** `/api/v1/status` (whole board), `/api/v1/jobs/<id>` (history). Same gate (cookie) OR the
  Hopper read token. Hopper's weekly "home server health" watch reads this instead of SSH-poking six apps.
- **Alerts**: state-change → ntfy POST to a private topic (topic name = secret, in `.env`). Fires on
  OK→LATE/FAIL/STALE and on recovery. Digest suppression: one alert per job per state transition, no repeats.

## State machine (per job)
- `OK`: last run success within cadence+grace AND (if probed) destination fresh.
- `LATE`: no heartbeat within cadence+grace (dead-man's switch).
- `FAIL`: last heartbeat status=fail.
- `STALE_DEST`: heartbeat says ok but destination probe disagrees (the 2026-08 nightly-backup case).
- `BEHIND`: manual job over its lag target.
- `UNKNOWN`: never heard from.

## Security posture
- Ingest tokens per job; constant-time compare; per-IP rate limit on ping + login.
- Ingest bound to the box's Tailscale IP only; the tunnel exposes only the read side.
- No secrets in repo: `jobs.yml`, `.env` gitignored; `.env.example` + `jobs.example.yml` committed.
- Container runs non-root, read-only FS except the data volume; no docker socket, no host rclone config.
- One writer: the container owns the SQLite file; everything external arrives via the ingest API.
- Container is non-root; rclone conf is copied at entrypoint, never written back to the host.

## Box facts (recon 2026-09-04, read-only)
Ubuntu 24.04, Python 3.12, Docker 29 + Compose v5, host rclone 1.60 (old), curl present, no sqlite3 CLI.
`graham` uid 1000 in `docker` group, `Linger=no` → system units only. Timers: `km-backup.timer`,
`todoist-points-backup.timer` (every 5 min, `Type=oneshot`, `User=graham`, `TimeoutStartSec=300`,
`NoNewPrivileges`, `PrivateTmp`; no OnFailure/ExecStopPost today; logs → journal only). Backup scripts snapshot
via sqlite backup API → sha256 → skip if unchanged → `rclone copy` throttled ≥15 min → state files. Exit 0 on a
skipped push. `km-tracker_default` bridge has 7 members incl. `km-tracker-cloudflared-1` (remote-managed tunnel,
`TUNNEL_TOKEN` in km `.env`). Only host-published port today: `100.101.1.28:8008` (synapse).

## Hosting
Box: `~/hopper-dashboard`, compose service `hopper-dashboard` → `http://hopper-dashboard:8080` on
`km-tracker_default`; tunnel ingress + proxied CNAME `dashboard.graham-williams.com` (single-label host).
Ingest port published on `<tailscale-ip>:8081` only. Backups of its own SQLite: it's derived state — no
off-box backup needed (re-populates within one probe cycle). It appears in INVENTORY.md and watches itself
(its own probe heartbeat is a job).

## Open questions / spikes
- ~~Drive Computers-section listability~~ RESOLVED, see `drive_mirror`.
- ~~Heartbeat hook mechanism~~ RESOLVED: systemd ExecStopPost drop-ins (see Inputs §1).
- ntfy: public ntfy.sh with a random topic vs self-hosted ntfy container. Default: ntfy.sh, random 32-char topic;
  revisit if Graham wants everything on-box.

## API contract (frozen so the app and the probes can be built in parallel)

Ingest (Tailscale-only port, default 8081):
- `POST /api/v1/ping/<job_id>` — `Authorization: Bearer <JOB_TOKEN>` (one shared ingest token in v1,
  `INGEST_TOKEN` env; per-job tokens are a later hardening). Body JSON, all optional except `status`:
  ```json
  {"status": "ok|fail|skipped|metric", "started_at": "<iso8601>", "finished_at": "<iso8601>",
   "reason": "pushed|skipped-unchanged|skipped-throttle|timeout|error|...", "exit_code": 0,
   "note": "free text ≤ 500 chars",
   "metrics": {"bytes": 0, "files": 0, "db_sha256": "…", "lag_bytes": 0, "lag_files": 0,
               "dest_newest_iso": "…", "dest_count": 0, "disk_free_bytes": 0, "<anything>": 1}}
  ```
  `status: "metric"` is a **metrics-only update**: it shallow-merges `metrics` into the job's
  `last_metrics` (so the Mac probe can report offload lag / dest freshness) but is NOT a run — it never
  counts as a heartbeat or a success, never resets `last_run`/`last_success`, and LATE is always computed
  from the last real run. `skipped` IS a heartbeat and counts as a success (a backup that ran, found the DB
  unchanged and skipped the push did its job). `metrics` values must be numbers, booleans, null, strings
  ≤ 1000 chars, or flat lists of those (≤ 100 items); nested objects are rejected (400). `note` is
  truncated to 500 chars; the whole body is capped at 64 KB (413). `last_metrics` is a shallow merge across
  pings, so a bare systemd form ping never wipes the `db_sha256` a richer script heartbeat sent earlier.
  Also accepts `application/x-www-form-urlencoded` with `result=` + `exit=` (what a systemd
  `ExecStopPost` curl sends): `result=success` → ok; anything else → fail, `reason=<result>`. A non-numeric
  `exit=` (signal name) and an `exit_code=exited|killed|dumped` field are accepted and folded into `note`,
  never rejected.
  Auth is checked BEFORE the job lookup, so an unauthenticated caller can't enumerate ids (401, empty body).
  Unknown `job_id` → 404 (jobs must be declared in `jobs.yml`; no auto-registration, so a typo can't
  create a phantom "healthy" job). Per-IP rate limit 120 pings/min → 429.
  Responds `{"ok": true, "state": "<computed state>"}`.
- `GET /healthz` → 200 on both ports.

Read side (behind the tunnel + password gate; also accepts `Authorization: Bearer <READ_TOKEN>` for Hopper):
- `GET /api/v1/status` → `{"generated_at": …, "summary": {"ok": n, "late": n, …},
  "jobs": [{"id", "name", "machine", "kind", "state", "since", "last_run", "last_success",
            "cadence_s", "grace_s", "destination", "protects", "method", "lag": {...}|null,
            "dest": {"newest": …, "count": …, "fresh": true|false|null}, "last_metrics": {...}}]}`
  `last_run` / `last_success` are ISO-8601 UTC strings (server receive time) or null; `lag` is null except for
  manual jobs (always present) and any job that reported `lag_bytes`/`missing_bytes`; `dest.fresh` is null when
  the destination cannot be judged yet. Additive, non-contract fields the app also emits (safe to ignore):
  `last_run_status`, `last_run_reason`, `last_run_note`, `late_means`, `informational`, `expect`,
  `summary.total`, `summary.computed_at` (newest state recompute — a stale value means the scheduler is down).
  Unauthenticated API calls get a JSON 401, not a redirect.
- `GET /api/v1/jobs/<id>?limit=100` → `{generated_at, job, runs, state_changes, probes|null}`.
- `GET /` HTML board; `GET /jobs/<id>` HTML detail.

`jobs.yml` schema (gitignored on the box; `jobs.example.yml` committed):
```yaml
jobs:
  - id: km-backup                 # [a-z0-9-]+, used in the ping URL
    name: km-tracker DB → Drive
    machine: box                  # box | mac
    kind: db_snapshot             # db_snapshot | rclone_copy_tree | drive_mirror | container | manual | probe
    protects: km-tracker SQLite (prod)
    method: sqlite backup API → sha256 dedup → rclone copy (additive)
    destination: gdrive:km-tracker-backups
    cadence_s: 300
    grace_s: 600
    probe:                        # optional, only for kinds the box container can probe itself
      rclone_path: gdrive:km-tracker-backups
      state_dir: /state/km        # bind-mounted :ro host state dir
    manual:                       # only for kind: manual
      max_age_s: 604800
      max_lag_bytes: 10737418240
    expect: [km-tracker-app-1]    # only for kind: container — names that must appear in metrics.running
    late_means: Mac offline or asleep   # optional text shown instead of the generic LATE reason
```
Validation is strict and fails startup with the job id + field: ids `^[a-z0-9-]+$` (unique, ≤64), `machine`
∈ box|mac, `kind` ∈ the six kinds, `cadence_s`+`grace_s` required for every kind except `manual` (and
forbidden on manual), `db_snapshot` requires `probe.rclone_path`, `rclone_copy_tree` requires `destination`,
`container` requires a non-empty `expect`, unknown keys anywhere are errors.
State computation runs on every ping (for ALL jobs, so LATE keeps firing even if the ticker thread dies) and
on a 60 s ticker (so LATE fires without traffic). Every state transition is written to `state_changes` and
dispatched to ntfy (`NTFY_URL` + `NTFY_TOPIC` env; disabled when empty) with title `[dashboard] <job> → <STATE>`
and priority high for FAIL/STALE_DEST, default otherwise. The very first `UNKNOWN → OK` is recorded but not
alerted (it is not a recovery). ntfy failures are logged and swallowed — they never reach request handling.

### State precedence (as implemented)
Scheduled kinds (`db_snapshot`, `rclone_copy_tree`, `drive_mirror`, `container`, `probe`):
`UNKNOWN` (no run yet — metrics alone don't count) → `LATE` (silence > cadence+grace, judged on server receive
time; wins even if the last word was "fail", because silence is the more urgent fact) → `FAIL` (last run
status=fail, or a `container` job whose `metrics.running` lacks an `expect` name) → kind-specific →
`OK`. Kind-specific: `db_snapshot` → `STALE_DEST` when newest Drive object is older than `cadence*12` AND
(state-file `last_drive.sha256` ≠ heartbeat `metrics.db_sha256`, or — with no heartbeat sha — the state
file's `last_drive_push.epoch` is >1 h newer than anything on Drive); an unchanged DB with an old newest file
is OK (dedup-aware), and with neither sha nor epoch available we don't guess. `rclone_copy_tree` →
`STALE_DEST` when the run said ok but reported `missing_bytes`/`lag_bytes` > 0. `drive_mirror` → `BEHIND`
when `pending + mismatch > 0`. `manual`: never LATE; `FAIL` if last run failed; `BEHIND` if age of last
success > `max_age_s` or `lag_bytes` > `max_lag_bytes`; no thresholds = informational, never alerts.
`dashboard-probes` is the scheduler's own heartbeat: a run is recorded every probe cycle, `fail` (with the
error in `note`) if any rclone probe errored — so a broken probe shows up as a FAIL card, never a crash.
