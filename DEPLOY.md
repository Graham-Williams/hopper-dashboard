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
`box`: `km-backup`, `todoist-points-backup`, `box-containers`, `box-disk`, `dashboard-probes`,
`inbox-github-sync`, `hopper-dashboard-backup` ·
`mac`: `mac-probe`, `pa-backup`, `drive-mirror`, `minecraft-offload`, `mac-disk`, `taste-twin-publish`,
`jjho-refresh`, `baby-pool-sync`, `inbox-transcribe`, `inbox-backlog`. **Seventeen.**

**The Inbox release adds four of those, plus two whole components** — see §1d-ii (the `jobs.yml` blocks,
which `git pull` cannot add), §2b (the box backup: this app now stores ORIGINAL data and had no backup at
all) and §4b (the Mac transcription worker, which is the only path from a recorded voice note to readable
text). It also changes one **value**, §1c: the dashboard gets its OWN `APP_PASSWORD` instead of the shared
house word, because the board is linked from the public apex page and now holds recordings of Graham's
voice.

**Adding a job id is app-first, probe-second.** The registry is loaded once at start-up, so a new id must be
in the live (gitignored) `jobs.yml` **and the container restarted** *before* anything posts to it — `docker
compose up -d` in `~/hopper-dashboard` after editing the file. In the other order every ping 404s, the Mac
probe counts that as a failed sub-probe and turns `mac-probe` into `fail`, and the box unit exits non-zero
every 5 minutes.

**Getting that order wrong used to be LOUD and is now QUIET.** Pre-0.2 the failing `mac-probe` pushed an ntfy
alert on every hourly transition; under the episode model it pages **once, after 72 h** (`mac-probe`'s
threshold), and a `fail` that clears inside that window pages not at all. So do not expect the phone to catch
this for you — check the board, or `journalctl -u dashboard-containers.service` / the Mac probe log, straight
after adding an id.

---

## 1. Box — app container

```bash
ssh <user>@<box-tailscale-ip>
git clone https://github.com/Graham-Williams/hopper-dashboard ~/hopper-dashboard
cd ~/hopper-dashboard
TS_IP="$(tailscale ip -4)"

# .env — never committed.
# ⚠️ APP_PASSWORD IS THIS APP'S OWN PASSWORD — NOT the shared house word (changed 2026-09-19).
# It used to be copied verbatim out of ~/km-tracker/.env. It is not any more: the same word
# opens km-tracker, taste-twin, jjho and todoist-points, Graham has given it to friends, and
# the dashboard is linked from the public apex page. That was fine while the board only showed
# backup timestamps. It is not fine now that /inbox stores RECORDINGS OF HIS VOICE.
# Set it by hand — Graham types it, the recipe never sees it (§1c below).
cp .env.example .env && chmod 600 .env
sed -i "s|^SESSION_SECRET=.*|SESSION_SECRET=$(openssl rand -hex 32)|" .env
sed -i "s|^INGEST_TOKEN=.*|INGEST_TOKEN=$(openssl rand -hex 32)|" .env
sed -i "s|^READ_TOKEN=.*|READ_TOKEN=$(openssl rand -hex 32)|" .env
sed -i "s|^NTFY_TOPIC=.*|NTFY_TOPIC=hopper-$(openssl rand -hex 16)|" .env
# .env.example already ships INGEST_BIND=127.0.0.1, so a `grep -q || echo` would never fire — replace in place:
sed -i "s|^INGEST_BIND=.*|INGEST_BIND=${TS_IP}|" .env                   # Tailscale IP ONLY — ufw is inactive
# The read role trusts CF-Connecting-IP only from the tunnel container's network:
sed -i "s|^TRUSTED_PROXY_CIDR=.*|TRUSTED_PROXY_CIDR=$(docker network inspect km-tracker_default -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}')|" .env
# leave APP_HOST=dashboard.graham-williams.com and APP_ENV=prod as shipped
# PROBE_INTERVAL_S / DASHBOARD_RCLONE_TIMEOUT_S / PROBE_FAIL_THRESHOLD / PROBE_NO_SUCCESS_S are optional —
# compose supplies the defaults (300 / 240 / 2 / 3600), so an existing .env needs no edit. See DESIGN.md
# "Probe cadence and flap damping" before changing any of them. NOTE the timeout knob is
# DASHBOARD_RCLONE_TIMEOUT_S: `RCLONE_TIMEOUT*` is rclone's own env namespace (`--timeout`).

# The Inbox's six keys. All optional: with INBOX_TOKEN empty the board boots, /inbox works in a
# browser, and only the MACHINE endpoints (the Mac worker + the backlog mirror) fail closed with
# 401. None of them is a `${VAR:?}` in compose, deliberately — the board being up matters more
# than the Inbox being up. Skip this block to deploy the board without the Inbox's machine side.
sed -i "s|^INBOX_TOKEN=.*|INBOX_TOKEN=$(openssl rand -hex 32)|" .env
# The ten PUBLIC repos. The five private ones (hopper, arbinator, odds-scraper, arb-detector,
# mbcexercise) are deliberately out: the mirror runs unauthenticated. The list is VALIDATED AT
# STARTUP, so a typo fails the container immediately rather than at the first sync.
# .env.example already ships this exact line — check it rather than retyping it:
grep -c 'Graham-Williams/' .env        # expect 1 line listing ten repos
# INBOX_AUDIO_MAX_BYTES / INBOX_AUDIO_MAX_TOTAL_BYTES / INBOX_AUDIO_RETENTION_DAYS /
# INBOX_GITHUB_INTERVAL_S / INBOX_PRUNE_INTERVAL_S all have compose defaults (2 MiB per note /
# 3 GB tree / 90 d / 900 s / 3600 s); leave them. One of them is not just a door policy:
# INBOX_AUDIO_MAX_BYTES is rendered into the CAPTURE PAGE, where the recorder stops a take just
# under it and keeps what it already recorded ("Stopped at the 2 MB limit — saved what was
# recorded"). So changing it changes the maximum LENGTH of a voice note — ~5 min at 2 MB, since
# audio is requested at 48 kbps — rather than deciding which uploads get a 413. Lowering it
# shortens takes, with the user told on screen; it never loses a recording that was made.

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

### 1c. Set the dashboard's own password — GRAHAM DOES THIS, BY HAND

This app has its OWN `APP_PASSWORD`, not the shared house word. **Nothing in this file echoes,
prints, accepts or stores that password** — the same rule `deploy/box/install.sh` and
`deploy/mac/install.sh` already apply to tokens, both of which refuse `--token` on the command
line because an argument lands in shell history and in `ps`. So this is an operator action, not
a recipe step, and it is the one thing here Graham types himself:

```bash
# On the box, in ~/hopper-dashboard. Pick a NEW word — not the house password.
# `read -s` echoes nothing; `set +o history` keeps the line out of the history file; the editor
# below prints the LENGTH, never the value.
set +o history
read -r -s -p "new dashboard APP_PASSWORD (hidden): " PW; echo
[ -n "$PW" ] || echo "STOP: empty password"
PW="$PW" python3 -c 'import os; \
p=os.path.expanduser("~/hopper-dashboard/.env"); pw=os.environ["PW"]; \
ls=open(p).read().splitlines(); \
open(p,"w").write("\n".join(("APP_PASSWORD="+pw) if l.startswith("APP_PASSWORD=") else l for l in ls)+"\n"); \
os.chmod(p,0o600); print("APP_PASSWORD set (%d chars)" % len(pw))'
unset PW; set -o history
docker compose up -d            # picks the new value up; no rebuild needed
```

The password goes in via the ENVIRONMENT, not argv — `ps` shows a process's arguments to every
user on the box, and `PW=... python3 -c` keeps the value out of them.

**Consequences, stated so nobody debugs them twice.** The shared house word no longer opens
`dashboard.graham-williams.com`. km-tracker, taste-twin, jjho and todoist-points are untouched
and keep sharing theirs. Graham needs the new word in his password manager, and anyone he has
given the house word to can no longer reach the board — which is the point, now that `/inbox`
stores recordings of his voice. The **local-QA recipe is unaffected**: it runs `APP_ENV=dev
APP_PASSWORD=`, and the read side only refuses to boot on an empty password under
`APP_ENV=prod`.

### 1c-ii. Start and verify

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

### 1d. ⚠️ Upgrading an EXISTING box: the `jobs.yml` edit `git pull` cannot make

**`jobs.yml` is gitignored** (it carries real Drive paths and machine topology), so a pull brings the new
*schema* but never new *values*. It keeps working if you skip it, silently and wrongly — the container starts
fine either way, so nothing will tell you. A **fresh** install that copied `jobs.example.yml` needs none of
this.

**Items 1 and 4 are outstanding on this box.** Items 2 and 3 below landed on earlier deploys and
are already in the live file (`grace_s: 900`, and both `disk` blocks — verified). They stay documented
because the script still checks them and because a rebuilt box would need them, but expect them to be no-ops.

1. **Alert thresholds** (this release — the one that is actually outstanding). Every key is optional, so a
   live file with none of them falls back to the 24 h default for *every* job — including the four that
   should never page; `box-containers`, which pages at 35 min when configured and **23.7 h late** on the
   default; and `mac-probe`, which would then page after every long weekend the Mac sleeps.
2. **`dashboard-probes: grace_s` 600 → 900** (PR #8, already applied here). A probe cycle can now spend
   budget + one `DASHBOARD_RCLONE_TIMEOUT_S` ≈ 540 s before writing its heartbeat; with 600 s of grace a slow
   cycle trades FAIL flapping for LATE flapping.
3. **`box-disk` and `mac-disk` must EXIST** (PR #9, already applied here) **before their probes first post.**
   Ingest rejects an undeclared job id with 404, and the Mac probe treats that as a failed sub-probe — so a
   missing `mac-disk` turns `mac-probe` itself into an hourly failing ping. Copy both blocks verbatim from
   `jobs.example.yml` (they contain no secrets) into the right machine sections. The script below **fails**
   if either is absent rather than inventing it.
4. **THE INBOX'S FOUR NEW JOBS** (this release, outstanding). `inbox-github-sync` and
   `hopper-dashboard-backup` on the box; `inbox-transcribe` and `inbox-backlog` on the Mac. Same 404 rule as
   item 3, and the same consequence in a nastier place: **`inbox-backlog` is a sub-probe of the hourly Mac
   probe**, so until the id exists in `jobs.yml` its ping is rejected and `mac-probe` reports a failed
   sub-probe every hour. (It only posts once `INBOX_URL`/`INBOX_TOKEN` are in the Mac env file, so the safe
   order is: declare the jobs here FIRST, then §4b.) Copy the four blocks — §1d-ii below does it from
   `jobs.example.yml` so nothing is retyped.

```bash
set -e                                           # ⚠️ REQUIRED — see below
cd ~/hopper-dashboard
# TIMESTAMPED, not just dated. `$(date +%F)` meant a second run on the same day
# overwrote the pre-edit backup WITH THE EDITED FILE, destroying the only copy of
# pre-upgrade state — which the Rollback section now depends on.
cp jobs.yml "jobs.yml.bak.$(date +%F-%H%M%S)"    # the live file is the only copy of the real paths/ids
python3 - <<'PY'
import re, sys
p = "jobs.yml"; s = open(p).read()
# One page only when a job has been CONTINUOUSLY not-OK this long (DESIGN.md "Alerting
# rules"). mac-probe is 72 h and NOT `alert: never`: it is the job whose alert mutes its
# Mac siblings, so `never` there means a Mac gone for a week pages nothing at all. The
# two disk gauges are 1 h, not the 24 h default: a dead gauge is already LATE only after
# 48 h, and a capacity threshold is a level, not a flap.
#
# These are THRESHOLDS, not times-to-page: the clock starts when the job goes not-OK,
# which is already cadence+grace after its last good run. End-to-end each job pages at
# km-backup 24.3h / todoist-points-backup 24.3h / box-containers 35m / box-disk 1h /
# dashboard-probes 6.3h / mac-probe 87h / mac-disk 1h / pa-backup 44h / drive-mirror 39h.
# pa-backup is 21600 (6 h) and NOT the old 108000: its LATE deadline alone is 38 h, so
# 108000 paged at 68 h ~ 2.8 days. If a box file still carries 108000 this script leaves
# it alone (it already has a policy) -- fix that one by hand, see the note after the
# expected-output block below.
AFTER = {"km-backup": 86400, "todoist-points-backup": 86400, "box-containers": 1200,
         "box-disk": 3600, "dashboard-probes": 21600, "mac-probe": 259200,
         "mac-disk": 3600, "pa-backup": 21600, "drive-mirror": 86400}
