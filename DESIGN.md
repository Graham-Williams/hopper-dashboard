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
   ONLY on the box's Tailscale IP `<box-tailscale-ip>:<port>` (precedent: another Tailscale-only service on the
   box) — reachable from the box itself and the Mac over the tailnet, never through the Cloudflare tunnel. ufw
   is INACTIVE on the box, so a `0.0.0.0` publish would be LAN-exposed: always bind explicitly. The IP itself
   is deployment config (`INGEST_BIND` in the box `.env`, `DASHBOARD_URL` in the probes' env files) and is
   deliberately NOT a default anywhere in the code. **Second reachability path:** the container joins the
   shared `km-tracker_default` network, so every sibling container there (km-tracker, todoist-points,
   taste-twin, jjho, baby-pool, cloudflared) can reach `hopper-dashboard:8081` by service name — the
   ingest port is token-gated, not network-private. A compromised sibling could post fake heartbeats only
   with `INGEST_TOKEN`; keep that token out of every other app's env.
2. **Destination probes (pull).** Run INSIDE the dashboard container by a scheduler thread (every 5–10 min;
   `rclone lsjson --recursive` on a small backup folder is ~1 s; a ~1000-object tree is far slower and gets
   its own `probe.interval_s` — see "Probe cadence and flap damping"). The host's `~/.config/rclone/rclone.conf` is
   bind-mounted `:ro` and an entrypoint copies it to a writable `RCLONE_CONFIG` path (rclone rewrites the conf
   on token refresh, so a bare `:ro` mount fails). Host state dirs `~/km-backups/state` and
   `~/todoist-points/data/.backup-state` are bind-mounted `:ro` so `last_drive.sha256` /
   `last_drive_push.epoch` can be cross-checked against the newest Drive object. Container probes for
   `container` type use `docker ps` output pushed by a tiny host-side heartbeat instead of a socket mount
   (NO docker socket in the container). ⚠️ SCOPE: the box's *writer* `gdrive:` remote sees ONLY
   `km-tracker-backups/` and `todoist-points-backups/`, so the missing/differ verdicts for `Backups/` (Hopper
   docs) and `Gremlins/` (Minecraft) run in the MAC probe (input 3) and arrive as metrics. The container's
   read-only `gdrive-ro` remote (scope `drive.readonly`, DEPLOY.md §1b) CAN list those folders, so a
   `rclone_copy_tree` or `manual` job may *optionally* carry `probe.rclone_path` (e.g. `gdrive-ro:Backups`)
   to get newest-object time + count from the box; without it the card shows only the Mac-reported count.
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
     local byte count (skip symlinks, `.DS_Store`, 0-byte). The four folder ids involved — the computer
     root `<my-mac-root-id>` and the mirrored folders `<documents-root-id>`, `<desktop-root-id>`,
     `<minecraft-channel-root-id>` — live ONLY as a YAML **comment** under `drive-mirror` in the gitignored
     `jobs.yml` and in the operator notes (Hopper's memory), never in this repo (they are not credentials,
     but they identify a private Drive layout). They are notes, not config: the schema has no field for them
     (`registry.py` rejects unknown keys) and `probes/drivefs.py` discovers the roots from the DriveFS DB, so
     the ids only serve this manual `rclone size` cross-check. Discovery command for a fresh setup:
     `rclone backend query gdrive: "mimeType='application/vnd.google-apps.folder' and 'me' in owners and trashed=false"`
     → keep entries with no `parents`. Bytes matched exactly local↔cloud on 2026-09-04 for all three roots;
     the only recurring log errors are 3 symlinks in a venv that Drive can't upload (benign).
   - `manual`: no schedule; freshness target expressed as max age and/or max lag (bytes/files behind), lag
     supplied by whichever machine can compute it.
   - `disk`: filesystem capacity for one machine, pushed as metrics (`disk_free_bytes` /
     `disk_total_bytes`) by that machine's existing probe — the Mac's hourly launchd run (`mac-disk`) and
     the box's 5-minute containers timer (`box-disk`). A **gauge, not a job**: no `cadence_s`/`grace_s`,
     no per-cadence dead-man's switch, no destination probe. Liveness is already owned by `mac-probe` /
     `box-containers`, which carry these metrics in on the same run, so a second silence alarm for the
     same silence would only double the alerts. `BEHIND` when `disk_free_bytes < disk.min_free_bytes`
     or used-percent > `disk.max_used_pct`; with neither threshold set it is informational (shown, never
     alerted), exactly like a thresholdless `manual` job. Two things outrank the figures: a `fail` ping
     that nothing has superseded (→ `FAIL`, "capacity unreadable"), and a reading older than
     `state.DISK_METRIC_MAX_AGE_S` = **48 h** (→ `LATE`). That ceiling is the backstop for *partial*
     silence: a feeder that stops on its own (probe renamed, moved, interpreter gone) posts nothing
     at all, while the probe job it rides on keeps reporting `OK` — so without it a frozen gauge reads
     as a healthy one for ever. 48 h sits above `mac-probe`'s ~15 h LATE deadline on purpose, so a
     machine merely switched off for a day does not page twice for one fact. A gauge that has NEVER
     reported a reading is `UNKNOWN` only for `state.DISK_FIRST_READING_GRACE_S` = **6 h** after it was
     registered, then `LATE` — the `disk` analogue of the never-pinged rule for scheduled kinds, and the
     case where the job exists in `jobs.yml` but its feeder was never deployed (UNKNOWN is also the
     initial stored state, so nothing would ever be alerted). Negative capacity metrics are treated as
     absent — corrupt, not small — so the card says the capacity is unknown rather than printing an
     invented >100% figure. `used_pct` is
     `(total - available) / total` as `statvfs` reports them, and `null` — never a ZeroDivisionError —
     when the total is missing or 0. Note that the free **bytes** equal `df`'s Avail but the
     **percentage** does not equal `df`'s `Use%`: on macOS/APFS `df` divides by a larger free figure
     that `statvfs` never exposes (measured: 79.5% here, 78% there), so a 90% ceiling trips at roughly
     88.5% as `df` prints it. Expected, not a bug to fix — `f_bavail` is the right number because it
     is the space a recording can actually use.
3. **Mac probe (push).** A launchd job (`com.hopper.dashboard-probe`, hourly, plus RunAtLoad so it fires after
   wake) that computes: recordings/world-backups/replays not yet on Drive (rclone check --one-way, byte total),
   nightly backup last outcome (parse `~/Library/Logs/hopper-backup.log` last line), disk free (as its own
   `mac-disk` gauge; `minecraft-offload` also keeps the same two metrics as a footnote), DriveFS roots
   state; POSTs each as a heartbeat/metrics to the box over Tailscale. If the Mac is asleep the box simply marks
   the Mac probe "not heard from" after its grace period — that itself is a signal (Mac offline).

Store: SQLite (`jobs`, `runs`, `probes`, `state_changes`). Job registry seeded from a gitignored `jobs.yml`
(committed `jobs.example.yml`) — names, type, cadence, grace, expectations, description, destination.

Outputs:
- **HTML** `/` — one card per job grouped by machine; state chip (OK / LATE / FAIL / STALE-DEST / UNKNOWN),
  last run, last success, destination freshness, lag for manual jobs, a capacity gauge for disk jobs
  (percent used + free/total in GiB + an SVG bar with the threshold marked — SVG, not a styled div, because
  `style-src 'self'` forbids the inline width), sparkline of recent runs. Theme-aware,
  mobile-friendly, same visual language as the other apps. Password gate.
- **JSON** `/api/v1/status` (whole board), `/api/v1/jobs/<id>` (history). Same gate (cookie) OR the
  Hopper read token. Hopper's weekly "home server health" watch reads this instead of SSH-poking six apps.
- **Alerts**: state-change → ntfy POST to a private topic (topic name = secret, in `.env`). Fires on
  OK→LATE/FAIL/STALE and on recovery. Digest suppression: one alert per job per state transition, no repeats.

## State machine (per job)
- `OK`: last run success within cadence+grace AND (if probed) destination fresh.
- `LATE`: no heartbeat within cadence+grace (dead-man's switch) — including a job that has NEVER pinged once
  cadence+grace has elapsed since it was registered (`jobs.created_at`); for a `disk` gauge, which has no
  cadence, a capacity reading older than 48 h — or no reading at all more than 6 h after registration.
- `FAIL`: last heartbeat status=fail (for a `disk` gauge, a `fail` ping no later metric has superseded).
- `STALE_DEST`: heartbeat says ok but destination probe disagrees (the 2026-08 nightly-backup case). For copy
  trees only **missing** (never uploaded) bytes count; **differ** (edited since the last copy) is normal lag.
- `BEHIND`: manual job over its lag target, or a `disk` job under its free-space floor / over its
  used-percent ceiling. Capacity deliberately reuses `BEHIND` rather than adding a state: it is already
  the non-urgent "you need to do something" colour, and a seventh state would touch the notifier,
  the CSS, the summary tiles and every state test for no new meaning.
- `UNKNOWN`: never heard from (and registered less than cadence+grace ago).

## Security posture
- Ingest tokens per job; constant-time compare; per-IP rate limit on ping + login, plus a **global**
  failed-login cap (100 / 15 min across all clients) so many source IPs can't defeat the per-IP limiter.
- Client IP: the **ingest** role keys its limiter on the TCP peer only (no proxy in front of it — a forwarded
  header there is always attacker-controlled). The **read** role trusts `CF-Connecting-IP` only when the peer
  is inside `TRUSTED_PROXY_CIDR` (the tunnel container's network); empty = never trust it.
- Ingest bound to the box's Tailscale IP only; the tunnel exposes only the read side (the read role has no
  ping route at all → 404). Sibling containers on `km-tracker_default` can reach both ports (token-gated).
- **Fail fast, never fail open:** the read role refuses to start with `APP_ENV=prod` and an empty
  `APP_PASSWORD`, or with `APP_PASSWORD` set but no `SESSION_SECRET` (multi-worker logins would loop);
  compose additionally hard-requires the four secrets (`${VAR:?}`).
- No secrets in repo: `jobs.yml`, `.env`, `rclone.conf`, `ingest.env` gitignored; `.env.example` +
  `jobs.example.yml` committed. No machine IPs / account ids / Drive folder ids in code or docs.
- Container: read-only FS except the data volume + tmpfs `/tmp`; no docker socket. PID 1 starts as **root only
  to copy the 0600 host rclone.conf** into a 0700 tmpfs dir owned by the app user, then `setpriv`s to
  `dashboard` (uid 10001, `--no-new-privs`) and re-execs; nothing else ever runs as root, the host conf is
  never written back. The probes use a **`drive.readonly`-scope** rclone remote (own conf file) so a
  compromised container cannot write to or delete anything on Drive; the full-scope conf is a documented
  fallback (DEPLOY.md §1b).
- Ingest parsing is bounded: 64 KB body, ≤50 flat metrics, ints within ±2^63, keys `fullmatch`ed, deeply
  nested JSON → 400 (RecursionError caught), rclone paths passed after `--`. The ingest app has no static
  route. All ISO timestamps are clamped to `[1970, 9999]` and never raise (a poisoned metric can't 500 the
  board).
- ntfy (a third party) receives only `job_id: FROM → TO` — never the free-text reason (container names,
  client notes, rclone stderr stay on the board).
- CSP: `default-src 'self'`, `script-src 'nonce-<per-request>'` allowing exactly one inline script (the
  timestamp localizer in `base.html`); no `'self'`/`'unsafe-inline'` for scripts, no CDN.
- One writer: the container owns the SQLite file; everything external arrives via the ingest API.

## Box facts (recon 2026-09-04, read-only)
Ubuntu 24.04, Python 3.12, Docker 29 + Compose v5, host rclone 1.60 (old), curl present, no sqlite3 CLI.
the login user (uid 1000) in the `docker` group, `Linger=no` → system units only. Timers: `km-backup.timer`,
`todoist-points-backup.timer` (every 5 min, `Type=oneshot`, `User=<login>`, `TimeoutStartSec=300`,
`NoNewPrivileges`, `PrivateTmp`; no OnFailure/ExecStopPost today; logs → journal only). Backup scripts snapshot
via sqlite backup API → sha256 → skip if unchanged → `rclone copy` throttled ≥15 min → state files. Exit 0 on a
skipped push. `km-tracker_default` bridge has 7 members incl. `km-tracker-cloudflared-1` (remote-managed tunnel). The only
host-published port today is another Tailscale-only service (bound to the Tailscale IP, not `0.0.0.0`) — the
precedent the ingest port follows. Host `~/.config/rclone/rclone.conf` is `0600`, owned by the login (uid 1000)
and its `gdrive` remote is `scope = drive.file`; the container's app user is uid 10001, so the conf can only be
read by a root staging step (see Security posture) and a separate read-only-scope remote is used for the probes.

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

Read side (behind the tunnel + password gate; also accepts `Authorization: Bearer <READ_TOKEN>` for Hopper —
whose reads go through the public hostname, `https://dashboard.graham-williams.com/api/v1/status`; a loopback
read from inside the container must send `Host: <APP_HOST>` or the Host pin answers 403):
- `GET /api/v1/status` → `{"generated_at": …, "summary": {"ok": n, "late": n, …},
  "jobs": [{"id", "name", "machine", "kind", "state", "since", "last_run", "last_success",
            "cadence_s", "grace_s", "destination", "protects", "method", "lag": {...}|null,
            "dest": {"newest": …, "count": …, "fresh": true|false|null}, "last_metrics": {...}}]}`
  `last_run` / `last_success` are ISO-8601 UTC strings (server receive time) or null; `lag` is null except for
  manual jobs (always present) and any job that reported `lag_bytes`/`missing_bytes`/`differ_*`; for
  `rclone_copy_tree` jobs `lag.bytes`/`lag.files` are the MISSING figures and `lag.differ_bytes`/
  `lag.differ_files` the informational lag; `dest.fresh` is null when the destination cannot be judged yet.
  Additive, non-contract fields the app also emits (safe to ignore):
  `disk` (**`kind: disk` only**, `null` otherwise — `{measured_at, free_bytes, total_bytes, used_bytes,
  used_pct, min_free_bytes, max_used_pct, low, low_on}`; `used_pct` and `used_bytes` are null when the
  total is unknown, `low_on` lists which thresholds tripped: `free` and/or `used_pct`),
  `state_reason` (why the job is in its current state), `last_run_status`, `last_run_reason`, `last_run_note`,
  `late_means`, `informational`, `expect`, `never_run` (true until the first real run — manual cards say
  "Never run" explicitly), `created_at`,
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
    probe:                        # required for db_snapshot; optional for rclone_copy_tree / manual (box lists the dest)
      rclone_path: gdrive:km-tracker-backups
      state_dir: /state/km        # bind-mounted :ro host state dir
      interval_s: 1800            # optional: probe THIS dest every 1800 s instead of every PROBE_INTERVAL_S
                                  # cycle (big trees; see "Probe cadence and flap damping")
    manual:                       # only for kind: manual
      max_age_s: 604800
      max_lag_bytes: 10737418240
    disk:                         # only for kind: disk; omit both keys for an informational gauge
      min_free_bytes: 26843545600 # 25 GiB
      max_used_pct: 90
    expect: [km-tracker-app-1]    # only for kind: container — names that must appear in metrics.running
    late_means: Mac offline or asleep   # optional text shown instead of the generic LATE reason
```
Validation is strict and fails startup with the job id + field: ids `^[a-z0-9-]+$` (unique, ≤64), `machine`
∈ box|mac, `kind` ∈ the seven kinds, `cadence_s`+`grace_s` required for every kind except `manual` and
`disk` (and forbidden on both), `db_snapshot` requires `probe.rclone_path`, `rclone_copy_tree` requires
`destination`, a `probe` block is accepted only on `db_snapshot` / `rclone_copy_tree` / `manual`, a `disk`
block only on `disk` (with `max_used_pct` bounded to 1-100, so bytes pasted into the wrong field are
rejected rather than silently disabling the threshold), `container` requires a non-empty `expect`,
`probe.interval_s` is a positive int and on a `db_snapshot` may not exceed half the freshness window
(`cadence_s * DEST_FRESH_MULTIPLIER / 2`), unknown keys anywhere are errors.
State computation runs on every ping (for ALL jobs, so LATE keeps firing even if the ticker thread dies) and
on a 60 s ticker (so LATE fires without traffic). Every state transition is written to `state_changes` and
dispatched to ntfy (`NTFY_URL` + `NTFY_TOPIC` env; disabled when empty) with title `[dashboard] <job> → <STATE>`
and priority high for FAIL/STALE_DEST, default otherwise. The very first `UNKNOWN → OK` is recorded but not
alerted (it is not a recovery). ntfy failures are logged and swallowed — they never reach request handling.

### State precedence (as implemented)
Scheduled kinds (`db_snapshot`, `rclone_copy_tree`, `drive_mirror`, `container`, `probe`):
**never pinged** → `UNKNOWN` while `now − jobs.created_at ≤ cadence+grace`, then `LATE` ("never pinged, and
registered more than Ns ago — is the heartbeat installed?"; `late_means` wins if set) — a mis-installed
drop-in can't stay silent forever (metrics alone never count as a run) → `LATE` (silence > cadence+grace,
judged on server receive time; wins even if the last word was "fail", because silence is the more urgent
fact) → `FAIL` (last run status=fail, or a `container` job whose `metrics.running` lacks an `expect` name) →
kind-specific → `OK`. Kind-specific: `db_snapshot` → `STALE_DEST` when newest Drive object is older than
`cadence*12` AND (state-file `last_drive.sha256` ≠ heartbeat `metrics.db_sha256`, or — with no heartbeat sha
— the state file's `last_drive_push.epoch` is >1 h newer than anything on Drive); an unchanged DB with an old
newest file is OK (dedup-aware), and with neither sha nor epoch available we don't guess. `rclone_copy_tree`
→ `STALE_DEST` **only when `missing_files > 0` or `missing_bytes > 0`** (bytes that have never reached the
destination); `differ_files`/`differ_bytes` (present on both sides but edited locally since the nightly copy)
are informational lag shown on the card and mentioned in the OK reason — a working tree always trails its
03:00 snapshot, and judging on the combined figure is what made the `pa-backup` card flap. The legacy
combined `lag_bytes` no longer drives staleness for this kind. `drive_mirror` → `BEHIND` when
`pending + mismatch > 0` (the Mac probe reports each read of the mirror DB as an `ok` RUN, since reading it
IS the check; no DB → `fail`). `manual`: never LATE; `FAIL` if last run failed; `BEHIND` if age of last
success > `max_age_s` or `lag_bytes` > `max_lag_bytes`; no thresholds = informational, never alerts.
`max_age_s` is inert until the first real run (the card says **Never run** until then — seed one ping after
the first manual run). `disk`, in this precedence: `FAIL` when the newest word is a `fail` ping ("capacity
unreadable (<note>)" — the probe could not `statvfs` at all, or its wrapper reported that the probe never
ran); `UNKNOWN` until the first capacity metric arrives, but only for `state.DISK_FIRST_READING_GRACE_S`
(6 h) after registration — after that, a gauge with no reading at all is `LATE` too; `LATE` when the newest
reading is older than `state.DISK_METRIC_MAX_AGE_S` (48 h — a gauge nobody is feeding, or one whose
`last_metrics_at` will not parse: both time comparisons fail safe); `BEHIND` when
free < `min_free_bytes` or used-percent > `max_used_pct` (both comparisons strict, so a threshold reads as
"worse than this", not "at this"; the reason names the threshold that tripped and the actual figures); else
`OK`. Capacity arrives as `status: "metric"` pings, which are not runs — so the `fail` check compares the
failed run's receive time against `last_metrics_at` rather than just reading `last_run`: without that, one
transient `statvfs` error would pin the card to `FAIL` for ever, because a `metric` ping can never become
the newest `runs` row. `dashboard-probes` is the scheduler's own heartbeat: a run is recorded every probe
cycle, and it is `fail` (with the error in `note`) whenever any probed job is in a tripped failure state —
immediately for a hard error, after `PROBE_FAIL_THRESHOLD` **consecutive failed probes of that job** for a
transient one — so a broken probe shows up as a FAIL card, never a crash, and a single transient one does not
page (see below).

### Probe cadence and flap damping
Measured in production over a 4-day window: `dashboard-probes` flapped FAIL→OK **27 times**, pushing ~55 ntfy
alerts, because Google Drive answered `rateLimitExceeded` or the 90 s rclone timeout expired on 21 of 1159
`gdrive:Backups` probes (~1000 objects) and 6 of 1159 `gdrive:Gremlins` probes. The small DB-snapshot trees
failed 0 times, no failures were adjacent, and no destination was actually broken. The cost was not the noise
itself but what it did to the signal: a genuine `pa-backup` failure landed in the same window and was
indistinguishable from the flapping. Five mechanisms, all aimed at making a persistent failure legible rather
than at silencing failures:
1. **Per-job consecutive-failure damping.** `PROBE_FAIL_THRESHOLD` (default **2**, clamped to 1–10)
   consecutive failed probes **of one job** before `dashboard-probes` records a `fail` run. A damped failure
   records `ok` with the error kept in the run's `reason` ("probe-error damped (… failure 1 of 2 …)") and
   `note`, and the failed probe rows are written as always, so the board and job page never hide it.
   **Recovery is immediate** — one successful probe of the offending job.
   The streak is **derived from the `probes` table**, not stored as a counter, which is what makes it correct:
   it is per job, so a failure on a job probed every 1800 s is not erased by the cycles in which that job was
   not due (a cycle counter could never reach the threshold for such a job, so a permanently broken
   destination alerted *never*); a job's streak clears only when that job itself probes successfully; it
   survives a container restart; and the ingest route cannot write the `probes` table, so damping state is not
   forgeable by a holder of `INGEST_TOKEN` (it previously lived in an ingest-writable metric).
   A cycle in which nothing was due records the self-heartbeat with `reason: no probes due` — distinct from
   `probed` — and neither invents a success nor clears a pending failure.
2. **Hard errors are not damped at all.** Damping exists for quota/timeout noise. A failure whose text does
   not match `probes.TRANSIENT_MARKERS` — a missing directory, a revoked token — is not noise but the answer,
   and trips FAIL on the **first** failure, the same latency the undamped version had.
3. **A no-success backstop.** `PROBE_NO_SUCCESS_S` (default **3600 s**; 0 disables) trips FAIL for any probed
   job whose newest *successful* probe is older than that, whatever the streak arithmetic says: damping may
   delay an alert, it may never cancel one. 3600 s is 12 cycles at the 300 s default and equals the
   `db_snapshot` destination-freshness window (`cadence_s * 12`) — past that point the destination check is no
   longer being refreshed inside the window it is judged against, which is precisely the condition this job
   exists to report. It is far outside observed noise (isolated single failures, never adjacent), and it is
   what makes a threshold above 2 or a long `interval_s` safe to configure.
4. **`DASHBOARD_RCLONE_TIMEOUT_S` (default 240 s, was a hard-coded 90).** Drive pages a recursive listing 1000
   objects at a time and rclone backs off on rate limits; 90 s was itself a leading cause of "failure". The
   name is deliberately prefixed: `RCLONE_TIMEOUT` is rclone's own env var for `--timeout`, so a knob in that
   namespace would one rename later be silently reconfiguring rclone's networking. Probes are **serial**, so
   the real invariant is `n_probed_per_cycle * timeout < PROBE_INTERVAL_S`, not `timeout < PROBE_INTERVAL_S`.
   Two mechanisms enforce the consequence rather than leaving it to arithmetic: the effective timeout is
   clamped to at most `PROBE_INTERVAL_S`, and each cycle has a **wall-clock budget** of one
   `PROBE_INTERVAL_S`, after which the remaining due jobs are deferred (counted in `metrics.deferred`) and
   picked up next cycle. `due_probes()` orders least-recently-probed first, so deferral rotates and cannot
   starve the slowest job.
5. **Per-job `probe.interval_s`.** Probe a big tree every 1800 s instead of every 300 s cycle (≈6× less Drive
   traffic, which also reduces the rate limiting at source); absent = every cycle, i.e. the global
   `PROBE_INTERVAL_S`. Dueness is computed from the persisted `probes.probed_at`, so a restart does not
   re-probe a big tree early, and a skipped cycle leaves the previous listing standing (the card keeps its
   newest/count).
**Cycle cost and the clock.** A cycle is stamped and judged at its **end** (`now` + measured wall time): with
serial probes, recomputing against the start-of-cycle clock meant deadlines were judged against a clock up to
a full cycle stale, so LATE could lag ~2× the cycle duration. The scheduler likewise re-arms from the
**post**-cycle clock (`Core.last_cycle_end`) — from the start-of-cycle clock, an overrunning cycle made the
next one due the instant it returned, i.e. continuous back-to-back listing of the very remote that had just
rate-limited us. `dashboard-probes`' own `grace_s` must cover the worst-case cycle (budget + one timeout) on
top of its cadence, or a slow cycle trades FAIL flapping for LATE flapping; see the comment on that job in
`jobs.example.yml`.
**Why this cannot make a destination wrongly read stale:** `dest_fresh_s` = `cadence_s * 12` is compared
against the destination's **own newest-object timestamp**, an absolute time that does not drift as the probe
row ages — a longer interval only delays noticing a *new* object. The only kind whose state reads that age is
`db_snapshot`, and those keep the default interval (300 s against a 3600 s window); `registry.py` refuses an
`interval_s` above half the window for that kind so the invariant is enforced, not just reasoned about. The
two jobs set to 1800 s are `rclone_copy_tree` and `manual`, whose `STALE_DEST`/`BEHIND` verdicts come from
heartbeat `missing_*`/lag metrics, and whose freshness windows (12 d / `max_age_s` 14 d) dwarf 1800 s.
**Transient vs real:** a failure whose text matches `probes.TRANSIENT_MARKERS` (`rateLimitExceeded`, 429,
503/backendError, timeouts, …) is prefixed `transient (Drive quota/timeout):` in the probe row's `error` and
counted in `metrics.failed_transient`, and the self-job's `reason` names it — so "Drive pushed back" never
reads like "the destination is missing files". rclone echoes the path it was working on — annotated (`(dir
…)`), quoted, **and bare** (`open /srv/backups/…: permission denied`, usually the same name twice) — so all
three forms are stripped before classification, and 429/503 match only in their real HTTP spellings
(`Error 429:`, `code 503`, `503 Service Unavailable`) rather than as bare substrings. This is not only about
hostile names from a shared folder: the backup trees are dated, and `km_tracker-20260503-0312.db` contains
`503`, so bare-substring markers let a permission failure on an ordinary nightly snapshot buy itself damping
instead of paging at once. Transients are **not** exempt
from the consecutive count or the backstop: a *persistent* quota failure is a real problem — it is one way a
nightly backup stops working — and must still reach FAIL, including for a job with its own `interval_s`.

### Alerting rules

> **The governing rule: every ambiguity resolves toward paging, never toward silence.**
> Paging on every transition was noisy but *accidentally self-healing* — a dropped push, a reset clock or a
> weird intermediate state was corrected by the next transition, which paged again. One page per episode
> removes that net, so every path that can consume or reset the single page must be exactly right, and every
> undecidable case must err toward the phone. The rule applies to the safeguards themselves: each one keeps
> an episode alive, and an episode that never ends holds its unspent page hostage too — so each is bounded,
> and every bound is stated below and at its implementation in `services.py`.

**ntfy hears about a SUSTAINED problem, not a transition.** Measured in production, 09-07 → 09-11: ~55 pushes,
essentially all of them one job flapping on transient Google Drive errors. Graham's requirement was *"when
something fails on ntfy, it actually means something. Like it should be pretty rare. Only if like backups
missed more than 24 hours or something."* So:

- A job pages **once**, after it has been **continuously not-OK for its own `alert_after_s`** — an *episode*.
  It pages again only after that episode ends and a new one starts. A `→ OK` recovery is sent only if the
  episode was actually paged for.
- **The board is unchanged.** Every blip is still computed, written to `state_changes`, and shown on the card
  and in `/api/v1/status`. This filters the phone, not the history.
- **Dispatch is LEVEL-triggered, not edge-triggered.** `db.set_state` returns None when nothing changed, so an
  edge-driven dispatcher can never say "still FAIL, and now past six hours" — which is the whole event. The
  episode pass (`Core._resolve_alerts`) therefore walks **every** job on **every** recompute. Both write paths
  — the 60 s ticker (`recompute_all`) and every ping (`record_ping`) — run the identical pass.

**The episode** lives in two `jobs` columns, `bad_since` and `alerted_at` (additive `ALTER TABLE`; NULL for
every existing row, so a job already broken at upgrade time starts a fresh clock and pages one threshold late
— late, never silent). `jobs.since` cannot serve: it is reset by every state change, and a `LATE → FAIL`
mid-episode is the *same* outage. Episode state is deliberately **not** in `last_metrics`, which the ingest
route can write.

| rule | why |
|---|---|
| `bad_since` is set on the first not-OK recompute and never moved until the episode ends | the question is "has this been broken for a day?", not "did something change?" |
| `alerted_at` caps the episode at ONE page | the noise this feature exists to remove |
| `UNKNOWN` is not alertable and starts no episode; entering it **clears** the bookkeeping with no recovery | "no data yet" ≠ "broken" (real silence is already the never-pinged → LATE rule). Clearing matters: a paged job that passed through UNKNOWN used to keep `alerted_at` for ever and be un-pageable |
| a failed ntfy POST rolls `alerted_at` back to NULL so a later tick retries, keyed on **episode identity** (`bad_since`+`alerted_at`), never on "is the job not-OK now" | one unlucky POST otherwise bought permanent silence. The identity test is load-bearing: the dwell and the hold both keep an episode open while the job reads OK, so a state-based guard skips exactly the case it was written for |
| retries are spaced `ALERT_RETRY_MIN_S` (5 min) per episode; a **first** page is never delayed | "the next tick" is the ticker *plus* every ping — 72 blocking 5 s POSTs an hour into a dead ntfy, inside the scheduler thread and the ingest request |
| a failed **recovery** is logged (`recovery push for …`) and dropped | its episode is already closed, so there is nothing to hand it back to; re-sending later could announce "→ OK" for a job that has broken again. Losing it costs good news, never a page |
| an episode ends only after the job holds a **verified** OK for `min(5 min, alert_after_s/10)` (the *dwell*) | one OK tick used to end it, so a container on `restart: unless-stopped` backoff (19 min down, 1 min up — broken 95% of the day) reset its clock 72×/day and never paged. Cost: a recovery arrives up to 5 min late, which beats announcing a recovery that is about to be taken back |
| an **unverified** OK does not end the episode at all (the *hold*) | see below |
| an unparseable or **future** `bad_since` is healed — clock restarted, `alerted_at` dropped | an NTP step backwards makes `now − bad_since` permanently negative: a job that can never page |
| `alert_after_s` is capped at **30 days** at parse time, loudly | magnitude was the one hostile input the validator accepted; one extra digit silently means "never". Say `alert: never` if that is what you mean |

**"OK" is not always evidence of health.** `compute_state` must return one of six states, so a check that
*could not be made* falls through to OK on the heartbeat alone (`db_snapshot_stale` returning `None` matches
neither branch). Clearing the episode on that is how a genuinely stale backup goes quiet for ever: one failed
rclone listing a day resets a 24 h clock, and a failing destination probe is exactly what a rate-limited Drive
produces. Two shapes of unverified OK are held, on different grounds:

- **`state.ok_is_unverified`** — the destination check did not happen (no usable probe, no heartbeat evidence
  of its own). This holds a `STALE_DEST`/`BEHIND` episode only: those were statements *about the destination*.
  A `LATE`/`FAIL` episode is about silence or a failed run, which the returning heartbeat positively settles —
  holding those broke recovery for every unprobed job.
- **the damped self-heartbeat** — `dashboard-probes` writes its own run, and the flap damping above records it
  as `ok` while a probed destination is failing. That `ok` is not evidence of anything, so it holds whatever
  the episode was about. Derived from the `probes` table (`db.failing_probe_job_ids`), never from a metric.
  Without it, an alternating fail/damped-ok pattern resets the clock every few minutes and the page is
  deferred **indefinitely** — `PROBE_NO_SUCCESS_S` guarantees the *state* reaches FAIL, not that a page behind
  a timer ever fires.

Both holds are bounded, and the bound has two halves:

- **The hard ceiling.** While an episode is held, the *episode clock is still authoritative*: once it passes
  `alert_after_s` it pages anyway, naming the state it is really about (`snap: STALE_DEST for over 1h`, not
  the OK on the card). Without this, an episode that spends its whole threshold inside a blind window never
  gets to speak.
- **`ok_hold_s` = `max(5 min, alert_after_s)`.** After that much continuous OK the episode closes **silently**
  (no recovery — nothing was verified fixed). Held for ever, a destination that can never be probed again (a
  revoked remote, rclone's shared Drive OAuth client being retired) would pin `alerted_at` and mute every
  later failure of that job, including the backup dying outright. At most one threshold is spent on a
  destination we cannot see.

**Residual, stated rather than hidden:** the ceiling bounds the *window*, not the silence. A new failure that
lands while a hold is still running joins the still-open episode and gets no push of its own for as long as
that episode lasts. That is correct under one-page-per-episode — the job was never verifiably OK in between,
and it did page once — but it is the sharp edge. `dest.probe_error` on the card and `dashboard-probes` going
FAIL are what name a dead probe. Likewise, a job that recovers *verifiably* within one tick of crossing its
threshold closes its episode during the dwell without paging: the page is suppressed only while the job is in
a **verified** OK, which is a statement we can stand behind.

**Resolving a job's policy** (`registry.parse_job`, surfaced at `/api/v1/status` → `jobs[].alert` with a
`source` field so it never has to be inferred from `jobs.yml`):

| declared | result | `source` |
|---|---|---|
| `alert: never` | never pages | `alert` |
| `alert_after_s: N` | pages after N s continuously not-OK (0 = on the first not-OK recompute; still one page per episode) | `alert_after_s` |
| nothing, and `Job.informational` | never pages | `informational` |
| nothing otherwise | `DEFAULT_ALERT_AFTER_S` = 86400 | `default` |

`alert` and `alert_after_s` are mutually exclusive **checked on key presence** — with an `is not None` test,
`alert: never` + `alert_after_s: null` parsed to "never", i.e. silence that reads like a threshold in the file.
An explicit `alert_after_s` wins even on an informational job, so there is always a way to alert on one.
**`informational` now means what it always claimed.** `registry.py` documented it as "shown, never alerted
on" and *nothing consulted it* — such a job pushed on every transition like any other. It is now resolved into
the alert policy at parse time rather than being a second, overlapping concept.

**Shipped thresholds** (`jobs.example.yml` carries the per-job rationale):

| job | `alert_after_s` | why |
|---|---|---|
| `km-backup`, `todoist-points-backup` | 86400 (24 h) | 5-min cadence: one missed run is nothing, a day is a gap in the history |
| `pa-backup` | 108000 (30 h) | nightly, so 24 h could fire for a merely-late run; 30 h means a night was genuinely missed |
| `box-containers` | 1200 (20 m) | a container down IS the outage; long enough to ride out our own deploys and a reboot's restart storm |
| `dashboard-probes` | 21600 (6 h) | the job that flapped 27× in four days; six hours means the checks have really stopped |
| `drive-mirror` | 86400 (24 h) | pending uploads clear themselves once the Mac is awake |
| `mac-probe` | 259200 (72 h) | with its 15 h LATE deadline ≈ 87 h: a weekend with the lid shut pages nobody, a dead Mac pages once. **Never `alert: never`** — see below |
| `box-disk`, `mac-disk` | 3600 (1 h) | the gauge already has a 48 h fuse of its own (`DISK_METRIC_MAX_AGE_S`), so a day on top would mean hearing about a dead gauge at 72 h; and a capacity threshold is a *level*, not a flap — an hour only rides out a reading hovering at the boundary |
| `minecraft-offload`, `taste-twin-publish`, `jjho-refresh`, `baby-pool-sync` | `never` | on-demand; "behind" is information, not an incident |

- `UNKNOWN → OK` (first sighting) is never alerted.
- **Machine-offline rule:** each machine's `kind: probe` job (`mac-probe`; the dashboard's own
  `dashboard-probes` never counts) stands for "this machine is reachable". While it is LATE, the other jobs on
  that machine going LATE is the same single fact — the Mac is asleep — so their `→ LATE` alerts are
  suppressed and only the probe job's alert goes out ("Mac offline"). Recovery mirrors it: a sibling's plain
  `LATE → OK` is muted while the probe job is still LATE **or** recovers in the same recompute batch. The
  "still LATE" half is the one that matters in practice — `mac_probe.py` posts `pa-backup`, `drive-mirror`,
  then its own heartbeat as three separate HTTP requests (three recomputes), so the siblings always recover
  one batch *before* the probe. Net effect, tested end-to-end against `jobs.example.yml`: **one alert when the
  Mac goes quiet, one when it comes back.** A sibling waking into `FAIL` / `STALE_DEST` / `BEHIND` is not a
  plain recovery and alerts normally. Box jobs are never suppressed (the box has no standalone probe job).
  Graces on the hourly Mac jobs are 14 h so an ordinary night's sleep never pages, and every Mac sibling's
  `grace_s` is ≥ `mac-probe`'s + 120 s: the siblings are pinged seconds *before* the probe, so with equal
  graces their deadline would fall first and a 60 s ticker tick landing in that gap would page for the
  sibling (probe still OK → nothing to suppress against) and then again for the probe.
- ntfy body is `job_id: FROM → TO` only; title `[dashboard] <job name> → <STATE>`; priority high for
  FAIL/STALE_DEST.
