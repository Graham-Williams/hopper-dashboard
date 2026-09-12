# CLAUDE.md — hopper-dashboard

Jobs & backups dashboard for Graham's self-hosted estate: one page + one JSON endpoint answering, for every
scheduled or on-demand job, what it protects, how, last run, last success, whether the destination actually has
fresh bytes, and how far behind manual jobs are. Read `DESIGN.md` first — it is the spec and the frozen API
contract (the "State precedence (as implemented)" section there is the authoritative state-machine spec).

## Stack
Python 3.12, Flask 3.1.3, SQLite (WAL; the ingest process is the only writer), PyYAML for `jobs.yml`, gunicorn,
rclone inside the container for destination probes, ntfy for alerts. No CDN assets, no webfonts, and exactly
ONE inline script (the `<time datetime>` localizer in `base.html`) allowed by a per-request CSP nonce —
`default-src 'self'; script-src 'nonce-…'`. Docker + compose on the box behind the existing `km-tracker`
Cloudflare tunnel; shared `APP_PASSWORD` gate (same pattern as km-tracker / todoist-points / taste-twin /
jjho / baby-pool).

## Two roles, one container
`create_app(role)` builds either app; both load the same `jobs.yml` and share `data/dashboard.db`.

| role     | port | routes | extras |
|----------|------|--------|--------|
| `read`   | 8080 | `/`, `/jobs/<id>`, `/api/v1/status`, `/api/v1/jobs/<id>`, `/login`, `/logout`, `/healthz`, `/static/*` | password gate + `Authorization: Bearer $READ_TOKEN` on `/api/v1/*`; `APP_HOST` Host/Origin pin; per-IP **and global** failed-login caps; `CF-Connecting-IP` trusted only from `TRUSTED_PROXY_CIDR`; **fails fast** (`ConfigError`) if `APP_ENV=prod` without `APP_PASSWORD`, or `APP_PASSWORD` without `SESSION_SECRET` |
| `ingest` | 8081 | `POST /api/v1/ping/<id>`, `/healthz` (no static route) | bearer `INGEST_TOKEN`; rate-limit keyed on the TCP peer only; **single worker** — owns the scheduler thread (60 s state ticker + rclone probes every `PROBE_INTERVAL_S`) and all DB writes |

`entrypoint.sh` starts as root ONLY to copy the `:ro`-mounted 0600 host rclone.conf into a 0700 tmpfs dir
owned by `dashboard`, then `setpriv`s to `dashboard` (uid 10001, `--no-new-privs`) and re-execs itself;
as the app user it starts both gunicorns and exits if either dies (compose restarts the unit). There is no
`USER` in the Dockerfile on purpose — the drop happens in the entrypoint. `python -m dashboard` runs both
roles in one process for local dev.

## Run / test
```
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
# (or: python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt)
.venv/bin/python -m pytest -q                         # ~355 tests, no network, < 5 s
/usr/bin/python3 -m pytest -o addopts="" tests/test_probes_*.py -q   # ~100 probe tests, MUST pass stdlib-only
/usr/bin/python3 -m compileall -q probes/              # 3.9 syntax gate (CI runs this on 3.9 too)

cp jobs.example.yml jobs.yml                          # local only; gitignored
export APP_PASSWORD=devpass SESSION_SECRET=devsecret INGEST_TOKEN=devtoken READ_TOKEN=devread
# (APP_ENV=dev to run with the gate off; prod refuses to start without APP_PASSWORD)
.venv/bin/python -m dashboard                         # read http://127.0.0.1:8080  ingest :8081
# env knobs: DASHBOARD_DATA (default ./data locally, /app/data in the image), JOBS_FILE, READ_PORT,
#            INGEST_PORT, DASHBOARD_BIND, PROBE_INTERVAL_S, TICK_INTERVAL_S, DASHBOARD_NO_SCHEDULER=1

curl -X POST -H 'Authorization: Bearer devtoken' -d result=success -d exit=0 localhost:8081/api/v1/ping/km-backup
curl -X POST -H 'Authorization: Bearer devtoken' -H 'Content-Type: application/json' \
     -d '{"status":"ok","reason":"pushed","metrics":{"db_sha256":"…","bytes":1234}}' localhost:8081/api/v1/ping/km-backup
curl -H 'Authorization: Bearer devread' localhost:8080/api/v1/status | python3 -m json.tool

docker build -t hopper-dashboard .                    # digest-pinned base, checksum-verified rclone, HEALTHCHECK
docker compose up -d --build                          # on the box; needs .env + jobs.yml (see DEPLOY.md)
```
`docker compose config` fails loudly if any of the four secrets is missing (`${VAR:?}`). `curl` is purged
from the image — check health from inside with `docker exec hopper-dashboard python -c "import urllib.request as u; …"`.
Gotcha: the session cookie is `Secure`, so the browser gate only works over HTTPS (the tunnel). For local
browser testing either leave `APP_PASSWORD` unset (gate OFF) or use curl with a cookie jar.

