# CLAUDE.md — hopper-dashboard

Jobs & backups dashboard for Graham's self-hosted estate: one page + one JSON endpoint answering, for every
scheduled or on-demand job, what it protects, how, last run, last success, whether the destination actually has
fresh bytes, and how far behind manual jobs are. Read `DESIGN.md` first — it is the spec and the frozen API
contract (the "State precedence (as implemented)" section there is the authoritative state-machine spec).

## Stack
Python 3.12, Flask 3.1.3, SQLite (WAL; the ingest process is the only writer), PyYAML for `jobs.yml`, gunicorn,
rclone inside the container for destination probes, ntfy for alerts. No CDN assets, no webfonts, and exactly
ONE inline script (the `<time datetime>` localizer in `base.html`) allowed by a per-request CSP nonce —
`default-src 'self'; script-src 'nonce-…'`. Docker + compose on the box behind the existing `km-tracker`
Cloudflare tunnel; shared `APP_PASSWORD` gate (same pattern as km-tracker / todoist-points / taste-twin /
jjho / baby-pool).

## Two roles, one container
`create_app(role)` builds either app; both load the same `jobs.yml` and share `data/dashboard.db`.

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
- `db.py` — schema (`jobs` incl. `created_at`, `bad_since`, `alerted_at`; `runs`, `probes`,
  `state_changes`), WAL connection, all queries, ISO helpers (`from_iso` clamps to 1970..9999 and never
  raises). **"Which probe row is newest" is decided by `id` (insert order), never by `probed_at`** —
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
  docstring before touching any of it). A job pages once, after it has been continuously not-OK for its own
  `alert_after_s`, and recovers only if that page went out. Three `jobs` columns hold it: `bad_since` (episode
  start; `jobs.since` cannot serve, it resets on every state change), `alerted_at` (one page per episode) and
  `last_paged_at` (the per-job COOLDOWN, which spans episodes — see below).
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
  `notify_alert(name, id, state, after_s)` for a sustained problem (`job_id: STATE for over 6h`),
  `notify_recovery(name, id, from_state)` for the end of an episode that was paged. No reason text ever
  leaves the box; never raises; returns False on a failed POST, which `Core` acts on (see below).
- `ingest.py` — blueprint + pure payload parsers (`parse_json_payload`, `parse_form_payload`, `parse_metrics`).
- `web.py` — read blueprint: gate, host pin, security headers (per-request CSP nonce), HTML + JSON routes.
- `views.py` — builds the `/api/v1/status` contract and job detail from the store.
- `password_gate.py`, `ratelimit.py` — gate helpers (`client_ip(trusted_cidrs)` vs `remote_ip()`) +
  sliding-window limiters (hard key cap with stalest-eviction, keys truncated to 64 chars).
- `humanize.py` — relative/absolute times, human bytes/durations, `human_gib` (binary GiB, used for disk
  capacity — `human_bytes` is decimal GB) (Jinja filters).
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
  Prefer reusing an existing state over adding one to `STATES` — `disk` capacity reuses `BEHIND` rather
  than inventing a seventh state, which would have touched `notify.py`, `app.css`, the summary tiles and
  every state test for no new meaning.
- Adding a metric the UI should understand: read it via `state._first_num` with a key tuple (see
  `LAG_BYTES_KEYS`) so scripts can use either spelling.
- No inline `style=` attributes either: the CSP is `style-src 'self'` with no `'unsafe-inline'`, so a
  data-driven width must be an SVG attribute (see the `disk_gauge` / `history_strip` macros), not a style.
  A styled bar would render empty in a browser while passing every server-side test.
- JS is limited to the one nonce'd inline script in `base.html` (timestamp localization; the page must
  work identically without it). Don't add a second `<script>` — the CSP nonce is generated once per request
  via `csp_nonce()`, and anything else that needs interactivity should be reconsidered.
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

## Git workflow
Feature branches only; `main` is protected and only Graham merges (via PR). Commit/push freely on branches.
Squash-merge; branches auto-delete on merge. Every PR description must match the diff — update it after every
follow-up push. CI (`.github/workflows/ci.yml`, `timeout-minutes: 15`) runs pytest and a docker build + smoke
of both roles.

## Secret safety
`.env`, `jobs.yml` (it lists real Drive paths/ids and machine topology), `rclone.conf`, `ingest.env`,
`*.sqlite*` and `data/` are gitignored. Only `.env.example` and `jobs.example.yml` are committed, with
placeholders. Never log tokens; failed logins log the IP only. `INGEST_TOKEN`/`READ_TOKEN` empty = fail
closed (every bearer request 401); `APP_PASSWORD` empty in prod = refuse to start. Install scripts never
accept a token on the command line (`--token-file` / hidden prompt only). Supply chain: `python:3.12-slim`
is digest-pinned in the Dockerfile, GitHub Actions are SHA-pinned in `ci.yml` (`permissions: contents: read`),
and rclone is checksum-verified.

## Self-maintenance
When you add or change a capability, job kind, endpoint, dependency, deploy step, or architectural decision,
update this file, `DESIGN.md` and `DEPLOY.md` before the task is done. This is how context persists for the
next agent that enters the repo.

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
- Manual jobs: `probes/ping.sh <job_id> <ok|fail|skipped> [note]`. `minecraft-offload` needs one seed ping
  after the first offload or its `max_age_s` stays inert (card says "Never run").
- Metrics-only updates use `status: "metric"` (not a run); `ok|fail|skipped` are real runs.
- When the backup script's filter lists or trees change, update `rclone_check.PA_BACKUP_FILTERS`,
  `CLAUDE_CONFIG_FILTERS`, `PA_BACKUP_TREES` and their tests in the same change.
Full recipe + "how it fails quietly" table: `DEPLOY.md`. Verify shell with `bash -n`, the plist with
`plutil -lint`, and units on the box with `systemd-analyze verify --man=no` in a scratch dir.
