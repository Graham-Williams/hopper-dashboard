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
`box`: `km-backup`, `todoist-points-backup`, `box-containers`, `box-disk`, `dashboard-probes` ·
`mac`: `mac-probe`, `pa-backup`, `drive-mirror`, `minecraft-offload`, `mac-disk`, `taste-twin-publish`,
`jjho-refresh`, `baby-pool-sync`.

**Adding a job id is app-first, probe-second.** The registry is loaded once at start-up, so a new id must be
in the live (gitignored) `jobs.yml` **and the container restarted** *before* anything posts to it — `docker
compose up -d` in `~/hopper-dashboard` after editing the file. In the other order every ping 404s, which is
not silent: the Mac probe turns `mac-probe` into `fail` (an ntfy alert **every hour**) and the box unit exits
non-zero every 5 minutes.

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
# jobs.yml must be world-readable: the container reads it as uid 10001. A `umask 077` shell (e.g. after the
#   rclone-conf step below) produces a 0600 file and the container restart-loops with
#   `PermissionError: /app/jobs.yml`.
chmod 644 jobs.yml
# drive-mirror root ids (My Mac / Documents / Desktop / minecraft-channel) are NOT in the repo. They are NOT
# config either: registry.py rejects unknown keys, and probes/drivefs.py discovers the mirrored roots from the
# DriveFS DB itself. Keep them as the YAML COMMENT under drive-mirror (placeholder shipped in jobs.example.yml)
# and in the operator notes (personal-assistant memory) — their only use is the independent
# `rclone size gdrive: --drive-root-folder-id <id>` cross-check in DESIGN.md → drive_mirror, which is also
# where the rediscovery command lives. Folder ids, not secrets, but they stay out of the public repo.
# Verify the container-name list under box-containers.expect against `docker ps`.
```

### 1b. Read-only rclone remote for the probes (least privilege)

The host's `~/.config/rclone/rclone.conf` (`gdrive`, `scope = drive.file`, 0600, owned by the login) is the
**backup writer's** credential. Do not hand it to the dashboard: a compromised container could then delete
the backups it is supposed to watch. Create a **second remote in its own file** with `scope = drive.readonly`
and mount THAT. The box is headless, so the OAuth step happens on the **Mac's** browser and the finished
config file is shipped over. This procedure is **version-proof** — it does not depend on the box's rclone at
all. (The older `rclone config create … config_is_local false` dance does NOT work with the box's rclone
1.60: it neither prompts nor prints the authorize command there; don't use it.)

```bash
# --- 1. on the MAC: run the OAuth flow with the read-only scope baked into the request.
#        A browser tab opens; sign in as the Drive account and approve READ-ONLY access.
rclone authorize drive "$(printf '{"scope":"drive.readonly"}' | base64 | tr -d '=\n')"
# rclone then prints a base64 "config token" blob between two marker lines:
#     Paste the following into your remote machine --->
#     <blob>
#     <---End paste
# --- 2. decode the blob. It is base64 of JSON like {"token": "<json string>", ...}; the base64 may lack
#        '=' padding, so re-pad before decoding (plain `base64 -d` works once padded).
BLOB='<paste the blob here>'
TOKEN="$(python3 -c 'import base64,json,sys; b=sys.argv[1]; b+="="*(-len(b)%4); print(json.loads(base64.b64decode(b))["token"])' "$BLOB")"
echo "$TOKEN" | head -c 40; echo   # {"access_token":"ya29…  — a JSON string, keep it verbatim
# --- 3. build the conf locally with a 0600 umask, ship it, remove the local copy.
( umask 077; printf '[gdrive-ro]\ntype = drive\nscope = drive.readonly\ntoken = %s\n' "$TOKEN" > ~/dashboard-ro.conf )
ssh <user>@<box-tailscale-ip> 'install -d -m 0700 ~/.config/rclone'
scp ~/dashboard-ro.conf <user>@<box-tailscale-ip>:~/.config/rclone/dashboard-ro.conf
ssh <user>@<box-tailscale-ip> 'chmod 600 ~/.config/rclone/dashboard-ro.conf'
rm -P ~/dashboard-ro.conf 2>/dev/null || rm ~/dashboard-ro.conf
unset TOKEN BLOB
```

Verify on the box — it must **read** the backup folder and must **fail to write**:

```bash
RC=~/.config/rclone/dashboard-ro.conf
RCLONE_CONFIG=$RC rclone lsf gdrive-ro:km-tracker-backups | head -2          # lists snapshot files
RCLONE_CONFIG=$RC rclone lsf gdrive-ro:todoist-points-backups | head -2
# Prove read-only: a write must be refused (403 insufficientPermissions). If this SUCCEEDS the scope is
# wrong — stop, delete the stray folder with the writer remote (`rclone rmdir gdrive:write-test`), redo step 1.
RCLONE_CONFIG=$RC rclone mkdir gdrive-ro:write-test && echo "STOP: remote can WRITE" || echo "read-only confirmed"
```

Then point every `probe.rclone_path` / `destination` in `jobs.yml` at `gdrive-ro:` instead of `gdrive:`
(`sed -i 's|gdrive:|gdrive-ro:|g' jobs.yml`). `.env` already defaults `RCLONE_CONF` to this file.

> **⚠️ Follow-up (not a blocker): rclone's shared Google Drive `client_id` is being retired during 2026.**
> rclone 1.75 prints a warning about it. Every remote that relies on the built-in client — this `gdrive-ro`,
> **and** the existing backup writers `gdrive` on the box and on the Mac — will stop authenticating when it
> goes, i.e. all Drive backups and the probes that watch them fail together. The fix is an OAuth **Desktop**
> client of our own in GCP (enable the Drive API on the existing project, create the client, keep the secret
> in 1Password): add `client_id = …` / `client_secret = …` to each remote's section in its conf and pass them
> in the authorize blob — `printf '{"scope":"drive.readonly","client_id":"…","client_secret":"…"}' | base64`
> — then re-authorize each remote once. Track it as a repo issue + an INVENTORY.md follow-up; nothing here
> depends on it today.

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
# PRE-FLIGHT — the rclone conf must EXIST as a file before the first `up`. Compose bind-mounts it; if the
# path is missing Docker silently creates a root-owned DIRECTORY there, the entrypoint's `cp` fails, the
# container restart-loops, and the later `rclone authorize` step can't write the file until the dir is
# removed (`sudo rmdir`). Same path as RCLONE_CONF in .env:
RCLONE_CONF_PATH="${RCLONE_CONF_PATH:-$HOME/.config/rclone/dashboard-ro.conf}"
test -f "$RCLONE_CONF_PATH" || { echo "STOP: rclone conf missing (Docker would create a root-owned DIRECTORY there)"; exit 1; }
docker compose up -d --build
docker ps --filter name=hopper-dashboard --format '{{.Names}} {{.Status}}'   # (healthy) after ~30 s
# curl is purged from the image — probe from inside with python:
docker exec hopper-dashboard python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read())"
docker exec hopper-dashboard python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8081/healthz', timeout=5).read())"
# the ingest port from the host, on the published address:
curl -fsS "http://${TS_IP}:8081/healthz"
# nothing runs as root after start-up (only the staging step did); the staged conf belongs to uid 10001.
# `-u 10001` matters: `docker exec` itself runs as root by default, so without it the checker shows up
# as the one root process and the check can never come back clean.
docker exec -u 10001 hopper-dashboard sh -c 'for p in /proc/[0-9]*; do echo "$(stat -c %u $p) $(cat $p/comm)"; done | sort | uniq -c'
docker exec -u 10001 hopper-dashboard stat -c '%u %a' /tmp/rclone/rclone.conf        # 10001 600
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
  `deploy/box/containers_probe.sh` → `statvfs /` → `box-disk` ping **and** `docker ps` → `box-containers`
  ping, in that order: the cheap probe goes first so `docker ps` trouble can never eat the disk reading
  inside `TimeoutStartSec=210` (≈133 s worst case: 35 s disk probe + 33 s fallback curl + 65 s containers
  probe — they are additive). The dashboard container itself has no docker socket. If the disk probe
  cannot run at all the wrapper posts the `fail` ping for it, so the failure reaches the board and not
  just the journal; when BOTH probes fail the unit's exit status carries the CONTAINERS code, since the
  disk failure is already on the board),
- `daemon-reload`, then prints `systemd-analyze verify`, `systemctl cat`, `list-timers` and a dry run.

Verify within 5 minutes (the backup timers tick every 5 min, so all three box jobs should report):

```bash
systemctl list-timers --no-pager | grep -E 'km-backup|todoist-points-backup|dashboard-containers'
journalctl -u dashboard-containers.service -n 3 --no-pager          # "sent box-containers ok → HTTP 200"
journalctl -u km-backup.service -n 20 --no-pager | grep -i curl      # one line per run; healthy looks like:
#   km-backup.service … curl[1234]: {"ok":true,"state":"OK"}         # -fsS prints the body on success (not silent)
#   a wrong URL/token shows `curl: (22) The requested URL returned error: 401` (or a connection error) instead
# read side, inside the container with the READ_TOKEN (no cookie needed; curl is not in the image).
# The read role pins Host to APP_HOST on every route except /healthz, so a bare 127.0.0.1 request is a 403 —
# pass the public hostname as the Host header:
RT="$(grep '^READ_TOKEN=' ~/hopper-dashboard/.env | cut -d= -f2-)"
AH="$(grep '^APP_HOST=' ~/hopper-dashboard/.env | cut -d= -f2-)"
docker exec -e RT="$RT" -e AH="$AH" hopper-dashboard python -c "import os,json,urllib.request as u; r=u.Request('http://127.0.0.1:8080/api/v1/status', headers={'Authorization':'Bearer '+os.environ['RT'],'Host':os.environ['AH']}); d=json.load(u.urlopen(r, timeout=5)); print(d['summary']); print([(j['id'], j['state']) for j in d['jobs']])"
```

The in-container read is only for verifying before §3 is wired. Hopper's normal bearer read goes through the
public hostname — `curl -sS -H "Authorization: Bearer $READ_TOKEN" https://dashboard.graham-williams.com/api/v1/status`
— which carries the right Host by construction.

