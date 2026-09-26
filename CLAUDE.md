# CLAUDE.md — hopper-dashboard

Jobs & backups dashboard for Graham's self-hosted estate: one page + one JSON endpoint answering, for every
scheduled or on-demand job, what it protects, how, last run, last success, whether the destination actually has
fresh bytes, and how far behind manual jobs are. Read `DESIGN.md` first — it is the spec and the frozen API
contract (the "State precedence (as implemented)" section there is the authoritative state-machine spec).

## Stack
Python 3.12, Flask 3.1.3, SQLite (WAL), PyYAML for `jobs.yml`, gunicorn, rclone inside the container for
destination probes, ntfy for alerts. No CDN assets, no webfonts, and one inline script per page (the
`<time datetime>` localizer in `base.html`; `/inbox` adds an external `static/inbox.js`) allowed by a
per-request CSP nonce — `default-src 'self'; script-src 'nonce-…'`. Docker + compose on the box behind the
existing `km-tracker` Cloudflare tunnel.

**TWO SQLite files, with different rules.** `dashboard.db` has ONE request-path writer, the ingest role; the
read role's request-path connections are `PRAGMA query_only=ON`, so a stray write raises `SQLITE_READONLY`
rather than being discouraged by a comment. `inbox.db` (added with `/inbox`) is a separate file with two
in-container writers — the read role for browser writes, the ingest scheduler for the GitHub mirror and the
audio prune — coordinated by WAL. Do not "unify" them; DESIGN.md records why.

**`APP_PASSWORD` is THIS APP'S OWN password, not the shared house word** (changed 2026-09-19). It used to be
the same word as km-tracker / todoist-points / taste-twin / jjho / baby-pool, copied verbatim from
`~/km-tracker/.env`. It is not any more: that word has been given to friends and the board is linked from
the public apex page, which stopped being acceptable when `/inbox` began storing recordings of Graham's
voice. The gate CODE is unchanged — this is a value in the box `.env` only (DEPLOY.md §1c), so nothing
enforces it and the only check is trying the house word at `/login` and seeing it refused.

## Two roles, one container
`create_app(role)` builds either app; both load the same `jobs.yml`, share `data/dashboard.db`, and both run the additive schema migration for `data/inbox.db` at start-up (under `BEGIN IMMEDIATE`, duplicate-column-tolerant — `entrypoint.sh` starts both gunicorns together, so they race).

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
.venv/bin/python -m pytest -q                         # ~530 tests, no network, ~15 s
/usr/bin/python3 -m pytest -o addopts="" tests/test_probes_*.py -q   # ~100 probe tests, MUST pass stdlib-only
/usr/bin/python3 -m compileall -qf probes/             # 3.9 syntax gate (CI also RUNS the probe tests on 3.9)

cp jobs.example.yml jobs.yml                          # local only; gitignored
export APP_PASSWORD=devpass SESSION_SECRET=devsecret INGEST_TOKEN=devtoken READ_TOKEN=devread
# (APP_ENV=dev to run with the gate off; prod refuses to start without APP_PASSWORD)
.venv/bin/python -m dashboard                         # read http://127.0.0.1:8080  ingest :8081
# env knobs: DASHBOARD_DATA (default ./data locally, /app/data in the image), JOBS_FILE, READ_PORT,
#            INGEST_PORT, DASHBOARD_BIND, PROBE_INTERVAL_S, TICK_INTERVAL_S, DASHBOARD_NO_SCHEDULER=1,
#            DASHBOARD_RCLONE_TIMEOUT_S (240 — NOT RCLONE_TIMEOUT*, that namespace is rclone's own),
#            PROBE_FAIL_THRESHOLD (2), PROBE_NO_SUCCESS_S (3600)
# Inbox knobs (all optional; empty INBOX_TOKEN = the machine endpoints fail closed, browser side still works):
#            INBOX_TOKEN, INBOX_AUDIO_MAX_BYTES (2 MiB/note), INBOX_AUDIO_MAX_TOTAL_BYTES (3 GB tree),
#            INBOX_AUDIO_RETENTION_DAYS (90; audio also goes unconditionally at 2x that age),
#            INBOX_GITHUB_REPOS (validated at startup), INBOX_GITHUB_TOKEN, INBOX_GITHUB_INTERVAL_S (900),
#            INBOX_PRUNE_INTERVAL_S (3600)

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
  `probe.interval_s` (optional per-job probe cadence) is capped twice: semantically at half the freshness
  window on db_snapshot, the one kind whose STALE_DEST verdict reads the probe's newest-object time, and by
  magnitude at `MAX_PROBE_INTERVAL_S` (86400) on **every** kind. The second cap is not redundant — the
  semantic one does not apply to `rclone_copy_tree`/`manual`, which are precisely the two DEPLOY.md §1d has
  the operator hand-edit, and `interval_s: 18000000` there means 208 days of never looking with nothing on
  the board to show for it.
  `disk` is a kind but NOT in `SCHEDULED_KINDS`/`PROBEABLE_KINDS`: it is a capacity gauge (`disk:` block,
  `min_free_bytes` / `max_used_pct`), never probed, no cadence, and `informational` when both thresholds
  are omitted — same rule as a thresholdless `manual` job. It has no *per-cadence* dead-man's switch, but
  it is not exempt from silence: see `state.DISK_METRIC_MAX_AGE_S`.
  **Alert policy** (`alert_after_s: N` | `alert: never`, mutually exclusive *by key presence*) is resolved
  HERE, once, into `Job.alert_after_s` / `alert_never` / `alert_source` — `never` > explicit `N` >
  `informational` > `DEFAULT_ALERT_AFTER_S` (86400). Nothing downstream re-decides what an absent key means.
  **`informational` and `alert_never` are NOT the same question and the UI must never conflate them** —
  `minecraft-offload` is `alert: never` AND carries `manual:` thresholds, so `informational` is False and a
  board caption keyed on it skipped the one job whose resting state is BEHIND. Caption off "will this job
  ever page" (`j.alert.never`), and state the capacity thresholds from the thresholds. Same rule in the disk
  gauge macro, where the two used to be read off one `if`.
  `informational` is no longer a broken promise: it feeds this resolution instead of being a second,
  overlapping concept nothing consulted. `alert_after_s` is capped at `MAX_ALERT_AFTER_S` (30 d) — magnitude
  is the one hostile input this validator would otherwise accept, since one extra digit silently means
  "never page". A **present-but-null** `alert` / `alert_after_s` is an ERROR, not a resolution: alone it
  escaped the presence check (nothing to be exclusive with) and fell through to what an ABSENT key means,
  which on an informational job is `never` — silence filed under a key that reads like a threshold.
  Every schema string is also rejected if it contains a **control character**: `job.name` is the only free
  text that leaves the box (the ntfy `Title` header), and a CR/LF in it makes `http.client` refuse the POST
  for ever — not an injection (zero bytes reach the socket) but a job that can never page and never spend
  its page, which is worse. Loud at parse time; the container refuses to start, naming the job.