NEVER = ("minecraft-offload", "taste-twin-publish", "jjho-refresh", "baby-pool-sync")
# PR #8 raised this job's worst-case cycle; 600 s of grace would trade FAIL flapping for
# LATE flapping. Scoped to its own block, and only ever raised.
GRACE = {"dashboard-probes": 900}

def block(jid, text):            # one job's own lines, and nothing after them
    # `\s*$`, not `$`: a TRAILING SPACE after the id is invisible in a diff, makes this
    # regex miss, and silently drops that job to the 24 h default. Worst case mac-probe.
    return re.search(r"^  - id: %s\s*$(?:\n(?!  - id:).*)*" % re.escape(jid), text, re.M)

def replace(text, m, new):       # splice a rewritten block back in by POSITION
    return text[:m.start()] + new + text[m.end():]

missing = [j for j in list(AFTER) + list(NEVER) if block(j, s) is None]
for jid, secs in list(AFTER.items()) + [(j, None) for j in NEVER]:
    m = block(jid, s)
    if m is None:
        continue
    body = m.group(0)
    if re.search(r"^    alert(_after_s)?:", body, re.M):
        continue                 # already carries a policy of its own — leave it alone
    line = "    alert: never\n" if secs is None else "    alert_after_s: %d\n" % secs
    # Insert after the job's own `name:` line (required on every job, so it always
    # exists). `(?:\n|$)`: a valid YAML file whose LAST line has no trailing newline
    # would otherwise not match here at all and crash on `nm.end()`.
    nm = re.search(r"^    name: .*(?:\n|$)", body, re.M)
    if nm is None:
        sys.stderr.write("!! %s has no `name:` line -- not a valid job block\n" % jid)
        sys.exit(1)
    s = replace(s, m, body[:nm.end()] + line + body[nm.end():])
for jid, want in GRACE.items():
    m = block(jid, s)
    if m is None:
        continue
    body = m.group(0)
    g = re.search(r"^    grace_s: (\d+)", body, re.M)
    if g is None or int(g.group(1)) >= want:
        continue
    s = replace(s, m, body[:g.start()] + "    grace_s: %d" % want + body[g.end():])
open(p, "w").write(s)
if missing:
    # EXIT NON-ZERO. This used to warn and `exit 0`, immediately upstream of a
    # `docker compose up -d --build`: the warning scrolled away, the deploy went ahead
    # on 24 h defaults for the named jobs, and nothing downstream ever said so.
    sys.stderr.write("!! NOT IN jobs.yml, so they got no policy: %s\n"
                     "   Add the job (see DEPLOY.md 1d) and re-run.\n" % ", ".join(missing))
    sys.exit(1)
PY
grep -nE '^  - id:|^    (alert|grace_s|late_means)' jobs.yml   # 9 thresholds, 4 nevers, grace_s 900
docker compose up -d --build                      # validation is strict: a bad edit = a loud restart loop
docker logs --tail 20 hopper-dashboard            # `jobs.yml: job '<id>': …` names the offending field
```

**`set -e` and the `sys.exit(1)` are the point, not decoration.** Every failure path in the old version
ended in `exit 0` inside a block with no `set -e`, and the next two lines were a long `grep` and a
`docker compose up -d --build`. So a run that edited nothing at all scrolled its one warning off the screen
and then deployed, on defaults, looking exactly like a success. With both in place the block stops at the
warning and never reaches the build.

**If the box file already carries `pa-backup: alert_after_s: 108000`,** this script will NOT change it — a
job that already has a policy is deliberately left alone, which is what makes the script idempotent. That
value pages at 68 h (its LATE deadline alone is 38 h), so fix it by hand and re-`up`:

```bash
sed -i 's/^    alert_after_s: 108000.*$/    alert_after_s: 21600        # 6 h -> pages at 44 h/' jobs.yml
grep -n -A1 'id: pa-backup' -A12 jobs.yml | grep alert_after_s      # must read 21600
```

**Every check in that script is scoped to one job's own block.** A file-wide search finds a *later* job's key,
concludes "already done", and skips this one silently — a clean parse, no error, and the edit you thought you
made never happened.

**Idempotency, verified by running it 3× over five input shapes** — the file as shipped, a pre-feature file, a
file where one job already carries a hand-written `alert:`, a pre-#8/#9 file (600 s grace, no disk jobs), and
one whose **jobs are in reverse order**: byte-identical after every run, `load_registry` green on all fifteen
outputs, no job left inheriting the 24 h default, `grace_s` 900, and the missing-disk-job case warned instead
of guessing. On the shipped file it is a true no-op (same bytes in and out).

**Then one hand edit the script deliberately does not make.** `mac-probe`'s `late_means:` is operator prose
shown on the card, and on a pre-feature box it describes the behaviour this release changed. Make it say what
now happens (this is the wording in `jobs.example.yml`):

```
    late_means: Not heard from the Mac in 15 hours — probably offline or asleep, not broken. Only a Mac that stays gone for ~3.5 days pages.
```

Nothing depends on the string — it is the sentence Graham reads on the card at 2 a.m., which is exactly why a
stale one matters.

Finally, confirm the **app** agrees with the file. This is the check that proves the edit landed — and it
**exits non-zero when it has not**, which the previous version of this command could not do:

```bash
RT="$(grep '^READ_TOKEN=' ~/hopper-dashboard/.env | cut -d= -f2-)"
AH="$(grep '^APP_HOST=' ~/hopper-dashboard/.env | cut -d= -f2-)"
docker exec -i -e RT="$RT" -e AH="$AH" hopper-dashboard python - <<'PY'
import json, os, sys, urllib.request as u
req = u.Request("http://127.0.0.1:8080/api/v1/status",
                headers={"Authorization": "Bearer " + os.environ["RT"],
                         "Host": os.environ["AH"]})
jobs = json.load(u.urlopen(req, timeout=10))["jobs"]
bad = []
for j in jobs:
    # .get(), so an OLD IMAGE reports itself instead of raising a KeyError traceback
    # that looks like the no-op this command used to be.
    a = j.get("alert", "NO-ALERT-FIELD (still on the old image)")
    print("%-24s %s" % (j["id"], a))
    src = a.get("source") if isinstance(a, dict) else str(a)
    if src in ("default", "informational") or not isinstance(a, dict):
        bad.append("%s=%s" % (j["id"], src))
print()
print("%d jobs; %d explicit; %d never" % (
    len(jobs),
    sum(1 for j in jobs if j.get("alert", {}).get("source") == "alert_after_s"),
    sum(1 for j in jobs if j.get("alert", {}).get("source") == "alert")))
if bad:
    print("FAIL: no job may resolve to default/informational/NO-ALERT-FIELD -> " +
          ", ".join(bad))
    sys.exit(1)
