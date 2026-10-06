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
   `rclone_copy_tree` or `manual` job may *optionally* carry `probe.rclone_path` (e.g. `gdrive-ro:Hopper/Backups`)
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
- **A voice note's AUDIO never goes to Anthropic or any speech service; its TRANSCRIPT and TITLE go to
  Anthropic, with the setup brief.** (Changed 2026-09-28 with drafting — see "Drafts, Needs review and the
  Hub"; the brief was added 2026-09-29.) The audio lives on the box, on the Mac only while it is being
  transcribed (a temp file, removed after), and add-only in Graham's Google Drive backup, where it is kept
  even after Delete or the retention prune until removed by hand. The page says: "Audio never goes to
  Anthropic or any speech service. It lives on this box, on your Mac only while it is being transcribed, and
  in the Google Drive backup, which only ever adds: a recording stays there after Delete or the retention
  prune until you remove it by hand. The transcript, the title and a short brief of your setup are sent from
  the Mac to Anthropic (Claude) to draft the item." (Also sent: the project hint and the list of project
  names.) The setup
  brief is `INBOX_DRAFT_CONTEXT_FILE` on the Mac — a page Graham (or Hopper) keeps describing his projects
  and their nicknames, so the drafter can tell what "the backup thing" means. It rides stdin as a
  `context` field marked as reference data, never argv; the system prompt says to use it to pick the
  project and word the description and never to copy it into the output. It is read fresh each run
  (`inbox_draft.read_context`): opened `O_NOFOLLOW|O_NONBLOCK` and `fstat`ed on the descriptor read, at
  most 256 KiB, NULs stripped BEFORE the 6000-character cap, decoded with `errors="replace"` (a bad byte
  or a cut character never drops the brief), and marked only when something was cut. Refused, with no
  brief and one log line (not systemic, burns nothing): missing, unreadable, a symlink, not a regular
  file, not owned by the worker's user, group/world-WRITABLE, more than one hard link, empty, the claude
  TOKEN file or the worker's own ENV file (same `st_dev`/`st_ino`), or a brief whose text contains a
  credential (INBOX_TOKEN, INGEST_TOKEN or the claude token — compared in memory, never logged). Writable-by-others is refused because anyone who
  can write it can steer every draft; readable-by-others (0644) is allowed because, unlike the OAuth
  token, it is not a secret. Its text never reaches the log or the heartbeat — only a character count or
  the warning's path and reason. The browser still talks to nothing but this origin. The browser uploads audio to the box and does
  nothing else with it: there is deliberately no in-browser speech recognition, because the Web Speech API
  streams the microphone to Google's or Apple's servers to do the work — inherent to it, not a setting.
  Transcription instead happens on Graham's own Mac, by mlx-whisper running locally. So the full path of a
  recording is browser → the box → that Mac, all of them his. The audio is stored as files under
  `/app/data/inbox/audio`, served only over `/inbox/audio/<id>` behind the session or `INBOX_TOKEN` gate
  with `Cache-Control: private, no-store`, and backed up only to his own Drive.
  The cost of that choice, stated so it is not mistaken for a bug: **a voice note has no transcript at all
  until the Mac worker runs.** Every one is created `transcript_status='pending'` and shows as
  "transcribing…" until then. Nothing is lost while the worker is down — the audio is kept and the next run
  picks it up — but the Mac worker is the ONLY path from audio to text, which is why it runs every 5
  minutes and has a job of its own (`inbox-transcribe`) on the board.
- CSP: `default-src 'self'`, `script-src 'nonce-<per-request>'` allowing exactly one inline script (the
  timestamp localizer in `base.html`); no `'self'`/`'unsafe-inline'` for scripts, no CDN.
- **Write paths, stated precisely (amended by the Inbox release — the old one-line claim is no longer
  true, and quietly leaving it would be worse than the change it hides).** It used to read *"one writer:
  the container owns the SQLite file; everything external arrives via the ingest API."* What is true now:
  - `dashboard.db` has **one request-path writer, the ingest role.** Both roles run its additive schema
    migration at start-up under `BEGIN IMMEDIATE`; after that the READ role's request-path connections are
    `PRAGMA query_only=ON`, so a stray write to `jobs`/`runs`/`probes`/`state_changes` from a read worker
    raises `SQLITE_READONLY` at runtime instead of being merely discouraged by a comment. The invariant is
    now ENFORCED, where before it was a convention.
  - `inbox.db` is a **separate file with two in-container writers** — the read role (the browser creates,
    ticks and edits rows) and the ingest role's scheduler (the GitHub mirror, the audio prune) — coordinated
    by WAL + `busy_timeout`. Concurrency is a non-issue at this scale: a handful of sub-millisecond writes a
    day against a 60 s ticker.
  - Everything external still arrives via an API. Nothing outside the container writes either file.
  The reason for the split rather than proxying the browser's writes to ingest: ingest is a SINGLE worker
  that also owns the scheduler and its serial, up-to-240 s blocking rclone probes, every proxied request
  would arrive from 127.0.0.1 and collapse ingest's peer-keyed rate limiter into one bucket for the world,
  and it would add a 60 s-timeout hop to a write Graham is watching on his phone.

## The Inbox (`/inbox`) — added 2026-09-19

A voice-first work queue on the same app: Graham talks a bug or an idea into his phone, and `/inbox` is then
the ONE place everything pending shows up — his own notes, every open GitHub issue across his ten public
repos, and `~/personal-assistant/backlog.txt`. Filed rows stay, with their issue links and live open/closed
state; "reviewed" is his green light for Hopper to act, not a done-marker.

It rides this app rather than being a new one because the dashboard already had every piece: the password
gate, the tunnel hostname, the ingest port, the scheduler, the CSP, and a board Graham already opens. What it
did NOT have was a backup, and that is the change with the longest tail — see "Audio lifecycle" below.

### Store: a SECOND SQLite file

```sql
inbox_items(id TEXT PK,                     -- uuid4 hex; opaque, appears in URLs
  source, title, title_source, body, project, created_at, updated_at,
  reviewed, reviewed_at, state, closed_at, archived_at,
  transcript_status, transcript_at, transcribe_attempts,
  audio_path, audio_bytes, audio_mime, audio_sha256, audio_secs, audio_pruned_at,
  mirror_key, mirror_url, mirror_seen_at,
  -- added 2026-09-28 through INBOX_COLUMNS (the additive migration's first real use):
  draft_title, draft_body, draft_project, draft_status, draft_at, draft_attempts,
  draft_model, draft_src_sha, draft_edited_at,
  filed_backlog_at, filed_backlog_line,       -- issue #33
  draft_copied_at,
  closed_by,                                  -- 'backlog' | 'issues' when that rule closed it, else NULL
  backlog_absent_pushes)                      -- a filed note: consecutive complete pushes without its tag
inbox_issues(id INTEGER PK, item_id → inbox_items(id), repo, number, url, title,
  state, linked_at, checked_at, closed_at)
inbox_mirror_state(key PK, etag, last_sync_at, last_status, last_error,
  rate_remaining, rate_reset_at, backoff_until)
```

Two unique indexes carry most of the correctness:

- `UNIQUE(mirror_key) WHERE mirror_key IS NOT NULL` — keys are `github:<owner>/<repo>#<n>` and
  `backlog:<sha256 of the normalised What: line>[:16]`. It is the upsert key, so re-mirroring is idempotent.
  **The backlog key is derived, because backlog.txt has no identifiers**: an entry is addressable only by its
  `What:` text. Case and whitespace are normalised so a re-wrap does not orphan a row; any other edit IS a
  different item, which archives the old row and creates a new one (honest and cheap for v1). That
  derivation exists TWICE — `dashboard/inbox_db.normalise_backlog_key` and `probes/backlog.py`, because
  `probes/` is stdlib-only and cannot import Flask — and `tests/test_backlog_mirror.py` imports both and
  pins their agreement. Drift there is not cosmetic: it would archive every mirrored row on the next sync
  and re-create it under a new key, losing its reviewed tick and its linked issues.
- `UNIQUE(item_id, repo, number)` on `inbox_issues` — one LINK per (note, issue); several notes may link
  the same issue (G-20: two notes about one bug), and each closes and reopens with it. What stops the repo
  mirror cloning a note Hopper has already filed as an issue is the scan's lookup (`linked_issue`, on a
  plain `(repo, number)` index): a linked issue is never mirrored, and linking one that already had a
  mirror row archives that row (G-19). The unique index KEEPS the name it had when it was
  `UNIQUE(repo, number)` (`inbox_issues_repo_number`; the migration swaps the definition once): an older
  image's start-up `CREATE UNIQUE INDEX IF NOT EXISTS inbox_issues_repo_number ON (repo, number)` is then
  a no-op, instead of failing over duplicate links and restart-looping a rolled-back container.

`transcript_status` has **five** values, not two: `pending` (audio, nothing transcribed yet) · `whisper` ·
`typed` · `failed` (Whisper gave up after 3 attempts) · `live` (legacy; no longer produced — see the privacy
note under Security posture). Without `pending` and `failed` the board cannot tell "transcribing…" from
"this will never transcribe", and the Mac worker has nothing to back off from.

### Item lifecycle: what "closed" and "archived" mean