- `db.py` — schema (`jobs` incl. `created_at`, `bad_since`, `alerted_at`, `alerted_state`, `last_paged_at`;
  `runs`, `probes`, `state_changes`), WAL connection, all queries, ISO helpers (`from_iso` clamps to
  1970..9999 and never raises). `not_ok_seconds` reconstructs a job's state timeline from `state_changes` for
  the alert accumulator — derived, not stored, for the same reasons `probe_fail_streak` is (nothing to
  migrate, unforgeable by INGEST_TOKEN), and walked by rowid (`sc_job_seq`) so a clock step cannot reorder it.
  Accuracy is **measured, not assumed** (the docstring used to overclaim): exact for a monotone timeline,
  under-counting only where history is missing, over-counting by <1 s per span because ISO stamps are whole
  seconds, and inflatable by an *oscillating* clock up to the window itself — bounded in every case by
  `end - start`, i.e. twice the bar it is compared against, so the worst case is a page one window early.
  **"Which probe row is newest" is decided by `id` (insert order), never by `probed_at`** —
  `last_probe`, `last_ok_probe`, `oldest_probe`, `probe_fail_streak` and `failing_probe_job_ids` all order by
  id, and `probes_job_seq` is the index for it. `probed_at` is the writer's wall clock, so one row written
  while the clock was ahead outranks every real probe after it for ever: the newest row reads `ok`, the
  damped-OK hold switches off, and PR #8's damping can never un-damp. Clamping at insert cannot fix that (at
  insert time the value IS now), and `prune` already keeps rows by `id DESC`. Durations are still measured
  from `probed_at`; only row selection changed. Additive `jobs` columns go in `JOBS_COLUMNS` **and** `SCHEMA`; `init_schema` migrates under
  `BEGIN IMMEDIATE` with a duplicate-column-tolerant `_add_column`, because `create_app` runs it for BOTH
  roles and `entrypoint.sh` starts every gunicorn together — the loser of that race used to kill a worker
  and restart-loop the container. Don't "simplify" either guard away.
- `state.py` — pure state machine: `compute_state(job, Facts, now)`, `lag_info`, `dest_info`, `disk_info`
  (capacity block for `kind: disk`; `used_pct` is None on a 0/missing total — never a ZeroDivisionError),
  `db_snapshot_stale` (dedup-aware), `copy_tree_stale` (missing vs differ), never-pinged → LATE via
  `Facts.created_at`. Unit-tested with a fixed clock. The `disk` branch has three rules worth knowing
  before touching it: a `fail` ping outranks the stored figures (**compared against `last_metrics_at`**,
  because a successful capacity ping is `status: metric` and therefore never a run — without that
  comparison one transient `statvfs` error pins the card to FAIL for ever); a reading older than
  `DISK_METRIC_MAX_AGE_S` (48 h) is LATE, which is the only signal for a feeder that stopped on its own
  while its machine's probe job kept reporting OK (and no reading at all more than
  `DISK_FIRST_READING_GRACE_S` = 6 h after registration is LATE too — the feeder that never STARTED; both
  time comparisons fail safe on an unparseable timestamp, and negative capacity metrics are dropped); and `_num` caps metric magnitude as well as rejecting
  non-finite values (a numeric *string* metric bypasses the ingest ceiling).
  `used_pct` is `(total-available)/total`, so it does NOT match `df`'s `Use%` — the free bytes do. Say so
  rather than "fixing" it; DESIGN.md → `disk` and `probes/common.disk_free` carry the measurement.