print("PASS: every job carries an explicit policy")
PY
echo "exit=$?"     # MUST be 0
```

Three things that were wrong with the old one-liner, all of which made a broken box read as a clean one:

- **`docker exec` without `-i` does not forward stdin.** `python -` then reads EOF, **prints nothing and
  exits 0** — verified on the live box. The operator sees no `source: default` lines and concludes "clean",
  which is exactly the wrong conclusion from exactly the wrong evidence. `-i` is the whole fix.
- The token was pasted **on the command line**, where it lands in shell history and `ps`. §2 already passes
  it via `-e`; this now matches.
- `j["alert"]` raises `KeyError` on an image that predates the field — a traceback that, in the middle of a
  deploy, reads like the same no-op. `.get()` with a named placeholder makes an old image say so.

Expect nine `"source": "alert_after_s"`, four `"source": "alert"` with `"never": true`, and **no**
`"source": "default"` and no `"source": "informational"` at all — a `default` means that job was missed, and
an `informational` means an `alert: never` line went missing and the job is only silent by accident.
`mac-probe` must read `{"after_s": 259200, "never": false}`; `never: true` there silences the whole Mac.
Each `alert` block also carries `cooldown_s` and `last_paged_at` (the per-job page rate limit) and
`alerted_state` — what the episode's page actually said, which is not always the state on the card — and
`window_s`, the window the accumulator adds not-OK time up over (`2 x alert_after_s`).

The `bad_since` / `alerted_at` / `alerted_state` / `last_paged_at` columns are added to the existing SQLite by
`init_schema` at start-up — additive `ALTER TABLE`, no data loss, NULL for every row. A job that is already
broken at upgrade time therefore starts a **fresh** episode and pages one threshold later: late, never silent.
A NULL `last_paged_at` reads as "has never paged", so no job starts life inside a cooldown it did not earn. The
one row shape that could confuse the newest column — an episode that is open AND already paged when
`alerted_state` arrives — is healed on the first recompute after the deploy (`docker logs hopper-dashboard |
grep 'recording .* as the paged state'`), with no extra push. An index (`sc_job_seq`) is created on
`state_changes` at the same time; on this box's row counts that is instant.

### 1d-ii. Copying the Inbox's four job blocks into the live `jobs.yml`

These four carry **no secrets and no machine-specific values** — unlike `drive-mirror` (Drive folder ids) or
`box-containers` (`expect:` names) — so they are copied verbatim out of `jobs.example.yml` rather than
hand-written. The script is idempotent, refuses to duplicate an id that is already there, and inserts each
block into the section its `machine:` says.

```bash
set -e
cd ~/hopper-dashboard
cp jobs.yml "jobs.yml.bak.$(date +%F-%H%M%S)"     # timestamped: the live file is the only copy of the real ids
python3 - <<'PY'
import re, sys
WANT = ["inbox-github-sync", "hopper-dashboard-backup",      # box
        "inbox-transcribe", "inbox-backlog"]                 # mac
MAC_MARKER = "  # ------------------------------------------------------------------ mac --"

def block(jid, text):
    # `\s*$` not `$`: a trailing space after the id is invisible in a diff and makes this miss.
    return re.search(r"^  - id: %s\s*$(?:\n(?!  - id:).*)*" % re.escape(jid), text, re.M)

def trim(body):
    # A block runs to the NEXT `- id:`, so the last box job's match also swallows the blank
    # line and the `# --- mac --` SECTION MARKER that follow it. Copying that verbatim moves
    # the marker into the box section, and the next run then cannot find it — the script
    # stops with "no mac section marker" and the Mac jobs are never added. Drop trailing
    # blank lines and top-level (exactly two-space) comments; deeper comments, e.g. the ones
    # inside a `probe:` block, are part of the job and stay.
    lines = body.rstrip("\n").split("\n")
    while lines and (not lines[-1].strip() or re.match(r"^  #", lines[-1])):
        lines.pop()
    return "\n".join(lines)

example = open("jobs.example.yml").read()
live = open("jobs.yml").read()
if MAC_MARKER not in live:
    sys.stderr.write("!! jobs.yml has no `# --- mac --` section marker; add the blocks by hand\n")
    sys.exit(1)
added = []
for jid in WANT:
    if block(jid, live) is not None:
        continue                                   # already present — never duplicated
    m = block(jid, example)
    if m is None:
        sys.stderr.write("!! %s is not in jobs.example.yml — wrong checkout?\n" % jid)
        sys.exit(1)
    body = trim(m.group(0)) + "\n\n"
    machine = re.search(r"^    machine: (\w+)", body, re.M).group(1)
    if machine == "box":
        live = live.replace(MAC_MARKER, body + MAC_MARKER, 1)   # last box job, before the marker
    else:
        live = live.rstrip("\n") + "\n\n" + body              # end of the mac section
    added.append(jid)
open("jobs.yml", "w").write(live)
print("added: %s" % (", ".join(added) or "nothing (all four already present)"))
missing = [j for j in WANT if block(j, live) is None]
if missing:
    sys.stderr.write("!! still missing: %s\n" % ", ".join(missing))
    sys.exit(1)
