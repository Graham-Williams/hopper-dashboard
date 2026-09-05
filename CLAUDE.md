# CLAUDE.md — hopper-dashboard

Jobs & backups dashboard for Graham's self-hosted estate: one page + one JSON endpoint answering, for every
scheduled or on-demand job, what it protects, how, last run, last success, whether the destination actually has
fresh bytes, and how far behind manual jobs are. Read `DESIGN.md` first — it is the spec and the frozen API
contract.

## Stack
Python 3.12, Flask, SQLite (single writer: the app), PyYAML for `jobs.yml`, rclone inside the container for
destination probes, ntfy for alerts. Docker + compose on the box behind the existing `km-tracker` Cloudflare
tunnel; shared `APP_PASSWORD` gate (same pattern as km-tracker / todoist-points / taste-twin / jjho / baby-pool).

## Run / test
```
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt -r requirements-dev.txt
cp jobs.example.yml jobs.yml && cp .env.example .env   # local only
python -m pytest -q
DASHBOARD_DATA=./data python -m dashboard            # serves read side :8080 and ingest :8081
```

## Git workflow
Feature branches only; `main` is protected and only Graham merges (via PR). Commit/push freely on branches.
Squash-merge; branches auto-delete on merge. Every PR description must match the diff — update it after every
follow-up push.

## Secret safety
`.env`, `jobs.yml` (it lists real Drive paths/ids and machine topology) and `data/` are gitignored. Only
`.env.example` and `jobs.example.yml` are committed, with placeholders. Never log tokens.

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