Every kind of item is a row in ONE table, `inbox_items`, and they share two independent flags: `state`
(`open` | `closed`, with `closed_at`) and `archived_at` (NULL = live). **Closed** means the work is done and
the row stays on the board with a badge (the state filter shows it). **Archived** means the thing it
mirrored no longer exists upstream (or a note now represents it); archived rows are left out of the
default list and every count, and the State filter's `archived` option (`?state=archived`) lists them with
their "archived upstream" badge — in the API too, so `GET /api/v1/inbox/items?state=archived` is readable
with `READ_TOKEN`, like the rest of the list. Nothing is ever deleted except by Delete — which
exists for voice and typed notes only: on a `github` or `backlog` row the page shows no Delete and the
server answers 409 with where to act instead ("This mirrors GitHub — close the issue there" / "This
mirrors backlog.txt — remove or ✅ DONE the line there"), because deleting a view cannot touch what it
mirrors and the row would come back on a later sync. "By hand"
below means the Close / Reopen button every voice and typed note has in its action row (C-12; mirrored
rows have none — they follow upstream), which is `PATCH /api/v1/inbox/items/<id> {"state": …}` with a
session. A hand change clears `closed_by`, so it sticks until the next upstream change.

| source | becomes closed | becomes archived | reopens | who |
|---|---|---|---|---|
| `voice` | (1) by hand; (2) the ISSUES rule: a complete scan moves its LAST open linked issue to closed (edge-triggered: the stored `inbox_issues.state` before vs after the scan; `closed_by='issues'`; every note linking that issue follows it — many notes may link one issue; a link merged by the boot-time spelling canonicalisation is reset to 'open', so the first full scan fires its real transition); (3) the BACKLOG rule: it was filed to backlog.txt and its `(voice <id8>)` line was marked ✅ DONE/RESOLVED (closes on that push) or has been missing from 2 consecutive COMPLETE pushes (`backlog_absent_pushes`; `closed_by='backlog'`) | never | by hand; or automatically, ONLY by the rule that closed it: `issues` when a scan moves any linked issue from closed to open, `backlog` when a tagged What: line is live and open again. A note closed by hand is never reopened automatically, and a hand reopen sticks until the next real change upstream | (1) by hand, (2)(3) automatic |
| `typed` | the same as `voice` | never | the same as `voice` | the same as `voice` |
| `github` (mirrored issue) | automatically, when a complete open-issue scan of its repo no longer lists it (closed, transferred or deleted upstream); a hand state change is refused (409 "This mirrors GitHub — close or reopen the issue there") | automatically: (a) when a note LINKS its issue (`POST /issues`, G-19 — the note represents it now; every scan that meets a linked issue also archives a live mirror row of it, and a start-up migration repaired old duplicates); (b) when its repo is no longer in `INBOX_GITHUB_REPOS` (G-22, on every sync, even one with no repos) | automatically, on every complete scan that lists it as open; un-archived on a complete scan of its (again watched) repo once no note links it. A LOCAL change that affects a repo — unwatched, a link made, a linking note deleted — forgets that repo's stored ETag, so the next scan is a full one and a 304 can never strand an archived row; and every BOOT forgets every ETag, so a change made while an older image ran is caught too | automatic, on EVERY scan, by design: the row is a VIEW of the issue, so upstream always wins (act on it on GitHub — the row links there; it has no Delete) |
| `backlog` (mirrored line), also a VIEW of upstream (no Delete; edit backlog.txt) | automatically, when its What: line carries a done marker anywhere (`inbox_db.BACKLOG_DONE_RE`: `✅️?\s*(DONE\|RESOLVED)\b`, case-insensitive — "✅ DONE 2026-08-21 — …", "… — ✅ DONE 2026-07-08 via …", "✅ RESOLVED"): the state is set from the text on EVERY upsert, complete push or not (and `closed_by` cleared); a hand state change is refused (409 "This mirrors backlog.txt — ✅ DONE, remove or restore the line there") | automatically, when a COMPLETE push no longer has that exact What: line — removed, or edited: any What: edit (adding or removing ✅ DONE or ⭐ included) is a new key, so the old row archives and a new one appears | by the text: taking ✅ DONE off posts the unmarked line again, which un-archives its earlier open row (or creates a new one) | automatic: the text wins |

Removal is ARCHIVE, not close, for a backlog row: the file is the only record, and "removed" and "done"
are different facts (the ✅ DONE convention keeps done entries in the file, but a shipped entry is often
just deleted). For a FILED NOTE both mean done, and both close it.

A backlog row whose What: line carries a filed note's tag is HIDDEN from the default list and the counts
WHENEVER that note exists, in any state (see "Filing to backlog.txt" below) — it is the same piece of
work, and the note is where its state shows. It shows again only if the note is deleted. A tag counts
only in the What: line (anywhere in it) — never under Why:/Notes:/Context: — in all three matchers
(`filed_line_status`, `backlog_copies`, `_FILED_COPY_SQL`, via `backlog_tags` / `WHAT_LINE_SQL`).

The backlog rule runs once per COMPLETE push, after the whole push is applied (never per row), so an edit
that swaps keys within one push never flaps a note. Done acts on the transition into done (a hand reopen
of a done note sticks); removal needs the tag missing from TWO consecutive complete pushes (a capped
count on the note), so one truncated read of the file, or a filing call that beat its line into the
file, closes nothing — and a note whose line was added and removed between two pushes still closes. The issues rule is edge-triggered the same way, per scan
(`inbox_db.apply_issue_transitions`, fed the stored `inbox_issues` states from before the scan): a hand
reopen of a note whose issues are all closed sticks until an issue next changes upstream, and a change
that happened during a mirror outage is caught by the first good scan, because it compares stored state,
not the previous scan's answer (a 304 "nothing changed" answer is not a scan and changes nothing).
Mirrored rows are the opposite on purpose: `github` and `backlog` rows are views of upstream, so every
scan or push re-applies what upstream says — which is why PATCH `state` on one is a 409 saying where to
act (C-04/05/06), like Delete.

**Known limits, documented rather than fixed:**

- **G-25 — a change on page 2+ of a big repo waits for page 1.** A scan sends `If-None-Match` with the
  FIRST page's ETag, and a 304 ends it. In a repo with more than 100 open issues, an old issue that closes
  (it lives on page 2+) does not change page 1, so the scan is a 304 and the close is picked up only by the
  first scan after something on page 1 changes. Every watched repo is far below 100 open issues today. The
  fix, if one ever is: send `If-None-Match` only when the previous listing fit on one page (or keep
  per-page ETags).
- **X-05 — the tag is 8 hex characters of a uuid4.** Two FILED notes whose ids share the first 8 would
  share one backlog line: only one reports it as its copy, the copy stays hidden while either exists, and
  both follow that line. The chance is about n²/2³³ over n filed notes (≈1e-4 at a thousand). Negligible;
  the fix, if it ever matters, is a longer tag.
- **B-05 — any edit to a backlog What: line is a new row.** The key is the What: line, so a typo fix
  archives the old row and creates a new one; a review tick or manual title on the old row stays with it
  (findable under `?state=archived`). A FILED note is unaffected: its tag moves with the line.
- **B-14 — a truncated read of backlog.txt archives the entries after the cut** until the next complete
  push (an hour later) brings them back with the same ids. Filed notes do not flap on it (the two-push
  grace period).
- **A merged link can re-close a note Graham reopened by hand.** When the boot-time spelling
  canonicalisation merges a note's two links to one issue, the kept link is reset to 'open' (so the first
  full scan fires the real transition). If that issue was already closed and Graham had reopened the note
  by hand, the scan sees open → closed and closes the note again. It needs links stored under two
  spellings of one repo, which only pre-upgrade data can have, so it is rare; reopen the note again.
- **G-26 — a repo with more than 1000 open issues mirrors nothing** (the listing is treated as partial, so
  nothing is upserted or closed). Far beyond any watched repo.

### Three credentials, and why it is three and not one

| token | reaches | over | used by |
|---|---|---|---|
| `INGEST_TOKEN` | `POST /api/v1/ping/<job>` | Tailscale-only `:8081` | every heartbeat, incl. the Inbox's own jobs |
| `READ_TOKEN` | `GET /api/v1/status`, `/api/v1/jobs/<id>` | the public host | Hopper's health watch. **Read-only, and it stays that way** |
| `INBOX_TOKEN` | a fixed list of Inbox endpoints, nothing else | the public host | the Mac transcription worker + the backlog mirror |

`READ_TOKEN` is explicitly REFUSED on the Inbox's mutating routes. It is the credential Hopper's watch
carries around, and the moment it can write, a read credential has quietly become a write one. `INBOX_TOKEN`
is scoped by endpoint name (`inbox.MACHINE_ENDPOINTS`, consulted by `web.auth_kind`) so it cannot become a
second read token for the rest of the API either. Empty `INBOX_TOKEN` = fail closed, exactly like
`INGEST_TOKEN`, and it is deliberately not a `${VAR:?}` in compose: the board booting matters more than the
Inbox booting.

Two consequences that surprise people:

- **A session cookie OUTRANKS a bearer** in `auth_kind`, so a logged-in browser cannot call the
  `INBOX_TOKEN`-only endpoints — they are not for humans. Testing one by pasting a URL into a browser tab
  will always 401, and that is correct.
- **The Origin/Referer CSRF pin exempts a bearer-authenticated call.** A bearer is not an ambient
  credential, and `curl` sends neither header — without the exemption every Mac-worker POST would 403 on
  prod (where `APP_HOST` is set) while passing every test (where it is not).

### Audio lifecycle

Audio is **files, never BLOBs**: `/app/data/inbox/audio/<yyyy>/<mm>/<id>.<ext>`, with the path built from the
server-generated id only — the client's filename is never used. Blobs would make every backup snapshot
byte-unique and defeat the sha256 dedupe the whole backup design rests on, and they would turn the read
role's writes into long transactions. The price is that files need reconciliation rather than hope, so the
scheduler sweeps for both directions of orphan: a row whose `audio_path` points at a file that is gone (the
path is cleared; the row keeps its transcript and simply shows no player), and a file no row references.
**The orphan sweep defers anything newer than the moment it snapshotted the known-paths set** — a live
upload landing mid-sweep is not yet in that set, and deleting it would destroy a recording seconds after
Graham made it.

**`INBOX_AUDIO_MAX_BYTES` is load-bearing on the BROWSER side, not just at the door.** The server renders
the configured value into the capture page, and the recorder ends a take just under it and KEEPS
everything recorded up to that point — "Stopped at the 2 MB limit — saved what was recorded" — instead of
letting the upload be rejected at submit time with the whole recording lost, which is the bug this
replaced. So the knob now sets the maximum LENGTH of a voice note: lowering it shortens takes visibly,
raising it allows longer ones; it no longer decides which recordings die. The page shows a measured
minutes-remaining and a fill bar, and audio is requested at 48 kbps so the cap maps to a predictable
number of minutes (~5 min at 2 MB) — but every size decision is made on MEASURED bytes, never on the
bitrate assumption.