PY
chmod 644 jobs.yml                                 # uid 10001 reads it; a 0600 file restart-loops the container
grep -cE '^  - id:' jobs.yml                       # expect 17
docker compose up -d --build
docker logs --tail 20 hopper-dashboard             # `jobs.yml: job '<id>': …` names any bad field
```

**Then re-run the §1d verification command** (the `/api/v1/status` one): all four new jobs must show
`"source": "alert_after_s"` with `after_s: 86400`. A `"source": "default"` there means the block landed
without its `alert_after_s:` line — same 24 h number by luck, but nothing in the file says so.

**`hopper-dashboard-backup` will read LATE until §2b installs its timer, and that is correct** — nothing is
posting it yet. The same is true of `inbox-transcribe` and `inbox-backlog` until §4b.

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
- writes `/etc/hopper-dashboard/ingest.curlrc` (**owned by the service user, 0600**) holding one line —
  `header = "Authorization: Bearer <token>"` — which is how the `hopper-dashboard-backup` heartbeat
  authenticates. It exists because systemd expands `${INGEST_TOKEN}` into the ExecStopPost child's argv,
  and argv is world-readable in `/proc/<pid>/cmdline` on a default Ubuntu; curl reading the header from a
  0600 file keeps it off the command line. (The journal was never affected — systemd logs the argv
  unexpanded.) It must be readable by the service user, unlike root-only `ingest.env`, which costs nothing:
  that user already owns `~/hopper-dashboard/.env`, where the token comes from. **If you rotate
  `INGEST_TOKEN`, re-run `install.sh` — this file does not update itself, and a stale one 401s every tick**
  (`journalctl -u hopper-dashboard-backup.service | grep 401`). ⚠️ The two OLDER drop-ins
  (`km-backup`, `todoist-points-backup`) still carry the token in argv — pre-existing, tracked separately;
  do not "fix" them here, they belong to other repos' units.
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

### 2b. Box — the DB + audio backup (NEW; `hopper-dashboard-backup`)

**Why this app now needs a backup at all.** Until `/inbox`, every row in `dashboard.db` was a heartbeat or a
probe reading the next tick reproduces — there was genuinely nothing to lose. `inbox.db` is different: it
holds the transcripts of Graham's voice notes and his triage state (what he has reviewed, what became which
GitHub issue), and `/app/data/inbox/audio` holds the recordings. **There is no other copy of either.** The
app whose entire job is catching unbacked-up data was about to become the last thing on this box without a
backup.

`deploy/box/install.sh` (§2) installs the timer, its unit and its heartbeat drop-in **together** — there is
no flag to install one without the others, because an unmonitored backup is the exact silent failure this
repo exists to catch. So §2 already did this if you ran it after this release. What still needs doing by hand
is the Drive destination, which lives outside the repo:

```bash
cd ~/hopper-dashboard
# The backup pushes with the WRITER remote (`gdrive:`), not the read-only `gdrive-ro:` the probes use —
# a backup that cannot write is not a backup. Same remote km-tracker and todoist-points already push with.
rclone listremotes | grep -qx 'gdrive:' || echo "STOP: no writer remote; the local ring still works, Drive does not"
# 0600 is REQUIRED, not tidiness: backup.sh `source`s this file — executing it as shell, as a user in
# the docker group, every 5 minutes — and REFUSES to source it if it is group- or other-writable.
( umask 077; cat > deploy/box/.env.backup <<'EOF'
# hopper-dashboard backup config (gitignored, 0600). Every key is optional; these are the ones
# that differ from the defaults in deploy/box/backup.sh.
RCLONE_DEST=gdrive:hopper-dashboard-backups
EOF
)
# Run it once by hand before trusting the timer. Expect two "saved …" lines and a Drive push.
deploy/box/backup.sh
ls -la ~/hopper-dashboard-backups/snapshots/       # dashboard_<ts>.db + inbox_<ts>.db
rclone lsf gdrive:hopper-dashboard-backups         # both, plus daily/ and audio/
systemctl start hopper-dashboard-backup.service    # and once through systemd, to prove the unit works
journalctl -u hopper-dashboard-backup.service -n 20 --no-pager | grep -i curl   # the heartbeat line
```

What it does, and the two things that are non-negotiable about how:

- **The snapshot runs INSIDE the container**, via `docker exec … python3 -` using SQLite's online backup
  API. It cannot run host-side: both DBs are WAL-mode and their `-wal`/`-shm` sidecars are owned by the
  container's uid 10001, so the backup API — which must WRITE those sidecars to take its read lock — fails
  with *"attempt to write a readonly database"* even when the source is opened `mode=ro`. The finished file
  is integrity-checked in the container, `docker cp`'d out, and **re-verified on the host** (a truncated copy
  would otherwise reach Drive undetected, since everything downstream only sha256s the host file).
- **The DB snapshots are additive; the AUDIO TREE IS A MIRROR.** The two databases go up with
  `rclone copy` into a ring plus a `daily/` tier: the upload never deletes, and a snapshot leaves Drive
  only when enough newer ones have pushed it out by retention count. Nothing that happens to the live DB
  can remove an off-box DB snapshot.
  The **audio tree is the deliberate exception** (Graham's decision, 2026-09-19): it mirrors the
  container, deletions included, because an additive audio backup defeats both the Inbox's Delete control
  — sold as the way to retract "a password read aloud" — and its 180-day privacy ceiling. Each run
  `docker cp`s the tree into a **fresh** staging directory (copying into a persistent one would resurrect
  deleted files, since `docker cp` only adds) and then `rclone copy`s it up and deletes the remote files
  the container no longer has, one at a time, logged.
- **The brakes on that mirror — three of them, and each can refuse on its own.** A mass deletion is far
  likelier to be a wiped volume or a mis-set path than an intentional purge, so the run REFUSES to
  propagate one and exits non-zero (the heartbeat goes `fail`, the board pages after the job's threshold):

  | Knob | Default | Refuses when |
  |---|---|---|
  | `AUDIO_MAX_DROP_PCT` | `50` | the count has fallen by more than this share of the baseline |
  | `AUDIO_MAX_DROP_FILES` | `25` | more than this many files would be deleted in ONE run, whatever the share |
  | `AUDIO_DROP_WINDOW_MIN` | `1440` | the percentage is measured against the highest count in this window, not only the previous run |

  It also refuses, with no knob involved, when: the container's audio directory is missing entirely (that
  is a *skip*, never a deletion); the STAGED tree does not hold what the container said it holds; the
  remote cannot be listed; or the container has no recordings at all while Drive has some. Drive keeps
  what it has in every one of those cases, and the remembered count does not move.

  **Why three.** A percentage alone cannot see cumulative loss: 45% per run against a 50% brake never
  trips, and a 1024-file tree walks down to 1 in ten runs — fifty minutes at the five-minute cadence. The
  absolute limit catches the big single step; the window catches the drip.

  ⚠️ **`AUDIO_MAX_DROP_PCT` is validated to 1–99 and `100` is REFUSED.** It is a percentage, not "a
  positive integer": at `100` the comparison becomes `count * 100 < prev * 0`, which is never true, so
  the brake would be silently OFF on every run; at `200` the right-hand side goes negative, which is off
  *and* inverted. A leading zero (`050`) is forced to base 10 rather than read as octal 40. To opt out of
  the audio mirror entirely, set `BACKUP_AUDIO=0` — there is no "percentage that means no brake".

  **The baseline is what DRIVE holds, not a file on the box.** With no remembered count — the first run
  after deploy, a cleared state dir, a changed `BACKUP_ROOT`/`HOME`, a rebuild-from-Drive — it comes from
  an `rclone lsf` of `…/audio`. (It used to fall back to `~/hopper-dashboard-backups/audio`, a directory
  nothing ever created, so the baseline was 0 and a baseline of 0 opens the brake completely — on exactly
  the runs that need it most.) A run that **cannot** obtain that listing uploads and deletes nothing, and
  deliberately records no baseline either: recording one it just failed to verify is how the next run
  would be handed a licence to delete whatever it could not see.

  **For a deliberate purge, the override names the count it should LEAVE BEHIND.** It is one-shot by
  construction — a boolean left behind in `.env.backup` would disable every brake for ever with nothing
  but a WARN line, whereas a count describes one specific purge and, once that purge has happened,
  authorises nothing. The refusal message tells you the exact number to use:
  ```bash
  # the log line says: "... re-run ONCE with AUDIO_ALLOW_MASS_DELETE=7"
  cd ~/hopper-dashboard && AUDIO_ALLOW_MASS_DELETE=7 deploy/box/backup.sh
  ```
  Then remove it from the environment. Leaving it set is harmless but pointless: the next purge will have
  a different resulting count and will be refused.
- **⚠️ Delete does not retract the TEXT from backups already taken.** Delete removes the row and the
  recording immediately, and the recording leaves Drive within one backup cycle. But every `inbox_*.db`
  snapshot already on Drive — the ring plus the `daily/` tier — still contains the transcript, and those
  age out on `DAILY_RETENTION`, i.e. **up to 30 days**. Do not "fix" this by rewriting historical
  snapshots; a backup that can be edited after the fact is not a backup. Keeping the DB backups is
  correct, and the UI and DESIGN.md say so plainly instead of promising more than Delete delivers.
- **Retention values are validated before anything is pruned.** `LOCAL_RETENTION`, `DRIVE_RETENTION`,
  `DAILY_RETENTION`, `AUDIO_MAX_DROP_FILES` and `AUDIO_DROP_WINDOW_MIN` must each be an integer ≥ 1 (and
  at most 9 digits) or the run dies with a named error; `AUDIO_MAX_DROP_PCT` must be 1–99. A value of `0`
  (or `" "`) is NOT caught by `${VAR:-60}` — it is non-empty — and would make every prune slice cover the
  whole list, deleting every snapshot on the box AND in `gdrive:hopper-dashboard-backups` on a single tick.
- **These guards are tested by running the real script**, not by grepping it: `tests/test_deploy_backup.py`
  puts a fake `docker` and a fake `rclone` (`tests/fakes/`) on `PATH`, drives `deploy/box/backup.sh`
  end-to-end against a fake container and a fake remote, and asserts on what is left in the remote
  afterwards. If you change a guard, that is where to prove it still refuses.
- **`deploy/box/.env.backup` is refused if it is group- or other-writable.** It is `source`d — executed as
  shell — every five minutes as a user in the `docker` group, so `chmod 600` it (the recipe above does).
- **`~/hopper-dashboard-backups/` and everything under it is created 0700**, and audio files are tightened
  to owner-only after the `docker cp` (which brings the container's ~0644 modes with it). These are
  recordings of Graham's voice; the on-box mirror should not be readable by every account on the box.

Also: sha256 dedupe (an unchanged DB does not create a new file), a 60-deep local ring, a Drive push
throttled to ~15 minutes, a `daily/` tier keeping one snapshot per UTC day for 30 days, and a guard that
**refuses a snapshot in which every table is empty while the previous one had data** — the container runs its
schema migration on every boot, so a `/app/data` remounted empty yields a valid, integrity-ok, completely
empty DB, and snapshotting that would rotate every good copy out of the ring and both tiers. Override with
`ALLOW_EMPTY_SNAPSHOT=1` only when the DB really was emptied on purpose.

**Restoring is NOT a `cp`.** Stop the container, delete the live DB's stale `-wal`/`-shm` sidecars, copy the
snapshot in, and re-own it to uid 10001 — otherwise SQLite replays the old WAL over the restored image and
silently hands back the PRE-restore data, with no error, and the next checkpoint bakes it in:

```bash
cd ~/hopper-dashboard && docker compose stop
V=$(docker volume inspect hopper-dashboard_hopper-dashboard-data -f '{{.Mountpoint}}')
sudo rm -f "$V/inbox.db-wal" "$V/inbox.db-shm"
sudo cp ~/hopper-dashboard-backups/snapshots/inbox_<ts>.db "$V/inbox.db"
sudo chown 10001:10001 "$V/inbox.db"
docker compose up -d
```

Audio files are restored by copying them back under `/app/data/inbox/audio/<yyyy>/<mm>/` with the same
ownership. A row whose `audio_path` points at a file that is not there is **not** a crash: the scheduler's
reconcile clears the dangling path and the board shows the item with its transcript and no player.

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

**The durable Mac checkout is `~/code/hopper-dashboard`.** Every Mac path in this file assumes it. For a
preview the feature branch is checked out *there* (`git fetch && git checkout <branch>`); after the merge it
goes back to `main` (`git checkout main && git pull`). (`feature/dashboard-app` used to be named here; that
branch is long merged and gone — do not go looking for it.)

**⚠️ For THIS release the Mac side needs NOTHING — do not reinstall it.** The alert-threshold work is
entirely app-side: `git diff origin/main...HEAD -- probes/ deploy/ docker-compose.yml Dockerfile
entrypoint.sh` is **empty**, so the launchd plist, the probe scripts and the env file are all unchanged. A
`git pull` in `~/code/hopper-dashboard` is enough to keep the checkout current; re-running
`deploy/mac/install.sh` is unnecessary and is the step most likely to disturb a working probe. Do **not** install from a worktree
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
`job_id: STATE for over <duration>` (plus `… in the last <window>` when the accumulator decided it, or
`job_id: FROM → TO` for an escalation or a recovery) — never free text. Test from the box:
`curl -d "dashboard test" https://ntfy.sh/$(grep '^NTFY_TOPIC=' ~/hopper-dashboard/.env | cut -d= -f2-)`.

