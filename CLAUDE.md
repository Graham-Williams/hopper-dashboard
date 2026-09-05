# CLAUDE.md — hopper-dashboard

Jobs & backups dashboard for Graham's self-hosted estate: one page + one JSON endpoint answering, for every
scheduled or on-demand job, what it protects, how, last run, last success, whether the destination actually has
fresh bytes, and how far behind manual jobs are. Read `DESIGN.md` first — it is the spec and the frozen API
contract (the "State precedence (as implemented)" section there is the authoritative state-machine spec).

## Stack
Python 3.12, Flask 3, SQLite (WAL; the ingest process is the only writer), PyYAML for `jobs.yml`, gunicorn,
rclone inside the container for destination probes, ntfy for alerts. No JS, no CDN assets, no webfonts — the
CSP is `default-src 'self'; script-src 'none'`. Docker + compose on the box behind the existing `km-tracker`
Cloudflare tunnel; shared `APP_PASSWORD` gate (same pattern as km-tracker / todoist-points / taste-twin /
jjho / baby-pool).

## Two roles, one container
`create_app(role)` builds either app; both load the same `jobs.yml` and share `data/dashboard.db`.

| role     | port | routes | extras |
|----------|------|--------|--------|
| `read`   | 8080 | `/`, `/jobs/<id>`, `/api/v1/status`, `/api/v1/jobs/<id>`, `/login`, `/logout`, `/healthz`, `/static/*` | password gate + `Authorization: Bearer $READ_TOKEN` on `/api/v1/*`; `APP_HOST` Host/Origin pin |
| `ingest` | 8081 | `POST /api/v1/ping/<id>`, `/healthz` | bearer `INGEST_TOKEN`; **single worker** — owns the scheduler thread (60 s state ticker + rclone probes every `PROBE_INTERVAL_S`) and all DB writes |

`entrypoint.sh` copies the `:ro`-mounted rclone.conf to tmpfs, starts both gunicorns and exits if either dies
(compose restarts the unit). `python -m dashboard` runs both roles in one process for local dev.

## Run / test
```
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
# (or: python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt)
.venv/bin/python -m pytest -q                         # ~150 tests, no network, < 5 s

cp jobs.example.yml jobs.yml                          # local only; gitignored
export APP_PASSWORD=devpass SESSION_SECRET=devsecret INGEST_TOKEN=devtoken READ_TOKEN=devread
.venv/bin/python -m dashboard                         # read http://127.0.0.1:8080  ingest :8081
# env knobs: DASHBOARD_DATA (default ./data locally, /app/data in the image), JOBS_FILE, READ_PORT,
#            INGEST_PORT, DASHBOARD_BIND, PROBE_INTERVAL_S, TICK_INTERVAL_S, DASHBOARD_NO_SCHEDULER=1

curl -X POST -H 'Authorization: Bearer devtoken' -d result=success -d exit=0 localhost:8081/api/v1/ping/km-backup
curl -X POST -H 'Authorization: Bearer devtoken' -H 'Content-Type: application/json' \
     -d '{"status":"ok","reason":"pushed","metrics":{"db_sha256":"…","bytes":1234}}' localhost:8081/api/v1/ping/km-backup
curl -H 'Authorization: Bearer devread' localhost:8080/api/v1/status | python3 -m json.tool

docker build -t hopper-dashboard .                    # pinned, checksum-verified rclone; non-root; HEALTHCHECK
docker compose up -d --build                          # on the box; needs .env + jobs.yml (see DEPLOY.md)
```
Gotcha: the session cookie is `Secure`, so the browser gate only works over HTTPS (the tunnel). For local
browser testing either leave `APP_PASSWORD` unset (gate OFF) or use curl with a cookie jar.

## Module map (`dashboard/`)
- `__init__.py` — `create_app(role, settings=None, registry=None, notifier=None)`; wires limiters, filters.
- `__main__.py` — local dev runner (both roles, Werkzeug).
- `config.py` — `Settings` dataclass (`from_env()`); tests construct it directly.
- `registry.py` — `jobs.yml` schema + strict validation → `Registry` of frozen `Job`s. Kinds, states, and
  `DEST_FRESH_MULTIPLIER` (12) live here.