The **aggregate** ceiling (`INBOX_AUDIO_MAX_TOTAL_BYTES`) is checked from an indexed row SUM with a 256 MB
walk margin: near the cap it falls back to a real directory walk, so the cheap path can never admit an
upload that the true on-disk total would refuse.

Automatic deletion happens on **either** of two rules. The convenience prune needs **all three** of: the
transcript is `whisper`-quality, the item has been reviewed, and it is older than
`INBOX_AUDIO_RETENTION_DAYS` (90). By the time all three hold, the text has been in the DB backup for
months and the recording is no longer the only copy of the thought — which is what makes pruning
tolerable at all. The **privacy ceiling** then deletes any audio at TWICE that age regardless of state.
That second rule is not redundant: the first one is satisfiable only by Graham's own action, so a note he
never reviews, or one Whisper failed three times on, would otherwise keep its recording of his voice
forever — the setting would read like a maximum and behave like a minimum.
(Both rules act on the box. Since 2026-09-29 the Drive backup of the audio is add-only by default, so a
recording already backed up outlives both on Drive until removed by hand — see "Mirror mode" below.)

**A recording that goes before it was transcribed** can never be transcribed, so the note must not sit
at "pending" in no queue for ever (P-12/P-14). Pruning it (the ceiling) or the hourly sweep finding its
FILE gone flips a `pending` transcript to `failed`, which puts the note in Needs review; the sweep also
stamps `audio_missing_at`. The row says which: "Recording expired before it was transcribed" (pruned) or
"Recording missing" (lost) — write the draft from memory, or delete it. The page never renders a player
for a file that is gone: before the sweep has noticed, the board `stat`s each recording it lists (W-04).
The migration applies the same rule to notes already stranded that way.

`DELETE /api/v1/inbox/items/<id>` (session only, Origin-pinned, write-limited) removes a note and its
audio immediately (voice and typed notes only; a mirrored row is a 409 — see "Item lifecycle"; the item
JSON carries `deletable`). It exists because the one class of data here that is unambiguously personal is the one
class a user must be able to retract without `docker exec` and hand-written SQL — a recording that caught
a background conversation, or a password read aloud. The row goes first and the file second; the orphan
sweep makes that ordering safe.