**A push means a SUSTAINED problem, not a blip.** A job pages once, after it has been not-OK for its own
`alert_after_s` (§1d; per-job rationale in `jobs.example.yml`, model in DESIGN.md "Alerting
rules"), and sends a `→ OK` only if that page actually went out. So the board and `/api/v1/status` will
regularly show a `LATE` or `FAIL` the phone never heard about — that is the design, not a bug. Two
consequences when reading the board:
- a state change alone is **not** an alert; check the job page's *Alerts* line or `alert.bad_since` in the
  API to see whether an episode is running and whether it has been paged;
- a job can read `OK` with its episode still open (it has not held OK long enough to be believed, or the
  evidence for the OK is missing). The card says so.

**Two push shapes to recognise, both added after the first release:**
- `dashboard-probes: FAIL for over 6h in the last 12h` — the "in the last …" clause means the job was not
  broken for six unbroken hours but for six of the last twelve. A destination that fails for an hour and then
  lists successfully once used to reset its clock for ever and page **never**; a job can now reach its bar
  cumulatively (DESIGN.md "the accumulator"). Same threshold, same cooldown; only the way the bar is reached
  is new, and the body always says which rule spoke.
- `box-disk: BEHIND → FAIL` at **high** priority, after a `BEHIND for over 1h` at default — an *escalation*.
  An episode that already paged may page once more if it gets strictly worse, at the worse state's own
  priority. This is why a gauge that pages "low on space" and then fills up is no longer silent. At most one
  escalation per episode; the job page's *Alerts* line shows what was last paged about.

The phone therefore sees up to three pushes per cooldown window for one job (page + escalation + recovery)
rather than two. Reaching that takes a job that crosses its threshold, gets worse, and genuinely recovers,
every six hours.

**Overnight sleep is not an incident — but a Mac that stays gone is.** The hourly Mac jobs carry a 14 h
grace, and while `mac-probe` is LATE the dashboard suppresses the LATE alerts of the other Mac jobs (states
still recorded and shown). `mac-probe` itself pages only after **72 h of being LATE**, i.e. ~87 h ≈ 3.6 days
of total silence. **So: a night, and a whole weekend with the lid shut, page NOBODY; a Mac that is dead,
migrated, or whose launchd probe was unloaded pages exactly ONCE, and once more when it comes back** (both
tested end-to-end against `jobs.example.yml`). `mac-probe` must **never** be `alert: never` — it is the job
whose alert the suppression borrows, so silencing it silences the whole machine; the code refuses to suppress
behind a probe job that cannot page, but the threshold is what makes the rule worth having. When the Mac
wakes, `mac_probe.py` posts `pa-backup`, then `drive-mirror`, then its own heartbeat — three requests, three
recomputes — and the siblings' plain `LATE → OK` recoveries are muted while `mac-probe` is still LATE. A
sibling that wakes into `FAIL` / `STALE_DEST`
/ `BEHIND` still alerts on its own — that is news, not the Mac coming back. Keep every Mac sibling's
`grace_s` ≥ `mac-probe`'s + 120 (as `jobs.example.yml` does): the probe posts the siblings *before* itself,
so with equal graces a sibling's deadline falls a few seconds earlier and a ticker tick landing in that gap
would page for the sibling first, then again for the probe.

### 4b. Mac — the Inbox transcription worker (NEW; `inbox-transcribe` + `inbox-backlog`)

**This worker is the ONLY path from a recorded voice note to readable text.** The browser uploads audio to
the box and nothing else; there is no live/in-browser transcript. So while this agent is not running, voice
notes sit on the board reading "transcribing…" indefinitely. **Nothing is lost** — the audio is kept, the
item is intact, and the next run picks it up — but nobody can read or search what he said until it runs.
That is why it is on a 5-minute `StartInterval` and not the probe's hour.

It also has a privacy consequence worth stating: because transcription happens here, on Graham's own Mac,
**the audio of a voice note never leaves his own machines.** It goes browser → the box, and box → this Mac.
No third party is ever sent it.

**Prerequisites on the Mac**, both of which the installer only WARNS about (it cannot fix them for you):

```bash
# 1. ffmpeg. mlx-whisper shells out to it; without it every transcription fails.
brew install ffmpeg && which ffmpeg                  # expect /opt/homebrew/bin/ffmpeg
# 2. an interpreter that has mlx-whisper. NOT /usr/bin/python3 — mlx cannot be installed there.
#    Today it borrows the jjho repo's venv; a dedicated one is cleaner and needs no code change:
#    python3 -m venv ~/.local/venvs/whisper && ~/.local/venvs/whisper/bin/pip install mlx-whisper
ls -l ~/code/jjho-fan-almanac/.venv/bin/python
```

```bash
cd ~/code/hopper-dashboard && git pull
deploy/mac/install.sh --inbox        # prompts for INBOX_URL, then INBOX_TOKEN with a HIDDEN read
```

`--inbox` **appends** five keys to the existing `~/.config/hopper-dashboard/env` (it never rewrites the file,
and re-running is a no-op once `INBOX_TOKEN=` is present), then renders and loads a second launchd agent,
`com.hopper.inbox-transcribe`:

| key | what it is |
|---|---|
| `INBOX_URL` | the **PUBLIC** host, `https://dashboard.graham-williams.com` — *not* the Tailscale ingest URL |
| `INBOX_TOKEN` | the box `.env`'s `INBOX_TOKEN`. A **third** credential, separate from `INGEST_TOKEN` and `READ_TOKEN` |
| `INBOX_WHISPER_PYTHON` | the venv interpreter that has mlx-whisper |
| `INBOX_WHISPER_MODEL` | `mlx-community/whisper-large-v3-turbo` (weights already cached in `~/.cache/huggingface`) |
| `INBOX_BACKLOG_FILE` | `~/personal-assistant/backlog.txt`, read by the `inbox-backlog` sub-probe |

**Two credentials, two hosts, and it is not redundancy.** The worker pulls the queue and posts transcripts
over the PUBLIC host with `INBOX_TOKEN`; it posts its own heartbeat over the **Tailscale-only ingest port**
with `INGEST_TOKEN`. Keeping them apart is what makes "is the Mac running this loop?" answerable when
Cloudflare or the password gate is the thing that is broken. Non-interactive install: pre-create the env file
with both tokens pulled over ssh (same shape as the §4 block), then `deploy/mac/install.sh --inbox` finds the
keys already there and only installs the agent.

**⚠️ THE ffmpeg/PATH TRAP — the single most likely way this breaks.** `mlx_whisper.load_audio()` runs a
**bare `ffmpeg`** resolved from `PATH`, and launchd gives an agent the minimal `PATH=/usr/bin:/bin:/usr/sbin:/sbin`,
which does not contain `/opt/homebrew/bin`. The plist therefore injects `PATH` explicitly, and
`deploy/mac/install.sh` refuses to install a plist that has lost that line. What makes it worth this much
prose is how it *presents*: the failure happens inside `load_audio`, not at import, so it looks like a corrupt
recording rather than a configuration problem — and it only happens under launchd, never when you run the
same command by hand in a shell that has Homebrew on its `PATH`. The worker itself checks `PATH` before doing
anything and aborts the whole run with a diagnostic naming ffmpeg, rather than reporting a per-item failure:
three of those would mark every queued voice note permanently un-transcribable.

Verify:

```bash
launchctl list | grep com.hopper.inbox-transcribe          # 2nd column 0 = last exit ok
HOPPER_DASHBOARD_ENV=~/.config/hopper-dashboard/env /usr/bin/python3 \
  ~/code/hopper-dashboard/probes/inbox_transcribe.py --dry-run     # lists the queue, posts nothing
tail -5 ~/Library/Logs/hopper-inbox-transcribe.log         # "queue: N item(s)" then "run ok in …"
# the backlog mirror rides the HOURLY probe, not this agent:
/usr/bin/python3 ~/code/hopper-dashboard/probes/mac_probe.py --dry-run --only inbox-backlog
```

A healthy hourly probe log line now reads **`run ok in Ns (5 sub-probes, 0 failed, 5 pings)`** — five, not
four. Until `INBOX_URL`/`INBOX_TOKEN` exist in the env file the `inbox-backlog` sub-probe skips silently and
the line still says 4 pings, which is also correct: nothing is running it.

**If you remove the worker** (`deploy/mac/uninstall.sh --inbox-only`), `inbox-transcribe` stops heartbeating
and goes LATE after ~14 h. That is right — nobody is running it — and no audio is lost.

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
- [ ] `/api/v1/status` (READ_TOKEN) lists all **17** jobs; after ≤5 min box jobs are `OK`, after ≤1 h Mac jobs are
      `OK`/`BEHIND` (not `UNKNOWN`), manual jobs show **Never run** until their first `ping.sh`.
- [ ] HTTPS at the origin: `curl -sI https://dashboard.graham-williams.com/healthz | grep -i strict-transport`
      → `max-age=31536000` (no `includeSubDomains`, no `preload`), and a forwarded-http request **307s** to
      the pinned host without reflecting the one it was sent, uncacheable and `Vary`'d (`curl` is purged from
      the image — run it from the box against the container, or from the Mac against the public host):
      ```bash
      docker exec -i hopper-dashboard python3 - <<'PY'
      import os, urllib.request as u
      host = os.environ.get("AH") or "dashboard.graham-williams.com"
      r = u.Request("http://127.0.0.1:8080/healthz",
                    headers={"Host": "evil.example", "X-Forwarded-Proto": "http"})
      try:
          u.urlopen(r, timeout=5)
      except u.HTTPError as e:
          loc = e.headers.get("Location", "")
          ok = (e.code == 307 and loc == f"https://{host}/healthz"
                and e.headers.get("Cache-Control") == "no-store"
                and "x-forwarded-proto" in (e.headers.get("Vary") or "").lower())
          print(("PASS " if ok else "FAIL ")
                + f"{e.code} {loc} cc={e.headers.get('Cache-Control')} vary={e.headers.get('Vary')}")
          raise SystemExit(0 if ok else 1)
      print("FAIL no redirect"); raise SystemExit(1)
      PY
      ```
      Pass `-e AH="$(grep '^APP_HOST=' ~/hopper-dashboard/.env | cut -d= -f2-)"` to check the real pin.
      **`-i` is required** — without it the heredoc is discarded and the check silently "passes".
- [ ] `APP_HOST` actually reached the container. It lives in `.env` (gitignored), so a PR cannot put it
      there — an older `.env` would ship the redirect silently disabled. Compose now defaults it to
      `dashboard.graham-williams.com`, which is the belt; this is the braces:
      ```bash
      ssh <user>@<box-tailscale-ip> "grep -c '^APP_HOST=' ~/hopper-dashboard/.env"   # expect 1
      ssh <user>@<box-tailscale-ip> "docker logs hopper-dashboard 2>&1 | grep -i 'redirect is DISABLED'"  # expect EMPTY
      ```
      A non-empty second command means `APP_HOST` is set to something that is not a bare hostname (the
      Host/Origin pin still works — it compares rather than emits — but the 307 is off).
- [ ] The heartbeats still land after that deploy — the ingest listener is exempt from the redirect by
      design, so prove it rather than assume it: `~/code/hopper-dashboard/probes/ping.sh jjho-refresh skipped
      "https deploy check"` → 200, and `/api/v1/status` shows a fresh `last_run` for it.
- [ ] `minecraft-offload` seeded with one `ok` ping after the first offload (§4).
- [ ] `/api/v1/status` shows the right `alert` block per job (§1d): **13** `"source": "alert_after_s"`, 4
      `"source": "alert"` / `"never": true`, **zero** `"source": "default"` and zero `"source":
      "informational"`, and `mac-probe` reading `{"after_s": 259200, "never": false}` (never `true` — see the
      table below). The §1d command exits non-zero if any of that is wrong; check `echo $?`.
