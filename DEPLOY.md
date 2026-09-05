# DEPLOY.md — hopper-dashboard

Step-by-step recipe for bringing the dashboard **and its probes** live. Written for Hopper to execute
over Tailscale SSH (`ssh graham@100.101.1.28`) and on the Mac. Follow the order — the app must be up
before the heartbeats have somewhere to land, and the ingest token is minted in step 1 and copied
everywhere else.

Two halves:
- **App** (owned by `dashboard/`, Dockerfile, compose): the read side on `:8080` (behind the tunnel +
  password gate) and the ingest side published ONLY on the box's Tailscale IP `100.101.1.28:8081`.
- **Probes** (`probes/`, `deploy/`): what pushes heartbeats/metrics INTO the ingest port — systemd drop-ins
  and a container timer on the box, an hourly launchd job on the Mac, and `probes/ping.sh` for manual jobs.

Job ids are fixed and must match `jobs.yml` (unknown id → 404, by design):
`box`: `km-backup`, `todoist-points-backup`, `box-containers`, `dashboard-probes` ·
`mac`: `mac-probe`, `pa-backup`, `drive-mirror`, `minecraft-offload`, `taste-twin-publish`, `jjho-refresh`,
`baby-pool-sync`.

---

## 1. Box — app container

```bash
ssh graham@100.101.1.28
git clone https://github.com/Graham-Williams/hopper-dashboard ~/hopper-dashboard
cd ~/hopper-dashboard

# .env — never committed. APP_PASSWORD is the shared house password (same word as km/todoist/taste-twin/jjho).
cp .env.example .env && chmod 600 .env
grep '^APP_PASSWORD=' ~/km-tracker/.env                      # copy this value into .env
sed -i "s|^SESSION_SECRET=.*|SESSION_SECRET=$(openssl rand -hex 32)|" .env
sed -i "s|^INGEST_TOKEN=.*|INGEST_TOKEN=$(openssl rand -hex 32)|" .env
sed -i "s|^READ_TOKEN=.*|READ_TOKEN=$(openssl rand -hex 32)|" .env
sed -i "s|^NTFY_TOPIC=.*|NTFY_TOPIC=hopper-$(openssl rand -hex 16)|" .env
grep -q '^INGEST_BIND=' .env || echo 'INGEST_BIND=100.101.1.28' >> .env   # Tailscale IP ONLY — ufw is inactive
$EDITOR .env                                                  # set APP_PASSWORD; leave APP_HOST=dashboard.graham-williams.com

# jobs.yml — never committed (real Drive folder ids + machine topology).
cp jobs.example.yml jobs.yml
# Fill the drive_mirror root ids from DESIGN.md → "drive_mirror" (My Mac / Documents / Desktop /
# minecraft-channel). They are folder ids, not secrets, but they stay out of the repo.
$EDITOR jobs.yml

docker compose up -d --build
docker exec hopper-dashboard curl -fsS localhost:8080/healthz      # read side, inside the container
docker exec hopper-dashboard curl -fsS localhost:8081/healthz      # ingest side, inside the container
```

From the **Mac** (over Tailscale) — this is the path every Mac heartbeat will take:

```bash
curl -fsS http://100.101.1.28:8081/healthz
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://100.101.1.28:8081/api/v1/ping/mac-probe   # expect 401 (no token)
```

Also confirm the ingest port is **not** on the LAN interface: `ss -ltnp | grep 8081` on the box must show
`100.101.1.28:8081` only, never `0.0.0.0:8081`.

## 2. Box — heartbeats (systemd drop-ins + container timer)

```bash
cd ~/hopper-dashboard
sudo deploy/box/install.sh --token "$(grep '^INGEST_TOKEN=' .env | cut -d= -f2-)"
```

What it does (idempotent; never restarts a container or the backup units):
- writes `/etc/hopper-dashboard/ingest.env` (root:root **0600** — systemd reads `EnvironmentFile=` as PID 1
  even though the units run `User=graham`, so graham never needs to read it),