**Backup.** This is the part with the longest tail. Before the Inbox, this app's data was entirely
re-derivable and it had no off-box backup — verified on the box: only `km-backup.timer` and
`todoist-points-backup.timer` existed. `inbox.db` and the audio tree changed that, so
`deploy/box/backup.sh` + `hopper-dashboard-backup.{service,timer}` now snapshot BOTH DBs from inside the
container (the WAL sidecars are owned by uid 10001; a host-side online backup fails "attempt to write a
readonly database"), stream each checked snapshot out on the exec's stdout (never `docker cp`: the
container's `/tmp` is a tmpfs, which `docker cp` cannot read), re-check it on the host, and push them to
Drive with **`rclone copy`** into a retention ring plus a `daily/`
tier. The upload itself never deletes; the ring and the daily tier prune by COUNT, deliberately, so a
snapshot leaves Drive only when enough newer ones have replaced it. Nothing that happens to the live DB
can remove an off-box DB snapshot.

**The audio tree is ADD-ONLY by default (`BACKUP_AUDIO_MODE=copy`, Graham's decision 2026-09-29).**
Recordings on Drive are only ever added (`rclone copy --immutable --exclude '*.part'` straight from a
staging dir that is removed after: a recording that differs on Drive is refused, never overwritten; an
in-flight upload is skipped), and the box keeps NO copy of the audio: a note deleted in the Hub (or aged
out by the prune) loses its recording from the Hub at once, and a copy already backed up stays in Drive
until removed there by hand. With no box copy there is nothing for a shrink brake to guard, so none can
false-alarm; a successful copy-mode run removes a box copy and brake state left by mirror mode. That
reverses the 2026-09-19 decision below, which is kept as `BACKUP_AUDIO_MODE=mirror`, and the brakes
described next are mirror mode's. Every run holds `flock -n` on `state/backup.lock` (an overlapping run
exits 0 and does nothing) and, under it, sweeps every leftover `.audio.*` staging dir. The rest of this
section up to "What Delete does" describes mirror mode.

**Mirror mode: the recordings MIRROR the container, deletions included.** Graham's
decision, 2026-09-19 (no longer the default), after a reviewer pointed out that an additive audio backup quietly defeats both
controls above — Delete is sold here as the way to retract "a password read aloud", and the privacy
ceiling is sold as a maximum age, and neither is true if every recording lives on Drive forever. So both
hops mirror: each run `docker cp`s the tree into a FRESH staging directory (an additive `docker cp` into a
persistent one would itself resurrect deleted files) and the upload is `rclone copy` followed by an
explicit delete pass for remote files the container no longer has. Delete now means deleted everywhere.
The databases are untouched by this and stay additive.

**The brakes on that — three of them, each able to refuse on its own.** A mass deletion is far more
likely to be a wiped volume, a mis-set path or a prune bug than an intentional purge, so `push_audio`
refuses to propagate one. It refuses when the container's audio directory is missing entirely; when the
count has fallen by more than `AUDIO_MAX_DROP_PCT` (default 50, validated to 1–99 — **100 is rejected,
because it would silently disable the brake**); when more than `AUDIO_MAX_DROP_FILES` (default 25) files
would be deleted in a single run, whatever the proportion; when the count has fallen that far from the
highest count seen in the last `AUDIO_DROP_WINDOW_MIN` (default 24 h), which is what makes a steady
sub-threshold drip visible; when the staged tree does not hold what the container said it holds; when the
remote cannot be listed; or when the container has no recordings at all while Drive has some. In every
one of those cases Drive keeps what it has, the remembered count does not move, and the run FAILS loudly.

Two things about those brakes are load-bearing and were learned the hard way:

- **The baseline comes from the REMOTE, not from a file on the box.** With no remembered count — the
  first run after deploy, a cleared state dir, a changed `BACKUP_ROOT`/`HOME`, a rebuild-from-Drive —
  the comparison is taken from an `rclone lsf` of `…/audio`, i.e. what Drive actually holds. A run that
  cannot obtain that listing does not delete anything at all and records no baseline: "could not list
  the remote" is not "the remote is empty".
- **Everything is measured on the STAGED tree, the exact set that is uploaded and diffed.** Measuring
  the brake on one set and running the deletion against another is the same bug in a different costume.

`AUDIO_ALLOW_MASS_DELETE` is the deliberate override and it is **one-shot by construction**: it carries
the exact count the purge should leave behind (`AUDIO_ALLOW_MASS_DELETE=7`), so a value left behind in
`.env.backup` authorises a state that has already happened — which is to say, nothing. Only a clean
mirror updates the remembered count. Details and the restore procedure in DEPLOY.md §2b.

**What Delete does and does not reach.** Delete removes the note and its recording from the Hub (the
container's store) immediately; in the default copy mode the box keeps no other copy of the audio, and
**a recording already backed up stays in Drive** until removed there by hand (in mirror mode it is gone
from Drive, and from mirror mode's box copy, at the next successful backup run). The page says: "Delete
removes a note and its recording from the Hub at once; the box keeps no other copy of the audio, and a copy
already in Google Drive stays there until you remove it by hand." The Delete confirmation names the file:
`…/audio/<yyyy>/<mm>/<note id>.*` in the backup folder. **The
transcript TEXT is not retracted from backups already taken.** Every `inbox_*.db` snapshot — the local ring,
the Drive ring and the `daily/` tier — still contains whatever was said. Each tier has an age cap on top of
its count cap (`SNAPSHOT_MAX_AGE_DAYS`, 30), so those snapshots are **removed within about 30 days**, and a
removed Drive file may then sit in Drive's trash for up to 30 more days. The age cap cannot empty a tier:
the newest snapshot always stays; so does the STATE AS OF A WEEK AGO — the newest snapshot stamped more
than 7 days back — whatever its age or the count cap (a bad change is undone from it, and later writes such
as a review tick or a boot's ETag forget, each a new snapshot, can never push it out early); at most 5 go
by age per tier per run; and
a clock that reads earlier than the last run, or more than 7 days after it, skips age removal for that
run. Every run that reaches Drive prunes both Drive tiers for every DB, uploaded or not (a quiet DB was
never pruned there, BK-19). The page says: "Database backups already taken still hold the transcript text;
they are removed within about 30 days (the state as of a week ago is always kept, so a bad change can be
undone), and may then sit in Google Drive's trash for up to 30 more days." That is
the correct trade: rewriting historical database snapshots to erase a row would mean a backup that can be
edited after the fact, which is not a backup. But the claim has to be stated honestly rather than sold as
"deleted everywhere" — the words persist for about 30 days in dated database backups (plus Drive's
trash), and (in copy mode) the recording persists in Drive
until removed by hand.

### What the Inbox adds to the board

Four jobs, all with a 24 h alert threshold because none of them is urgent and all of them fail INVISIBLY:

- `inbox-github-sync` (box, `kind: worker`) — the in-container mirror. A dead mirror is the failure that
  looks fine: the board keeps rendering the issues it last saw.
- `hopper-dashboard-backup` (box, `kind: db_snapshot`) — above.
- `inbox-transcribe` (mac, `kind: worker`) — the only path from audio to text. **One item can never
  wedge the queue**: the queue is served oldest-first, so an item the server keeps rejecting (a 413 on an
  over-long transcript, a 400, a persistent 429) would otherwise abort every run at the same row, burn no
  attempt, and silently starve every newer voice note behind it. The download, the transcription AND the
  success-path POST are all contained per item: a failure is reported as THAT item's (burning one of its
  three attempts) and the run moves on. Only an `EnvironmentFault` (no ffmpeg, no mlx) or an auth-shaped
  401/403 aborts a run — both of which would fail identically for every item, so burning attempts on them
  would destroy transcripts rather than save them. Transcripts are also truncated to the server's
  `MAX_TEXT` before posting, so length alone can never be the rejection.
- `inbox-backlog` (mac, `kind: worker`) — a sub-probe of the hourly Mac probe.

`kind: worker` is a correctness decision, not a label. `services._machine_probe` returns the FIRST
`kind: probe` job for a machine, so a second probe job on `mac` would make the machine-offline rule depend
on the ORDER of `jobs.yml`, and the first one ever on `box` would silently switch sibling-LATE suppression
on for every box job at once. `worker` carries no kind-specific state and flows through the generic
scheduled precedence — never-pinged → LATE → FAIL → OK — which is exactly what "did this loop run?" means.

**`inbox-backlog`'s failures are its own, never `mac-probe`'s.** It posts to the PUBLIC host while every
heartbeat rides the Tailscale ingest port — two completely different failure surfaces. `mac-probe` means
"the Mac is awake and probing", and the machine-offline rule mutes every other Mac job's LATE alert while it
is LATE, so letting a Cloudflare 502 mark the Mac as failing would mute the very jobs that say the backups
stopped. The sub-probe therefore never raises: a delivery or parse failure comes back as `inbox-backlog`'s
own `fail` ping. That guarantee was ONE EXCEPTION WIDE until the security gate found it — `api_request`
caught `URLError`/`OSError`, but `http.client.BadStatusLine` and `LineTooLong` are `HTTPException` and
neither, so a MALFORMED response (a captive portal, a truncated edge response) escaped the retry loop,
reached mac_probe's generic handler and flipped `mac-probe` itself to `fail` — reading as "the Mac is
down" and muting `pa-backup` and `minecraft-offload`. Fixed in both places: those are now retryable
transport errors, and the sub-probe catches `Exception`, not just `ProbeError`.

**Both Mac Inbox jobs carry `grace_s: 50520`**, like every other Mac job — 14 h + 2 min, so their deadline
can never land before `mac-probe`'s. The cost is that plain SILENCE from the transcription worker only reads
as LATE after ~14 h. A worker that CRASHED is a different story and is red immediately, because it posts its
own `fail`; the slow half only covers "launchd never ran it at all".

- **HTTPS enforced at the origin, not only at the edge** (`web._https_redirect`, `_security_headers`).
  Cloudflare's zone-wide "Always Use HTTPS" is one dashboard toggle away from regressing, so the READ role
  enforces it itself: when cloudflared forwards `X-Forwarded-Proto: http` it answers `307` to
  `https://<APP_HOST><path?query>`, and every read-side response carries
  `Strict-Transport-Security: max-age=31536000` (no `includeSubDomains`, no `preload` — each hostname owns
  its own policy and preload is effectively irreversible; matches the apex landing page, the reference
  implementation). Three rules make it safe rather than merely present:
  - **Only an exactly-`http` header redirects** (case/whitespace normalised). An ABSENT header never does —
    the container HEALTHCHECK and Hopper's in-network read (`curl -H 'Host: <APP_HOST>' http://…`) send none,
    and redirecting them would break monitoring rather than protect anything. No per-path exemptions.
  - **The target is built from the `APP_HOST` pin, never from the request's Host/URL** (reflection would be an
    open redirect), from the RAW request target (`RAW_URI`/`REQUEST_URI`) so percent-encoding round-trips
    byte-for-byte — `request.path` is already decoded, so `/a%2Fb` and `/a/b` are indistinguishable there.
    A forged/absolute-form/over-long/control-byte target falls back to a re-quoted path, so no header
    injection. `APP_HOST` itself is read through `Settings.https_redirect_host`, which yields it only when
    it is a **bare hostname** — `host@evil.example` would otherwise emit a Location the browser resolves to
    `evil.example` (WHATWG userinfo) and an embedded CRLF would 500 every request. Unset **or malformed**
    `APP_HOST` → **no redirect** (fail open: local dev, the local visual-QA path, tests), logged at start-up.
    ⚠️ Both `_SAFE_TARGET_RE` and `_HOSTNAME_RE` are safe only under `.fullmatch()`; `.match()` accepts a
    trailing newline and so would `^…$`.
  - **`307`, with `Cache-Control: no-store` and `Vary: X-Forwarded-Proto`** — not `301`. The `Location` is
    byte-identical to the requested URL, so a cacheable answer is self-referential: RFC 9111 makes a 301
    with no `Cache-Control` heuristically cacheable *indefinitely*, which would make a misdeployed
    `APP_HOST` sticky in every visitor's browser with no recall, and would let a shared cache serve an
    https visitor a redirect to itself. 307 also preserves the method, so a plain-http POST is not
    downgraded to a bodiless GET. HSTS provides the durable upgrade.
  - **The INGEST role is deliberately exempt**, structurally: both hooks live on `web.bp`, which only the read
    role registers. `:8081` is reached directly over the tailnet, serves no TLS, and its heartbeats (systemd
    `ExecStopPost` curls, `dashboard-containers.timer`, the Mac launchd probe) are plain HTTP with no
    `X-Forwarded-Proto`. A redirect there would silently stop every heartbeat and leave the board lying.
    Never move these hooks onto the app factory. Session cookies are `Secure` + `HttpOnly` + `SameSite=Lax`.
- One writer: the container owns the SQLite file; everything external arrives via the ingest API.

## Drafts, Needs review and the Hub — added 2026-09-28

**The product is called Hub** (`hub.graham-williams.com`; the repo keeps its name). `/` is a landing page
with two equal cards — the Inbox first (Needs-review count, a big Record link to `/inbox#capture`, open
count), then a one-row health strip of the board (OK · behind · failing, glyph + label, and the
stale-scheduler warning). The board moved to `/dashboard`; `/jobs/<id>` is unchanged. The Hub has exactly
one `<script>` like the board, and the Record button is a plain link so it works with JS off.

**Old hostname.** `APP_LEGACY_HOSTS` (validated like `APP_HOST`, never equal to it) are answered by
`web._legacy_host_redirect` with a **307 for every method** to `https://<APP_HOST><raw target>`,
`Cache-Control: no-store`, before the gate and the Host pin; `/healthz` is exempt; a malformed `APP_HOST`
fails closed (403). 307 and not 301 for the same reason as the http→https redirect: a permanent redirect
built from a wrong `APP_HOST` would stick in every browser. It is done in the app because the Cloudflare
token cannot write redirect rules; the old hostname keeps its tunnel rule and CNAME.

**Drafts.** After Whisper, the Mac worker (`probes/inbox_draft.py`, phase two of `inbox-transcribe`) asks
`claude -p` for a title, a short description and a project, and posts it back. The draft lives in the
`draft_*` columns ALONGSIDE the note — the Mac never writes `title`/`body`/`project`, because `body` is the
transcript and the audio prune depends on it being Whisper's. Rules:

- `draft_status`: NULL (no transcript yet, an empty one, or a pre-drafting row) → `pending` (a transcript
  landed) → `ready` | `failed` (three bad results). A new transcript, or Graham editing `body` on an
  unreviewed voice note whose draft he has not edited, makes it `pending` with zero attempts — even from
  NULL, so a note whose Whisper transcript FAILED can be drafted from what he types; an empty body makes it
  NULL (never a permanent "Drafting…"). The queue drafts every `pending` note with text; `draft_src_sha` (sha256 of the transcript it was made from) makes a draft
  of an older transcript stale — the POST is a 409 and burns nothing.
- Graham editing any draft field stamps `draft_edited_at`: from then on no machine draft may overwrite it
  (409), and the draft is `ready`. A manual title typed at capture is forced into `draft_title`, and the
  capture form's project seeds `draft_project`.
- **A voice note can be ticked Reviewed only once it has a draft** (R-03): a draft title that is `ready`
  (the machine's, or Graham's own edit), or one written in the same request. Before that the PATCH is a
  409 ("nothing to review yet — wait for the draft, or write one with Edit draft") and the page shows no
  Reviewed box (`inbox.can_tick_reviewed`): ticking a note that is still transcribing used to hand Hopper's
  filing loop an "(untitled)" note with an empty body. Unticking is always allowed; typed and legacy
  notes are unchanged.
- **Ticking Reviewed on a voice note copies the draft**: `title := draft_title` (made `manual`; only over a
  still-derived title, or a draft edited in the same request, so a re-tick never takes back a rename) and
  `project := draft_project` — only on the FIRST copy (`draft_copied_at IS NULL`; `reviewed_at` cannot say
  it, an untick clears it; the migration backfills it from `reviewed_at` for voice notes reviewed before it
  existed) or when `draft_project` is in the same request. **Editing the draft after review**
  also writes `title` (manual) and `project`, so what is filed is what Graham sees.
- **Every write that acts on a draft is a conditional UPDATE** (`set_draft`: `reviewed=0 AND draft_edited_at
  IS NULL AND body IS <the body it hashed>`; the review copy: `reviewed=0` and the draft values it read;
  `note_draft_attempt`: the attempt count it read). A lost race is a 409, never a stale write. Hopper's filing loop keeps reading `title`/`project`, and
  gains `draft.body` as the issue description.
- **Needs review** = voice, open, not archived, `reviewed=0`, and `draft_status IN ('ready','failed')` OR
  `transcript_status='failed'` — failures too, so nothing strands; the row says why ("Couldn't draft — edit
  to write one" / "Whisper couldn't transcribe this — play it and write the draft"). It sorts first, then
  awaiting-filing, then newest.
- **Reviewed is a voice-note control.** The checkbox renders on voice rows and on OLDER typed notes,
  ticked or not, so an accidental tick can be undone. "Older" is `reviewed_at != created_at`: a typed note
  made since the release is born reviewed in the same INSERT, so its two timestamps are identical; a legacy
  note's are NULL or a later tick. (Unticking a new typed note makes it look legacy — harmless: it gets a
  checkbox back.)
  a PATCH of `reviewed:true` on anything else is a 400 (unticking stays allowed; items expose
  `reviewable`). Typed notes are now created with `reviewed=1` — typing one is the deliberate act — so the
  filing loop picks them up. No data was migrated.

**Contract addendum** (additive; `/api/v1/status` is unchanged):

- Every item gains `needs_review` (bool) and `draft: {title, body, project, status, at, attempts, model,
  edited_at}`. `counts` gains `needs_review`; `?awaiting=review` filters to it. `awaiting_filing`,
  `?awaiting=filing|transcription` and the other counts are unchanged (the filing loop uses them). Search
  also matches the draft title and body.
- `PATCH /api/v1/inbox/items/<id>` accepts `draft_title`, `draft_body`, `draft_project` (voice only; same
  session / Origin / limiter rules).
- PATCH answers with the item AND fresh `counts`; DELETE answers `{deleted, had_audio, audio_removed,
  counts}` — the same `counts()` the tiles are rendered from — and a DELETE of a note that is already
  gone is a 404 that still carries `counts`. The page shows an answer's counts at once, then **settles
  to the server's counts after each burst**: whenever an action request settles (success, 409, 404 or
  no answer at all) and no other is in flight — a capture save counts as one — it waits ~300 ms, then
  makes ONE `GET /api/v1/inbox/counts` (session or READ_TOKEN, `{counts}`) and applies it as the final
  word, unless another action has started since. Answers can land out of order (two gunicorn workers,
  two rows) and a refusal carries no counts, so the tiles may be briefly wrong in between; the settle
  read is what makes them right. (No per-answer stamps: a stamp read apart from the SELECT could not
  order two answers truthfully.) That is what lets the page update in place:
  **no row action reloads it** (the Reviewed tick, Close, Reopen, a draft save, Delete). Each row is
  rendered with both states of everything an action can change, the one not in force `hidden`, and
  inbox.js flips `hidden`, classes and data-* from the answer (never markup). A refusal shows "Not saved —
  <reason>" on the row and leaves every control showing the true state. Capture save still reloads — the
  new row needs server rendering — but never over input that is not saved anywhere (a take recording,
  starting or held, capture text, an open draft editor with changes): then it resets what it sent and
  says "Added — refresh to see it in the list". A `beforeunload` guard asks before leaving while any of
  that exists.
- A CLOSED note's [Edit draft][Reviewed] group keeps its slot and shows nothing (`.dormant`,
  `visibility: hidden`), so Reopen sits exactly where Close was, and is `inert`, so nothing in it takes
  focus or a tap even if app.css never loads. Closing a note closes its draft editor
  if it holds no changes; one with changes stays open (it is unsaved input), but its Save reads "Reopen
  to save" and stays disabled until the note is open again.