- [ ] **The dashboard's own password** (§1c): the shared house word is REFUSED at
      `https://dashboard.graham-williams.com/login`, and the new one is accepted. Check the other four apps
      still take the house word — nothing about them changed, but confirm rather than assume.
- [ ] **Backup** (§2b): `deploy/box/backup.sh` run by hand leaves `dashboard_<ts>.db` **and** `inbox_<ts>.db`
      in `~/hopper-dashboard-backups/snapshots/`, `rclone lsf gdrive:hopper-dashboard-backups` lists both
      plus `daily/`, and `hopper-dashboard-backup` reads `OK` within 5 minutes of the timer's first tick.
- [ ] **Transcription** (§4b): record a short voice note at `/inbox`; within ~5 minutes its row shows a
      Whisper transcript. `tail ~/Library/Logs/hopper-inbox-transcribe.log` shows `queue: 1 item(s)` then
      `transcribed … chars`. If it stays "transcribing…", check `launchctl list | grep inbox-transcribe`
      and then the log for the ffmpeg/PATH diagnostic — that is the expected first failure.
- [ ] **Backlog mirror** (§4b): the hourly probe log reads `run ok in Ns (5 sub-probes, 0 failed, 5 pings)`
      and `/inbox` lists the backlog entries. Four pings instead of five means `INBOX_URL`/`INBOX_TOKEN` are
      not in the Mac env file, which is a correct skip, not a failure.
- [ ] Kill test: `sudo systemctl stop dashboard-containers.timer` → `box-containers` goes `LATE` after
      cadence+grace (visible on the board immediately) and the ntfy alert arrives **`alert_after_s` later** —
      so **35 minutes end to end** for this job (15 min of deadline + the 20 min threshold), not 20. Don't
      conclude it's broken at minute 5, or at minute 25; `/api/v1/jobs/box-containers` shows `alert.bad_since`
      counting. `start` → recovery alert, but only if the page had already gone out.
- [ ] Flap test (this is the feature): stop the timer and `start` it again inside 35 min → a `LATE` and an
      `OK` in `state_changes`, and **zero** ntfy pushes. Leave it started for **≥10 min** before repeating:
      the episode only closes once the job has held OK for its dwell, which for `box-containers` is
      `2 × cadence` = 600 s — TWO of the 5-minute `docker ps` posts, because one OK sample cannot tell a
      recovery from the up-phase of a crash loop. So stop → start → stop inside that window is deliberately
      ONE outage.
      **Do not repeat this test several times in quick succession and read a push as a bug.** Unpaged not-OK
      time now accumulates inside `2 × alert_after_s` (40 min for this job), so three drills in half an hour
      total more than the 20 min threshold between them and DO page — correctly: a container that is down
      eleven minutes out of every twenty is broken, and that is the silence the accumulator closes. Wait out
      the window between drills, or read `state_changes` instead of the phone.
- [ ] Cooldown test (the other half): after a page for `box-containers`, break it again → the board shows the
      new episode with `alert.bad_since` counting and `alert.alerted_at` **null**, and **no push** until
      `alert.last_paged_at + alert.cooldown_s` (6 h). Leave it broken across that moment and it DOES page —
      delayed, never cancelled. If you want to see it inside a deploy window, read `alert.cooldown_s` and
      `alert.last_paged_at` from `/api/v1/jobs/box-containers` rather than waiting.
- [ ] Fail test (safe): `~/code/hopper-dashboard/probes/ping.sh jjho-refresh fail "drill"` → `FAIL` on the
      board. `jjho-refresh` is `alert: never`, so **no push** — that is correct. To drill the *alert* path end
      to end, temporarily set `alert_after_s: 0` on one job, `docker compose up -d`, ping `fail`, then put the
      real value back.
- [ ] `INVENTORY.md` in `~/personal-assistant` updated: new container, new public hostname, new box timer,
      new Mac launchd job, new credential locations (`/etc/hopper-dashboard/ingest.env`,
      **`/etc/hopper-dashboard/ingest.curlrc` — a second copy of the ingest token, owned by the service
      user 0600, rewritten by `install.sh`**, `~/.config/hopper-dashboard/env`, box `.env`,
      `~/.config/rclone/dashboard-ro.conf`).

## Rollback

### ⚠️ Rolling the IMAGE back to `main` requires restoring `jobs.yml` FIRST

**The obvious rollback bricks the container.** Once §1d has written `alert_after_s:` / `alert:` into the
box's `jobs.yml`, that file is newer than `main`'s schema. `main`'s registry rejects unknown keys — loudly
and by design — so a rolled-back image dies at start-up with

```
RegistryError: jobs.yml: job 'km-backup': unknown job key(s): alert_after_s (allowed: …)
```

and compose restart-loops it. Verified. The strict validator is doing its job; the ORDER is what was
missing here, and `jobs.yml` is gitignored so nothing but the backup has the old values.

**⚠️ THE INBOX RELEASE ADDS A SECOND, IDENTICAL TRAP — `kind: worker`.** Once §1d-ii has copied
`inbox-github-sync`, `hopper-dashboard-backup`, `inbox-transcribe` and `inbox-backlog` into the box's
`jobs.yml`, rolling the image back to a pre-Inbox `main` fails the same way, with a different message:

```
RegistryError: jobs.yml: job 'inbox-github-sync': unknown kind 'worker' (allowed: db_snapshot, rclone_copy_tree, drive_mirror, container, manual, probe, disk)
```

Same cause, same cure, same ORDER — **restore `jobs.yml` from its timestamped backup BEFORE rolling the
image back**, never after. The sequence below already does it in the right order; the only thing to get
right is picking a backup from before the upgrade you are undoing.

**Also on an Inbox rollback**, and neither costs data: the box backup timer keeps running against an image
that no longer has an `inbox.db` (the snapshot for it is skipped with a WARN — the run still succeeds on
`dashboard.db`, and every inbox snapshot already taken stays in the ring and on Drive, because the push is
`rclone copy`), and the Mac's `inbox-transcribe` agent starts getting 404s from a read role that no longer
serves the Inbox endpoints, which shows up as that job going FAIL. Stop it with
`deploy/mac/uninstall.sh --inbox-only` if the rollback is more than momentary. **`inbox.db` itself is never
touched by a rollback** — it is a separate file in the same volume, and an older image simply never opens
it.

```bash
cd ~/hopper-dashboard
ls -1t jobs.yml.bak.*                            # newest first; §1d stamps these to the SECOND
cp -p "$(ls -1t jobs.yml.bak.* | head -1)" jobs.yml    # 1. the FILE goes back first
chmod 644 jobs.yml                               # the container reads it as uid 10001
git checkout main && git pull                    # 2. then the image
docker compose up -d --build
docker ps --filter name=hopper-dashboard --format '{{.Names}} {{.Status}}'   # (healthy), not Restarting
docker logs --tail 20 hopper-dashboard | grep -i registryerror || echo "registry OK"
```

Pick the backup from BEFORE the upgrade, not merely the newest — if §1d ran more than once there will be
several. (That is also why they are timestamped to the second now: `$(date +%F)` meant a second run on the
same day overwrote the pre-edit backup with the edited file, destroying the only copy of the state this
procedure depends on.)

**The databases need nothing.** `bad_since`, `alerted_at`, `alerted_state` and `last_paged_at` are additive
columns (and `sc_job_seq` an additive index); an older image simply never reads them, and leaving them in
place is harmless — there is no down-migration to run and nothing to drop. Rolling forward again finds them
already there. Rolling BACK loses the escalation and the accumulator, so an intermittently-failing destination
goes quiet again: that is the bug the roll-forward fixed, not a new one.