## Module map (`dashboard/`)
- `__init__.py` — `create_app(role, settings=None, registry=None, notifier=None)`; wires limiters, filters.
- `__main__.py` — local dev runner (both roles, Werkzeug).
- `config.py` — `Settings` dataclass (`from_env()`); tests construct it directly.
- `registry.py` — `jobs.yml` schema + strict validation → `Registry` of frozen `Job`s. Kinds, states,
  `PROBEABLE_KINDS` (`probe` block: required on db_snapshot, optional on rclone_copy_tree / manual — the
  box's `gdrive-ro` remote can list `Backups/` and `Gremlins/`), and `DEST_FRESH_MULTIPLIER` (12) live here.
  `disk` is a kind but NOT in `SCHEDULED_KINDS`/`PROBEABLE_KINDS`: it is a capacity gauge (`disk:` block,
  `min_free_bytes` / `max_used_pct`), never probed, no cadence, and `informational` when both thresholds
  are omitted — same rule as a thresholdless `manual` job. It has no *per-cadence* dead-man's switch, but
  it is not exempt from silence: see `state.DISK_METRIC_MAX_AGE_S`.
- `db.py` — schema (`jobs` incl. `created_at`, `runs`, `probes`, `state_changes`), WAL connection, all
  queries, ISO helpers (`from_iso` clamps to 1970..9999 and never raises).
- `state.py` — pure state machine: `compute_state(job, Facts, now)`, `lag_info`, `dest_info`, `disk_info`
  (capacity block for `kind: disk`; `used_pct` is None on a 0/missing total — never a ZeroDivisionError),
  `db_snapshot_stale` (dedup-aware), `copy_tree_stale` (missing vs differ), never-pinged → LATE via
  `Facts.created_at`. Unit-tested with a fixed clock. The `disk` branch has three rules worth knowing
  before touching it: a `fail` ping outranks the stored figures (**compared against `last_metrics_at`**,
  because a successful capacity ping is `status: metric` and therefore never a run — without that
  comparison one transient `statvfs` error pins the card to FAIL for ever); a reading older than
  `DISK_METRIC_MAX_AGE_S` (48 h) is LATE, which is the only signal for a feeder that stopped on its own
  while its machine's probe job kept reporting OK; and `_num` caps metric magnitude as well as rejecting
  non-finite values (a numeric *string* metric bypasses the ingest ceiling).
  `used_pct` is `(total-available)/total`, so it does NOT match `df`'s `Use%` — the free bytes do. Say so
  rather than "fixing" it; DESIGN.md → `disk` and `probes/common.disk_free` carry the measurement.
- `services.py` — `Core`: record ping → shallow-merge metrics → recompute ALL jobs → persist transitions →
  notify (with the **machine-offline rule**: sibling `→ LATE` alerts muted while the machine's `probe` job is
  LATE, and sibling plain `LATE → OK` recoveries muted while the probe is still LATE or recovers in the same
  batch — the Mac probe posts its sub-jobs before its own heartbeat, so siblings recover one batch early;
  FAIL/STALE_DEST/BEHIND after LATE always alert); `run_probe_cycle` (probes + the `dashboard-probes`
  self-heartbeat + `prune` — one `DELETE … NOT IN (… ORDER BY id DESC LIMIT n)` per table, not O(n²)).
- `scheduler.py` — daemon thread; `step()` is exposed for tests; never lets an exception kill the loop.
- `probes.py` — `rclone lsjson --recursive --files-only` (argv, 90 s timeout) + state-file reader.
- `notify.py` — ntfy `Notifier`; body is `job_id: FROM → TO` only (no reason text leaves the box);
  `should_notify` suppresses `UNKNOWN→OK`; never raises.
- `ingest.py` — blueprint + pure payload parsers (`parse_json_payload`, `parse_form_payload`, `parse_metrics`).
- `web.py` — read blueprint: gate, host pin, security headers (per-request CSP nonce), HTML + JSON routes.
- `views.py` — builds the `/api/v1/status` contract and job detail from the store.
- `password_gate.py`, `ratelimit.py` — gate helpers (`client_ip(trusted_cidrs)` vs `remote_ip()`) +
  sliding-window limiters (hard key cap with stalest-eviction, keys truncated to 64 chars).
- `humanize.py` — relative/absolute times, human bytes/durations, `human_gib` (binary GiB, used for disk
  capacity — `human_bytes` is decimal GB) (Jinja filters).
- `templates/`, `static/app.css`, `static/favicon.svg` — theme-aware (light/dark tokens), mobile-first,
  Okabe–Ito state colours always paired with a text label + glyph.

## Conventions
- Timestamps in the DB are ISO-8601 UTC strings with `Z`; heartbeat "when" is **server receive time**
  (`received_at`), never the client's `started_at`/`finished_at`, so clock skew can't fake liveness.
- Only the ingest process writes. The read process opens short-lived connections and only SELECTs.
- `last_metrics` is a shallow merge across pings — never replace it wholesale (a bare form ping would erase
  `db_sha256`).
- Adding a job kind: extend `KINDS` + per-kind validation in `registry.py`, the branch in
  `state.compute_state`/`dest_info`, a test per transition in `tests/test_state.py`, and DESIGN.md.
  Prefer reusing an existing state over adding one to `STATES` — `disk` capacity reuses `BEHIND` rather
  than inventing a seventh state, which would have touched `notify.py`, `app.css`, the summary tiles and
  every state test for no new meaning.
- Adding a metric the UI should understand: read it via `state._first_num` with a key tuple (see
  `LAG_BYTES_KEYS`) so scripts can use either spelling.
- No inline `style=` attributes either: the CSP is `style-src 'self'` with no `'unsafe-inline'`, so a
  data-driven width must be an SVG attribute (see the `disk_gauge` / `history_strip` macros), not a style.
  A styled bar would render empty in a browser while passing every server-side test.
- JS is limited to the one nonce'd inline script in `base.html` (timestamp localization; the page must
  work identically without it). Don't add a second `<script>` — the CSP nonce is generated once per request
  via `csp_nonce()`, and anything else that needs interactivity should be reconsidered.