- The "nothing to review yet" 409 on a Reviewed tick carries `code: "nothing_to_review"` and `item`, the
  row as it is now, so the page hides the box and shows "Drafting…" or the failed-draft line instead of
  an unticked box that would only be refused again. (Not "not_reviewable": every voice note IS
  `reviewable`; it just has no draft yet.)
- `GET /api/v1/inbox/draft/queue` (INBOX_TOKEN) → `{items: [{id, transcript, manual_title, project, sha}],
  known_projects, max_attempts, max_title, max_body}`. Oldest first; includes the backfill (transcribed
  notes with no draft) and notes whose draft is stale. `known_projects` = the repo names of
  `INBOX_GITHUB_REPOS` plus the board's projects.
- `POST /api/v1/inbox/items/<id>/draft` (INBOX_TOKEN) with `{title, body, project, src_sha, model}` or
  `{failed: true, error, src_sha, final?}` (`src_sha` is required on both; `final: true` burns every
  remaining attempt at once — the worker's breaker giving up). Title one line ≤ 120, body ≤ 2000, both through `clean_text`; an unknown
  project becomes NULL; stale sha / reviewed / edited → 409, no attempt burned. The caps are pinned between
  `inbox_db` and `probes/inbox_draft.py` by a test.

**Filing to backlog.txt instead of an issue (issue #33).** Hopper's filing loop takes every note that is
awaiting filing (`?awaiting=filing`: reviewed, open, local, no linked issue, not filed to the backlog) and
files it from `title`, `project` and `draft.body`: repo work becomes a GitHub issue recorded with
`POST /api/v1/inbox/items/<id>/issues`; anything else (Hopper itself, the Mac or box, a chore) becomes ONE
`backlog.txt` ENTRY in Hopper's own words whose What: line ends `(voice <first 8 chars of id>)` (the
format is pinned below), recorded with
`POST /api/v1/inbox/items/<id>/filed-backlog {"line": …}` (INBOX_TOKEN; the note must be reviewed, open
and not archived, else 409; one line, `clean_text`, ≤ `MAX_BACKLOG_LINE` = 500, must carry the tag;
idempotent — the first `filed_backlog_at` is kept). The note
then leaves awaiting-filing, and the backlog-mirror row that entry comes back as (matched by the tag in
its What: line, whichever arrives first) is **hidden from the default list and the counts whenever that
note exists** (shown again only if the note is deleted) — the simpler of hiding or
nesting — and linked as `filed_backlog.mirror_key` on the note. `?source=backlog` still lists it.

**Removing the line, or marking it done, closes the note** (the backlog twin of "all linked issues
closed → close"). One push is ONE transaction (`inbox_db.apply_backlog_push`; these connections are
autocommit, so `with conn:` would not be one). It first sets each row's state from its What: line (done
marker → closed, otherwise open; `closed_by` cleared). Then, ONLY for a COMPLETE push and only after all
of it — every upsert and the archive — `inbox_db.apply_filed_backlog_rule` looks at each filed note's tag
in the What: lines: `open` (some live tagged row is open), `done` (live tagged rows, all closed) or none
live. `done` closes an open note on the push it becomes done; none live counts the push in the note's
`backlog_absent_pushes` (capped at 2) and closes it on the SECOND consecutive one; `open` resets the count
and reopens a note with `closed_by='backlog'`. Nothing else is touched: a note closed by hand has
`closed_by` NULL (every hand state change clears it) and the issues rule (`apply_issue_transitions`) writes
`issues`. Every write is by primary key. What this means in practice:

- a push is ALL OR NOTHING: `POST /api/v1/inbox/mirror/backlog` validates the whole payload before it
  writes anything, so a 400 (a non-object item, a bad project) changes nothing at all — it used to commit
  every item upserted before the bad one — and the writes themselves are one transaction. A push whose
  entries are all blank after cleaning is refused like an empty one (a complete one would otherwise archive
  every row). The route has its own 1 MB body allowance (the real file is ~45 KB of JSON, 69% of the 64 KB
  global cap that every other route keeps; the probe has no smaller cap of its own);
- each key is DERIVED from the entry's What: line on the server, and a client `key` that differs refuses
  the WHOLE push with a 400 (B-30). Intended: a mismatch means the probe and the server disagree about
  identity, and applying the rest would archive and re-create rows — so it is loud instead (the
  `inbox-backlog` heartbeat fails until the two agree; `tests/test_backlog_mirror.py` pins them together);
- an UNCHANGED push writes nothing at all (no `updated_at`/`mirror_seen_at` churn, the absence count is
  capped), so the 5-minute backup does not re-snapshot and re-upload `inbox.db` for nothing;
- a partial (`complete: false`), refused or failed push closes, reopens and counts nothing;
- a reworded line that keeps the tag, or the tag moving to another entry's What: line, closes nothing
  (the tag is still on a live open row at the end of the push); a tag only under Why:/Notes:/Context:
  does not count;
- two lines with the same tag, one done and one open, keep the note open; both done closes it;
- a filed note whose line never reached the mirror (Hopper's filing call and the hourly push can land in
  either order; the line may be added and removed between pushes) closes after two complete pushes
  without it, like a removal; one truncated read of the file (only the first N entries) closes nothing.

**The filing format** Hopper writes is a proper entry, appended with its own separator:

```
---
What: <what to do, in Hopper's words> (voice <id8>)
```

A bare line appended without `---` becomes a continuation of the PREVIOUS entry's last field, not an
entry of its own: it links nothing and the note would close after two pushes. The tag may sit anywhere in
the What: line (a later "— ✅ DONE <date>" suffix is fine) but must not be moved to another field.

Why a column (`closed_by`) rather than inferring from timestamps: it is the one fact each reopen needs
("did this rule close it?"), and a hand close in the same second as a push would otherwise be
indistinguishable. The push response adds `closed_notes` and `reopened_notes`; the GitHub sync result
adds `reopened_items`. Rows closed before the column existed have `closed_by` NULL, so they are treated as
closed by hand: no rule reopens them.