- `db.py` — schema (`jobs`, `runs`, `probes`, `state_changes`), WAL connection, all queries, ISO helpers.
- `state.py` — pure state machine: `compute_state(job, Facts, now)`, `lag_info`, `dest_info`,
  `db_snapshot_stale` (dedup-aware). Unit-tested with a fixed clock.
- `services.py` — `Core`: record ping → shallow-merge metrics → recompute ALL jobs → persist transitions →
  notify; `run_probe_cycle` (probes + the `dashboard-probes` self-heartbeat + `prune`).
- `scheduler.py` — daemon thread; `step()` is exposed for tests; never lets an exception kill the loop.
- `probes.py` — `rclone lsjson --recursive --files-only` (argv, 90 s timeout) + state-file reader.
- `notify.py` — ntfy `Notifier`; `should_notify` suppresses `UNKNOWN→OK`; never raises.
- `ingest.py` — blueprint + pure payload parsers (`parse_json_payload`, `parse_form_payload`, `parse_metrics`).
- `web.py` — read blueprint: gate, host pin, security headers, HTML + JSON routes.
- `views.py` — builds the `/api/v1/status` contract and job detail from the store.
- `password_gate.py`, `ratelimit.py` — ported gate helpers + sliding-window limiters.
- `humanize.py` — relative/absolute times, human bytes/durations (Jinja filters).
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
- Adding a metric the UI should understand: read it via `state._first_num` with a key tuple (see
  `LAG_BYTES_KEYS`) so scripts can use either spelling.
- No JS. If something needs interactivity, reconsider; the CSP forbids scripts on purpose.
- Tests must stay network-free: mock `probes.probe_job` / `subprocess.run` and use `RecordingNotifier`.

## Git workflow
Feature branches only; `main` is protected and only Graham merges (via PR). Commit/push freely on branches.
Squash-merge; branches auto-delete on merge. Every PR description must match the diff — update it after every
follow-up push. CI (`.github/workflows/ci.yml`, `timeout-minutes: 15`) runs pytest and a docker build + smoke
of both roles.

## Secret safety
`.env`, `jobs.yml` (it lists real Drive paths/ids and machine topology) and `data/` are gitignored. Only
`.env.example` and `jobs.example.yml` are committed, with placeholders. Never log tokens; failed logins log the
IP only. `INGEST_TOKEN`/`READ_TOKEN` empty = fail closed (every bearer request 401).

## Self-maintenance
When you add or change a capability, job kind, endpoint, dependency, deploy step, or architectural decision,
update this file, `DESIGN.md` and `DEPLOY.md` before the task is done. This is how context persists for the
next agent that enters the repo.

## Probes (`probes/`, `deploy/`, `tests/test_probes_*.py`)
The push side. **Stdlib only, Python 3.9-compatible** — `probes/mac_probe.py` runs under macOS's stock
`/usr/bin/python3` from launchd (no venv, minimal PATH → rclone resolved at `/opt/homebrew/bin/rclone`
then `/usr/local/bin/rclone`). Pure logic lives in `probes/{common,backup_log,rclone_check,drivefs,containers}.py`
and is tested with fixtures and no network (`/usr/bin/python3 -m pytest tests/test_probes_*.py -q`).
- Mac: hourly `com.hopper.dashboard-probe` (`deploy/mac/`) → `pa-backup` (log tail, deduped via
  `~/.config/hopper-dashboard/state.json`, + rclone dest lag), `minecraft-offload` (rclone lag per pair +
  disk free), `drive-mirror` (copied DriveFS sqlite), then its own `mac-probe` heartbeat. `--dry-run` prints.
- Box: systemd drop-ins `deploy/box/*.service.d/heartbeat.conf` (`ExecStopPost` curl with
  `$SERVICE_RESULT`/`$EXIT_STATUS`) + `dashboard-containers.timer` → `probes/containers_probe.py` (`docker ps`).
  Credentials in `/etc/hopper-dashboard/ingest.env` (root 0600, read by systemd).
- Manual jobs: `probes/ping.sh <job_id> <ok|fail|skipped> [note]`.
- Metrics-only updates use `status: "metric"` (not a run); `ok|fail|skipped` are real runs.
Full recipe + "how it fails quietly" table: `DEPLOY.md`. Verify shell with `bash -n`, the plist with
`plutil -lint`, and units on the box with `systemd-analyze verify --man=no` in a scratch dir.