**Re-installing after a change to the box probes:** the service runs
`deploy/box/containers_probe.sh` straight out of the checkout, so a `git pull` in `~/hopper-dashboard`
is enough for a *script* change to take effect on the next tick. Re-run `sudo deploy/box/install.sh …`
(idempotent; it never restarts a container or the backup units) when a **unit file** changed — as it did
when `box-disk` was added to this timer, which rewrote the unit's Description. Confirm what is live with
a dry run, which prints **both** pings:

```bash
cd ~/hopper-dashboard && set -a; . /etc/hopper-dashboard/ingest.env; set +a
deploy/box/containers_probe.sh --dry-run     # → /api/v1/ping/box-containers AND /api/v1/ping/box-disk
journalctl -u dashboard-containers.service -n 4 --no-pager   # after the next tick: two "sent … HTTP 200"
```

`km-backup`, `todoist-points-backup`, `box-containers`, `box-disk` should be `OK`; the Mac jobs are still `UNKNOWN` (they
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

**The durable Mac checkout is `~/code/hopper-dashboard`.** Every Mac path in this file assumes it. For the
preview the feature branch is checked out *there* (`git fetch && git checkout feature/dashboard-app`);
after the merge it goes back to `main` (`git checkout main && git pull`). Do **not** install from a worktree
(e.g. `~/code/hopper-dashboard-app`): `deploy/mac/install.sh` bakes the **absolute repo directory** into the
launchd plist at install time (`@@REPO@@` → the checkout it is run from), so a plist rendered from a worktree
points at a directory that vanishes when the worktree is removed and the hourly probe dies silently
(`launchctl list` shows a non-zero exit). Switching branches in place needs no reinstall (same path);
moving the checkout does (re-run `install.sh`).

**Non-interactive path (what the real deploy used — Hopper runs this with no prompts).** The installer leaves an
existing env file alone, so pre-create it with the token pulled over ssh; the token never touches a command
line, shell history or `ps` on the Mac:

```bash
cd ~/code/hopper-dashboard && git pull                              # on the branch being deployed
mkdir -p ~/.config/hopper-dashboard && chmod 700 ~/.config/hopper-dashboard
( umask 077; {
    echo "DASHBOARD_URL=http://<box-tailscale-ip>:8081"
    echo "INGEST_TOKEN=$(ssh <user>@<box-tailscale-ip> "grep '^INGEST_TOKEN=' ~/hopper-dashboard/.env | cut -d= -f2-")"
  } > ~/.config/hopper-dashboard/env )
grep -q '^INGEST_TOKEN=..*' ~/.config/hopper-dashboard/env || echo "STOP: empty token (ssh failed?)"
ls -l ~/.config/hopper-dashboard/env                                # must be -rw------- (0600)
deploy/mac/install.sh                                               # prints "env file exists, leaving it alone", no prompts
```

**Interactive alternative:** `deploy/mac/install.sh --url http://<box-tailscale-ip>:8081` and type the
`INGEST_TOKEN` at the hidden prompt. **Never put the token on the command line** (`--token` is rejected: it
would land in shell history and `ps`). If `--url` is omitted it prompts for that too (there is no default URL
in the code).

What it does (idempotent): writes `~/.config/hopper-dashboard/env` (chmod 600, never overwritten if present),
renders `deploy/mac/com.hopper.dashboard-probe.plist` → `~/Library/LaunchAgents/` with absolute paths
(`/usr/bin/python3`, `~/code/hopper-dashboard/probes/mac_probe.py`), `launchctl bootout` + `bootstrap`, then
runs `probes/mac_probe.py --dry-run` and prints the pings it would send. `RunAtLoad` means the first real run
is triggered by the `bootstrap` itself — it takes **~100 s** (three `rclone check` trees for `pa-backup` plus
the minecraft pairs; `launchctl list | grep hopper` shows a PID while it runs, then exit `0`), so don't read
"still UNKNOWN" as a failure for the first couple of minutes; then hourly (`StartInterval 3600`), and again
after every login/wake.

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
  not yet on `gdrive:Gremlins/…` (`rclone check --one-way --size-only --min-age 15m`; pairs parsed live from
  `scripts/offload-recordings.sh`), per-pair keys `lag_bytes_<pair>`, plus `disk_free_bytes` for
  `/System/Volumes/Data`. Never uploads. `--size-only` is deliberate: these trees are tens of GB of video,
  and an hourly MD5 pass against the 120 s probe timeout produced spurious `mac-probe FAIL`s; the small
  `pa-backup` trees keep the full checksum check.
- `mac-disk` — `metric`: `disk_free_bytes` / `disk_total_bytes` / `disk_path` for
  `/System/Volumes/Data` (`PROBE_DISK_PATH`), rendered as the capacity gauge and thresholded in
  `jobs.yml` (`disk.min_free_bytes` 25 GiB / `disk.max_used_pct` 90 → `BEHIND`). A `disk` job is a gauge
  with no cadence, so this is metrics-only and `mac-probe` remains the Mac's liveness signal — but a
  reading nothing refreshes for 48 h still goes `LATE` (`state.DISK_METRIC_MAX_AGE_S`) — as does a gauge
  that has never reported at all more than 6 h after registration (`DISK_FIRST_READING_GRACE_S`) — and a
  `statvfs` error posts a `fail` run (it is NOT raised: a raise would only mark `mac-probe` FAIL and leave
  this card on its last reading) that the card shows as **FAIL, capacity unreadable**. Note the percentage will not
  match `df`'s `Use%` (see DESIGN.md → `disk`): the free bytes do, the percent does not.
  `minecraft-offload` still reports the same two figures as a footnote on its own card — that is
  deliberate duplication, not drift.
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
(states still recorded and shown). When the Mac wakes, `mac_probe.py` posts `pa-backup`, then
`drive-mirror`, then its own heartbeat — three requests, three recomputes — and the siblings' plain
`LATE → OK` recoveries are muted while `mac-probe` is still LATE, so the wake-up is one alert too
("Mac probe → OK"). **Expect exactly two alerts per long absence: one when the Mac goes quiet, one when it
comes back** (tested end-to-end against `jobs.example.yml`). A sibling that wakes into `FAIL` / `STALE_DEST`
/ `BEHIND` still alerts on its own — that is news, not the Mac coming back. Keep every Mac sibling's
`grace_s` ≥ `mac-probe`'s + 120 (as `jobs.example.yml` does): the probe posts the siblings *before* itself,
so with equal graces a sibling's deadline falls a few seconds earlier and a ticker tick landing in that gap
would page for the sibling first, then again for the probe.

### Manual jobs: `probes/ping.sh`

`taste-twin-publish`, `jjho-refresh` and `baby-pool-sync` have nothing to compute on a timer; they are
`kind: manual` with a max-age target, and the run that Hopper performs by hand ends by pinging:

**Day one they show `UNKNOWN` — "never heard from" / "Never run" on the card — and that is expected, not a
defect.** Nothing pings a manual job automatically; each stays `UNKNOWN` until Hopper's next real run of that
task ends with `probes/ping.sh <job> ok`. (The max-age target is inert until that first ping, so they don't
alert either.) Only `minecraft-offload` is worth seeding by hand (§4); leave these three alone until the
work actually happens.

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
- [ ] No root process in the container after start-up (check with `docker exec -u 10001 …`, §1c);
      `/tmp/rclone/rclone.conf` is `10001 600`.
- [ ] `https://dashboard.graham-williams.com/` → login page; `/healthz` → 200; `POST /api/v1/ping/x` via the
      public host **with the READ_TOKEN** is 404/405 (ingest isn't tunnelled; see §3 for why 401 proves nothing).
- [ ] `/api/v1/status` (READ_TOKEN) lists all 13 jobs; after ≤5 min box jobs are `OK`, after ≤1 h Mac jobs are
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
| Container restart-looping on config | Board unreachable, `docker ps` shows `Restarting` | `docker logs hopper-dashboard` → `ConfigError` (missing APP_PASSWORD/SESSION_SECRET), `jobs.yml:` validation error, or `PermissionError: /app/jobs.yml` (file is 0600 from a `umask 077` shell; the container reads it as uid 10001 → `chmod 644 jobs.yml`). Loud by design. |
| Ingest bound to `0.0.0.0` | Token-only protection exposed to the LAN | `ss -ltnp \| grep 8081` on the box; `INGEST_BIND` in `.env` |
| Drop-in not loaded (no `daemon-reload`, typo in path) | Backups run fine, dashboard shows `km-backup` `UNKNOWN` then **`LATE` after cadence+grace** — looks like a backup problem | `systemctl cat km-backup.service` must show `heartbeat.conf` **and** its `ExecStopPost=` line; `journalctl -u km-backup.service` shows `curl[…]: {"ok":true,"state":"OK"}` per run when healthy and curl errors if the URL/token are wrong (`-fsS` prints both). The never-pinged → LATE rule exists so this can't stay `UNKNOWN` forever. |
| `/etc/hopper-dashboard/ingest.env` missing/rotated token | `ExecStopPost` curl 401s, silently ignored (`-` prefix) → `LATE` | `journalctl -u km-backup.service \| grep 401`; compare token with box `.env` |
| Backup script exit 0 on skipped push | Heartbeat says `ok` while Drive hasn't received anything new | That's what the app's destination probe (`db_snapshot` newest object vs `db_sha256`) is for → `STALE_DEST` |
| Read-only rclone remote's token revoked/expired | Every `db_snapshot` probe errors → `dashboard-probes` `FAIL` with `rclone exit …` in the note; `dest.probe_error` on the cards | Re-run the `rclone authorize` flow in §1b; `RCLONE_CONFIG=~/.config/rclone/dashboard-ro.conf rclone lsd gdrive-ro:` on the box |
| `dashboard-containers.timer` stopped / user dropped from `docker` group | `box-containers` `LATE`; or `fail` ping with "permission denied" in the note | `systemctl list-timers`; `journalctl -u dashboard-containers.service` |
| Disk gauge stops being fed (probe moved/renamed, `statvfs` on a path that vanished) | Was: the gauge kept showing its LAST reading for ever, because a `disk` job has no cadence — a frozen number looked like a healthy one | Four layers now, so a frozen gauge cannot read as healthy: (a) a reading older than **48 h** (`state.DISK_METRIC_MAX_AGE_S`) is `LATE`, alerted like any other dead-man's switch — suppressed only while that machine's probe job is itself LATE; (b) an unreadable path is a `fail` ping on BOTH machines → **FAIL, "capacity unreadable (statvfs …)"**, which outranks the stored figures (the Mac probe returns that ping rather than raising — a raise would only mark `mac-probe` FAIL and leave this card on its last reading); (c) a box probe that never ran at all is reported by its wrapper as a `fail` on `box-disk` (`result=probe-failed`, note pointing at `journalctl -u dashboard-containers.service`) rather than only as the unit's exit status; (d) a gauge that has NEVER reported goes `LATE` **6 h** after registration (`DISK_FIRST_READING_GRACE_S`) — the feeder that was never deployed, which `UNKNOWN` would otherwise hide for ever without alerting at all. The card still prints **measured &lt;when&gt;** under the bar. |
| Board's used-percent does not match `df` | Looks like an arithmetic bug, invites a "fix" that would break the threshold | Expected: the free **bytes** match `df`'s Avail exactly, the **percent** does not (macOS/APFS hands `statvfs` a smaller free figure than `df` uses — measured 79.5% vs 78%, so the 90% ceiling trips near 88.5% on `df`). `f_bavail` is the right number: it is what can actually be written. DESIGN.md → `disk` and `probes/common.disk_free` both say so. |
| Mac asleep / logged out | `mac-probe` LATE after 15 h; the other Mac jobs go LATE too but their alerts are suppressed — you get ONE alert, and ONE more (`mac-probe → OK`) when it wakes; the siblings' `LATE → OK` are muted while the probe is still LATE | That IS the signal (Mac offline). If only some Mac jobs are late, read the `mac-probe` note — it names the sub-probe that errored. |
| Sibling Mac job pages "→ LATE" a tick before `mac-probe` does | Two alerts for one night's sleep | A sibling's `grace_s` dropped below `mac-probe`'s + 120 in `jobs.yml` (the probe posts siblings before itself). Restore the margin. |
| rclone's shared Google `client_id` retired (2026) | Every Drive remote using the built-in client fails to refresh at once — probes AND the backup writers on box + Mac | `rclone lsd` interactively shows the OAuth error; rclone 1.75+ warns ahead of time. Fix = own GCP OAuth Desktop client in each conf (§1b follow-up). |
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