**The `claude -p` call** is locked down: `--safe-mode --setting-sources "" --tools "" --strict-mcp-config
--no-session-persistence --disable-slash-commands --output-format json --json-schema … --model sonnet
--system-prompt …`, the transcript (and the optional setup brief as a `context` field — see the privacy
bullet above) as JSON on stdin framed as data, an empty temp cwd, an allowlisted env
plus `CLAUDE_CODE_OAUTH_TOKEN` from a 0600 file. Never `--bare` (it ignores OAuth). The result envelope was
recorded from the real CLI: success is `is_error:false` with the object in `structured_output`; every API
problem is `is_error:true`, `terminal_reason:"api_error"`, `api_error_status` = HTTP status or null, and
`subtype` still reads "success". Systemic is NARROW — missing/relative binary, non-envelope output, an
auth failure or usage limit, an `api_error` other than 400/413 — and stops drafting and fails the
heartbeat without burning attempts. Anything else is per-note, subject to a circuit breaker (a run that ENDS with no success and 2+ bad results burns nothing (the whole batch is still worked, so a good note behind bad ones is drafted); on the 3rd such run in a row, counted in `draft-state.json`, the held notes go straight to `failed` (every remaining attempt burned, via `"final": true` on the failed POST), so they show in Needs review after 3 runs, not 9) and a timeout rule (systemic unless the same note timed out
last run too). No error message raised from the CLI's output ever carries stdout (it can be model text).

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
`km-tracker_default`; tunnel ingress + proxied CNAME `hub.graham-williams.com` (single-label host). The
legacy `dashboard.graham-williams.com` keeps its ingress rule and CNAME and is answered by the app with a
307 to the Hub (`APP_LEGACY_HOSTS`).
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
whose reads go through the public hostname, `https://hub.graham-williams.com/api/v1/status`; a loopback
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
  `alert` (`{after_s, never, source, bad_since, alerted_at, alerted_state, window_s, cooldown_s,
  last_paged_at}` — the RESOLVED policy plus the open episode: `after_s` is null when the job never pages,
  `source` is one of
  `alert_after_s` / `alert` / `informational` / `default` (see "Resolving a job's policy"), `bad_since`
  non-null means an episode is running — which it can be while the state reads OK — and `alerted_at` non-null
  means it was paged, `alerted_state` naming WHAT it was paged about (not always the state on the card: an
  episode paged as BEHIND that has since gone FAIL reads `state: FAIL` beside `alerted_state: BEHIND` until
  the escalation goes out), `window_s` the accumulator's window — this job also pages after `after_s` of
  not-OK time inside it, not only after `after_s` unbroken. `cooldown_s` + `last_paged_at` are the per-job
  page rate limit, and reading them together answers the only question worth asking when the phone is quiet
  but the board is not: is this job unpaged because nothing crossed a threshold, or because it paged
  recently?),
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
    destination: gdrive:Hopper/km-tracker-backups
    cadence_s: 300
    grace_s: 600
    probe:                        # required for db_snapshot; optional for rclone_copy_tree / manual (box lists the dest)
      rclone_path: gdrive:Hopper/km-tracker-backups
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
every schema string is rejected if it contains a control character (C0 or DEL — `job.name` becomes the ntfy
`Title` header, and a CR/LF there makes `http.client` refuse every POST for that job for ever: not an
injection, but a job that can never page and can never spend its page, so it is caught where a human sees it),
`probe.interval_s` is a positive int, may not exceed `MAX_PROBE_INTERVAL_S` (86400 — a magnitude cap on
EVERY kind, because `interval_s: 18000000` is 208 days of not looking and nothing on the board says so), and
on a `db_snapshot` may not exceed half the freshness window (`cadence_s * DEST_FRESH_MULTIPLIER / 2`),
unknown keys anywhere are errors.
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
success > `max_age_s` or `lag_bytes` > `max_lag_bytes`; no thresholds = informational, which RESOLVES to
`alert: never` — but note the two are not the same question, and the board now captions off the resolved
policy rather than off `informational`. `minecraft-offload` is the case that matters: `alert: never` AND
carrying `manual:` thresholds, so it is not informational, and a caption keyed on `informational` skipped it
entirely — leaving the one job that sits at BEHIND as its resting state with nothing saying it never pages.
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
`interval_s` above half the window for that kind so the invariant is enforced, not just reasoned about. On
top of that, **every** kind is capped at `MAX_PROBE_INTERVAL_S` = 86400: the semantic cap does not apply to
`rclone_copy_tree`/`manual`, which are exactly the two the operator hand-edits (DEPLOY.md §1d), so an extra
digit there used to buy months of silent not-looking. The
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

**"Newest probe row" means newest by `id`, not by `probed_at`.** `probed_at` is the writer's wall clock, so a
row written while the clock was ahead (an RTC booting wrong, an NTP step backwards afterwards) sits in the
future and outranks every real probe after it, permanently — the newest row reads `ok`, so
`db.failing_probe_job_ids` is empty, `db.probe_fail_streak` is 0, the damped-OK hold is switched off and the
damping above can never un-damp. Clamping at insert does not help (at insert time the value *is* now; it
becomes the future later), so every "which row is current" query orders by the rowid, which is monotonic
because `probes` has exactly one writer. Durations are still measured from `probed_at`.

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

**The episode** lives in three `jobs` columns — `bad_since` (when it started), `alerted_at` (when it paged)
and `alerted_state` (what that page said) — with `last_paged_at` a fourth holding the per-job cooldown, which
spans episodes rather than belonging to one. All additive `ALTER TABLE`s, NULL for every existing row, so a job
already broken at upgrade time starts a fresh clock and pages one threshold late — late, never silent. The one
row shape that needs care is an episode that is open AND paged when `alerted_state` arrives (`alerted_at` set,
`alerted_state` NULL): it is **healed** on the next recompute by recording the state the episode is about then.
Ranking a NULL as "worse than nothing" would duplicate the page that already went out; ranking it at the top
would swallow a real escalation. `jobs.since` cannot serve as the episode start: it is reset by every state
change, and a `LATE → FAIL` mid-episode is the *same* outage. Episode state is deliberately **not** in
`last_metrics`, which the ingest route can write — and the accumulator's input is `state_changes`, which it
cannot write either.

| rule | why |
|---|---|
| `bad_since` is set on the first not-OK recompute and never moved until the episode ends | the question is "has this been broken for a day?", not "did something change?" |
| `alerted_at` caps the episode at ONE page | the noise this feature exists to remove |
| `UNKNOWN` is not alertable and starts no episode; entering it **clears** the bookkeeping with no recovery | "no data yet" ≠ "broken" (real silence is already the never-pinged → LATE rule). Clearing matters: a paged job that passed through UNKNOWN used to keep `alerted_at` for ever and be un-pageable |
| a failed ntfy POST rolls `alerted_at` back to NULL so a later tick retries, keyed on **episode identity** (`bad_since`+`alerted_at`), never on "is the job not-OK now" | one unlucky POST otherwise bought permanent silence. The identity test is load-bearing: the dwell and the hold both keep an episode open while the job reads OK, so a state-based guard skips exactly the case it was written for |
| retries are spaced `ALERT_RETRY_MIN_S` (5 min) per **page** — the episode *and* the severity rank, held side by side in one entry per job; a **first** page (or first escalation) is never delayed | "the next tick" is the ticker *plus* every ping — 72 blocking 5 s POSTs an hour into a dead ntfy, inside the scheduler thread and the ingest request. The two ranks must be separate slots, not one slot per job with the rank folded into a key: folded, they evict each other, and a state oscillating across the rank boundary (a disk gauge flipping BEHIND ↔ FAIL) reads every pass as a page it has never tried — **measured at 60 attempts/hour against 12 for a non-oscillating episode**, i.e. the backoff not applying at all. Per rank the worst case is 2 attempts per window, ≤ 24/hour |
| a failed **recovery** is logged (`recovery push for …`) and dropped | its episode is already closed, so there is nothing to hand it back to; re-sending later could announce "→ OK" for a job that has broken again. Losing it costs good news, never a page |
| an episode ends only after the job holds a **verified** OK for the *dwell*: `max(min(5 min, alert_after_s/10), min(2 × cadence_s, 15 min))` | one OK tick used to end it, so a container on `restart: unless-stopped` backoff (19 min down, 1 min up — broken 95% of the day) reset its clock 72×/day and never paged. The **cadence term** is the second half of that fix: a dwell shorter than the job's own sampling interval is decided by ONE observation, and `box-containers` is sampled every 300 s while its threshold-derived dwell was 120 s — so a container crash-looping at 50/67/75 % down for six hours closed its episode over and over and paged NOTHING. Cost: a recovery arrives up to one dwell late (600 s for the box jobs, 900 s for the Mac's), which beats announcing a recovery that is about to be taken back. Cadence-less jobs (`manual`, `disk`) keep the threshold-derived value — there is nothing to sample. The 15 min cap is not cosmetic: `pa-backup`'s 24 h cadence would otherwise give it a two-DAY dwell, i.e. a page held hostage for two days |
| **the per-job cooldown**: after a page for a job, the next PAGE for that job waits `max(alert_after_s, 6 h)`, persisted in `jobs.last_paged_at` | one page per EPISODE says nothing about how often an episode may RESTART. Measured over a simulated day at `box-containers`' real threshold: **132 pushes** at 19-min-down / 20-min-up, and an independently measured 30-min crash-loop cycle at 48/day. It **delays** a page and can never **cancel** one: a held page stamps nothing, so the episode keeps it and the ceiling re-offers it every pass. It binds on exactly three shipped jobs (threshold under 6 h); above the floor a new episode already takes longer than the cooldown to reach its own threshold. Persisted, not in memory, because an in-memory cooldown resets on every deploy and a deploy is what makes containers flap |
| **the accumulator**: an episode is ALSO past its bar when the job has spent `alert_after_s` in *the state being paged about* within the last `BAD_WINDOW_MULTIPLE × alert_after_s` (= 2×, i.e. "bad more than half the time, over two thresholds") | one verified OK longer than the dwell destroyed an episode outright, so an intermittent destination reset its clock for ever: 60 min down / 15 min up FAILED 73% of its probes and paged **zero times a day**; 300/30 failed 90% and paged **zero**. The damping has exactly this backstop one layer down (`PROBE_NO_SUCCESS_S`, from the last *successful* probe) and the episode had none. It is an **OR** beside the clock, never a replacement — a rule that counts only not-OK seconds can only page LATER, which is how the two attempts in #13 item 4 each produced a silence bug. Per-STATE, not "any badness": counting everything laundered deliberately muted time (a sleeping Mac's sibling LATE) into an instant page for an unrelated BEHIND. **What per-state does NOT close** (measured, and previously mis-stated here as impossible): muted LATE time still counts toward a later *LATE* page, because the mute reads the probe's state now and the sum reads across episodes — on the shipped file that is reachable on `drive-mirror` and nothing else (see "Muted time is not a cooldown" below). Derived from `state_changes` (`db.not_ok_seconds`), which ingest cannot write, and walked by rowid so **a** single clock step cannot inflate it. Accuracy, measured rather than asserted: exact for a monotone timeline, under-counting only when history is missing, over-counting by <1 s/span from whole-second stamps, and inflatable by a *sawtooth* clock up to the window itself (42 900 s of a 43 200 s window). The hard ceiling is always `end - start` = twice the bar, so the worst case is a page one window early |
| **escalation**: an episode that has already paged pages ONCE more if its state gets strictly worse, carrying that state's own priority. `jobs.alerted_state` records what was sent; severity is read off `HIGH_PRIORITY_STATES`, so there are two ranks and no third | one page per episode is inverted for a *gauge*: `box-disk` paged "BEHIND for over 1h" at priority `default`, then free space fell to 1 GiB, then `statvfs` failed (FAIL) — **no further push, and no high-priority push ever**. BEHIND is the early warning; FAIL is the event. Reading the rank off the same table as the `Priority` header is what stops the two disagreeing, which was the defect. Two ranks IS the bound: nowhere above rank 2 to go, so worsening cannot storm. Deliberately **not** held by the cooldown (a strictly worse state is a different fact, and it is already bounded at one per paged episode) but it **stamps** `last_paged_at`, so the next episode's first page moves out a full cooldown |
| a **recovery** names `alerted_state`, not the latest non-OK state | `about` reads the newest non-OK transition, so an episode paged as "BEHIND for over 1h" that later touched FAIL recovered as "FAIL → OK" — a resolution for an alert that was never sent, which on a phone reads as a page you missed |
| **the hard ceiling**: an open episode past its threshold and unpaged pages on **every** pass, whatever the job currently reads — the not-OK branch, the unverifiable-OK hold, the pass that gives that hold up, and the dwell | every branch that can end an episode is a way to lose its page. See "Both holds are bounded" below for the two reproductions that came from scoping it to one branch |
| an **unverified** OK does not end the episode at all (the *hold*) | see below |
| an unparseable or **future** `bad_since` is healed — clock restarted, `alerted_at` dropped | an NTP step backwards makes `now − bad_since` permanently negative: a job that can never page |
| an unparseable or **future** `last_paged_at` is refused **and** healed to NULL | the same trap on the cooldown's column: a negative age is a cooldown that never expires. Refused in `_cooling_until` (which runs before the healing loop) and cleared in the loop, so neither order can trust it |
| a failed ntfy POST returns `last_paged_at` as well as `alerted_at` | half a rollback is worse than none: the episode gets its page back and then cannot spend it for six hours, because a POST that never reached anyone still looked like a page to the rate limiter |
| a failed **escalation** POST is rolled back to the `(alerted_at, alerted_state)` pair it replaced, not to NULL | NULL means "this episode has never paged", so the retry would re-send the episode's FIRST page as well. Restoring the pair leaves the escalation still due (the worse state is still worse), i.e. delayed and never cancelled. The retry backoff is keyed on the episode **and the rank**, so an escalation seconds after a successful first page is not mistaken for a retry of it and held 5 min |
| `alert_after_s` is capped at **30 days** at parse time, loudly, and `probe.interval_s` at **24 h** on every kind | magnitude was the one hostile input the validator accepted; one extra digit silently means "never page" / "never look again". Say `alert: never` if that is what you mean |

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
  **This hold covers only the sub-case where the probe is failing RIGHT NOW** (`Episode.damped_failure` reads
  the newest probe row), so a destination that genuinely lists once an hour and fails the rest of the time
  clears it, ends the episode, and restarts the clock — issue #15, measured at **zero pushes/day** for three
  real patterns. That is what the accumulator below closes; the hold and the accumulator are two halves of
  the same sentence and neither covers the other. `Episode.damped_failure` now has direct tests
  (`test_damped_failure_is_set_only_for_the_self_job_and_only_while_a_probe_fails`,
  `test_a_damped_failure_holds_an_episode_about_any_state`); it previously had none, because every damping
  test reached it through a 1-cycle good run that the hold survives either way.