- Tests must stay network-free: mock `probes.probe_job` / `subprocess.run` and use `RecordingNotifier`.
- Test fixtures pin `jobs.created_at` to 2030 (`conftest.pin_created_at`) so the never-pinged → LATE rule
  only fires in the tests that set `created_at` explicitly. Remember this when a new test uses a fixed clock.
- Machine IPs, account ids and Drive folder ids never go in code, tests or docs — use
  `<box-tailscale-ip>`-style placeholders; real values live in the gitignored `.env`/`jobs.yml`/env files.
- Hopper's bearer reads go through the public hostname (`https://dashboard.graham-williams.com/api/v1/status`
  with `Authorization: Bearer $READ_TOKEN`) — that is the intended path. An in-container read against
  `127.0.0.1:8080` must also send `Host: <APP_HOST>` or the Host pin returns 403 (only `/healthz` is exempt).

## Git workflow
Feature branches only; `main` is protected and only Graham merges (via PR). Commit/push freely on branches.
Squash-merge; branches auto-delete on merge. Every PR description must match the diff — update it after every
follow-up push. CI (`.github/workflows/ci.yml`, `timeout-minutes: 15`) runs pytest and a docker build + smoke
of both roles.

## Secret safety
`.env`, `jobs.yml` (it lists real Drive paths/ids and machine topology), `rclone.conf`, `ingest.env`,
`*.sqlite*` and `data/` are gitignored. Only `.env.example` and `jobs.example.yml` are committed, with
placeholders. Never log tokens; failed logins log the IP only. `INGEST_TOKEN`/`READ_TOKEN` empty = fail
closed (every bearer request 401); `APP_PASSWORD` empty in prod = refuse to start. Install scripts never
accept a token on the command line (`--token-file` / hidden prompt only). Supply chain: `python:3.12-slim`
is digest-pinned in the Dockerfile, GitHub Actions are SHA-pinned in `ci.yml` (`permissions: contents: read`),
and rclone is checksum-verified.