### Full teardown

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
| Read-only rclone remote's token revoked/expired | Every `db_snapshot` probe errors → `dashboard-probes` `FAIL` on the FIRST cycle (a revoked token is not a quota error, and hard errors are not damped) with `rclone exit …` in the note; `dest.probe_error` on the cards | Re-run the `rclone authorize` flow in §1b; `RCLONE_CONFIG=~/.config/rclone/dashboard-ro.conf rclone lsd gdrive-ro:` on the box |
| Drive rate-limits a big-tree probe (`rateLimitExceeded` / rclone timeout) | A probe fails but `dashboard-probes` stays **OK** — by design, damped until `PROBE_FAIL_THRESHOLD` (2) consecutive failed probes **of that job**. Undamped, this flapped FAIL→OK 27× in 4 days with nothing broken, which masked a real backup failure in the same window | The errors are still recorded: `reason` says `probe-error damped (transient quota/timeout, failure 1 of 2 before FAIL)`, `metrics.failed_transient` counts them, and the job page's probe table shows each `transient (Drive quota/timeout): …` row. If it does not clear, the job's **next** failed probe trips FAIL and pages — for a job with `interval_s: 1800` that is one interval later, not one cycle; and either way `PROBE_NO_SUCCESS_S` (3600 s) trips FAIL once nothing has listed that destination for an hour. Reduce traffic with `probe.interval_s` on the big tree before raising the threshold. |
| `probe.interval_s` set too high on a `db_snapshot` | Would make a fresh destination read `STALE_DEST` from an aged probe row | Can't happen: `registry.py` refuses `interval_s` above half the freshness window (`cadence_s * 12 / 2`) for that kind and the container won't start |
| `probe.interval_s` mistyped with an extra digit on a `rclone_copy_tree` / `manual` job (`18000000`) | Would be 208 days between probes — the destination is simply never looked at again, and NOTHING says so: a cycle with nothing due still records `dashboard-probes` as ok and the card keeps reporting whatever the last probe saw | Can't happen either: capped at `MAX_PROBE_INTERVAL_S` (86400) on every kind, loudly, at parse time. These are exactly the two kinds the operator hand-edits (the commented `probe:` templates under `pa-backup` and `minecraft-offload` in `jobs.example.yml`, uncommented into the box's gitignored `jobs.yml`), and the db_snapshot semantic cap does not cover them. The box's live file is `interval_s: 1800` on both — well inside the cap |
| `dashboard-containers.timer` stopped / user dropped from `docker` group | `box-containers` `LATE`; or `fail` ping with "permission denied" in the note | `systemctl list-timers`; `journalctl -u dashboard-containers.service` |
| Disk gauge stops being fed (probe moved/renamed, `statvfs` on a path that vanished) | Was: the gauge kept showing its LAST reading for ever, because a `disk` job has no cadence — a frozen number looked like a healthy one | Four layers now, so a frozen gauge cannot read as healthy: (a) a reading older than **48 h** (`state.DISK_METRIC_MAX_AGE_S`) is `LATE`, alerted like any other dead-man's switch — suppressed only while that machine's probe job is itself LATE; (b) an unreadable path is a `fail` ping on BOTH machines → **FAIL, "capacity unreadable (statvfs …)"**, which outranks the stored figures (the Mac probe returns that ping rather than raising — a raise would only mark `mac-probe` FAIL and leave this card on its last reading); (c) a box probe that never ran at all is reported by its wrapper as a `fail` on `box-disk` (`result=probe-failed`, note pointing at `journalctl -u dashboard-containers.service`) rather than only as the unit's exit status; (d) a gauge that has NEVER reported goes `LATE` **6 h** after registration (`DISK_FIRST_READING_GRACE_S`) — the feeder that was never deployed, which `UNKNOWN` would otherwise hide for ever without alerting at all. The card still prints **measured &lt;when&gt;** under the bar. |
| Board's used-percent does not match `df` | Looks like an arithmetic bug, invites a "fix" that would break the threshold | Expected: the free **bytes** match `df`'s Avail exactly, the **percent** does not (macOS/APFS hands `statvfs` a smaller free figure than `df` uses — measured 79.5% vs 78%, so the 90% ceiling trips near 88.5% on `df`). `f_bavail` is the right number: it is what can actually be written. DESIGN.md → `disk` and `probes/common.disk_free` both say so. |
| Mac asleep / logged out | `mac-probe` LATE after 15 h; the other Mac jobs go LATE too but their alerts are suppressed — a night or a weekend with the lid shut pages NOBODY, and a Mac that stays gone pages exactly ONCE (`mac-probe`, at ~87 h = 15 h LATE + its 72 h threshold) | That IS the signal (Mac offline). If only some Mac jobs are late, read the `mac-probe` note — it names the sub-probe that errored. |
| `mac-probe` set to `alert: never` in `jobs.yml` | **The whole Mac goes dark.** The machine-offline rule mutes every sibling's LATE alert on the premise that the probe sends one alert for the machine — `never` deletes that one alert, so ten days of a dead Mac is `pa-backup` LATE with an 8-day-old `bad_since`, `alerted_at` NULL, and zero ntfy | `/api/v1/status` → `mac-probe`'s `alert` block must read `{"after_s": 259200, "never": false}`. The code refuses to suppress behind a probe that cannot page, so the siblings page instead — but then you get several alerts for one fact, which is the tell |
| `jobs.yml` on the box never got the alert thresholds (§1d) — it is gitignored, so `git pull` can't add them | Everything silently falls back to 24 h: `box-containers` pages **23.7 h late** (35 min configured vs 24.3 h on the default), and `mac-probe` pages after 39 h instead of 87 h, i.e. every long weekend the Mac sleeps. Nothing errors | `/api/v1/status` → each job's `alert.source`; **any `"default"` means that job was missed.** The job page says "Pages after 1d continuously not OK" where it should say 20m / Never |
| A threshold set far too long (or `alert: never` on something that matters) | A real outage never reaches the phone — the board is right and nobody looks at it | Deliberately the only silent failure this feature can cause, which is why the default is 24 h and not "off". `alert_after_s` is capped at 30 d, so the extra-digit version is rejected at startup instead of accepted. Re-read the table in `jobs.example.yml` when adding a job; `alert.bad_since` shows an episode is running even when it will never page |
| ntfy unreachable exactly when a threshold is crossed (429 from the shared host, 5xx, DNS, timeout) | The episode's one page is spent on a POST that never landed, and even the recovery is suppressed because it only fires for an episode that paged | Fixed: a failed push rolls `alerted_at` back to NULL and a later tick retries, spaced ≥5 min per episode so a dead ntfy is not POSTed 72×/h from inside the scheduler tick and the ingest request. `docker logs hopper-dashboard \| grep 'did not land'` shows each retry; a burst of them means ntfy, not the jobs |
| A heartbeat lands DURING the failed ntfy POST and the job is briefly OK (the 5-minutely `docker ps` cron vs a 5 s ntfy timeout) | A rollback that skipped "the job is no longer not-OK" would skip exactly this: the episode is still open (the dwell), so `alerted_at` stays stamped-but-undelivered and the outage runs on silently | The rollback keys on episode identity only (`bad_since`/`alerted_at` still match), which a genuinely closed episode cannot satisfy — it has `bad_since` NULL. Same `grep 'did not land'` |
| A recovery push fails (ntfy 429/5xx at exactly that moment) | The "→ OK" is dropped: its episode is already closed, so there is nothing to hand the page back to | Accepted, and it is loss of good news rather than silence — the next real problem opens a fresh episode with an unspent page. `docker logs hopper-dashboard \| grep 'recovery push for'` names any that were dropped. Re-sending later could announce "→ OK" for a job that has since broken again |
| A destination probe that fails once a day (rate-limited Drive) on a job whose destination IS stale | A failed probe leaves nothing to compare, so the state falls to `OK` — which would clear the episode clock. A backup genuinely 10 days stale flaps STALE_DEST↔OK and never pages | Fixed: an OK with no usable destination probe does not end a `STALE_DEST`/`BEHIND` episode, and the episode still pages at its own threshold even while the probe is blind. The job page says "episode still open"; `dashboard-probes` FAIL + `dest.probe_error` on the card name the probe itself |
| A `db_snapshot`'s destination probe breaks PERMANENTLY (remote revoked, rclone's shared Drive OAuth client retired) after that job has already paged | An unbounded hold would keep the episode — and its spent page — open for ever, so the backup itself dying and sitting LATE for 10 days would push nothing | The hold is bounded by the job's own `alert_after_s`; past that the episode closes silently (no recovery — nothing was verified fixed) and the next failure pages on a fresh clock. `docker logs hopper-dashboard \| grep 'closed unverified'`. **Residual — the bound limits the WINDOW, not the silence:** a new failure that lands while the hold is still running joins the still-open episode and gets no push of its own for as long as that episode lasts. Correct by the one-page-per-episode rule (never verifiably OK in between, and it did page once) but worth knowing |
| `dashboard-probes` reads `ok` while a probed destination is failing (PR #8's damping) | Treating that `ok` as a recovery closes the episode and resets the clock; under an alternating fail/damped-ok pattern the 6 h threshold is never reached and the phone never hears about probes that are broken most of the time | Fixed: the self-job's `ok` is judged against the `probes` table, not taken at face value, so the episode is held and the clock keeps running. `dest.probe_error` on each card, and the self-job's run `reason` says `probe-error damped (…)` |
| A job that flaps just under its threshold (a container on `restart: unless-stopped` backoff: 19 min down, 1 min up) | Broken 95% of the day, never pages — one OK tick would reset the whole clock | Fixed: the episode only ends after the job holds a VERIFIED OK for `min(5 min, threshold/10)`. The card shows "not OK since" with a state pill of OK while that is pending |
| A job's episode crosses its threshold while the job READS OK — because its probe went blind seconds in (the STALE_DEST → unverifiable-OK flip lands between two recomputes), or because it recovered for longer than the dwell | The page was decided inside one branch and inside one window, so an episode could end — hold expired, or dwell served — without ever speaking. Measured: a destination 30 days stale with the heartbeat still arriving, **zero pushes**, card green; and `box-containers` 19 min down / 3 min up, 86% broken indefinitely, zero pushes | Fixed: the hard ceiling is now unconditional — an open episode past its threshold and unpaged pages on EVERY pass, from every branch. Consequence to expect on the phone: an outage that outlives its threshold and then recovers sends a page AND a recovery, sometimes seconds apart. `alert.bad_since` on `/api/v1/status` shows an episode running while the card reads OK |
| Clock stepped backwards (NTP correction, box RTC ahead at boot) | A `bad_since` in the future makes `now - bad_since` permanently negative — the job can never reach its threshold; a `probes.probed_at` in the future outranks every later probe row for ever, so the newest probe always reads `ok` and the damped-OK hold switches off | Fixed both ends: an unparseable OR future `bad_since` is healed (clock restarts, `alerted_at` dropped with it) — `docker logs hopper-dashboard \| grep 'healing unusable bad_since'` — and every "which probe row is newest" query orders by `id` (insert order), which no clock can reorder. A retry whose backoff would be negative reads as due rather than as "wait" |
| A control character in a job's `name` in `jobs.yml` (a pasted CR/LF, an ANSI colour code) | The name becomes the ntfy `Title`; `http.client` refuses the header, so **every** POST for that job raises for ever. Nothing is injected (zero bytes reach the socket) — but the page is never delivered and never spent, so the job is permanently un-pageable and its recovery can never fire either. Measured: 24 attempts over 2 h, `alerted_at` NULL throughout | Can't happen: every schema string is rejected at parse time if it contains C0/DEL, naming the job, and the container refuses to start |
| `alert_after_s:` (or `alert:`) left with an empty value in `jobs.yml` — a commented-out value, a half-finished edit | Alone, a null escaped the mutual-exclusion check (nothing to be exclusive with) and resolved as if the key were ABSENT: on an informational job that is `never` — a permanent silence sitting in the file under a key whose name says a threshold was set | Can't happen: present-but-null is an error on both keys, at parse time |
| Both roles migrate the schema at once on the deploy that adds a `jobs` column | The loser of the `PRAGMA table_info` → `ALTER TABLE` race raises `duplicate column name` out of `create_app`; the entrypoint then stops the other gunicorn and the container restart-loops until the second boot finds the column there. It self-heals, but "the dashboard is down" is the loudest silence there is | Fixed: the migration runs under `BEGIN IMMEDIATE` and tolerates a duplicate column. If it ever recurs, `docker logs hopper-dashboard \| grep 'duplicate column'` names it |
| `box-disk` / `mac-disk` missing from the box `jobs.yml` while their probes post (§1d) | Ingest 404s the undeclared id; the Mac probe counts that as a failed sub-probe, so `mac-probe` becomes an hourly **failing** ping and the gauge is simply absent from the board | `grep -E '^  - id:' jobs.yml \| wc -l` must be **17**; the §1d script warns by name if either is absent |
| `inbox-backlog` missing from the box `jobs.yml` while the Mac posts it (§1d-ii) | Exactly the same shape as the row above, in a worse place: it is a sub-probe of the HOURLY Mac probe, so a 404 makes `mac-probe` itself report a failed sub-probe every hour — and `mac-probe` is the job whose alert mutes its siblings | Run §1d-ii (it is idempotent). Until `INBOX_URL`/`INBOX_TOKEN` are in the Mac env file the sub-probe skips silently and cannot cause this, which is why §1d-ii comes BEFORE §4b |
| The Mac transcription agent is unloaded, crashed, or was never installed (§4b) | Voice notes stay on the board reading "transcribing…" for ever. **Nothing is lost** — the audio is kept and a later run picks it up — but it is the ONLY path from a recording to text, and the page looks the same on minute one as on day ten | `inbox-transcribe` goes LATE after ~14 h (the mandatory Mac grace). A CRASHED worker is much faster: it posts its own `fail` and is red immediately. `launchctl list \| grep inbox-transcribe`; `~/Library/Logs/hopper-inbox-transcribe.log` |
| `ffmpeg` not on the launchd agent's PATH (the plist's `PATH` line lost, Homebrew moved) | **The nastiest one here.** mlx-whisper runs a bare `ffmpeg` from PATH inside `load_audio`, so it fails as though every recording were corrupt — and only under launchd; run the same command in your own shell and it works | The worker checks PATH first and aborts the RUN with a diagnostic naming ffmpeg, rather than reporting per-item failures (three of those would mark every queued note permanently un-transcribable). `inbox-transcribe` goes FAIL with that note; `deploy/mac/install.sh` also refuses to install a plist missing the line |
| The Inbox's audio prune deletes a file the backup has not copied yet | A recording is gone from both the box and Drive with nothing to say so | Bounded, not impossible — and deliberately so since 2026-09-19, because Delete must mean deleted everywhere. The prune needs the transcript to be Whisper-quality AND the item reviewed AND 90 days old, so by then the text has been in the DB snapshots for months. What the mirror cannot do is propagate a WIPE: a missing audio dir, a >50% drop in file count, or an empty tree against a non-empty Drive all refuse the deletion pass and fail the run loudly (`AUDIO_ALLOW_MASS_DELETE=1` overrides). The DB snapshots remain additive |
| `hopper-dashboard-backup` timer installed without its heartbeat drop-in | The backup runs (or stops running) and the board says nothing either way — an unbacked-up app with a green card, which is the exact failure this repo exists to catch | Can't happen via `deploy/box/install.sh`: the unit, timer and drop-in are installed in one loop iteration, a test asserts it, and `systemctl cat hopper-dashboard-backup.service` must show `heartbeat.conf` + its `ExecStopPost=` line |
| The dashboard is left on the shared house password (§1c) | The word Graham gave friends for km-tracker opens a board that now holds recordings of his voice, and the apex page links straight to it | Try the house word at `/login` — it must be REFUSED. Nothing in the code enforces this; it is a value in the box `.env`, so the only check is the one you run |
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
| A job flaps just under its threshold for ever (`box-containers` on `restart: unless-stopped` backoff) | One page per EPISODE says nothing about how often an episode may RESTART: measured 132 pushes/day at 19-min-down / 20-min-up, and 48/day on a 30-min crash-loop cycle. The alert storm this feature removed, relocated | Fixed twice over: the OK dwell is now at least two of the job's OWN samples (600 s for `box-containers`, sampled every 300 s) so a single OK sample cannot close an episode; and a per-job cooldown holds the next PAGE for `max(alert_after_s, 6 h)`. Measured after: 47 episodes/day → 4 pages + 4 recoveries. `alert.cooldown_s` / `alert.last_paged_at` on `/api/v1/status`; `docker logs hopper-dashboard \| grep 'held back'` |
| The per-job cooldown swallows a page instead of delaying it | Would be the worst failure this feature could have: a rate limit that silently becomes silence, on the one job whose outage IS the outage | Can't happen by construction: a held-back page stamps NOTHING, so the episode keeps its unspent page and the hard ceiling re-offers it on every pass — the moment the cooldown expires, a job still (or again) past its threshold pages. Has its own test and its own mutation. The cooldown also only ever BINDS on a job whose threshold is under 6 h (`box-containers`, `box-disk`, `mac-disk`); above that a new episode already takes longer than the cooldown to reach its own threshold |
| A failed ntfy POST leaves a cooldown behind | The episode gets its page back (the existing rollback) and then cannot spend it for six hours, because a POST that never reached anyone still looked like a page to the rate limiter. One unlucky 429 = a whole cooldown of silence | Fixed: the rollback returns `last_paged_at` alongside `alerted_at` — both halves or neither. `docker logs hopper-dashboard \| grep 'did not land'` |
| A destination that fails MOST of the time but lists successfully now and then (Drive `rateLimitExceeded`, which is intermittent by nature) | Every good listing ended the episode and restarted the 6 h clock, so the phone heard **nothing at all**: measured over 24 h, 60 min down / 15 min up failed 73% of probes and pushed **0**; 300 min down / 30 min up failed 90% and pushed **0**. The damping had a cumulative backstop (`PROBE_NO_SUCCESS_S`); the episode had none. This is the failure that took out the nightly backup on 2026-09-10 | Fixed: an episode is also past its bar after `alert_after_s` spent in that state within `2 x alert_after_s`. Those three patterns now push 3 alerts + 3 recoveries a day, capped by the same cooldown. The body says which rule spoke — `… FAIL for over 6h in the last 12h`. If one of these ever goes quiet again, check `alert.bad_since` + `state_changes` on the job page: the accumulator reads the transition log, so a job with no recorded transitions accrues nothing |
| A gauge pages while merely lowish and then genuinely fills up (`box-disk`: BEHIND at 40 GiB free, then 1 GiB, then `statvfs` fails) | One page per episode meant the escalation was swallowed — and with it the HIGH priority, since priority is read off the state and the state that paged was the mild one. Confirmed: 1 push, `priority=default`, and no high-priority push ever. For a capacity gauge that is inverted: BEHIND is the early warning, FAIL is the event | Fixed: an episode that gets strictly worse pages once more, at the worse state's priority (`box-disk: BEHIND → FAIL`, high). It is not held by the cooldown but does stamp it. At most one escalation per episode. `alert.alerted_state` on `/api/v1/status` says what the phone was actually told |
| A recovery names a state that was never paged (`… FAIL → OK` for an episode paged as BEHIND) | The recovery read the LATEST non-OK state while the alert named the state the episode started in, so the resolution looked like the tail of a page nobody received | Fixed: the recovery names `alerted_state`, i.e. exactly what was sent |
| `jobs.last_paged_at` in the FUTURE (clock step) or unparseable (hand-edited row) | `now - last_paged_at` is permanently negative, i.e. a cooldown that never expires — a job that can never page again. The same trap as a poisoned `bad_since`, on the column this release adds | Fixed the same way: an unusable stamp is refused AND healed to NULL. `docker logs hopper-dashboard \| grep 'healing unusable last_paged_at'` |
| A machine's probe job is inside its OWN cooldown when the machine dies | The machine-offline rule mutes every sibling on the premise that the probe sends one alert for the machine — but the probe's page is being held back, so nothing pages at all for the length of the cooldown. The static `alert: never` guard does not catch this, because the policy is fine; only the moment is wrong | Fixed: suppression may only borrow an alert that EXISTS, checked both statically (`alert_never`) and for the moment (the probe's cooldown). Siblings page instead — several alerts for one fact, which is the tell. Unreachable on the shipped file (`mac-probe` is 72 h, above the 6 h floor, so its cooldown can never bind) and reachable the moment anyone shortens that threshold |
| `alert_after_s` read as "the time until my phone knows" | It is not: the threshold clock starts when the job goes NOT-OK, which is already cadence+grace after the last good run. `pa-backup` shipped `108000` commented "30 h" and paged at **68 h** | Fixed in `jobs.example.yml`: every alerting job carries a `# TIME-TO-PAGE:` line with the end-to-end figure, and `test_every_alerting_job_states_its_real_time_to_page` recomputes all nine from that same file, so a comment cannot drift again |
| ntfy topic leaked or mistyped on the phone | Alerts fire (server-side) but never arrive | `curl -d test https://ntfy.sh/<topic>` and check the phone |
| The read role's http→https hook (`web._https_redirect`) or its HSTS header is moved onto the app factory instead of `web.bp` | **Every heartbeat stops, quietly.** The `:8081` ingest listener is Tailscale-only plain HTTP; a redirect there is followed by nothing — systemd's `ExecStopPost` curl, `dashboard-containers.timer` and the Mac launchd probe all just stop recording runs, and the board drifts every job to LATE over hours while rendering perfectly | Can't happen silently: three tests in `tests/test_ingest.py` pin the ingest app as redirect-free and HSTS-free (including with an attacker-supplied `X-Forwarded-Proto: http`). After any deploy that touches `web.py`, run the §5 heartbeat check — `probes/ping.sh jjho-refresh skipped` must return 200 |
