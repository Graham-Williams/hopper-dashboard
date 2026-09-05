# DEPLOY.md — hopper-dashboard

Step-by-step recipe for bringing the dashboard **and its probes** live. Written for Hopper to execute
over Tailscale SSH (`ssh <user>@<box-tailscale-ip>`) and on the Mac, **non-interactively** — every
edit below is a `sed`/heredoc, never `$EDITOR`. Follow the order — the app must be up before the
heartbeats have somewhere to land, and the ingest token is minted in step 1 and copied everywhere else.

`<box-tailscale-ip>` is the box's Tailscale address (`tailscale ip -4` on the box). It is deployment
config, deliberately absent from the code and from this file: it goes into the box `.env`
(`INGEST_BIND`) and into the two probe env files (`DASHBOARD_URL`).

Two halves:
- **App** (owned by `dashboard/`, Dockerfile, compose): the read side on `:8080` (behind the tunnel +
  password gate) and the ingest side published ONLY on `<box-tailscale-ip>:8081`.
- **Probes** (`probes/`, `deploy/`): what pushes heartbeats/metrics INTO the ingest port — systemd drop-ins
  and a container timer on the box, an hourly launchd job on the Mac, and `probes/ping.sh` for manual jobs.

Job ids are fixed and must match `jobs.yml` (unknown id → 404, by design):
`box`: `km-backup`, `todoist-points-backup`, `box-containers`, `dashboard-probes` ·
`mac`: `mac-probe`, `pa-backup`, `drive-mirror`, `minecraft-offload`, `taste-twin-publish`, `jjho-refresh`,
`baby-pool-sync`.

---

## 1. Box — app container

```bash
ssh <user>@<box-tailscale-ip>
git clone https://github.com/Graham-Williams/hopper-dashboard ~/hopper-dashboard
cd ~/hopper-dashboard
TS_IP="$(tailscale ip -4)"

# .env — never committed. APP_PASSWORD is the shared house password (same word as km/todoist/taste-twin/jjho).
cp .env.example .env && chmod 600 .env
PW="$(grep '^APP_PASSWORD=' ~/km-tracker/.env | cut -d= -f2-)"          # reuse the house password verbatim
sed -i "s|^APP_PASSWORD=.*|APP_PASSWORD=${PW}|" .env
sed -i "s|^SESSION_SECRET=.*|SESSION_SECRET=$(openssl rand -hex 32)|" .env
sed -i "s|^INGEST_TOKEN=.*|INGEST_TOKEN=$(openssl rand -hex 32)|" .env
sed -i "s|^READ_TOKEN=.*|READ_TOKEN=$(openssl rand -hex 32)|" .env
sed -i "s|^NTFY_TOPIC=.*|NTFY_TOPIC=hopper-$(openssl rand -hex 16)|" .env
# .env.example already ships INGEST_BIND=127.0.0.1, so a `grep -q || echo` would never fire — replace in place:
sed -i "s|^INGEST_BIND=.*|INGEST_BIND=${TS_IP}|" .env                   # Tailscale IP ONLY — ufw is inactive
# The read role trusts CF-Connecting-IP only from the tunnel container's network:
sed -i "s|^TRUSTED_PROXY_CIDR=.*|TRUSTED_PROXY_CIDR=$(docker network inspect km-tracker_default -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}')|" .env
# leave APP_HOST=dashboard.graham-williams.com and APP_ENV=prod as shipped
grep -E '^(APP_PASSWORD|SESSION_SECRET|INGEST_TOKEN|READ_TOKEN)=change-me' .env && echo "STOP: a secret is still the placeholder"

# jobs.yml — never committed (real Drive folder ids + machine topology).
cp jobs.example.yml jobs.yml
# drive-mirror root ids (My Mac / Documents / Desktop / minecraft-channel) are NOT in the repo: take them
# from the operator notes (personal-assistant memory) or rediscover with the `rclone backend query` in
# DESIGN.md → drive_mirror, then add them to jobs.yml. They are folder ids, not secrets, but they stay out
# of the public repo. Verify the container-name list under box-containers.expect against `docker ps`.
```

### 1b. Read-only rclone remote for the probes (least privilege)