- `services.py` — `Core`: record ping → shallow-merge metrics → recompute ALL jobs → persist transitions →
  run the **episode pass** → notify.
  **Alerting is by EPISODE, not by transition** (DESIGN.md "Alerting rules" is the spec; read the module
  docstring before touching any of it). A job pages once, after it has been not-OK for its own
  `alert_after_s`, and recovers only if that page went out. Four `jobs` columns hold it: `bad_since` (episode
  start; `jobs.since` cannot serve, it resets on every state change), `alerted_at` (one page per episode),
  `alerted_state` (WHAT that page said) and `last_paged_at` (the per-job COOLDOWN, which spans episodes — see
  below).
  **THE EPISODE IS NOT FLAT** — two things it could not express were each a silence, and both fixes live in
  `_page`:
  *"this has been bad a lot"* (`_past_bar` + `bad_window_s`, issue #15). An episode is past its bar
  **either** continuously for `alert_after_s` **or** cumulatively: `alert_after_s` spent in the state being
  paged about, inside the last `2 × alert_after_s`. Without it a destination that failed for an hour and
  listed once every quarter-hour restarted its 6 h clock for ever — measured at **zero pushes/day** while
  failing 73-90% of probes, which is exactly what a rate-limited Drive looks like. Three things make it safe
  and all three are load-bearing: it is an **OR** beside the clock (a rule that counts only not-OK seconds can
  only page LATER — that is how #13 item 4's two attempts each produced a silence bug); it counts **one state,
  not any badness** (counting everything laundered a sleeping Mac's deliberately-muted sibling LATE into an
  instant page for an unrelated BEHIND); and the cooldown below still caps the rate.
  **Known residual, don't re-close it in the silence direction:** per-state does NOT stop muted LATE time
  counting toward a *later LATE* page — a mute reads the probe's state now and stamps nothing, so muted time is
  unpaged badness with no cooldown behind it. Reachable on `drive-mirror` alone (its 24 h threshold is above its
  15 h LATE onset); two tests pin both the behaviour and the shipped set.
  *"this got worse"* (`alert_severity` + `alerted_state`, issue #16). An episode that has paged pages ONCE
  more if its state gets strictly worse, at that state's own priority. Severity is read off
  `notify.HIGH_PRIORITY_STATES` rather than invented, so the rank and the `Priority` header cannot disagree —
  that disagreement WAS the bug (a disk paged "BEHIND for over 1h" at `default`, then filled, then went
  unreadable, and no high-priority push was ever sent). Two ranks is the bound. It deliberately ignores the
  cooldown (a worse state is a different fact, and it is already one-per-paged-episode) but stamps
  `last_paged_at`. The **recovery names `alerted_state`**, not the latest non-OK state, or an episode paged as
  BEHIND recovers as "FAIL → OK" — a resolution for an alert nobody received.
  **Dispatch is LEVEL-triggered** — `_resolve_alerts` walks EVERY job on EVERY recompute, because "still
  FAIL, now past six hours" is the event and it is not a transition. `recompute_all` and `record_ping` are
  two entry points into the same `_recompute_pass`; keep them that way.
  **The governing rule: every ambiguity resolves toward paging, never toward silence.** Transition-paging was
  accidentally self-healing (the next transition re-paged); one page per episode removes that net. So: a
  failed POST hands the page back keyed on **episode identity**, never on "is the job not-OK now" (the dwell
  and the hold both keep an episode open while the job reads OK — a state-based guard skips exactly the case
  it was written for); an episode closes only after a **verified** OK holds for `ok_dwell_s` — which is
  `max(min(5 min, A/10), min(2 * cadence_s, 15 min))`, i.e. **at least two of the job's OWN samples**, because
  a dwell below the sampling interval is decided by one observation (`box-containers` is posted every 300 s
  and its threshold-derived dwell was 120 s, so a container crash-looping at 50-80 % down paged NOTHING); an
  *unverified*
  OK does not close it at all (`state.ok_is_unverified` for a destination that could not be checked, and
  `db.failing_probe_job_ids` for `dashboard-probes` reporting `ok` while the damping below suppresses a real
  probe failure); a future/unparseable `bad_since` is healed; UNKNOWN clears the episode without recovering.
  **THE HARD CEILING, which every hold is bounded by: while an episode is open (`bad_since` set), past its
  threshold and not yet paged, it pages — regardless of what the job's current state reads.** `_page` is
  called on EVERY pass over an open episode (not-OK; held open by an unverifiable OK, including the pass that
  gives that hold up; and the dwell), never inside one branch — a ceiling scoped to one branch was two
  separate blockers, because the episode could then end from a different branch without ever speaking. The
  second bound is `ok_hold_s`, after which the episode closes silently; without it a destination that can
  never be probed again pins `alerted_at` and mutes every later failure of that job. Accepted consequence,
  documented in DESIGN.md rather than re-suppressed: an outage that outlives the threshold and *then* recovers
  sends a page and a recovery seconds apart. When adding anything here, the question is never "is the job
  OK?" but "is the EPISODE still open?"
  **THE PER-JOB COOLDOWN** (`cooldown_s` = `max(alert_after_s, 6 h)`, persisted in `jobs.last_paged_at`): one
  page per episode says nothing about how often an episode may RESTART, and a container on `restart:
  unless-stopped` backoff restarts one every ~20 min for ever (measured: 132 pushes/day). It **delays** a page
  and can never **cancel** one — `_page` returns WITHOUT stamping `alerted_at`, so the episode keeps its
  unspent page and the ceiling re-offers it every pass; the moment the cooldown expires a job still (or again)
  past its threshold pages. **If you ever make that branch stamp anything, you have built the permanent
  silence this whole file is organised against.** It binds only on a job whose threshold is under the 6 h
  floor (shipped: `box-containers`, `box-disk`, `mac-disk`); above it a new episode already takes longer than
  the cooldown to reach its own threshold. Persisted, not in memory, because a deploy is exactly what makes
  containers flap. The failed-push rollback returns `last_paged_at` with `alerted_at` — half a rollback lets
  one 429 buy a whole cooldown of silence — and an unparseable or future `last_paged_at` is refused and
  healed, for the same reason `bad_since` is.
  **THE RETRY BACKOFF** (`ALERT_RETRY_MIN_S`, 5 min, `_retry_due`/`_note_attempt`) spaces *retries* of a page
  whose POST failed; a first page, and a first escalation, are never delayed. It is keyed on the episode **and
  the severity rank**, held as ONE entry per job with a timestamp per rank — not one slot per job with the rank
  folded into the key, which is what it was: folded, `bs@1` and `bs@2` evict each other, so a state oscillating
  across the rank boundary reads every pass as an untried page and the backoff stops applying (measured 60
  attempts/hour against 12). Per rank the ceiling is 2 attempts per window, ≤ 24/hour. In memory on purpose — a
  restart retries sooner, which is the safe direction — and single-process only because ingest runs
  `--workers 1`.
  Also the **machine-offline rule** ( sibling `→ LATE` alerts muted while the machine's `probe` job is
  LATE, and sibling plain `LATE → OK` recoveries muted while the probe is still LATE, recovers in the same
  batch, **or is still inside its own LATE episode** (`_returning_probes`) — the Mac probe posts its sub-jobs
  before its own heartbeat, so siblings recover first, and the hard ceiling made that gap last for the
  sibling's whole dwell rather than one batch: without the third clause every lid-shut weekend ends in a
  `drive-mirror: LATE for over 1d` push the moment the Mac wakes;
  FAIL/STALE_DEST/BEHIND after LATE always alert). It is **void when the machine's probe job can never page**
  (`alert_never`) **or while the probe's own page is held back by its cooldown** (`_cooled_probes`): the rule
  mutes siblings on the premise that the probe sends one alert for the machine, so without those guards a Mac
  gone for days pages nobody at all. "Suppression may only borrow an alert that EXISTS" is one static check
  and one for the moment. A suppressed alert must also never stamp
  `alerted_at` — it would burn the episode's one page and lose its recovery with it. `run_probe_cycle` (probes the jobs `due_probes()` says are
  due + the `dashboard-probes` self-heartbeat + `prune` — one `DELETE … NOT IN (… ORDER BY id DESC LIMIT n)`
  per table, not O(n²)). **Flap damping** (`probe_trouble` / `_classify_trouble`): the self-job records `fail`
  when any probed job is tripped — `hard` (error is not a quota/timeout: first failure, never damped),
  `streak` (`settings.probe_fail_threshold`, default 2, *consecutive failed probes of that job*), or
  `silence` (no successful probe of it for `PROBE_NO_SUCCESS_S`, default 3600 s — the backstop: damping may
  delay an alert, never cancel one). Recovery is one successful probe of the offending job. The streak is
  **derived from the `probes` table** (`db.probe_fail_streak`), NOT stored: per-job (so a job with
  `interval_s` is not reset by the cycles it wasn't due for — that bug meant it could never alert at all),
  restart-safe, and unforgeable through ingest, which cannot write `probes`. Don't move it back into
  `last_metrics`. Cycle accounting: each cycle has a wall-clock budget (`Settings.probe_cycle_budget_s`,
  deferrals in `metrics.deferred`, `due_probes()` ordered oldest-first so nothing starves) and is stamped and
  recomputed at its **end** (`Core.last_cycle_end`). Full rationale: DESIGN.md "Probe cadence and flap
  damping" — don't lower the threshold to 1, that is the behaviour that produced 27 FAIL→OK flips and ~55
  pushes in 4 days with nothing broken.
- `scheduler.py` — daemon thread; `step()` is exposed for tests; never lets an exception kill the loop. It
  calls `run_probe_cycle` on the global `PROBE_INTERVAL_S`, re-arming from `Core.last_cycle_end` (the
  POST-cycle clock — from the pre-cycle clock an overrunning cycle is instantly due again, which hammers the
  remote that just rate-limited us); per-job `probe.interval_s` is applied *inside* the cycle (dueness from
  the persisted `probes.probed_at`), not here.
- `probes.py` — `rclone lsjson --recursive --files-only` (argv, `DASHBOARD_RCLONE_TIMEOUT_S`, default 240 s —
  90 s was itself a leading cause of probe "failures" on a ~1000-object tree; the effective value is clamped
  to `PROBE_INTERVAL_S` since probes are serial) + state-file reader; `is_transient_error` /
  `TRANSIENT_MARKERS` label a Drive rate limit or timeout in the error text and set `ProbeResult.transient`,
  so quota push-back never reads like a wrong destination. That flag is load-bearing (transient = damped,
  hard = immediate FAIL), so rclone's echoed paths are stripped before matching — both the annotated/quoted
  forms and any bare `/`-containing token, since real rclone prints the failing path unquoted as well — and
  the HTTP statuses (429/503) only match in their real spellings (`Error 429:`, `code 503`, `503 Service
  Unavailable`), never as bare digits. A dated snapshot name like `km_tracker-20260503-0312.db` contains
  `503`; as bare substrings those markers made a permission-denied on an ordinary nightly path look
  transient. Don't classify on raw stderr, and don't re-add a bare status-code substring marker.
- `notify.py` — ntfy `Notifier`, deliberately dumb: it decides nothing about *whether* to page, `Core` does.
  `notify_alert(name, id, state, after_s, within_s=None)` for a sustained problem (`job_id: STATE for over
  6h`, or `… for over 6h in the last 12h` when the accumulator decided it — don't drop that clause, an alert
  that overstates what it saw is one you learn to discount), `notify_escalation(name, id, from, to)` for an
  episode that got worse (`job_id: BEHIND → FAIL`, at the worse state's priority), `notify_recovery(name, id,
  from_state)` for the end of an episode that was paged. `HIGH_PRIORITY_STATES` is the single source for both
  the `Priority` header and `services.alert_severity` — keep it that way. No reason text ever leaves the box;
  never raises; returns False on a failed POST, which `Core` acts on (see below).
- `ingest.py` — blueprint + pure payload parsers (`parse_json_payload`, `parse_form_payload`, `parse_metrics`).
- `web.py` — read blueprint: http→https redirect, gate, host pin, security headers (per-request CSP nonce +
  HSTS), HTML + JSON routes.
- `views.py` — builds the `/api/v1/status` contract and job detail from the store.
- `password_gate.py`, `ratelimit.py` — gate helpers (`client_ip(trusted_cidrs)` vs `remote_ip()`) +
  sliding-window limiters (hard key cap with stalest-eviction, keys truncated to 64 chars).
- `humanize.py` — relative/absolute times, human bytes/durations, `human_gib` (binary GiB, used for disk
  capacity — `human_bytes` is decimal GB) (Jinja filters).
- `templates/`, `static/app.css`, `static/favicon.svg` — theme-aware (light/dark tokens), mobile-first,
  Okabe–Ito state colours always paired with a text label + glyph.

### The Inbox (`/inbox`, added 2026-09-19)
- `inbox_db.py` — the `inbox.db` store: `INBOX_SCHEMA` + an additive `INBOX_COLUMNS` migration in the same
  shape as `db.py`, the queries, and the mirror bookkeeping. `normalise_backlog_key` lives here and is
  DUPLICATED in `probes/backlog.py` (stdlib-only, cannot import Flask); `tests/test_backlog_mirror.py` pins
  the two together — change both or neither.
- `inbox_audio.py` — the audio file store (mime allow-list + a MAGIC-BYTE check, sha256, path built from the
  server-generated id only, never the client filename) and the scheduler's prune + orphan reconcile. The
  prune needs ALL THREE of `transcript_status='whisper'`, `reviewed=1`, and older than the retention. It
  lives here rather than in `inbox.py` because `inbox.py` imports `web` → `views` → `services`, and the
  scheduler importing that would close a cycle.
- `inbox.py` — the blueprint. `MACHINE_ENDPOINTS` is what scopes `INBOX_TOKEN`; the create route raises
  `request.max_content_length` PER REQUEST (the global 64 KB cap in `__init__.py` protects every other
  route and must stay); the Origin/Referer CSRF pin covers POST/PUT/PATCH/DELETE but EXEMPTS a
  bearer-authenticated call, because `curl` sends neither header and every Mac-worker POST would 403 on
  prod while passing every test.
- `github_mirror.py` — `sync(conn, repos, now, fetch)` with `fetch` injected for tests. ETag-conditional,
  skips anything with a `pull_request` key, honours `X-RateLimit-Reset`/`Retry-After` into `backoff_until`,
  and closes items ONLY after a complete, error-free repo fetch (a partial page must never close anything).
- `templates/inbox.html`, `static/inbox.js` — see the JS convention above.
- Scheduler: `Scheduler.step()` carries `last_github` and `last_prune`, each re-armed from its own END clock
  and each in its own try/except, both writing `inbox.db` only and NEVER inside `run_probe_cycle`. A failing
  GitHub sync must not kill the probe cycle or the tick.

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
  work identically without it), PLUS the external `static/inbox.js` that only `/inbox` loads, via a
  `{% block scripts %}` no other template fills. Don't add a third — the CSP nonce is generated once per
  request via `csp_nonce()`, and `tests/test_web.py` asserts the board page still has exactly ONE
  `<script>` while `/inbox` has exactly two, both nonce'd. Note `script-src` is `'nonce-…'` with NO
  `'self'`, so an external `<script src>` works ONLY if it also carries the nonce; do not add `'self'`.
  `inbox.js` uses `textContent`, never `innerHTML`, and the rows are rendered server-side — it only
  shows/hides/reorders DOM that is already there, so the table works with JS off.
- Tests must stay network-free: mock `probes.probe_job` / `subprocess.run` and use `RecordingNotifier`.
  `run_probe_cycle` passes `timeout=` to `probe_job`, so a stub must accept it (`lambda job, **kw: …`).
- Test fixtures pin `jobs.created_at` to 2030 (`conftest.pin_created_at`) so the never-pinged → LATE rule
  only fires in the tests that set `created_at` explicitly. Remember this when a new test uses a fixed clock.
- `conftest.JOBS_DOC` is **index-addressed** by several suites (`doc["jobs"][4]`) — APPEND only, never
  insert. Every job in it carries `alert_after_s: 0` so those suites still assert dispatch at transition
  time; `info` is the deliberate exception (it declares nothing, so it exercises the informational → never
  resolution in place). The threshold layer itself is tested with realistic values in
  `tests/test_alert_thresholds.py`, whose `core_with()` opts every unnamed job out with `alert: never` so one
  job's episode is under test and the rest cannot add noise.
- **Never hard-code a dwell in a test.** It is cadence-aware, so a flat `+ 300` stops being long enough the
  moment a cadence moves — and "the test advanced the clock, but not past the dwell" fails as SILENCE, which
  is indistinguishable from the bugs these suites exist to catch. Use the `dwell(core, job_id)` /
  `_dwell(job_id)` helpers, which derive it from the job. Note `alert_after_s: 0` no longer implies an
  immediate recovery for a job WITH a cadence: its episode still has to serve two samples.
- **Two tests of the same job that both expect a page must be ≥ 6 h apart** (the per-job cooldown), and a
  loop that re-runs a scenario must build a FRESH `settings` each time — `core_with()` reuses the SQLite
  file, so `last_paged_at` carries over and the second iteration is silently suppressed. Parametrise instead
  of looping; both traps were hit writing these suites.
- `RecordingNotifier` overrides `_post`, not `send` — `send`'s try/except is part of what is under test, and
  a double that overrides `send` would make every retry path look like it worked. It has two failure modes
  (`fail` raises, `refuse` returns non-2xx) because `send` flattens both to False.
- **A silence test needs an UPPER bound too.** `assert notifier.sent` passes just as well for a fix that pages
  sixty times, which is the failure on the other side and the one this project has actually shipped. The
  accumulator suites assert `1 <= alerts <= DAY // COOLDOWN_FLOOR_S` and a total push bound; derive the bound
  from the cooldown rather than typing a number.
- **The long simulations use the `no_prune` fixture, and it is a speed patch, not a behaviour one.**
  `db.prune` runs inside every probe cycle and its `id NOT IN (SELECT … LIMIT n)` is correlated, so SQLite
  re-runs the subquery per candidate row: the 1152-cycle storm replay takes **113 s** with it and **2 s**
  without. (It is worth knowing for production too — that cost is paid every cycle against tables kept at
  2000 rows per job.)
- **A test that expects an ESCALATION has to page a default-priority state first** (BEHIND / LATE): the rank
  is read off `HIGH_PRIORITY_STATES`, so an episode that already paged FAIL or STALE_DEST is at the top and
  correctly sends nothing more. `disk` is the natural fixture — low space is BEHIND, an unreadable `statvfs`
  is FAIL.
- **Adding a job to `jobs.example.yml` means giving it an alert policy.** `registry` will fall back to the
  24 h default, but a shipped job inheriting the default is a job somebody forgot — `test_example_file_alert_
  policy_matches_the_documented_thresholds` asserts no job has `alert_source == "default"`. Say `alert: never`
  if that is what you mean, and put the reasoning next to it in the file.
  **It also means a `# TIME-TO-PAGE:` line.** `alert_after_s` is NOT the time to a page — the clock starts
  when the job goes not-OK, which for a scheduled job is already `cadence_s + grace_s` later, and for the
  long-grace Mac jobs the deadline is the bigger half. Four of those comments described the threshold as if
  it were the wait (`pa-backup` read "30 h" and paged at 68 h). Every alerting job now states the end-to-end
  figure and `test_every_alerting_job_states_its_real_time_to_page` recomputes all nine from the file, so a
  drifted comment is a red test rather than a wrong number somebody reads at 2 a.m.
- **`jobs.yml` on the box is gitignored, so a `git pull` delivers schema but never values.** Any new
  `jobs.yml` key needs a scripted, idempotent recipe in DEPLOY.md §1d, and every check in it must be scoped
  to one job's own block — a file-wide search finds a LATER job's key, concludes "already done", and skips
  this one silently, with a clean parse and no error. Three more rules that recipe earned the hard way:
  it must `set -e` and `sys.exit(1)` on every failure path (it sits immediately upstream of a
  `docker compose up -d --build`, so a warning + `exit 0` deploys on defaults and scrolls the warning away);
  its backup must be timestamped to the second (`$(date +%F-%H%M%S)`, because two runs in one day overwrote
  the pre-edit backup with the edited file); and **any new key makes an image rollback require restoring that
  backup FIRST** — `main`'s registry rejects unknown keys, so the container restart-loops otherwise.
- **A post-deploy check that cannot FAIL is not a check.** `docker exec … python - <<PY` without `-i` does
  not forward stdin: `python -` reads EOF, prints nothing, exits 0, and reads exactly like a clean pass.
  Verification commands in DEPLOY.md pass `-i`, get their secrets via `-e`, tolerate an old image with
  `.get()`, and end in an explicit PASS/FAIL that exits non-zero.
- Machine IPs, account ids and Drive folder ids never go in code, tests or docs — use
  `<box-tailscale-ip>`-style placeholders; real values live in the gitignored `.env`/`jobs.yml`/env files.
- Hopper's bearer reads go through the public hostname (`https://dashboard.graham-williams.com/api/v1/status`
  with `Authorization: Bearer $READ_TOKEN`) — that is the intended path. An in-container read against
  `127.0.0.1:8080` must also send `Host: <APP_HOST>` or the Host pin returns 403 (only `/healthz` is exempt).
- **HTTPS at the origin (`web._https_redirect`) redirects ONLY when `X-Forwarded-Proto` is exactly `http`.**
  An absent header must never redirect: the container HEALTHCHECK and that in-container/in-network read send
  none, and a redirect there would break monitoring instead of protecting it. The target is built from the
  `APP_HOST` pin (never the request's Host — reflection = open redirect) and from the RAW request target
  (`RAW_URI`/`REQUEST_URI`), because `request.path` is already URL-decoded and would silently rewrite
  `/a%2Fb` to `/a/b`. Unset `APP_HOST` → no redirect (fail open, which is what keeps the documented local
  visual-QA path and the test suite working). **Because that failure is silent-by-design, `docker-compose.yml`
  defaults `APP_HOST` to `dashboard.graham-williams.com` rather than to empty** — the value normally comes
  from the gitignored `.env`, which no PR can edit, so an empty default would let a box with an older `.env`
  bring the redirect up disabled (the same shape as the `jobs.yml` deploy trap). The app-level fail-open is
  unchanged; only the container's default differs.
- **`APP_HOST` is validated as a BARE hostname before it can reach a `Location`** — read it through
  `Settings.https_redirect_host`, never `settings.app_host`, on any path that emits it. It is operator-set,
  not attacker-set, but an unvalidated value is still a live footgun: `host@evil.example` parses as WHATWG
  *userinfo*, so the browser lands on `evil.example` while the URL still reads like this app;
  `host/evil.net` smuggles a path; and an embedded CRLF makes Werkzeug raise on **every** request — a
  whole-site 500, not just a broken redirect. A malformed value disables the **redirect only** (logged at
  start-up: *"is not a bare hostname"*) and leaves the Host/Origin pin alone, which fails *closed* because
  it compares rather than emits. Those are deliberately two different postures, which is why
  `https_redirect_host` is a separate property. **A bare hostname must also contain at least one DOT, and
  its final label may not be all-digits** (B1, from the 2026-09-19 break-staging sweep) — a public origin
  pin always has a dot, and without that rule `APP_HOST=localhost` (or a bare IPv4 literal, or
  `100.101.1.28`, or the compose service name `hopper-dashboard`) *validated*, so every plain-http visitor
  got a live `Location: https://localhost/…`: broken for everyone, and silent precisely BECAUSE the value
  passed validation, so the loud fail-open branch never fired. Those values now fail open + warn. Strictly
  a tightening — `dashboard.graham-williams.com`, the CI fixture `dashboard.ci.example`, the apex and the
  253-char boundary host all still pass.
- **⚠️ `_HOSTNAME_RE` and `_SAFE_TARGET_RE` are safe ONLY under `.fullmatch()`.** `_SAFE_TARGET_RE` is
  unanchored, so `.match('/x\n')` **succeeds** — one `fullmatch`→`match` slip is a header-injection hole.
  `^…$` would not save it either: in Python `$` also matches immediately before a *trailing* newline.
  Both patterns carry that warning at their definition and `tests/test_web.py` pins the newline rejection
  (and asserts the `.match()` trap explicitly, so it can't be "tidied" away).
- **The redirect is `307`, not `301`, and carries `Cache-Control: no-store` + `Vary: X-Forwarded-Proto`.**
  The `Location` is byte-identical to the requested URL, so a cacheable answer is self-referential: under
  RFC 9111 a 301 with no `Cache-Control` is heuristically cacheable *indefinitely*, which would make one
  misdeployed `APP_HOST` stick in every visitor's browser with no way to recall it, and would let a shared
  cache hand an https visitor a redirect to itself. 307 also preserves the method, so a plain-http POST is
  re-sent over https rather than silently downgraded to a bodiless GET. HSTS is the durable upgrade; the
  redirect does not need to be permanent. **`Vary: X-Forwarded-Proto` goes on EVERY read-side response,
  not just the 307** (B2, same sweep): the 200s/302s the redirect gates are equally scheme-dependent, so a
  shared cache could otherwise store an https-served 200 and later hand it to a plain-http request. It is
  stamped in `_security_headers` with **`resp.vary.add()`, never `headers["Vary"] = …`** — Flask appends
  `Cookie` to `Vary` itself when the session is touched, and assignment would silently clobber it;
  `.vary.add()` is idempotent, so the 307's own value is not doubled. The **ingest** role is untouched
  (see below) — `_security_headers` lives on `web.bp`, so the exemption stays structural.
- **The ingest listener is deliberately exempt from the redirect and from HSTS, and must stay that way.**
  Both hooks live on `web.bp`, registered only for the read role — the exemption is structural, not a
  condition. `:8081` is Tailscale-only, serves no TLS, and every heartbeat (systemd `ExecStopPost` curls,
  `dashboard-containers.timer`, the Mac launchd probe) is plain HTTP with no `X-Forwarded-Proto`. Moving
  those hooks onto the app factory would stop every heartbeat *quietly* — the board would keep rendering,
  just with everything drifting to LATE. `tests/test_ingest.py` pins this in three tests.

## Git workflow
Feature branches only; `main` is protected and only Graham merges (via PR). Commit/push freely on branches.
Squash-merge; branches auto-delete on merge. Every PR description must match the diff — update it after every
follow-up push. CI (`.github/workflows/ci.yml`, `timeout-minutes: 15`) runs pytest and a docker build + smoke
of both roles.

## Secret safety
`.env`, `jobs.yml` (it lists real Drive paths/ids and machine topology), `rclone.conf`, `ingest.env`,
`*.sqlite*` and `data/` are gitignored. Only `.env.example` and `jobs.example.yml` are committed, with
placeholders. Never log tokens; failed logins log the IP only. `INGEST_TOKEN`/`READ_TOKEN`/`INBOX_TOKEN`
empty = fail closed (every bearer request 401); `APP_PASSWORD` empty in prod = refuse to start. Install
scripts never accept a token on the command line (`--token-file` / hidden prompt only) — `deploy/mac/install.sh
--inbox` follows the same rule for `INBOX_TOKEN`, and DEPLOY.md §1c follows it for the password itself.

**Three credentials, deliberately not one.** `INGEST_TOKEN` (heartbeats, Tailscale-only `:8081`),
`READ_TOKEN` (Hopper's read-only `GET /api/v1/status`) and `INBOX_TOKEN` (the Mac worker + the backlog
mirror, scoped by endpoint name to `inbox.MACHINE_ENDPOINTS`). `READ_TOKEN` is explicitly REFUSED on the
Inbox's mutating routes: it is the credential Hopper's watch carries around, and it must not quietly become
a write one. Note a session cookie OUTRANKS a bearer in `web.auth_kind`, so a logged-in browser cannot call
the `INBOX_TOKEN` endpoints at all — tests of them need a session-free client. Supply chain: `python:3.12-slim`
is digest-pinned in the Dockerfile, GitHub Actions are SHA-pinned in `ci.yml` (`permissions: contents: read`),
and rclone is checksum-verified.

## Self-maintenance
When you add or change a capability, job kind, endpoint, dependency, deploy step, or architectural decision,
update this file, `DESIGN.md` and `DEPLOY.md` before the task is done. This is how context persists for the
next agent that enters the repo.

**⚠️ `jobs.yml` AND `.env` ARE GITIGNORED, so merging a PR whose value lives in either of them changes
NOTHING in production.** This has already bitten once for real: PR #8's per-job probe intervals merged and
were never enabled, so the two big Drive trees kept being listed every 5 minutes — the very thing causing
the rate-limiting it was meant to fix — until someone edited the box by hand. **Verify the box, not the
diff.** Every new key needs an explicit DEPLOY.md recipe (§1 for `.env`, §1d/§1d-ii for `jobs.yml`), and
the Inbox release adds four job ids plus six `.env` keys that ship nothing on merge. Rollback order matters
for the same reason: restore the `jobs.yml` backup BEFORE rolling the image back, or an older registry
rejects `kind: worker` (or `alert_after_s`) and the container restart-loops.

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
  **first**: it is the cheap one, and `TimeoutStartSec=210` has to cover both plus the fallback curl) and
  `probes/containers_probe.py` (`docker ps` → `box-containers`) off that one timer; each runs even if the
  other fails and the service exits non-zero if either did. A non-zero exit is journal-only, so the wrapper
  ALSO posts the `fail` ping for a disk probe that never ran — `disk_probe.py` exits **3** when it already
  delivered a `fail` itself, and any other non-zero rc means nothing landed and the wrapper reports it.
  When both probes fail the unit exits with the CONTAINERS code: the disk failure is already a ping on the
  board, a containers failure may be nowhere but the journal.
  Keep that exit-code contract if you touch either file. `disk_free` lives in
  `probes/common.py` (re-exported from `rclone_check` for the Mac probe's existing call site) so the box
  probe doesn't import an rclone module to call `statvfs`. Credentials in `/etc/hopper-dashboard/ingest.env` (root 0600,
  read by systemd); `install.sh --token-file <compose .env>` reads the token, never from argv.
- Mac, the Inbox (added 2026-09-19): a SECOND launchd agent `com.hopper.inbox-transcribe`
  (`deploy/mac/`, `StartInterval 300`) runs `probes/inbox_transcribe.py` — pull the transcription queue,
  download each clip, run Whisper, post the transcript back — plus an `inbox-backlog` sub-probe inside
  `mac_probe.py` that mirrors `~/personal-assistant/backlog.txt` via `probes/backlog.py`. Both are inert
  until `INBOX_URL` + `INBOX_TOKEN` are in `~/.config/hopper-dashboard/env`, which is what keeps this
  branch safe on the live hourly clone. `deploy/mac/install.sh --inbox` sets them up; `uninstall.sh
  --inbox-only` removes just the worker. Healthy hourly log line is now `(5 sub-probes, …, 5 pings)`.
  - **⚠️ NEVER `import mlx_whisper` anywhere under `probes/`.** CI compiles and runs this package under
    3.9 with no third-party packages; mlx exists only in a venv. It is subprocessed, with the interpreter
    from `INBOX_WHISPER_PYTHON` (today it borrows `~/code/jjho-fan-almanac/.venv` — a dedicated venv is the
    clean fix and needs no code change). A test asserts this with `ast` over every module in the package.
  - **⚠️ THE ffmpeg/PATH TRAP.** `mlx_whisper.load_audio()` runs a BARE `ffmpeg` from PATH, and launchd's
    PATH omits `/opt/homebrew/bin`. The plist injects PATH and `install.sh` refuses to install one that has
    lost the line. It presents as a corrupt recording, not a config error, and only under launchd — so the
    worker checks PATH itself and aborts the RUN (`EnvironmentFault`) rather than reporting a per-item
    failure, because three of those mark every queued voice note permanently un-transcribable.
  - **An environment fault and an item failure are different things** and the distinction is load-bearing.
    Keep it if you touch `handle_item` / `transcribe_file`.
  - `probes/backlog.py` DUPLICATES `dashboard/inbox_db.normalise_backlog_key` (stdlib-only, cannot import
    Flask). `tests/test_backlog_mirror.py` imports both and pins the agreement — drift archives every
    mirrored row and re-creates it, losing its reviewed tick and its linked issues. Change both together.
  - `inbox-backlog` NEVER raises: it posts to the PUBLIC host, while `mac-probe` rides Tailscale. A
    Cloudflare blip must not mark the Mac offline, because that mutes every other Mac job's alert.
- Box, the Inbox: `deploy/box/backup.sh` + `hopper-dashboard-backup.{service,timer}` +
  `hopper-dashboard-backup.service.d/heartbeat.conf`, all installed together by `deploy/box/install.sh`
  (there is no flag to install the timer without the heartbeat — an unmonitored backup is the exact failure
  this repo exists to catch). Snapshots `dashboard.db` AND `inbox.db` from INSIDE the container via
  `docker exec` (WAL sidecars are uid 10001; a host-side online backup fails "attempt to write a readonly
  database"), sha256-dedupes, keeps a local ring + a `daily/` tier, and pushes with **`rclone copy`, never
  `sync`** for the two DBs. The audio tree is the deliberate EXCEPTION: it MIRRORS deletions (copy, then an
  explicit logged delete pass for remote extras) so that Delete and the unconditional privacy ceiling
  actually reach the off-box copy — Graham's call 2026-09-19, because DESIGN.md sells Delete as the way to
  retract a recording that caught something private, and an additive backup silently broke that promise.
  Guarded against mirroring a wipe by THREE independent brakes, any one of which refuses and fails the run
  loudly: proportional (`AUDIO_MAX_DROP_PCT`, default 50, **validated 1–99** — `require_positive_int` was
  the wrong validator, since 100 makes the comparison never true and silently disables the brake, 200
  inverts it, and a leading zero would be read as octal), absolute (`AUDIO_MAX_DROP_FILES`, default 25 —
  a percentage alone cannot see a large tree losing a sub-threshold slice every five minutes), and
  windowed (`AUDIO_DROP_WINDOW_MIN`, default 24 h — the percentage is measured against the highest count
  in the window, so a cumulative drip is visible). It also refuses when a missing audio dir is found (skip,
  never a deletion), when the STAGED tree disagrees with the in-container count, when `rclone lsf` cannot
  list the remote, and when the container is empty while Drive is not. **The baseline is the REMOTE
  LISTING, not a file on the box** — falling back to a host directory made the brake fully open on exactly
  the runs that need it (first deploy, cleared state dir, changed `BACKUP_ROOT`, rebuild-from-Drive), and a
  run that cannot list the remote deletes nothing and records no baseline. `AUDIO_ALLOW_MASS_DELETE` is
  **one-shot by construction**: it carries the exact resulting count (`AUDIO_ALLOW_MASS_DELETE=7`), so a
  value left in `.env.backup` cannot authorise a later, different purge. Only a clean mirror advances the
  stored count. ⚠️ **Delete removes the row and the recording everywhere (Drive included, within one
  5-minute cycle), but the transcript TEXT stays in the `inbox_*.db` snapshots already on Drive for up to
  30 days (`DAILY_RETENTION`)** — rewriting historical snapshots would not be a backup, so the claim is
  documented honestly instead. All of this is pinned by a real behavioural harness in
  `tests/test_deploy_backup.py` (fake `docker`/`rclone` on PATH, the real script, assertions on the
  resulting fake remote) — the string-matching tests it replaced caught none of these.
  `deploy/box/verify_snapshot.py` holds the all-empty-snapshot guard (rc 3) so it is testable in
  Python rather than only in bash.
- Manual jobs: `probes/ping.sh <job_id> <ok|fail|skipped> [note]`. `minecraft-offload` needs one seed ping
  after the first offload or its `max_age_s` stays inert (card says "Never run").
- Metrics-only updates use `status: "metric"` (not a run); `ok|fail|skipped` are real runs.
- When the backup script's filter lists or trees change, update `rclone_check.PA_BACKUP_FILTERS`,
  `CLAUDE_CONFIG_FILTERS`, `PA_BACKUP_TREES` and their tests in the same change.
Full recipe + "how it fails quietly" table: `DEPLOY.md`. Verify shell with `bash -n`, the plist with
`plutil -lint`, and units on the box with `systemd-analyze verify --man=no` in a scratch dir.