- installs `/etc/systemd/system/{km-backup,todoist-points-backup}.service.d/heartbeat.conf`
  (`ExecStopPost=-/usr/bin/curl … result=${SERVICE_RESULT} exit=${EXIT_STATUS}` → `/api/v1/ping/<unit>`;
  fires on success, failure **and** the 300 s timeout kill; `-` prefixes mean a dead dashboard can never
  change the backup unit's own result),
- installs + enables `dashboard-containers.timer` (every 5 min → `deploy/box/containers_probe.sh` →
  `docker ps` → `box-containers` ping; the dashboard container itself has no docker socket),
- `daemon-reload`, then prints `systemd-analyze verify`, `systemctl cat`, `list-timers` and a dry run.

Verify within 5 minutes (the backup timers tick every 5 min, so all three box jobs should report):

```bash
systemctl list-timers --no-pager | grep -E 'km-backup|todoist-points-backup|dashboard-containers'
journalctl -u dashboard-containers.service -n 3 --no-pager          # "sent box-containers ok → HTTP 200"
journalctl -u km-backup.service -n 20 --no-pager | grep -i curl      # should be silent; errors would show here
# read side, from the box via the compose network (no cookie needed with the READ_TOKEN):
docker exec hopper-dashboard curl -fsS -H "Authorization: Bearer $(grep '^READ_TOKEN=' .env | cut -d= -f2-)" \
  localhost:8080/api/v1/status | python3 -m json.tool | grep -E '"id"|"state"'
```

`km-backup`, `todoist-points-backup`, `box-containers` should be `OK`; the Mac jobs are still `UNKNOWN`.

## 3. Cloudflare — public read side

Follow the **Cloudflare** section of `~/personal-assistant/CLAUDE.md` (token `cloudflare-api-token` in the
Hopper vault, field `api_token`; cache to a gitignored `chmod 600 .env.cloudflare-session`). Tunnel
`km-tracker` (`5782cc53-4741-4a7d-80ec-89c87070b7be`) is remotely managed, so ingress lives in the API:

1. Append an ingress rule **before the catch-all 404**: hostname `dashboard.graham-williams.com` →
   service `http://hopper-dashboard:8080` (compose service name; the container joins `km-tracker_default`).
2. Create a **proxied CNAME** `dashboard` → `5782cc53-4741-4a7d-80ec-89c87070b7be.cfargotunnel.com`.
3. **Single-label host only** (`dashboard`, never `dash.hopper`) — Universal SSL covers one label.
4. No Cloudflare Access app: the gate is the in-app shared `APP_PASSWORD` (account has zero Access apps).

Verify: `curl -sS -o /dev/null -w '%{http_code}\n' https://dashboard.graham-williams.com/` → `302` to
`/login`; `https://dashboard.graham-williams.com/healthz` → `200`; the **ingest** path must NOT be reachable
through the tunnel (`curl -X POST https://dashboard.graham-williams.com/api/v1/ping/mac-probe` → 404/405,
never 401 — 401 would mean ingest is exposed publicly).

## 4. Mac — hourly probe + phone alerts

```bash
cd ~/code/hopper-dashboard && git pull
deploy/mac/install.sh --token '<INGEST_TOKEN from the box .env>'     # prompts (hidden) if --token is omitted
```

What it does (idempotent): writes `~/.config/hopper-dashboard/env` (chmod 600, never overwritten if present),
renders `deploy/mac/com.hopper.dashboard-probe.plist` → `~/Library/LaunchAgents/` with absolute paths
(`/usr/bin/python3`, this checkout), `launchctl bootout` + `bootstrap`, then runs `probes/mac_probe.py
--dry-run` and prints the four pings it would send. `RunAtLoad` means the first real run happens immediately;
then hourly (`StartInterval 3600`), and again after every login/wake.

The probe posts:
- `pa-backup` — last line of `~/Library/Logs/hopper-backup.log` as an `ok`/`fail` run (**once per new line**,
  deduped via `~/.config/hopper-dashboard/state.json`) + a `metric` ping with `lag_files`/`lag_bytes`
  (files under `~/personal-assistant` missing from or differing at `gdrive:Backups/personal-assistant`, using
  the backup script's exact filter list) and `dest_count`/`dest_bytes`.
- `minecraft-offload` — `metric`: bytes/files under `~/minecraft-channel/{recordings,world backups,replays}`
  not yet on `gdrive:Gremlins/…` (`rclone check --one-way --min-age 15m`; pairs parsed live from
  `scripts/offload-recordings.sh`), per-pair keys `lag_bytes_<pair>`, plus `disk_free_bytes` for
  `/System/Volumes/Data`. Never uploads.
- `drive-mirror` — `metric`: DriveFS mirror queue (`pending`), `mismatch`, `roots`, `roots_list`, `db_age_s`
  from a **copy** of `mirror_sqlite.db{,-wal,-shm}`; `fail` ping if no mirror db exists.
- `mac-probe` — its own heartbeat: `ok`, or `fail` with a note naming the sub-probes that errored.

Verify:

```bash
launchctl list | grep com.hopper.dashboard-probe            # "-  0  com.hopper.dashboard-probe" → last exit 0
tail -5 ~/Library/Logs/hopper-dashboard-probe.log            # "sent pa-backup … HTTP 200" … "run ok in Ns"
/usr/bin/python3 ~/code/hopper-dashboard/probes/mac_probe.py --dry-run --only drive-mirror
```

**Phone alerts (Graham's manual step):** install the **ntfy** app (iOS/Android), tap *Subscribe to topic*,
enter the `NTFY_TOPIC` value from the box `.env` (server `https://ntfy.sh`). The topic name is the only
secret — anyone who knows it can read alerts, so don't paste it anywhere else. Test from the box:
`curl -d "dashboard test" https://ntfy.sh/$(grep '^NTFY_TOPIC=' ~/hopper-dashboard/.env | cut -d= -f2-)`.

### Manual jobs: `probes/ping.sh`

`taste-twin-publish`, `jjho-refresh` and `baby-pool-sync` have nothing to compute on a timer; they are
`kind: manual` with a max-age target, and the run that Hopper performs by hand ends by pinging:

```bash
P=~/code/hopper-dashboard/probes/ping.sh
# taste-twin (Mac; runs the pipeline on the residential IP then ships the report to the box)
cd ~/code/taste-twin && python scripts/publish.py mhgaillo && $P taste-twin-publish ok "mhgaillo" \
  || $P taste-twin-publish fail "publish.py failed"
# jjho transcript/episode refresh (Mac-generated DB shipped to the box)
$P jjho-refresh ok "spine+transcripts refreshed, DB shipped"        # or: $P jjho-refresh skipped "no new episodes"
# baby-pool Sheet → entries.json → box
$P baby-pool-sync ok "entries.json re-synced"
```

`ping.sh <job_id> <ok|fail|skipped> [note]` reads `DASHBOARD_URL`/`INGEST_TOKEN` from
`~/.config/hopper-dashboard/env` (override with `HOPPER_DASHBOARD_ENV=/etc/hopper-dashboard/ingest.env` on the
box), accepts `REASON=`, `EXIT_CODE=`, `STARTED_AT=`, `METRICS_JSON='{"files":3}'`, and `DRY_RUN=1` prints
instead of sending. Do **not** edit the other repos to call it; the call belongs to the Hopper run that wraps
them (a future hardening is a `--ping` flag in each script, but that is per-repo work).

## 5. Verification checklist

- [ ] `docker ps` shows `hopper-dashboard` healthy; `ss -ltnp | grep 8081` shows only `100.101.1.28:8081`.
- [ ] `https://dashboard.graham-williams.com/` → login page; `/healthz` → 200; `POST /api/v1/ping/...` via the
      public host is **not** 401 (ingest isn't tunnelled).
- [ ] `/api/v1/status` (READ_TOKEN) lists all 11 jobs; after ≤5 min box jobs are `OK`, after ≤1 h Mac jobs are
      `OK`/`BEHIND` (not `UNKNOWN`), manual jobs are `UNKNOWN` until their first `ping.sh`.
- [ ] Kill test: `sudo systemctl stop dashboard-containers.timer` → `box-containers` goes `LATE` after
      cadence+grace and an ntfy alert arrives; `start` → recovery alert. Re-enable afterwards.
- [ ] Fail test (safe): `~/code/hopper-dashboard/probes/ping.sh jjho-refresh fail "drill"` → `FAIL` + alert;
      then `… ok "drill over"` → recovery.
- [ ] `INVENTORY.md` in `~/personal-assistant` updated: new container, new public hostname, new box timer,
      new Mac launchd job, new credential locations (`/etc/hopper-dashboard/ingest.env`,
      `~/.config/hopper-dashboard/env`, box `.env`).

## Rollback

```bash
# Mac
~/code/hopper-dashboard/deploy/mac/uninstall.sh [--purge]        # --purge also removes the token + state
# Box — heartbeats (backup units keep running exactly as before)
sudo ~/hopper-dashboard/deploy/box/uninstall.sh [--purge]        # --purge also removes /etc/hopper-dashboard
# Box — app
cd ~/hopper-dashboard && docker compose down                     # add -v to drop the derived SQLite volume
# Cloudflare: delete the ingress rule + CNAME (reverse of step 3)
```

Nothing in the probes writes to Drive, to the backup repos, or to any container, so rollback never has to
restore data; the dashboard's own SQLite is derived state that repopulates within one probe cycle.

## How it fails quietly (INVENTORY.md spirit)

| Component | Quiet failure mode | What catches it / how to check |
|---|---|---|
| App container down | Every job goes `LATE` at once; no alerts because the alerter is the thing that died | `dashboard-probes` self-heartbeat is a job — but it can't alert on itself. Hopper's weekly health watch must `curl /healthz`; `docker ps` on the box. Consider an external dead-man (Healthchecks.io) later. |
| Ingest bound to `0.0.0.0` | Token-only protection exposed to the LAN | `ss -ltnp \| grep 8081` on the box; `INGEST_BIND` in `.env` |
| Drop-in not loaded (no `daemon-reload`, typo in path) | Backups run fine, dashboard shows `km-backup` `LATE`/`UNKNOWN` forever — looks like a backup problem | `systemctl cat km-backup.service` must show `heartbeat.conf`; `journalctl -u km-backup.service` shows curl errors if the URL/token are wrong (`-fsS` prints them) |
| `/etc/hopper-dashboard/ingest.env` missing/rotated token | `ExecStopPost` curl 401s, silently ignored (`-` prefix) → `LATE` | `journalctl -u km-backup.service \| grep 401`; compare token with box `.env` |
| Backup script exit 0 on skipped push | Heartbeat says `ok` while Drive hasn't received anything new | That's what the app's destination probe (`db_snapshot` newest object vs `db_sha256`) is for → `STALE_DEST` |
| `dashboard-containers.timer` stopped / graham dropped from `docker` group | `box-containers` `LATE`; or `fail` ping with "permission denied" in the note | `systemctl list-timers`; `journalctl -u dashboard-containers.service` |
| Mac asleep / logged out | `mac-probe` + all Mac jobs `LATE` together | That IS the signal (Mac offline). If only some Mac jobs are late, read the `mac-probe` note — it names the sub-probe that errored. |
| launchd job unloaded (plist edited by hand, Mac migrated) | Same as asleep, but permanent | `launchctl list \| grep com.hopper.dashboard-probe`; re-run `deploy/mac/install.sh` |
| rclone missing/moved (Homebrew relink) | `pa-backup`/`minecraft-offload` metrics stop; `mac-probe` `fail` with "rclone not found" | `mac-probe` note; `~/Library/Logs/hopper-dashboard-probe.log` |
| rclone Drive token expired | `rclone check` errors → sub-probe `fail`, lag unknown | Same log; `rclone lsd gdrive:` interactively |
| `hopper-backup.log` stops being written (launchd backup job unloaded) | `pa-backup` never gets a new run ping → `LATE` after 24 h + grace; the metric ping keeps flowing and `lag_files` climbs | Both signals appear on the card; `launchctl list \| grep personal-assistant-backup` |
| Backup log line format changes | Reported as `fail` with `reason=unparseable-log` — loud, not silent | Fix `probes/backup_log.py` |
| DriveFS schema drift (Google update renames a table) | `drive-mirror` `fail` with "schema drift: missing table" | Re-run the spike in DESIGN.md → `drive_mirror`; update `probes/drivefs.py` + fixture |
| DriveFS not running | `db_age_s` grows without bound while `pending` stays 0 — looks caught up | Alert/threshold on `db_age_s` in the app (suggested: > 6 h) |
| State file corrupt | Treated as "never reported" → the last backup line is re-sent once (harmless duplicate) | Nothing to do; it self-heals on the next successful send |
| Ingest 404 for a job (id typo / not in `jobs.yml`) | Probe logs `rejected: HTTP 404`, `mac-probe` `fail` | `mac-probe` note; the id list at the top of this file |
| `ping.sh` never called after a manual run | Manual job goes `BEHIND` past `max_age_s` even though the work was done | Make the ping part of the documented Hopper procedure for each manual job |
| ntfy topic leaked or mistyped on the phone | Alerts fire (server-side) but never arrive | `curl -d test https://ntfy.sh/<topic>` and check the phone |