The host's `~/.config/rclone/rclone.conf` (`gdrive`, `scope = drive.file`, 0600, owned by the login) is the
**backup writer's** credential. Do not hand it to the dashboard: a compromised container could then delete
the backups it is supposed to watch. Create a **second remote in its own file** with `scope = drive.readonly`
and mount THAT. Honest procedure (the box is headless, so the OAuth step happens on the Mac's browser):

```bash
# on the box — creates ~/.config/rclone/dashboard-ro.conf with a readonly-scope remote, no token yet.
# rclone prints an `rclone authorize "drive" "<blob>"` command for a machine with a browser:
RCLONE_CONFIG=~/.config/rclone/dashboard-ro.conf rclone config create gdrive-ro drive scope drive.readonly config_is_local false
# ... then on the MAC (browser opens; sign in as the Drive account; approve READ-ONLY access) ...
rclone authorize "drive" "<blob printed above>"
# ... it prints a `{"access_token": …}` JSON — paste that back into the box prompt.
chmod 600 ~/.config/rclone/dashboard-ro.conf
RCLONE_CONFIG=~/.config/rclone/dashboard-ro.conf rclone lsd gdrive-ro: | head            # must list Drive
RCLONE_CONFIG=~/.config/rclone/dashboard-ro.conf rclone lsf gdrive-ro:km-tracker-backups | head -2
```

Then point every `probe.rclone_path` / `destination` in `jobs.yml` at `gdrive-ro:` instead of `gdrive:`
(`sed -i 's|gdrive:|gdrive-ro:|g' jobs.yml`). `.env` already defaults `RCLONE_CONF` to this file.

Trade-off, stated plainly: `drive.readonly` can **read the whole Drive** (the writer's `drive.file` scope only
sees files rclone itself created), but it can **never write or delete**. For a watcher that is the right
side to err on. **Fallback** (if the authorize dance can't be done right now): set
`RCLONE_CONF=~/.config/rclone/rclone.conf` in `.env` and keep `gdrive:` in `jobs.yml` — it works (the
entrypoint stages the 0600 file as root and drops privileges), with the risk that the container then holds
a credential that can modify the backups. Note it in INVENTORY.md and come back to it.

Interactive-only step: the `rclone authorize` browser round-trip needs Graham (or a Hopper session with
Chrome) on the Mac; nothing else in this file does.

### 1c. Start and verify

```bash
docker compose config >/dev/null                                 # `${VAR:?}` catches a missing secret here
docker compose up -d --build
docker ps --filter name=hopper-dashboard --format '{{.Names}} {{.Status}}'   # (healthy) after ~30 s
# curl is purged from the image — probe from inside with python:
docker exec hopper-dashboard python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read())"
docker exec hopper-dashboard python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8081/healthz', timeout=5).read())"
# the ingest port from the host, on the published address:
curl -fsS "http://${TS_IP}:8081/healthz"
# nothing runs as root after start-up (only the staging step did); the staged conf belongs to uid 10001:
docker exec hopper-dashboard sh -c 'for p in /proc/[0-9]*; do echo "$(stat -c %u $p) $(cat $p/comm)"; done | sort | uniq -c'
docker exec hopper-dashboard stat -c '%u %a' /tmp/rclone/rclone.conf        # 10001 600
```

If the container restart-loops, `docker logs hopper-dashboard`: a `ConfigError` means `.env` is missing
`APP_PASSWORD` (prod) or `SESSION_SECRET`; `jobs.yml: …` means a registry validation error (job id + field).

From the **Mac** (over Tailscale) — this is the path every Mac heartbeat will take:

```bash
curl -fsS http://<box-tailscale-ip>:8081/healthz
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://<box-tailscale-ip>:8081/api/v1/ping/mac-probe   # expect 401 (no token)
```

Also confirm the ingest port is **not** on the LAN interface: `ss -ltnp | grep 8081` on the box must show
`<box-tailscale-ip>:8081` only, never `0.0.0.0:8081`.

## 2. Box — heartbeats (systemd drop-ins + container timer)

```bash
cd ~/hopper-dashboard
sudo deploy/box/install.sh --url "http://$(tailscale ip -4):8081" --token-file "$PWD/.env"
```

The token is read from the compose `.env`'s `INGEST_TOKEN=` line — it never appears on a command line
(`--token` is rejected on purpose). Without `--token-file` the script prompts with a hidden read.

What it does (idempotent; never restarts a container or the backup units):
- writes `/etc/hopper-dashboard/ingest.env` (root:root **0600** — systemd reads `EnvironmentFile=` as PID 1
  even though the units run as the login user, so that user never needs to read it),
- installs `/etc/systemd/system/{km-backup,todoist-points-backup}.service.d/heartbeat.conf`
  (`ExecStopPost=-/usr/bin/curl … result=${SERVICE_RESULT} exit=${EXIT_STATUS}` → `/api/v1/ping/<unit>`;
  fires on success, failure **and** the 300 s timeout kill; `-` prefixes mean a dead dashboard can never
  change the backup unit's own result),
- renders `dashboard-containers.service` (`User=` ← `--user`, default `$SUDO_USER`; must be in `docker`) and
  installs + enables `dashboard-containers.timer` (`OnCalendar=*:0/5` + `Persistent=true` → every 5 min →
  `deploy/box/containers_probe.sh` → `docker ps` → `box-containers` ping; the dashboard container itself has
  no docker socket),
- `daemon-reload`, then prints `systemd-analyze verify`, `systemctl cat`, `list-timers` and a dry run.

Verify within 5 minutes (the backup timers tick every 5 min, so all three box jobs should report):

```bash
systemctl list-timers --no-pager | grep -E 'km-backup|todoist-points-backup|dashboard-containers'
journalctl -u dashboard-containers.service -n 3 --no-pager          # "sent box-containers ok → HTTP 200"
journalctl -u km-backup.service -n 20 --no-pager | grep -i curl      # should be silent; errors would show here
# read side, inside the container with the READ_TOKEN (no cookie needed; curl is not in the image):
RT="$(grep '^READ_TOKEN=' ~/hopper-dashboard/.env | cut -d= -f2-)"
docker exec -e RT="$RT" hopper-dashboard python -c "import os,json,urllib.request as u; r=u.Request('http://127.0.0.1:8080/api/v1/status', headers={'Authorization':'Bearer '+os.environ['RT']}); d=json.load(u.urlopen(r, timeout=5)); print(d['summary']); print([(j['id'], j['state']) for j in d['jobs']])"
```

`km-backup`, `todoist-points-backup`, `box-containers` should be `OK`; the Mac jobs are still `UNKNOWN` (they
turn `LATE` on their own after cadence+grace if the Mac probe is never installed — that is the point).

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
`/login`; `https://dashboard.graham-williams.com/healthz` → `200`.

**Ingest isolation check.** The read role answers 401 to ANY unauthenticated `/api/v1/*` path, so "401 =
exposed" is NOT a valid test. Prove the ingest route simply does not exist on the public side by asking
*with* the read token:

```bash
RT="$(ssh <user>@<box-tailscale-ip> "grep '^READ_TOKEN=' ~/hopper-dashboard/.env | cut -d= -f2-")"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST -H "Authorization: Bearer $RT" \
  https://dashboard.graham-williams.com/api/v1/ping/x          # MUST be 404 or 405 — never 200/400/401
ssh <user>@<box-tailscale-ip> 'ss -ltnp | grep 8081'             # only <box-tailscale-ip>:8081, never 0.0.0.0
```

## 4. Mac — hourly probe + phone alerts

```bash
cd ~/code/hopper-dashboard && git pull
deploy/mac/install.sh --url http://<box-tailscale-ip>:8081        # then type the INGEST_TOKEN at the HIDDEN prompt
```

**Never put the token on the command line** (`--token` is rejected: it would land in shell history and
`ps`). The script prompts with a silent `read`; paste the value from the box `.env`. If `--url` is omitted it
prompts for that too (there is no default URL in the code).

What it does (idempotent): writes `~/.config/hopper-dashboard/env` (chmod 600, never overwritten if present),
renders `deploy/mac/com.hopper.dashboard-probe.plist` → `~/Library/LaunchAgents/` with absolute paths
(`/usr/bin/python3`, this checkout), `launchctl bootout` + `bootstrap`, then runs `probes/mac_probe.py
--dry-run` and prints the pings it would send. `RunAtLoad` means the first real run happens immediately;
then hourly (`StartInterval 3600`), and again after every login/wake.

The probe posts:
- `pa-backup` — last line of `~/Library/Logs/hopper-backup.log` as an `ok`/`fail` run (**once per new line**,
  deduped via `~/.config/hopper-dashboard/state.json`) + a `metric` ping covering **all three trees the backup
  script copies** (`~/personal-assistant` → `gdrive:Backups/personal-assistant`, the Hopper memory dir →
  `gdrive:Backups/hopper-memory`, `~/.claude` → `gdrive:Backups/claude-config`, each with the script's exact
  filter list; the tiny `dotfiles` allowlist copy is not checked). Reported **separately**: `missing_files`/
  `missing_bytes` (never uploaded → the dashboard's `STALE_DEST`) and `differ_files`/`differ_bytes` (edited
  locally since the 03:00 copy → informational lag on the card), summed plus per-tree `*_<tree>` keys,
  `matched_files`, `check_errors`, `trees_checked`, and `dest_count`/`dest_bytes` summed over the three
  destinations.
- `minecraft-offload` — `metric`: bytes/files under `~/minecraft-channel/{recordings,world backups,replays}`
  not yet on `gdrive:Gremlins/…` (`rclone check --one-way --min-age 15m`; pairs parsed live from
  `scripts/offload-recordings.sh`), per-pair keys `lag_bytes_<pair>`, plus `disk_free_bytes` for
  `/System/Volumes/Data`. Never uploads.
- `drive-mirror` — an **`ok` run** (reading the mirror DB *is* the check, so it counts as a heartbeat) with
  metrics: DriveFS mirror queue (`pending`), `mismatch`, `roots`, `roots_list`, `db_age_s` from a **copy** of
  `mirror_sqlite.db{,-wal,-shm}`; a `fail` run if no mirror db exists.
- `mac-probe` — its own heartbeat: `ok`, or `fail` with a note naming the sub-probes that errored.

Verify:

```bash
launchctl list | grep com.hopper.dashboard-probe            # "-  0  com.hopper.dashboard-probe" → last exit 0
tail -5 ~/Library/Logs/hopper-dashboard-probe.log            # "sent pa-backup … HTTP 200" … "run ok in Ns"
/usr/bin/python3 ~/code/hopper-dashboard/probes/mac_probe.py --dry-run --only drive-mirror
```

**Seed the manual offload job.** `minecraft-offload` is `kind: manual` with a 14-day `max_age_s`; that target
is **inert until the first real run ping** (there is no "last success" to age) and the card says **Never run**
until then. Right after the first successful `/offload-recordings` run, seed one:

```bash
~/code/hopper-dashboard/probes/ping.sh minecraft-offload ok "seeded after offload $(date +%F)"
```

**Phone alerts (Graham's manual step):** install the **ntfy** app (iOS/Android), tap *Subscribe to topic*,
enter the `NTFY_TOPIC` value from the box `.env` (server `https://ntfy.sh`). The topic name is the only
secret — anyone who knows it can read alerts, so don't paste it anywhere else. Alerts carry only
`job_id: FROM → TO` (no free text). Test from the box:
`curl -d "dashboard test" https://ntfy.sh/$(grep '^NTFY_TOPIC=' ~/hopper-dashboard/.env | cut -d= -f2-)`.

**Overnight sleep is not an incident.** The hourly Mac jobs carry a 14 h grace, and while `mac-probe` is
LATE the dashboard sends one "Mac probe → LATE" alert and suppresses the LATE alerts of the other Mac jobs
(states still recorded and shown). Expect exactly one alert if the Mac is away for more than ~15 h.

### Manual jobs: `probes/ping.sh`

`taste-twin-publish`, `jjho-refresh` and `baby-pool-sync` have nothing to compute on a timer; they are
`kind: manual` with a max-age target, and the run that Hopper performs by hand ends by pinging:

```bash
P=~/code/hopper-dashboard/probes/ping.sh
# taste-twin (Mac; runs the pipeline on the residential IP then ships the report to the box)
cd ~/code/taste-twin && python scripts/publish.py <letterboxd-user> && $P taste-twin-publish ok "<letterboxd-user>" \
  || $P taste-twin-publish fail "publish.py failed"
# jjho transcript/episode refresh (Mac-generated DB shipped to the box)
$P jjho-refresh ok "spine+transcripts refreshed, DB shipped"        # or: $P jjho-refresh skipped "no new episodes"
# baby-pool Sheet → entries.json → box
$P baby-pool-sync ok "entries.json re-synced"
```

`ping.sh <job_id> <ok|fail|skipped> [note]` reads `DASHBOARD_URL`/`INGEST_TOKEN` from
`~/.config/hopper-dashboard/env` (override with `HOPPER_DASHBOARD_ENV=/etc/hopper-dashboard/ingest.env` on the
box; both are **required** — there is no default URL), accepts `REASON=`, `EXIT_CODE=`, `STARTED_AT=`,
`METRICS_JSON='{"files":3}'`, and `DRY_RUN=1` prints instead of sending. Do **not** edit the other repos to
call it; the call belongs to the Hopper run that wraps them (a future hardening is a `--ping` flag in each
script, but that is per-repo work).

## 5. Verification checklist

- [ ] `docker ps` shows `hopper-dashboard` healthy; `ss -ltnp | grep 8081` shows only `<box-tailscale-ip>:8081`.
- [ ] No root process in the container after start-up; `/tmp/rclone/rclone.conf` is `10001 600`.
- [ ] `https://dashboard.graham-williams.com/` → login page; `/healthz` → 200; `POST /api/v1/ping/x` via the
      public host **with the READ_TOKEN** is 404/405 (ingest isn't tunnelled; see §3 for why 401 proves nothing).
- [ ] `/api/v1/status` (READ_TOKEN) lists all 11 jobs; after ≤5 min box jobs are `OK`, after ≤1 h Mac jobs are
      `OK`/`BEHIND` (not `UNKNOWN`), manual jobs show **Never run** until their first `ping.sh`.
- [ ] `minecraft-offload` seeded with one `ok` ping after the first offload (§4).
- [ ] Kill test: `sudo systemctl stop dashboard-containers.timer` → `box-containers` goes `LATE` after
      cadence+grace and an ntfy alert arrives; `start` → recovery alert. Re-enable afterwards.
- [ ] Fail test (safe): `~/code/hopper-dashboard/probes/ping.sh jjho-refresh fail "drill"` → `FAIL` + alert;
      then `… ok "drill over"` → recovery.
- [ ] `INVENTORY.md` in `~/personal-assistant` updated: new container, new public hostname, new box timer,
      new Mac launchd job, new credential locations (`/etc/hopper-dashboard/ingest.env`,
      `~/.config/hopper-dashboard/env`, box `.env`, `~/.config/rclone/dashboard-ro.conf`).

## Rollback

```bash
# Mac
~/code/hopper-dashboard/deploy/mac/uninstall.sh [--purge]        # --purge also removes the token + state
# Box — heartbeats (backup units keep running exactly as before)
sudo ~/hopper-dashboard/deploy/box/uninstall.sh [--purge]        # --purge also removes /etc/hopper-dashboard
# Box — app
cd ~/hopper-dashboard && docker compose down                     # add -v to drop the derived SQLite volume
# Cloudflare: delete the ingress rule + CNAME (reverse of step 3)
# Drive: revoke the read-only remote's OAuth grant at myaccount.google.com → Security → Third-party access
```

Nothing in the probes writes to Drive, to the backup repos, or to any container, so rollback never has to
restore data; the dashboard's own SQLite is derived state that repopulates within one probe cycle.

## How it fails quietly (INVENTORY.md spirit)

| Component | Quiet failure mode | What catches it / how to check |
|---|---|---|
| App container down | Every job goes `LATE` at once; no alerts because the alerter is the thing that died | `dashboard-probes` self-heartbeat is a job — but it can't alert on itself. Hopper's weekly health watch must hit `/healthz`; `docker ps` on the box. Consider an external dead-man (Healthchecks.io) later. |
| Container restart-looping on config | Board unreachable, `docker ps` shows `Restarting` | `docker logs hopper-dashboard` → `ConfigError` (missing APP_PASSWORD/SESSION_SECRET) or `jobs.yml:` validation error. Loud by design. |
| Ingest bound to `0.0.0.0` | Token-only protection exposed to the LAN | `ss -ltnp \| grep 8081` on the box; `INGEST_BIND` in `.env` |
| Drop-in not loaded (no `daemon-reload`, typo in path) | Backups run fine, dashboard shows `km-backup` `UNKNOWN` then **`LATE` after cadence+grace** — looks like a backup problem | `systemctl cat km-backup.service` must show `heartbeat.conf`; `journalctl -u km-backup.service` shows curl errors if the URL/token are wrong (`-fsS` prints them). The never-pinged → LATE rule exists so this can't stay `UNKNOWN` forever. |
| `/etc/hopper-dashboard/ingest.env` missing/rotated token | `ExecStopPost` curl 401s, silently ignored (`-` prefix) → `LATE` | `journalctl -u km-backup.service \| grep 401`; compare token with box `.env` |
| Backup script exit 0 on skipped push | Heartbeat says `ok` while Drive hasn't received anything new | That's what the app's destination probe (`db_snapshot` newest object vs `db_sha256`) is for → `STALE_DEST` |
| Read-only rclone remote's token revoked/expired | Every `db_snapshot` probe errors → `dashboard-probes` `FAIL` with `rclone exit …` in the note; `dest.probe_error` on the cards | Re-run the `rclone authorize` flow in §1b; `RCLONE_CONFIG=~/.config/rclone/dashboard-ro.conf rclone lsd gdrive-ro:` on the box |
| `dashboard-containers.timer` stopped / user dropped from `docker` group | `box-containers` `LATE`; or `fail` ping with "permission denied" in the note | `systemctl list-timers`; `journalctl -u dashboard-containers.service` |
| Mac asleep / logged out | `mac-probe` LATE after 15 h; the other Mac jobs go LATE too but their alerts are suppressed — you get ONE alert | That IS the signal (Mac offline). If only some Mac jobs are late, read the `mac-probe` note — it names the sub-probe that errored. |
| launchd job unloaded (plist edited by hand, Mac migrated) | Same as asleep, but permanent | `launchctl list \| grep com.hopper.dashboard-probe`; re-run `deploy/mac/install.sh` |
| rclone missing/moved (Homebrew relink) | `pa-backup`/`minecraft-offload` metrics stop; `mac-probe` `fail` with "rclone not found" | `mac-probe` note; `~/Library/Logs/hopper-dashboard-probe.log` |
| rclone Drive token expired (Mac) | `rclone check` errors → sub-probe `fail`, lag unknown | Same log; `rclone lsd gdrive:` interactively |
| `hopper-backup.log` stops being written (launchd backup job unloaded) | `pa-backup` never gets a new run ping → `LATE` after 24 h + 14 h; the metric ping keeps flowing and `missing_files` climbs | Both signals appear on the card; `launchctl list \| grep personal-assistant-backup` |
| Nightly copy trailing an edited tree | `differ_files` > 0 on the `pa-backup` card | Informational only — never `STALE_DEST`. Only `missing_*` is stale. |
| Backup log line format changes | Reported as `fail` with `reason=unparseable-log` — loud, not silent | Fix `probes/backup_log.py` |
| DriveFS schema drift (Google update renames a table) | `drive-mirror` `fail` with "schema drift: missing table" | Re-run the spike in DESIGN.md → `drive_mirror`; update `probes/drivefs.py` + fixture |
| DriveFS not running | `db_age_s` grows without bound while `pending` stays 0 — looks caught up | Alert/threshold on `db_age_s` in the app (suggested: > 6 h) |
| State file corrupt | Treated as "never reported" → the last backup line is re-sent once (harmless duplicate) | Nothing to do; it self-heals on the next successful send |
| Ingest 404 for a job (id typo / not in `jobs.yml`) | Probe logs `rejected: HTTP 404`, `mac-probe` `fail` | `mac-probe` note; the id list at the top of this file |
| `ping.sh` never called after a manual run | Manual job shows **Never run** forever, or goes `BEHIND` past `max_age_s` even though the work was done | Make the ping part of the documented Hopper procedure for each manual job; seed `minecraft-offload` once (§4) |
| ntfy topic leaked or mistyped on the phone | Alerts fire (server-side) but never arrive | `curl -d test https://ntfy.sh/<topic>` and check the phone |