## Self-maintenance
When you add or change a capability, job kind, endpoint, dependency, deploy step, or architectural decision,
update this file, `DESIGN.md` and `DEPLOY.md` before the task is done. This is how context persists for the
next agent that enters the repo.

## Probes (`probes/`, `deploy/`, `tests/test_probes_*.py`)
The push side. **Stdlib only, Python 3.9-compatible** — `probes/mac_probe.py` runs under macOS's stock
`/usr/bin/python3` from launchd (no venv, minimal PATH → rclone resolved at `/opt/homebrew/bin/rclone`
then `/usr/local/bin/rclone`). Pure logic lives in `probes/{common,backup_log,rclone_check,drivefs,containers}.py`
and is tested with fixtures and no network (`/usr/bin/python3 -m pytest -o addopts="" tests/test_probes_*.py -q`;
CI repeats this in a bare venv). **`DASHBOARD_URL` is required everywhere** (env file or environment) —
there is no default URL in the code, by design.
- Mac: hourly `com.hopper.dashboard-probe` (`deploy/mac/`) → `pa-backup` (log tail, deduped via
  `~/.config/hopper-dashboard/state.json`, + rclone check of the THREE trees the backup script copies —
  `rclone_check.PA_BACKUP_TREES`, filters copied verbatim from `scripts/backup-personal-assistant.sh` —
  reporting `missing_*` (never uploaded → stale) separately from `differ_*` (edited since → informational)),
  `minecraft-offload` (rclone lag per pair with `--size-only` — tens of GB of video can't be MD5'd hourly
  inside the 120 s timeout; the small pa-backup trees keep the checksum check — + disk free), `mac-disk`
  (`statvfs PROBE_DISK_PATH` as a metrics-only ping — its own gauge job; `minecraft-offload` keeps the same
  two metrics on purpose, don't "de-duplicate" them), `drive-mirror`
  (copied DriveFS sqlite, reported as an `ok` RUN because reading it is the check), then its own `mac-probe`
  heartbeat. `--dry-run` prints. Order matters for alerting: the siblings land BEFORE the probe's own
  heartbeat, which is why `jobs.example.yml` gives them `grace_s` ≥ mac-probe's + 120 and why `services.py`
  mutes their `LATE → OK` while the probe is still LATE.
- Box: systemd drop-ins `deploy/box/*.service.d/heartbeat.conf` (`ExecStopPost` curl with
  `$SERVICE_RESULT`/`$EXIT_STATUS`) + `dashboard-containers.timer` (`OnCalendar=*:0/5`, `Persistent=true`) →
  `dashboard-containers.service` (`User=@@USER@@` rendered by `install.sh`, default `$SUDO_USER`) →
  `deploy/box/containers_probe.sh`, which runs BOTH `probes/disk_probe.py` (`statvfs /` → `box-disk`,
  **first**: it is the cheap one, and `TimeoutStartSec=150` has to cover both) and
  `probes/containers_probe.py` (`docker ps` → `box-containers`) off that one timer; each runs even if the
  other fails and the service exits non-zero if either did. A non-zero exit is journal-only, so the wrapper
  ALSO posts the `fail` ping for a disk probe that never ran — `disk_probe.py` exits **3** when it already
  delivered a `fail` itself, and any other non-zero rc means nothing landed and the wrapper reports it.
  Keep that exit-code contract if you touch either file. `disk_free` lives in
  `probes/common.py` (re-exported from `rclone_check` for the Mac probe's existing call site) so the box
  probe doesn't import an rclone module to call `statvfs`. Credentials in `/etc/hopper-dashboard/ingest.env` (root 0600,
  read by systemd); `install.sh --token-file <compose .env>` reads the token, never from argv.
- Manual jobs: `probes/ping.sh <job_id> <ok|fail|skipped> [note]`. `minecraft-offload` needs one seed ping
  after the first offload or its `max_age_s` stays inert (card says "Never run").
- Metrics-only updates use `status: "metric"` (not a run); `ok|fail|skipped` are real runs.
- When the backup script's filter lists or trees change, update `rclone_check.PA_BACKUP_FILTERS`,
  `CLAUDE_CONFIG_FILTERS`, `PA_BACKUP_TREES` and their tests in the same change.
Full recipe + "how it fails quietly" table: `DEPLOY.md`. Verify shell with `bash -n`, the plist with
`plutil -lint`, and units on the box with `systemd-analyze verify --man=no` in a scratch dir.
