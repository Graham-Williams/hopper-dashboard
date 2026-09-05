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