Both holds are bounded, and the bound has two halves:

- **THE HARD CEILING — the invariant the whole episode model rests on:**

  > **While an episode is open (`bad_since` set), past its threshold, and not yet paged — page. Regardless of
  > what the job's current state reads.**

  `Core._page` is therefore called on **every** pass over an open episode: from the not-OK branch, from the
  held-open-by-an-unverifiable-OK branch *including the pass that gives that hold up*, and from the dwell.
  It names the state the episode is really about (`snap: STALE_DEST for over 1h`, not the OK on the card).
  The rule has no exceptions and no windows, and that is the point: the two structural bugs found in review
  were both a ceiling scoped to one branch, so the episode could end from another branch without ever
  speaking. Scoped inside the hold it was live only while `began + A ≤ now < since + A` — a window exactly as
  long as the job was *observed* not-OK — so a destination 30 days stale whose probe went blind five seconds
  in, heartbeat still arriving, produced **zero pushes** and then closed itself. The dwell had the same hole
  from the other side: any recovery longer than `min(5 min, A/10)` ended the episode, so a container 19 min
  down / 3 min up against a 20 min threshold was broken 86% of the time, indefinitely, and paged nothing.
- **`ok_hold_s` = `max(5 min, alert_after_s)`.** After that much continuous OK the episode closes **silently**
  (no recovery — nothing was verified fixed). Held for ever, a destination that can never be probed again (a
  revoked remote, rclone's shared Drive OAuth client being retired) would pin `alerted_at` and mute every
  later failure of that job, including the backup dying outright. At most one threshold is spent on a
  destination we cannot see.

**The ceiling's deliberate consequence:** a job that is broken for at least its threshold and *then* recovers
now produces a page **and** a recovery, sometimes seconds apart. That is correct — it crossed the bar Graham
set, so the gap is real news, and saying so late beats not saying it — and it is rare by construction: the job
has to outlive its whole threshold and then recover inside one dwell.

**The cost of that, which the cooldown exists to pay:** a job whose outages *each* exceed its threshold pages
once per outage, for ever. Measured over a simulated day at `box-containers`' real 20 min threshold: 19 min
down / 20 min up produced **132 pushes/day**, and a 30-min crash-loop cycle 48/day. "A genuinely broken
container reported once per genuine outage" is a fair description and still an alert storm — the exact shape
this feature exists to remove, relocated from "every transition" to "every episode". The answer is NOT a
longer threshold on that job: that re-opens the silence window the hard ceiling just closed, so a container
down for an hour would go quiet again. It is the per-job cooldown, which caps the RATE without touching the
bar. Same pattern after: 47 episodes/day → **4 pages + 4 recoveries**, because a held-back page is never
stamped and therefore never recovers either.

**The cooldown caps PAGES, not pushes, and the difference is stated rather than implied.** Two things ride
past it by design, and both are bounded by the episode rather than by the clock: a **recovery** goes out for
every episode that paged, and an **escalation** at most once per paged episode. So the worst case per job is
page + escalation + recovery per cooldown window — **12 pushes/day at the 6 h floor** — and reaching it takes
a job that crosses its threshold, gets strictly worse, and then genuinely recovers, every six hours, for ever.
Measured on a gauge hovering 3 h low / 1 h healthy for a week: **28 pages + 28 recoveries = 8 pushes/day**,
i.e. twice what "one page per 6 h" sounds like. **Recoveries are deliberately NOT capped**: one only ever
follows a page that was sent, so their rate is already the cooldown's, and dropping them would leave an alert
on the phone with no resolution — which reads as "still broken" and is a silence of its own.
`test_the_worst_case_push_rate_per_cooldown_window` asserts the bound.

**That 12/day is PER JOB**, which is worth spelling out because the incident being fixed was ~11 pushes/day
from *one* job. Fleet-wide the ceiling is `3 × 86400 / cooldown_s` summed over the jobs that alert: on the
shipped file, **82 pushes/day** across 13 alerting jobs — five sit at the 6 h floor and contribute 12 each
(60), and the eight with day-or-longer thresholds contribute 22 between them (3 each, except `mac-probe`'s
72 h threshold at 1). The Inbox release moved that figure from 70/9: it added four alerting jobs
(`inbox-github-sync`, `hopper-dashboard-backup`, `inbox-transcribe`, `inbox-backlog`), every one of them at
a 24 h threshold and therefore 3/day, precisely because none of them is urgent enough to earn a sub-6 h
threshold. That is the arithmetic worst case, with every
job simultaneously past its bar, worsening and recovering round the clock for a whole day; it is a bound, not a
forecast (the measured single-job figure above is 8/day). Asserted over the shipped file by
`test_the_fleet_wide_push_ceiling_is_the_sum_over_the_alerting_jobs`, so adding a fast-threshold job moves the
number in the doc rather than surprising anyone.

**Residuals, stated rather than hidden:**

- **The ceiling bounds the *window*, not the silence.** A new failure that lands while a hold is still
  running joins the still-open episode and gets no push of its own for as long as that episode lasts. That is
  correct under one-page-per-episode — the job was never verifiably OK in between, and it did page once — but
  it is the sharp edge. `dest.probe_error` on the card and `dashboard-probes` going FAIL are what name a dead
  probe.
- **The cooldown makes that window longer for the three jobs it binds on.** A second, genuinely different
  outage inside the same six hours is silent: `box-containers` pages for the tunnel dying at 09:00 and says
  nothing about `km-tracker-app-1` dying at 10:00. The board shows both; the phone hears one. Deliberate —
  they are the same job and the same fact ("a container is down") — but it is a rate limit on a pager and it
  does cost information, not just noise. It is **no longer flat across severity**: an episode that gets
  strictly worse (default-priority → high-priority) escalates through the cooldown, once. A worsening that
  stays inside one rank (LATE → FAIL is rank 1 → 2 and escalates; STALE_DEST → FAIL is 2 → 2 and does not)
  still pages once for the first of them.
- **The dwell makes every threshold SOFT by up to one dwell, in the paging direction**, and the accumulator
  makes it soft by up to one window's worth of *unpaged* not-OK time. The ceiling measures the episode, and an
  episode includes the dwell it is serving out — so a job broken for `alert_after_s − dwell` that then recovers
  still crosses the bar and pages. For `box-containers` that is a container down for 10 minutes paging against
  a 20 minute threshold. On top of that, a job that flapped unpaged earlier in the window reaches the bar that
  much sooner: five 5-minute blips make a later 6 h outage page at 5 h 35 m
  (`test_sustained_failure_after_flapping_still_pages` pins exactly that). Both err toward paging, both are
  rate-limited by the cooldown, and the second is bounded by *unpaged* badness specifically — anything that
  already paged runs into the cooldown instead of bringing the next page forward. The alternative, narrowing
  the ceiling to count only not-OK time, is what #13 item 4 rejected: it can only ever page later.
- **"Bad more than half the time" is the shorthand, not the arithmetic** — the window is exactly `2 ×
  alert_after_s`, so the bar is *half* the window and the comparison is `>=`. A job whose failures register on
  the spot and which is bad exactly half the time therefore sits ON the bar and pages: measured at a 6 h
  threshold, a 50% duty cycle pages **7.5 pushes/day** at a 1 h flap period. The same duty cycle on the DAMPED
  self-job pages **nothing** at short periods — the damping eats the first cycles of every down-span, putting it
  just under — until the period approaches the window, where two partial down-spans fit inside one window and it
  pages again (**3.75 pushes/day** at a 42 000 s period against the 43 200 s window). That period sensitivity is
  inherent to any fixed-window accumulator, it is bounded by the per-job cooldown either way, and it is the
  reason the shorthand should not be read as an exact duty-cycle threshold.

**The ceiling vs. the machine-offline rule.** The ceiling asks `_suppressed_offline` like every other page, so
a sibling's LATE episode that is muted behind a sleeping Mac stays muted — including for the minutes its
episode stays open after the sibling recovers (the dwell). That span is covered by `Core._returning_probes`:
the mute lasts while the *probe job's own* LATE episode is still open, which is bounded by the probe's dwell
and cannot outlive the sibling's, since the siblings are pinged first and `mac-probe`'s dwell is the longest
any job can have (the cadence term saturates at the 15 min cap, and its 1 h cadence reaches it). That last
clause used to read "no dwell exceeds 5 min", which the cadence-aware dwell made false; it is now asserted
directly (`test_the_machine_probes_dwell_is_never_shorter_than_its_siblings`) rather than argued.

**The mute may only borrow an alert that EXISTS**, and that is now two checks, not one. A probe with
`alert: never` voids the rule (otherwise a Mac gone for a week pages nothing at all) — and so does a probe
whose own page is currently held back by its cooldown, which is the same condition made temporary. Without
the second check, a Mac that dies a few hours after its probe last paged is: the probe silent on its
cooldown, every sibling muted behind it, a real multi-day outage and zero pushes. Unreachable on the shipped
file (`mac-probe`'s 72 h threshold is above the 6 h floor, so its cooldown can never bind) and reachable the
moment anyone shortens that threshold, which is not an edit anybody would expect to silence a machine.
Without it, every weekend with the lid shut would end in a `drive-mirror: LATE for over 1d` push the moment
the Mac woke — the exact noise the machine-offline rule exists to prevent. A sibling that comes back and is
*still* broken (or breaks again) is not a plain `LATE → OK` and pages normally.

**Muted time is not a cooldown — the accumulator's one real residual, corrected here after a review found the
previous claim false.** The accumulator's early fire is bounded by *unpaged* badness, because anything that
paged runs into the cooldown instead. A **suppressed** page deliberately stamps nothing (not `alerted_at`, so
the episode keeps its page; not `last_paged_at`, so no cooldown starts), which is exactly right for the mute
and makes muted time the one kind of unpaged badness with nothing behind it. The per-STATE rule above stops
muted LATE counting toward a page about a *different* state; it does **not** stop it counting toward a later
**LATE** page, because the mute is a function of the probe's state *now* while `not_ok_seconds` reads across
episode boundaries. "The same mute still gags it" was the claim, and it is false as soon as the machine is
back.

Reachable on the shipped file, and measured on `drive-mirror`'s real numbers: a 40 h Mac sleep accrues ~25 h of
muted LATE, the episode closes silently, and a fresh LATE 15 h later pages **the instant it opens** with
`LATE for over 1d in the last 2d` instead of a day in
(`test_muted_late_time_can_still_bring_a_later_late_page_forward`). The condition is
`alert_after_s > the job's own LATE onset` — `cadence_s + grace_s`, or `DISK_METRIC_MAX_AGE_S` for a gauge —
because otherwise the earlier badness has aged out of the 2× window before a new episode can even begin. On
the shipped file **three** jobs are over that line: `drive-mirror` (24 h against a 15 h onset) and, since
the Inbox release, `inbox-transcribe` (24 h against 14.1 h) and `inbox-backlog` (24 h against 15 h — in fact
numerically identical to `drive-mirror`). The two new ones widen this residual in `drive-mirror`'s exact
shape, and deliberately: a Mac job's ~14 h grace is mandatory (below it a missed run cannot be told apart
from a sleeping Mac), so the only way to stay under the line would be a threshold shorter than 14 h, which
would double each job's share of the fleet push ceiling to buy urgency neither of them has. `pa-backup`
(6 h against 38 h) and `mac-disk` (1 h against 48 h) are not over it, box jobs cannot be muted at all because
`_machine_probe("box")` is `None`, and the four `manual` Mac jobs never page.
`test_which_shipped_jobs_can_have_muted_time_brought_forward` pins that set, so a `jobs.yml` edit that adds
another one fails loudly instead of quietly widening this. **It is not silence** — the job really is in that
state, the seconds counted really were never reported, and it is still one page per episode — so it is left as
a documented residual rather than "fixed" by discarding accrued badness, which would push in the one direction
this feature may not go.

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
A **lone** `alert_after_s: null` (or `alert: null`) is an **error** for the same reason: it escapes the
presence check because there is nothing to be exclusive with, and then resolves as if the key were absent —
which on an informational job is `never`, a permanent silence filed under a key whose name says otherwise.
An explicit `alert_after_s` wins even on an informational job, so there is always a way to alert on one.
**`informational` now means what it always claimed.** `registry.py` documented it as "shown, never alerted
on" and *nothing consulted it* — such a job pushed on every transition like any other. It is now resolved into
the alert policy at parse time rather than being a second, overlapping concept.

**`alert_after_s` is NOT the time to a page.** The threshold clock starts when the job goes not-OK, which for
a scheduled job is already `cadence_s + grace_s` after its last good run — and for the long-grace Mac jobs the
deadline is the bigger half. End to end: `km-backup`/`todoist-points-backup` **24.3 h**, `box-containers`
**35 m**, `box-disk`/`mac-disk` **1 h** (capacity breach; 49 h for a gauge nothing feeds),
`dashboard-probes` **6.3 h**, `mac-probe` **87 h**, `pa-backup` **44 h**, `drive-mirror` **39 h**,
`inbox-github-sync` **24.5 h**, `hopper-dashboard-backup` **24.3 h**, `inbox-transcribe` **38.1 h**,
`inbox-backlog` **39 h**. Every job in `jobs.example.yml` states its own figure on a `# TIME-TO-PAGE:` line,
and `test_every_alerting_job_states_its_real_time_to_page` recomputes all thirteen from that same file —
four of those comments used to describe the threshold as if it were the wait, wrong by between 15 minutes
and 38 hours.

**Shipped thresholds** (`jobs.example.yml` carries the per-job rationale):

| job | `alert_after_s` | why |
|---|---|---|
| `km-backup`, `todoist-points-backup` | 86400 (24 h) | 5-min cadence: one missed run is nothing, a day is a gap in the history |
| `pa-backup` | 21600 (6 h) | **NOT 30 h.** Its LATE deadline alone is 38 h (cadence 86400 + grace 50520), so the old 108000 — commented "30 h means a whole night was genuinely missed" — actually paged at **68 h ≈ 2.8 days**, the worst miss in the file against Graham's stated bar of "backups missed more than 24 hours". 6 h on top of 38 h pages at 44 h. The 38 h is a hard floor (below it a missed backup is indistinguishable from a sleeping Mac) and a shorter threshold could not cause a false page anyway: while the Mac sleeps `mac-probe` is LATE and this job is muted entirely |
| `box-containers` | 1200 (20 m) | a container down IS the outage; long enough to ride out our own deploys and a reboot's restart storm |
| `dashboard-probes` | 21600 (6 h) | the job that flapped 27× in four days; six hours means the checks have really stopped — **or have stopped for six of the last twelve hours** (the accumulator; checks that stop and restart on the hour used to page never, which is exactly what a rate-limited Drive looks like) |
| `drive-mirror` | 86400 (24 h) | pending uploads clear themselves once the Mac is awake |
| `mac-probe` | 259200 (72 h) | with its 15 h LATE deadline ≈ 87 h: a weekend with the lid shut pages nobody, a dead Mac pages once. **Never `alert: never`** — see below |
| `box-disk`, `mac-disk` | 3600 (1 h) | the gauge already has a 48 h fuse of its own (`DISK_METRIC_MAX_AGE_S`), so a day on top would mean hearing about a dead gauge at 72 h; and a capacity threshold is a *level*, not a flap — an hour only rides out a reading hovering at the boundary |
| `inbox-github-sync`, `hopper-dashboard-backup`, `inbox-transcribe`, `inbox-backlog` | 86400 (24 h) | the Inbox's four. None is urgent, and all four fail INVISIBLY — a dead mirror keeps rendering the issues it last saw, a dead transcription worker leaves every voice note reading "transcribing…", and an unmonitored backup is the exact thing this app exists to catch. A day is the right bar for each. They are the reason the fleet ceiling moved 70 → 82 |
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
  FAIL/STALE_DEST. Three shapes, and no free text ever leaves the box (the `reason` can carry container names,
  client notes or rclone stderr, and stays on the board):
  | | body |
  |---|---|
  | alert (continuous) | `box-containers: FAIL for over 20m` |
  | alert (accumulator) | `dashboard-probes: FAIL for over 6h in the last 12h` |
  | escalation | `box-disk: BEHIND → FAIL` (at FAIL's priority) |
  | recovery | `box-disk: FAIL → OK` (naming what was PAGED) |

  The accumulator's wording is not cosmetic: "FAIL for over 6h" for a destination that was broken for six of
  the last twelve hours overstates what was seen, and an alert that overstates is an alert you learn to
  discount.
