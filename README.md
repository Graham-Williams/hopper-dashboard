# hopper-dashboard

A small self-hosted dashboard that shows the health of every backup and scheduled job across one home server
and one Mac — last run, last success, whether the destination really has fresh bytes, and how far behind the
manual jobs are. API-first (JSON) with an HTML board on top, dead-man's-switch heartbeats, destination probes
via rclone, and push alerts via ntfy.

Each job is one card with a state chip: **OK**, **LATE** (no heartbeat within cadence+grace), **FAIL**,
**STALE DEST** (the job said ok but the destination disagrees), **BEHIND** (manual job over its lag/age
target) or **UNKNOWN** (never heard from). Every transition is recorded and pushed to a private ntfy topic.

## Quick start (local)
```
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
cp jobs.example.yml jobs.yml
export INGEST_TOKEN=devtoken READ_TOKEN=devread APP_ENV=dev   # APP_ENV=dev + no APP_PASSWORD = gate off (prod refuses)
.venv/bin/python -m dashboard                             # board: http://127.0.0.1:8080  ingest: :8081
curl -X POST -H 'Authorization: Bearer devtoken' -d result=success http://127.0.0.1:8081/api/v1/ping/km-backup
```
`.venv/bin/python -m pytest -q` runs the suite (no network needed).

## How jobs report in
- **Heartbeats** — `POST /api/v1/ping/<job_id>` with `Authorization: Bearer $INGEST_TOKEN`, either JSON
  (`{"status":"ok|fail|skipped|metric", "reason":…, "metrics":{…}}`) or the form body a systemd
  `ExecStopPost` curl sends (`result=$SERVICE_RESULT&exit=$EXIT_STATUS`). Ingest is published only on the
  box's Tailscale IP. A scheduled job that has never pinged goes LATE after its cadence+grace, so a heartbeat
  that was never wired up can't hide as "new".
- **Destination probes** — the container runs `rclone lsjson` against each `db_snapshot` job's Drive folder
  and cross-checks the backup script's state files, so "the backup said ok" and "Drive has the bytes" are
  verified independently.
- **Mac probe** — an hourly launchd job posts what only the Mac can see (offload lag, Drive mirror state,
  nightly backup outcome, split into never-uploaded vs edited-since). If the Mac is asleep you get one
  "Mac offline" alert, not one per Mac job.

See `DESIGN.md` for the design and API contract, `CLAUDE.md` for how to run and test, `DEPLOY.md` for the box
recipe. Jobs are declared in `jobs.yml` (gitignored; start from `jobs.example.yml`).
