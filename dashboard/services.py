"""The write-side core shared by the ingest routes and the scheduler.

Everything that mutates the store goes through :class:`Core` so the rules
(record → recompute → episode → notify) live in one place. Each call opens its
own short-lived SQLite connection; WAL + busy_timeout make that safe across
gunicorn threads, the scheduler thread, and the read process.

**State and alert-worthiness are separate concerns.** Every transition is
persisted and shown on the board; only a job that has been *continuously*
not-OK for longer than its own ``alert_after_s`` reaches ntfy, and only once
per episode. See DESIGN.md "Alerting rules".

**THE GOVERNING RULE: every ambiguity resolves toward paging, never toward
silence.** The pre-0.2 behaviour — page on every transition — was noisy but
*accidentally self-healing*: a dropped push, a reset clock or a weird
intermediate state was corrected by the next transition, which paged again.
Collapsing to ONE page per episode removed that safety net, so every path that
can consume or reset the single page has to be exactly right. That rule is
where the failed-push rollback (:meth:`Core._return_unsent_pages`), the
unverified-OK hold (``state.ok_is_unverified``), the minimum OK dwell, the
hard ceiling that pages through every hold, the healing of an unusable
``bad_since``, and the refusal to suppress behind a probe job that cannot page
for itself all come from.

**The hard ceiling, stated once:** while an episode is open, past its
threshold and not yet paged, it pages — regardless of what the job's current
state reads. Not "while it is still failing", not "while a hold is running",
not "if a recompute happens to land in some window": *any* pass over *any*
open episode past its bar. Both of this branch's structural bugs were the same
mistake — a ceiling scoped to one branch, so the episode could end from
another one without ever speaking.

**The rule applies to the safeguards themselves.** Each of those safeguards
keeps an episode alive, and an episode that never ends holds its unspent page
hostage too — so each one is bounded and each bound is stated where it is
implemented. A guard that asks "is the job OK right now?" is almost always
asking the wrong question; ask whether the **episode** is still open.

**The episode is not flat.** Two things it could not express were each a
silence:

- *"this has been bad a lot"* — a destination that fails for an hour and then
  lists successfully once is a genuinely recovered job by the episode rules, so
  its clock restarted for ever and it paged NOTHING (issue #15; Drive's
  ``rateLimitExceeded`` is intermittent by nature). :func:`bad_window_s` plus
  :meth:`Core._past_bar` add the accumulator that closes it, as an OR beside
  the continuous rule and never as a replacement for it.
- *"this got worse"* — one page per episode meant a gauge paged for BEHIND and
  then said nothing when the disk filled and the reading failed outright, at
  the wrong ntfy priority (issue #16). :func:`alert_severity` plus
  ``jobs.alerted_state`` allow exactly one re-page per episode, when the
  episode's state crosses from a default-priority state to a high-priority one.

Both are bounded by the per-job cooldown, which stays the only rate guarantee
in the file: see :func:`cooldown_s` for the arithmetic, stated as pushes rather
than as pages.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass

from . import db, inbox_db, probes
from .config import Settings
from .notify import HIGH_PRIORITY_STATES, Notifier
from .registry import Job, Registry
from .state import ALERTABLE_STATES, Facts, compute_state, is_alertable, ok_is_unverified

log = logging.getLogger(__name__)

SELF_JOB_ID = "dashboard-probes"
NOTE_MAX = 500

# How long a job must hold a VERIFIED OK before its episode counts as over,
# capped at 5 minutes and never more than a tenth of its own threshold (so a
# job asking to be paged immediately — ``alert_after_s: 0`` — still clears
# immediately). Without a dwell, ONE OK tick ends an episode: a container
# flapping on `restart: unless-stopped` backoff (19 min down, 1 min up) is
# broken 95% of the day and would never page, because each minute of OK put the
# clock back to zero.
MAX_OK_DWELL_S = 300

# ...but a dwell derived from the THRESHOLD alone is blind to how often the job
# is actually looked at, and a dwell shorter than the sampling interval is not a
# dwell at all. `box-containers` is the worked example: threshold 1200 s gives
# 120 s, while the host cron posts `docker ps` every 300 s — so ONE OK sample
# always satisfied it and closed the episode. Measured over a 6 h crash-loop at
# 50/67/75/80% down: ZERO pushes. It took four consecutive failing samples to
# page at all.
#
# So the dwell must also span at least :data:`CADENCE_DWELL_SAMPLES`
# observations. Capped, because the other end is just as wrong: `pa-backup` has
# a 24 h cadence, and 2 × 86400 would be a two-day dwell — an episode that
# cannot close for two days is an episode holding its page hostage for two days,
# which the hard ceiling makes dangerous rather than merely slow.
CADENCE_DWELL_SAMPLES = 2
MAX_CADENCE_DWELL_S = 900

# The per-job page cooldown: after a page for job J, no further PAGE for J for
# this long. Floor, not the whole rule — see :func:`cooldown_s`.
#
# WHY THIS EXISTS. The one-page-per-episode rule caps an OUTAGE at one push; it
# says nothing about how often an episode may restart. A container on
# `restart: unless-stopped` backoff — 19 min down, 3 min up — crosses its
# threshold, recovers past the dwell, and opens a fresh episode with a fresh
# unspent page, for ever: measured at 40 pushes/day, and an independently
# measured ≥30-min crash-loop cycle at 48/day. That is the alert storm this
# branch exists to remove, relocated from "every transition" to "every episode".
#
# Six hours is the floor because it is the longest fuse already in the file
# (`dashboard-probes`), i.e. the most silence Graham has already accepted for a
# box job. Lengthening the offending job's THRESHOLD instead would have been
# wrong: that re-opens the silence window the hard ceiling just closed.
COOLDOWN_FLOOR_S = 21600

# THE EPISODE ACCUMULATOR, as a multiple of the job's own threshold: an episode
# is also past its bar when the job has been not-OK for `alert_after_s` seconds
# within the last `BAD_WINDOW_MULTIPLE * alert_after_s`.
#
# WHY THIS EXISTS. The damping in PR #8 has a cumulative backstop
# (`PROBE_NO_SUCCESS_S`, measured from the last SUCCESSFUL probe) which
# guarantees the STATE reaches FAIL however the streak arithmetic falls. The
# episode had no equivalent, so a destination that failed for an hour and then
# listed successfully once was, to the episode rules, a job that recovered: the
# clock restarted from zero, for ever. Measured over 24 h against the real
# `run_probe_cycle` with production values (threshold 6 h, cycle 300 s):
# 60 min down / 5 min up paged ONCE a day, and 60/15, 180/15 and 300/30 paged
# **zero times** while failing 73-90% of every probe. Google Drive's
# `rateLimitExceeded` — the failure that motivated PR #8, and which really did
# break the nightly backup on 2026-09-10 — is intermittent by nature, so this
# was the shape most likely to matter.
#
# WHY A MULTIPLE OF 2. The bar is "not-OK for one whole threshold inside the
# window", so the multiple IS the duty cycle it takes to page: 2 means "broken
# more than half the time, for two thresholds". One would be the existing
# continuous rule (it needs 100%) and therefore no help at all; four would page
# for a job broken a quarter of the time, which is a judgement nobody has asked
# for. It cannot page EARLIER than the continuous rule — accruing
# `alert_after_s` of not-OK time takes at least `alert_after_s` — so the
# accumulator only ever adds cases, never moves the existing one.
BAD_WINDOW_MULTIPLE = 2
# Bound on the transition-log scan behind it. `state_changes` is not pruned, and
# a job that flaps writes rows for ever; 2000 covers ~1000 not-OK spans inside
# any window, far past the point where the sum has already cleared the bar.
BAD_SCAN_LIMIT = 2000

# States whose verdict comes from the DESTINATION rather than from the
# heartbeat. An episode that was in one of these must not be closed by an OK we
# could not verify (see the hold in :meth:`Core._resolve_alerts`); an episode
# about silence (LATE) or a failed run (FAIL) IS positively resolved by the
# heartbeat itself, and holding those broke recovery for every unprobed job.
DEST_DRIVEN_STATES = ("STALE_DEST", "BEHIND")

# Shortest gap between two ntfy attempts for the SAME page — one episode, at one
# severity rank. The rollback in :meth:`Core._return_unsent_pages` makes the next
# pass retry, and "the next pass" is the 60 s ticker plus every ping that lands —
# ~72 blocking 5 s POSTs an hour while ntfy is down, inside the scheduler thread
# AND inside whichever ingest request delivered the heartbeat. The delay is only
# ever on a RETRY: the FIRST page of an episode is never held back (see
# Core._retry_due). There are two alertable ranks, so the worst case per job is
# two attempts per window (24/hour), not one.
ALERT_RETRY_MIN_S = 300
# How far back a per-job failure streak is counted. Far above any sane
# PROBE_FAIL_THRESHOLD; bounds the scan on a job that has been failing for
# weeks.
STREAK_SCAN_LIMIT = 500
# Retired metric key. The failure streak used to live in the self-job's
# `last_metrics`, which the ingest route can write: anything holding
# INGEST_TOKEN could POST `{"fail_streak": 0}` and suppress alerting for good.
# The streak is now derived per job from the `probes` table (which ingest
# cannot write at all); the stale/forged key is dropped on every cycle so it
# can't linger on the job page as a number nothing computes.
LEGACY_METRIC_KEYS = ("fail_streak",)


def _monotonic() -> float:
    """Indirection so tests can make a probe cycle "take" wall-clock time."""
    return time.monotonic()


def ok_dwell_s(job: Job) -> float:
    """Seconds of continuous, VERIFIED OK needed to end ``job``'s episode.

    Two independent floors, whichever is longer:

    - a tenth of the job's own threshold, capped at :data:`MAX_OK_DWELL_S`;
    - :data:`CADENCE_DWELL_SAMPLES` of the job's own reporting cadence, capped
      at :data:`MAX_CADENCE_DWELL_S`.

    The second is what stops a single OK sample closing an episode. A job whose
    truth arrives every ``cadence_s`` seconds is only *observed* that often, so
    a dwell below one cadence is decided by one observation and a dwell below
    two cannot tell "recovered" from "up for one sample of a crash loop".

    A job with no cadence (``manual``, ``disk``) keeps the threshold-derived
    value alone: there is nothing to sample. A disk gauge is a level that does
    not un-fill itself, and a manual job has no timer to be late against.
    """
    dwell = min(MAX_OK_DWELL_S, job.alert_after_s / 10)
    if job.cadence_s:
        dwell = max(dwell, min(CADENCE_DWELL_SAMPLES * job.cadence_s,
                               MAX_CADENCE_DWELL_S))
    return dwell


def cooldown_s(job: Job) -> float:
    """How long after a page for ``job`` the next PAGE for it is held back.

    ``max(alert_after_s, COOLDOWN_FLOOR_S)`` — never shorter than the threshold
    the operator chose, and never shorter than six hours.

    **It binds far less often than it looks.** A new episode cannot page before
    ``bad_since + alert_after_s``, and a new episode cannot open before the
    previous one closed, which is at or after the page that closed it. So for
    every job whose threshold is already ``>= COOLDOWN_FLOOR_S`` the next page
    is at least one threshold — i.e. one cooldown — after the last one anyway,
    and this rule changes nothing at all. On the shipped file it binds ONLY on
    ``box-containers`` (20 m), ``box-disk`` and ``mac-disk`` (1 h each). That is
    the intended blast radius: the three jobs that can flap fast.

    **The cooldown never spends the episode's page** (:meth:`Core._page` does
    not stamp ``alerted_at`` when it holds one back), so the episode stays open
    and unpaged and the hard ceiling re-tries it on every single pass. The
    moment the cooldown expires, a job that is still — or again — past its
    threshold pages. The cooldown can therefore delay a page; it can never
    cancel one. That is the whole reason it is safe, and it has its own test.

    **It caps PAGES, not pushes, and the difference is a factor of three.** Two
    things ride past it on purpose, and both are bounded by the episode rather
    than by the clock:

    - a **recovery** is sent for every episode that paged, and is deliberately
      not capped — see :meth:`Core._resolve_alerts`. Measured on a gauge
      hovering 3 h low / 1 h healthy for a week: 28 pages (the cap, working)
      and 28 recoveries, i.e. 8 pushes/day against a 6 h cooldown;
    - an **escalation** (:func:`alert_severity`) is at most one per *paged*
      episode, and a paged episode is itself at most one per cooldown.

    So the honest worst case per job is page + escalation + recovery per
    cooldown window — 12 pushes/day at the 6 h floor — and it takes a job that
    crosses its threshold, gets strictly worse, then genuinely recovers, every
    six hours for ever. `test_the_worst_case_push_rate_per_cooldown_window`
    asserts the bound rather than leaving it to this comment.
    """
    return max(float(job.alert_after_s), float(COOLDOWN_FLOOR_S))


def bad_window_s(job: Job) -> float:
    """How far back :meth:`Core._past_bar` adds up ``job``'s not-OK time.

    See :data:`BAD_WINDOW_MULTIPLE`. Zero for a job with a zero threshold —
    which pages on its first not-OK recompute anyway, so there is nothing an
    accumulator could add.
    """
    return BAD_WINDOW_MULTIPLE * float(job.alert_after_s)


def alert_severity(state: str) -> int:
    """How urgent a page about ``state`` is, as a rank an escalation compares.

    **Derived from the ntfy priority, not invented here.** The question the
    escalation exists to answer is "would this page have been louder than the
    one we already sent?", so the ordering is read off
    :data:`dashboard.notify.HIGH_PRIORITY_STATES` — the same table that decides
    the ``Priority`` header. Inventing a second ordering would let the two drift
    apart, and the whole defect in issue #16 was a push going out at the wrong
    priority.

    That gives exactly two alertable ranks, which is also the bound: one page at
    the default-priority rank (BEHIND / LATE) and at most one more when the
    episode reaches the high-priority rank (FAIL / STALE_DEST). A worsening
    cannot become a storm because there is nowhere above rank 2 to go.

    ``0`` is "not a state we page about" — UNKNOWN, OK, or a value we cannot
    read at all. Ranked BELOW every real state on purpose: an unreadable
    ``alerted_state`` then reads as "we have no idea what was sent", and the
    ambiguity resolves toward paging like everything else here. The one place
    that could turn into a duplicate page — a row paged by an older version,
    which has no ``alerted_state`` at all — is healed instead (see
    :meth:`Core._page`).
    """
    if state in HIGH_PRIORITY_STATES:
        return 2
    if state in ALERTABLE_STATES:
        return 1
    return 0


def ok_hold_s(job: Job) -> float:
    """How long an OK we cannot verify may hold ``job``'s episode open.

    The hold keeps the episode clock running while the evidence for "it is
    fine now" is missing, which is what stops one failed rclone listing a day
    from resetting a 24 h threshold. Unbounded, though, it is its own silence:
    a destination that can never be probed again — a revoked remote, rclone's
    shared Drive OAuth client finally retired — would keep ``alerted_at`` for
    ever, and the job's NEXT failure (the backup dying outright and sitting
    LATE for a week) could never page. One threshold is the most a single
    episode may cost, so the hold expires after the job's own
    ``alert_after_s`` — never less than the dwell cap, so a job that pages
    immediately still gets a few minutes for a transient probe error.

    **Never shorter than the job's own dwell**, either. The hold and the dwell
    are checked in that order, so a hold below the dwell would close the episode
    SOONER for an OK we cannot verify than for one we can — and closing on the
    hold path sends no recovery. Unreachable on the shipped file (every
    threshold there is well above the 900 s dwell cap) but free to guarantee,
    and the cadence-aware dwell is what made it reachable at all.
    """
    return max(MAX_OK_DWELL_S, ok_dwell_s(job), job.alert_after_s)


@dataclass(frozen=True)
class AlertIntent:
    """One ntfy dispatch decided by the episode rules and already committed to
    the DB (``alerted_at`` written / cleared). Sent after the transaction.

    ``bad_since``/``alerted_at`` carry the episode this intent belongs to, so a
    failed POST can hand the page back to exactly that episode and nothing else
    (:meth:`Core._return_unsent_pages`). ``prev_paged_at`` carries the job's
    cooldown stamp as it was BEFORE this page, so the same rollback can put that
    back too — a page that never left the box must not leave a cooldown behind.

    ``prev_alerted_at``/``prev_alerted_state`` are the same idea for an
    ESCALATION, which is the one page that lands on an episode that has already
    paged: rolling it back to NULL would re-arm the episode's *first* page as
    well, so the pair it replaced is carried instead and restored verbatim. For
    a first page they are both None, which is exactly what the rollback wrote
    before escalations existed.

    ``within_s`` is set only when the page was decided by the accumulator rather
    than by a continuous run (:meth:`Core._past_bar`); it is the window the
    not-OK time was added up over, and the ntfy body says so instead of claiming
    an outage that long.
    """
    job: Job
    kind: str          # "alert" | "escalation" | "recovery"
    state: str         # the state the episode is about
    bad_since: str | None = None
    alerted_at: str | None = None
    prev_paged_at: str | None = None
    prev_alerted_at: str | None = None
    prev_alerted_state: str | None = None
    within_s: float | None = None


@dataclass(frozen=True)
class Episode:
    """One job's finished recompute, handed to the episode/alert pass."""
    job: Job
    prev: str          # state before this recompute
    state: str         # state after it
    row: dict          # the job row as it was BEFORE this recompute
    # The state is OK, but only for want of evidence. Two independent flavours,
    # resolved differently (see Core._resolve_alerts):
    #   dest_unverified  — the destination check could not be made at all.
    #   damped_failure   — this job's own `ok` heartbeat is a damped probe
    #                      failure (`dashboard-probes` only).
    dest_unverified: bool = False
    damped_failure: bool = False


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


class Core:
    def __init__(self, settings: Settings, registry: Registry,
                 notifier: Notifier | None = None):
        self.settings = settings
        self.registry = registry
        self.notifier = notifier or Notifier(settings.ntfy_url,
                                             settings.ntfy_topic)
        self._warned_no_self_job = False
        # Logical clock at the END of the last probe cycle (start + measured
        # wall time). The scheduler schedules the next cycle from this, never
        # from the pre-cycle clock.
        self.last_cycle_end: float | None = None
        # job id -> (episode bad_since, {severity rank -> epoch of the last ntfy
        # attempt at that rank}). The rank has to be INSIDE the entry, not part
        # of a flat per-job key: a page and its escalation are two different
        # pages, each with its own backoff, and one per-job slot let them evict
        # each other on every flip (see _retry_due).
        # In memory on purpose (see _retry_due): a restart simply retries
        # sooner, which is the safe direction, and it needs no migration.
        self._alert_attempted: dict[str, tuple[str, dict[int, float]]] = {}

    # -- plumbing ---------------------------------------------------------- #

    def connect(self):
        return db.connect(self.settings.db_path)

    def init_store(self) -> None:
        conn = self.connect()
        try:
            db.init_schema(conn)
            db.ensure_jobs(conn, (j.id for j in self.registry))
        finally:
            conn.close()
        self.init_inbox_store()

    def init_inbox_store(self) -> None:
        """Create/migrate ``inbox.db`` — a SEPARATE file (see inbox_db).

        Run for BOTH roles and from every gunicorn worker, which is exactly why
        ``init_inbox_schema`` migrates under BEGIN IMMEDIATE with a
        duplicate-column-tolerant ALTER: the loser of that race must not kill a
        worker and restart-loop the container.
        """
        conn = inbox_db.connect(self.settings.inbox_db_path)
        try:
            inbox_db.init_inbox_schema(conn)
        finally:
            conn.close()

    def inbox_connect(self):
        return inbox_db.connect(self.settings.inbox_db_path)

    @staticmethod
    def gather_facts(conn, job: Job, row: dict | None = None) -> Facts:
        row = (db.job_row(conn, job.id) if row is None else row) or {}
        return Facts(
            last_run=db.last_run(conn, job.id),
            last_success=db.last_success(conn, job.id),
            last_metrics=row.get("last_metrics") or {},
            last_metrics_at=row.get("last_metrics_at"),
            probe=db.last_probe(conn, job.id) if job.has_probe else None,
            created_at=row.get("created_at"),
        )

    # -- state ------------------------------------------------------------- #

    def _recompute_locked(self, conn, job: Job, now: float
                          ) -> tuple[str, tuple | None, dict, Facts]:
        """Recompute inside an open transaction.

        Returns ``(state, transition, row, facts)`` where ``transition`` is
        ``(from, to, reason)`` or None, and ``row`` is the job row as it was
        BEFORE this recompute — its ``state``, ``since``, ``bad_since`` and
        ``alerted_at`` are what the episode bookkeeping reads. ``facts`` goes
        on to answer whether an OK was actually verified.
        """
        row = db.job_row(conn, job.id) or {}
        facts = self.gather_facts(conn, job, row)
        state, reason = compute_state(job, facts, now)
        prev = db.set_state(conn, job.id, state, db.to_iso(now), reason)
        if prev is None:
            return state, None, row, facts
        return state, (prev, state, reason), row, facts

    def _machine_probe(self, machine: str) -> Job | None:
        """The ``probe``-kind job that stands for "this machine is reachable"
        (e.g. ``mac-probe``). The dashboard's own probe job never counts."""
        for j in self.registry:
            if (j.kind == "probe" and j.machine == machine
                    and j.id != SELF_JOB_ID):
                return j
        return None

    def _returning_probes(self, conn, episodes: list[Episode]) -> set[str]:
        """Machine probe jobs still inside the LATE episode they are coming back
        from — "the machine has only just woken up".

        The machine-offline rule mutes a sibling's plain ``LATE → OK`` because
        that news belongs to the probe job. The siblings are pinged *before* the
        probe, so they recover first: originally by one batch, which the
        ``transitions`` check below covers. The hard ceiling widened what that
        gap costs — a sibling's episode stays open for its dwell after it
        recovers, and on any pass inside that window the ceiling would page for
        a LATE that was only the Mac asleep. So the mute lasts as long as the
        PROBE's own episode, which is bounded by the probe's dwell (≤5 min) and
        cannot outlive the sibling's, since the sibling recovered first and no
        dwell exceeds :data:`MAX_OK_DWELL_S`.

        Read from the PRE-recompute rows, so the answer cannot depend on where
        the probe job sits in registry order relative to its siblings.
        """
        returning: set[str] = set()
        for ep in episodes:
            if ep.job.kind != "probe" or ep.job.id == SELF_JOB_ID:
                continue
            if not ep.row.get("bad_since"):
                continue
            about = (ep.prev if ep.prev != "OK"
                     else db.last_non_ok_state(conn, ep.job.id))
            if about == "LATE":
                returning.add(ep.job.id)
        return returning

    def _cooled_probes(self, episodes: list[Episode], now: float) -> set[str]:
        """Machine probe jobs whose OWN page is currently held back by their own
        cooldown, and which have not already paged for the episode they are in.

        The machine-offline rule may only borrow an alert that EXISTS. That was
        already checked statically (``probe.alert_never``); the cooldown makes
        the same thing true dynamically and temporarily, so it has to be checked
        the same way. Without this, a Mac that dies three hours after its probe
        last paged is: probe page held by the cooldown, every sibling's page
        muted behind a probe that is not speaking — a real outage, zero pushes,
        for the length of the cooldown. Exactly the failure mode the static
        guard exists to prevent, reintroduced by a new mechanism.

        A probe that has ALREADY paged for its current episode
        (``alerted_at`` set) is fine to hide behind: the alert exists, it was
        sent, and the siblings genuinely are the same fact.

        Read from the PRE-recompute rows for the same reason ``returning`` is:
        so the answer cannot depend on registry order.
        """
        cooled: set[str] = set()
        for ep in episodes:
            if ep.job.kind != "probe" or ep.job.id == SELF_JOB_ID:
                continue
            if ep.row.get("alerted_at"):
                continue
            if self._cooling_until(ep.row, ep.job, now) is not None:
                cooled.add(ep.job.id)
        return cooled

    def _suppressed_offline(self, job: Job, prev: str, state: str,
                            states: dict[str, str],
                            transitions: dict[str, tuple],
                            returning: set[str],
                            cooled: set[str]) -> bool:
        """Machine-offline rule: while a machine's probe job is LATE, the other
        jobs on that machine going LATE is the same single fact ("the Mac is
        asleep"), so only the probe's own alert is sent. The sibling
        transitions are still recorded and shown.

        Recovery is mirrored: a sibling's ``LATE → OK`` is muted while the
        probe job is still LATE *or* recovers in the same batch. The Mac probe
        posts its sub-jobs first (pa-backup, drive-mirror, …) and its own
        heartbeat last, each as a separate HTTP request and therefore a
        separate recompute — so the siblings always recover one batch *before*
        the probe does, while it is still LATE. Without this the wake-up would
        page once per Mac job plus once for the probe. Only plain recoveries
        are muted: a sibling waking into FAIL / STALE_DEST / BEHIND is news of
        its own and alerts normally.

        Adapted to the level-triggered model: ``prev``/``state`` are still
        edge-shaped, but they are read off the *episode* rather than off "the
        last recompute changed something". Every caller passes the state the
        EPISODE is about as ``prev`` — for a job that is not-OK that is simply
        its current state, so a job sitting LATE past its threshold with no
        transition anywhere is still matched by the first rule; for a page or a
        recovery decided while the job already reads OK it is the state the
        episode started in, which is what the second rule needs (the dwell and
        the holds mean an episode can outlive the job's return to OK by
        minutes). ``returning`` covers the same span on the probe's side — see
        :meth:`_returning_probes`.

        **The rule is void when the probe job cannot page for itself.** The
        whole premise is "one alert for the machine instead of five" — a probe
        job that never pages deletes that one alert, and a Mac that is gone for
        days then pages NOTHING while ``pa-backup`` sits LATE with a week-old
        ``bad_since``. Suppression may only borrow an alert that exists — which
        is now two checks, one static (``alert_never``) and one for the moment
        (:meth:`_cooled_probes`), because the per-job cooldown can take the
        probe's alert away temporarily."""
        probe = self._machine_probe(job.machine)
        if probe is None or probe.id == job.id:
            return False
        if probe.alert_never or probe.id in cooled:
            return False
        probe_state = states.get(probe.id)
        if state == "LATE" and probe_state == "LATE":
            return True
        if prev == "LATE" and state == "OK":
            if probe_state == "LATE":
                return True
            probe_tr = transitions.get(probe.id)
            if probe_tr is not None and probe_tr[0] == "LATE":
                return True
            if probe.id in returning:
                return True
        return False

    @staticmethod
    def _ok_held_s(prev: str, row: dict, now: float) -> float:
        """How long the job has been continuously OK as of ``now``.

        ``jobs.since`` is when it entered its current state, so it only means
        "OK since" while ``prev`` is already OK; a job that turned OK in THIS
        recompute has held it for zero seconds. An unreadable or future
        ``since`` also reads as zero — the shortest dwell keeps the episode
        open, which is the paging direction.
        """
        if prev != "OK":
            return 0.0
        since = db.from_iso(row.get("since"))
        if since is None or since > now:
            return 0.0
        return now - since

    def _retry_due(self, job_id: str, bad_since: str, rank: int,
                   now: float) -> bool:
        """May we POST this page yet?

        Always yes for a page we have not tried to send — the FIRST alert of an
        episode is never delayed, which is the whole point of the threshold, and
        neither is the first escalation. Only a retry of the SAME page (the
        previous POST failed and :meth:`_return_unsent_pages` gave the page
        back) waits out ``ALERT_RETRY_MIN_S``. A clock that steps backwards
        reads as due rather than as "wait": late-but-noisy over silent, as
        everywhere else.

        **A page is identified by the episode AND the severity rank being
        paged**, never by ``bad_since`` alone: an escalation is a new page inside
        an episode that has already POSTed, so keyed on the episode alone it
        would read as a retry and be held back for five minutes — a "the disk is
        now unreadable" push delayed by a backoff that exists only to stop
        hammering a dead ntfy.

        **The two ranks are held SIDE BY SIDE, in one entry per job, and that is
        load-bearing.** A flat ``dict[job_id, (key, when)]`` with the rank folded
        into the key looks equivalent and is not: the two keys evict each other,
        so a job whose state oscillates across the rank boundary (a disk gauge
        flipping BEHIND ↔ FAIL) reads every single pass as a page it has never
        tried, and the backoff stops applying at all. Measured against a 60 s
        oscillation and a dead ntfy: **60** attempts an hour, against 12 for a
        non-oscillating episode — 83 % of the way back to the ~72 the backoff
        exists to prevent. Per-rank, the bound is what it claims to be: at most
        one attempt per rank per ``ALERT_RETRY_MIN_S``, two ranks, so ≤ 24 an
        hour whatever the state does
        (``test_an_oscillating_episode_cannot_defeat_the_retry_backoff``).

        A new episode replaces the whole entry, so the map holds at most one
        entry per job and at most two timestamps inside it — nothing accumulates
        over a job that flaps for a month.

        The map is per-``Core``, and only the ingest role dispatches — it runs
        ``gunicorn --workers 1 --threads 4`` (see ``entrypoint.sh``), so one
        process, one map, shared by the ticker thread and every ingest thread.
        If ingest is ever given more than one worker the backoff multiplies by
        the worker count; that is more pushes, not fewer, but check here first.
        """
        entry = self._alert_attempted.get(job_id)
        if entry is None or entry[0] != bad_since:
            return True
        when = entry[1].get(rank)
        if when is None:
            return True
        waited = now - when
        return waited >= ALERT_RETRY_MIN_S or waited < 0

    def _note_attempt(self, job_id: str, bad_since: str, rank: int,
                      now: float) -> None:
        """Record that we are POSTing this page now, for :meth:`_retry_due`.

        A rank of the CURRENT episode joins the existing entry; any other
        episode replaces it outright, which is what keeps the map at one entry
        per job.
        """
        entry = self._alert_attempted.get(job_id)
        if entry is None or entry[0] != bad_since:
            entry = (bad_since, {})
            self._alert_attempted[job_id] = entry
        entry[1][rank] = now

    @staticmethod
    def _past_bar(conn, job: Job, about: str, began: float,
                  now: float) -> tuple[bool, float | None]:
        """Has this episode earned a page yet? Returns ``(past, within_s)``,
        where ``within_s`` is the accumulator's window when that is what decided
        it and None when the episode simply ran long enough.

        Two independent ways past the bar, and the first is the original one:

        - **continuously** not-OK for ``alert_after_s`` — the episode clock,
          which includes the dwell it is serving out (that is the documented
          soft edge, and it errs toward paging);
        - **cumulatively** in ``about`` for ``alert_after_s`` inside the last
          :func:`bad_window_s` — the accumulator (:data:`BAD_WINDOW_MULTIPLE`),
          which is what makes an intermittent destination reachable at all.

        **It is an OR, and that is the whole safety argument.** Issue #13 item 4
        declined an accumulator here because the two attempts at it during
        review each produced a silence bug — both of them versions that
        *replaced* the clock with "count only the not-OK seconds", which can
        only ever page LATER than the clock does. Added beside the clock, the
        accumulator has no path to silence: every episode that paged before this
        change still reaches its bar at exactly the same moment, and the only new
        behaviour is extra pages. **"The same moment" is about the BAR, not about
        the push** — one push can still move. An escalation stamps
        `last_paged_at` (see :meth:`_page`), which is a page that did not exist
        before this change, so the FOLLOWING episode's first page can be held by
        a cooldown it would not have met on the old code — up to one
        `cooldown_s` later. It is a delay, never a cancellation (a held page
        stamps nothing and the ceiling re-offers it), and the cumulative push
        count is never below the old code's at any instant. Asserted, not
        argued, in `test_the_escalation_is_not_held_by_the_cooldown_but_does_stamp_it`.
        The second half of that argument is upstream:
        PR #8's damping means transient probe noise never reaches an alertable
        state at all, so the accumulator sits behind the filter and never sees
        the 27-flap storm that made this dangerous (asserted, not assumed —
        `test_the_historical_flap_storm_still_pages_nothing`).

        **Only time in ``about`` counts — the SAME state this page is about, not
        "any badness".** Found by an existing test rather than by reasoning:
        counting every alertable state let time that was deliberately MUTED be
        laundered into a page about something else. Every night the Mac sleeps,
        each of its jobs sits LATE for hours with its alert suppressed by the
        machine-offline rule ("one alert for the machine"); with a flat
        accumulator, the next morning's first *unrelated* `drive-mirror: BEHIND`
        episode paged the instant it opened, on the strength of a sleep that was
        explicitly declared not-news. Per-state closes exactly that: muted LATE
        time can no longer speak for a page about a DIFFERENT state. It is also
        the better semantics — "this exact problem keeps coming back", which is
        the question the episode asks, only cumulatively. Residual, stated: a job
        that alternates between DIFFERENT bad states across separate episodes
        accumulates neither. Within one episode the clock already covers it (a
        mid-episode LATE → FAIL does not reset it).

        **What per-state does NOT close, stated plainly because an earlier
        version of this docstring claimed it did: muted LATE time still counts
        toward a page about a LATER LATE episode.** The mute
        (:meth:`_suppressed_offline`) is a function of the probe's state RIGHT
        NOW, not of accumulated time, and :func:`db.not_ok_seconds` reads across
        episode boundaries — so "the same mute still gags it" is false as soon as
        the machine is back. Reachable on the shipped file, measured on
        `drive-mirror`'s real numbers
        (`test_muted_late_time_can_still_bring_a_later_late_page_forward`): a
        40 h Mac sleep accrues ~25 h of muted LATE, the episode closes silently,
        and a fresh LATE that starts 15 h later pages the instant it opens
        instead of a day in. It needs `alert_after_s` to exceed the job's own
        LATE onset — otherwise the earlier badness has aged out of the window
        before a new episode can even begin — which on the shipped file is true
        of `drive-mirror` and nothing else
        (`test_which_shipped_jobs_can_have_muted_time_brought_forward`).
        **Direction: it pages EARLY, never silently** — the job really is in that
        state now, and the seconds counted really were unreported — so it is a
        residual under the governing rule, not a defect. Two things must stay
        true for that to hold: the page is still at most one per episode, and it
        still takes a full `alert_after_s` of real, same-state, unreported time.

        **The early-fire is bounded by UNPAGED badness.** Not-OK time that
        already paged is absorbed by the cooldown instead — a second page inside
        `cooldown_s` is held whatever the accumulator says — so what the
        accumulator can bring forward is exactly the badness nobody was told
        about, which is the thing it exists to stop losing. That bound is also
        the precise reason the muted case above is the one that gets through: a
        suppressed page deliberately stamps NOTHING (not `alerted_at`, not
        `last_paged_at`, so the episode keeps its page), so muted time is unpaged
        time with no cooldown behind it. A mute is not a cooldown.

        Missing history reads as zero accrued time, not as badness: the scan
        stops at the oldest row it can see, and time before that is unaccounted
        rather than assumed bad. That direction can only ever fall back to the
        continuous rule, i.e. to the behaviour this file already had.
        """
        if now - began >= job.alert_after_s:
            return True, None
        window = bad_window_s(job)
        if window <= 0:                      # a 0-threshold job already paged
            return False, None
        if alert_severity(about) == 0:
            # Not a state we page about at all (an episode with no non-OK
            # transition on record can name OK here). Accumulating "time spent
            # OK" would page for health; the clock above is the only rule that
            # may speak for a state like this.
            return False, None
        bad = db.not_ok_seconds(conn, job.id, now - window, now, (about,),
                                BAD_SCAN_LIMIT)
        if bad >= job.alert_after_s:
            return True, window
        return False, None

    @staticmethod
    def _cooling_until(row: dict, job: Job, now: float) -> float | None:
        """When ``job``'s page cooldown expires, or None if it is not cooling.

        Reads the persisted ``last_paged_at``. A stamp that is unparseable or in
        the FUTURE (a clock step, a hand-edited row) is treated as **no
        cooldown at all** rather than as one that can never expire — the same
        reasoning as the ``bad_since`` healing, and the same direction: a
        cooldown that outlives every clock is a job that can never page again,
        which is the one outcome this feature may not produce.
        """
        if job.alert_never:
            return None
        last = db.from_iso(row.get("last_paged_at"))
        if last is None or last > now:
            return None
        until = last + cooldown_s(job)
        return until if until > now else None

    def _page(self, conn, ep: Episode, about: str, bad_since: str, began: float,
              alerted_at: str | None, alerted_state: str | None,
              states: dict[str, str],
              transitions: dict[str, tuple], returning: set[str],
              cooled: set[str], now: float,
              at: str, intents: list[AlertIntent]
              ) -> tuple[str | None, str | None]:
        """Spend this episode's page, if everything says we should.

        ``about`` is the state the page is *about* — the current state for a
        job that is not-OK, or the state the episode was in for one held open
        across an OK we cannot (yet) believe.

        Returns the episode's ``(alerted_at, alerted_state)`` as they now stand:
        the new pair if this call paged, otherwise the values passed in. Callers
        keep their local copies in step with the row, because what follows a
        ceiling page in the same pass — the recovery, above all — reads them.

        **THE HARD CEILING is the single rule this method exists to serve:**
        while an episode is open, past its threshold and unpaged, it pages —
        whatever the job currently reads. :meth:`_resolve_alerts` therefore
        calls this on *every* pass over an open episode (not-OK, held open by an
        unverifiable OK, or serving out the dwell), never only inside one branch
        or one window. An episode that has to wait for the job to be in some
        particular state to be allowed to speak is an episode that can be
        silenced by the job recovering, which is exactly backwards.

        **ONE PAGE PER EPISODE, PLUS ONE PER ESCALATION** (issue #16). A flat
        one-page rule meant a gauge that paged "BEHIND for over 1h" said nothing
        when the free space fell to 1 GiB and then the reading failed outright —
        and never sent a HIGH-priority push at all, because the priority is read
        off the state and the state that was paged was the mild one. For a
        capacity gauge that is exactly inverted: BEHIND is the early warning,
        FAIL is the event. So a page also goes out when the episode's state gets
        strictly WORSE than the state already paged (:func:`alert_severity`),
        carrying that state's own priority. ``jobs.alerted_state`` remembers
        what was sent; there are two ranks, so an episode can escalate at most
        once and a worsening cannot become a storm.
        """
        job = ep.job
        if job.alert_never:
            return alerted_at, alerted_state             # opted out
        escalation = False
        if alerted_at:
            if alerted_state is None:
                # Paged by a version that had no `alerted_state` (the upgrade
                # deploy, or a hand-edited row). Record what the episode is
                # about NOW rather than guessing: rank 0 would read as "worse
                # than nothing" and send a duplicate of the page that already
                # went out, and ranking it at the top would swallow a genuine
                # later escalation. Healed once, then it behaves normally.
                # What this costs, stated: if the state has ALREADY worsened by
                # the first recompute after the upgrade, the heal records the
                # WORSE state as "what was sent" and that worsening never
                # escalates — one lost escalation per episode open across the
                # deploy. Not a regression (the old code had no escalation at
                # all) and not silence (the episode's own page had already gone
                # out); the alternative is a duplicate of a page that has
                # already been read, for every such episode.
                log.info("recording %s as the paged state for %s's open episode "
                         "(it was paged before alerted_state existed)",
                         about, job.id)
                db.set_alert_episode(conn, job.id, bad_since, alerted_at, about)
                return alerted_at, about
            if alert_severity(about) <= alert_severity(alerted_state):
                return alerted_at, alerted_state         # already paged, no worse
            escalation = True
        past, within = self._past_bar(conn, job, about, began, now)
        if not past:
            return alerted_at, alerted_state  # not sustained long enough yet
        # THE COOLDOWN APPLIES TO A FIRST PAGE ONLY. An escalation skips it, on
        # purpose: the cooldown exists to stop the SAME fact being repeated, a
        # strictly worse state is a different fact, and the flat cooldown
        # swallowing it was a stated residual of PR #11 rather than a decision.
        # It is bounded without a timer — at most one per paged episode, and a
        # paged episode is itself at most one per cooldown — and it still STAMPS
        # `last_paged_at` below, so it pushes the NEXT episode's first page out
        # by a full cooldown. The rate limit is moved, not lifted.
        if not escalation:
            until = self._cooling_until(ep.row, job, now)
            if until is not None:
                # THE PER-JOB COOLDOWN. Deliberately does NOT stamp
                # `alerted_at`: the episode keeps its unspent page, so the
                # ceiling above re-tries this same call on every later pass and
                # the page goes out the moment the cooldown expires — delayed,
                # never cancelled. Stamping here would turn a rate limit into
                # exactly the permanent silence this whole file is organised
                # against.
                # Derived from `until` alone — no second parse of the row.
                # Re-reading `last_paged_at` here would couple this line to
                # _cooling_until's internals, and a None slipping through would
                # raise INSIDE the recompute transaction, i.e. lose the pass.
                log.info("page for %s held back by its cooldown (%ss); %ss still "
                         "to run, then it pages if it is still past its "
                         "threshold", job.id, int(cooldown_s(job)),
                         int(until - now))
                return alerted_at, alerted_state
        if self._suppressed_offline(job, about, ep.state, states, transitions,
                                    returning, cooled):
            # Deliberately do NOT stamp alerted_at: a suppressed alert must not
            # burn the episode's single page, or its recovery would be lost too.
            log.info("alert for %s suppressed: its machine's probe job is "
                     "offline (one alert for the machine instead)", job.id)
            return alerted_at, alerted_state
        rank = alert_severity(about)
        if not self._retry_due(job.id, bad_since, rank, now):
            # A POST for THIS page — this episode, at this rank — failed recently
            # and it was handed back. Retrying on every tick and every ping means
            # a blocking 5 s urllib call ~72×/hour while ntfy is down — in the
            # scheduler thread and in the ingest request. Space them out; nothing
            # is lost, the next attempt is a few minutes later. Only retries
            # wait.
            return alerted_at, alerted_state
        # Optimistic stamp, inside the transaction: it is what stops the 60 s
        # ticker paging again mid-batch. If the POST then fails, _dispatch hands
        # the page straight back (see _return_unsent_pages).
        self._note_attempt(job.id, bad_since, rank, now)
        db.set_alert_episode(conn, job.id, bad_since, at, about)
        prev_paged_at = ep.row.get("last_paged_at")
        db.set_last_paged_at(conn, job.id, at)
        intents.append(AlertIntent(job, "escalation" if escalation else "alert",
                                   about, bad_since=bad_since, alerted_at=at,
                                   prev_paged_at=prev_paged_at,
                                   prev_alerted_at=alerted_at,
                                   prev_alerted_state=alerted_state,
                                   within_s=within))
        return at, about

    def _resolve_alerts(self, conn, episodes: list[Episode],
                        states: dict[str, str], transitions: dict[str, tuple],
                        now: float) -> list[AlertIntent]:
        """Advance every job's alert episode and decide what reaches ntfy.

        Runs as a second pass inside the recompute transaction, once every
        job's new state is known (the machine-offline rule needs the whole
        batch). It is **level-triggered**: it walks every job on every
        recompute, not only the ones that changed, because "still FAIL, and now
        past six hours" is exactly the event this feature exists to send and it
        is not a transition. The episode clock is on the *episode*, not on the
        state: a job that goes LATE and later FAILs keeps its original
        ``bad_since``, which is the whole point — the question is "has this
        been broken for a day?", not "did something change?".

        There is one page per episode (plus at most one escalation, when the
        episode's state gets strictly worse — :func:`alert_severity`), so every
        way an episode can end is a way to lose a page. Each is therefore biased
        toward keeping the episode alive: an OK we could not verify does not end
        it, and neither does an OK too short to believe. Keeping an episode alive is itself
        bounded, though, because an episode that never ends holds its page too.
        The dwell is at most 5 minutes; the unverifiable-OK hold is at most one
        threshold (:func:`ok_hold_s`) and then closes silently.

        **THE INVARIANT that makes all of that safe:** *while an episode is open
        (``bad_since`` set), past its threshold and not yet paged, it pages —
        regardless of what the job's current state reads.* :meth:`_page` is
        therefore called on every pass over an open episode, from both the
        not-OK branch and the OK branch, before any branch that could end the
        episode. The rule has no exceptions and no windows: every "the page can
        wait until X" this code has ever had turned out to be "the page is lost
        if X never comes".
        """
        at = db.to_iso(now)
        intents: list[AlertIntent] = []
        returning = self._returning_probes(conn, episodes)
        cooled = self._cooled_probes(episodes, now)
        for ep in episodes:
            job, prev, state, row = ep.job, ep.prev, ep.state, ep.row
            bad_since = row.get("bad_since")
            alerted_at = row.get("alerted_at")
            # What was actually SENT about this episode — a different question
            # from what the episode is about NOW: the escalation compares against
            # it, and the recovery is named from it.
            alerted_state = row.get("alerted_state")

            # A `last_paged_at` we cannot use — unparseable, or in the FUTURE
            # after a clock step — would otherwise be a cooldown that never
            # expires, i.e. a permanently un-pageable job. `_cooling_until`
            # already refuses to honour one (so nothing below can be misled by
            # it), but clear it here as well so the row stops carrying a value
            # that reads like a policy, and so the log says what happened. Same
            # rule, same direction, as the `bad_since` healing below.
            paged_raw = row.get("last_paged_at")
            if paged_raw is not None:
                paged = db.from_iso(paged_raw)
                if paged is None or paged > now:
                    log.warning("healing unusable last_paged_at %r for %s",
                                paged_raw, job.id)
                    db.set_last_paged_at(conn, job.id, None)

            # A `bad_since` we cannot use: unparseable, or in the FUTURE after a
            # clock step (NTP walking the clock back, a box RTC ahead at boot).
            # A future start makes `now - began` forever negative — a job that
            # can never page. Heal rather than trust, and drop `alerted_at` with
            # it, because a fresh clock is a fresh episode. Done before anything
            # else reads the pair, so no branch below can trust a poisoned one.
            began = db.from_iso(bad_since)
            if bad_since is not None and (began is None or began > now):
                log.warning("healing unusable bad_since %r for %s", bad_since,
                            job.id)
                bad_since = began = alerted_at = alerted_state = None
                db.set_alert_episode(conn, job.id, None, None)
            elif bad_since is None and alerted_at is not None:
                # An `alerted_at` with no episode to belong to: nothing here ever
                # writes that pair (a close clears both), so it is a corrupt or
                # hand-edited row. Left alone it is a spent page attached to
                # nothing, which is the shape that makes a job un-pageable. Drop
                # it — the direction that can only ever page MORE.
                log.warning("healing an alerted_at with no open episode for %s",
                            job.id)
                alerted_at = alerted_state = None
                db.set_alert_episode(conn, job.id, None, None)

            if state == "OK":
                if not (bad_since or alerted_at):
                    continue                  # healthy, and was already
                # A deferred verdict can arrive while the job is already OK, so
                # name the state the episode is actually about.
                about = prev if prev != "OK" else (
                    db.last_non_ok_state(conn, job.id) or prev)
                held = self._ok_held_s(prev, row, now)
                # THE HARD CEILING, applied to the EPISODE and to nothing else:
                # an open episode that is past its threshold and unpaged pages
                # here, whatever the job currently reads, naming the state the
                # episode is really about. It is deliberately ABOVE every branch
                # below — the hold, the hold's expiry, and the dwell — because
                # each of those is a way for the episode to end, and an episode
                # must never end unpaged after it crossed the bar Graham set.
                # Scoped inside one branch it fired only while `began + A <= now
                # < since + ok_hold_s`; with `ok_hold_s == alert_after_s` for
                # every threshold over 5 min that window is exactly as long as
                # the job was OBSERVED not-OK, so a job that flipped to an
                # unverifiable OK a few seconds in had no recompute land in it
                # and went silent for good (a destination 30 days stale, the
                # heartbeat still arriving, zero pushes). The dwell had the same
                # hole from the other side: any recovery longer than
                # `min(5 min, A/10)` ended the episode, so 19 min down / 3 min
                # up — broken 86% of the time, for ever — never paged once.
                if bad_since is not None:
                    alerted_at, alerted_state = self._page(
                        conn, ep, about, bad_since, began, alerted_at,
                        alerted_state, states, transitions, returning, cooled,
                        now, at, intents)
                if self._holding(ep, about):
                    if held < ok_hold_s(job):
                        log.info("episode for %s held open: %s ended in an OK "
                                 "we cannot verify", job.id, about)
                        continue
                    # ...but only for one threshold. Held for ever, a probe that
                    # never comes back (revoked remote, retired OAuth client)
                    # keeps `alerted_at` and mutes every LATER failure of this
                    # job. Close it, with NO recovery: nothing was verified
                    # fixed, so "→ OK" would be a lie. A destination that is
                    # still stale re-opens the episode the moment a probe can
                    # see it, and a new failure pages on a clock of its own.
                    log.warning("episode for %s closed unverified: %s never "
                                "recovered usable evidence within %ss of OK",
                                job.id, about, ok_hold_s(job))
                    db.set_alert_episode(conn, job.id, None, None)
                    continue
                if held < ok_dwell_s(job):
                    # Too short to believe. A single OK tick used to end the
                    # episode outright, so a container flapping 19 min down /
                    # 1 min up — broken 95% of the day — reset its clock 72
                    # times and never paged once. The episode stays open, and
                    # with it `alerted_at`: closing it here and recovering would
                    # re-arm the page for the next 19 minutes and turn one
                    # outage into a flap storm on the phone.
                    continue
                # The episode really is over: close it, and say so if Graham was
                # told about it in the first place. The recovery is therefore up
                # to `ok_dwell_s` late (5 min at most, 0 for a 0 threshold) — a
                # deliberate trade against announcing a recovery that is about
                # to be taken back. `alerted_at` may have been stamped by the
                # ceiling a few lines up, in which case this recovery follows
                # its page by seconds: accepted on purpose. The job was broken
                # for longer than the threshold Graham set, so the outage is
                # real news even though it is over — and it is rare, because a
                # job has to cross its whole threshold and then recover inside
                # one dwell to do it.
                #
                # It names `alerted_state` — the state actually PAGED — not
                # `about`. `about` reads the LATEST non-OK state, so an episode
                # paged as "BEHIND for over 1h" that later touched FAIL
                # recovered as "FAIL → OK": a resolution for an alert that was
                # never sent, which reads like a missed page. They differ
                # only for an episode whose state moved, and `alerted_state` is
                # the half that matches what is on the phone. The fallback is
                # for a row paged before the column existed.
                #
                # NOT rate-limited, deliberately — see :func:`cooldown_s`. A
                # recovery only ever follows a page that was sent, so its rate
                # is already the cooldown's; capping it as well would leave a
                # page on the phone with no resolution, which reads as "still
                # broken" and is a silence of its own. The cost is honest and
                # measured: 2 pushes per cooldown window, not 1.
                if (alerted_at and not job.alert_never
                        and not self._suppressed_offline(
                            job, about, state, states, transitions,
                            returning, cooled)):
                    intents.append(AlertIntent(job, "recovery",
                                               alerted_state or about))
                db.set_alert_episode(conn, job.id, None, None)
                continue

            if not is_alertable(state):
                # UNKNOWN: "no data yet", not "broken". Clear the bookkeeping
                # exactly like an OK — a job that passes through UNKNOWN while
                # paged would otherwise keep `alerted_at` for ever and never be
                # pageable again — but send NO recovery: nothing was verified
                # fixed, and a "→ OK" for it would be a lie.
                if bad_since or alerted_at:
                    db.set_alert_episode(conn, job.id, None, None)
                continue

            # Not OK, and worth alerting on. An episode is only CLOSED by a
            # recompute that sees the dwell served (the branch above). The 60 s
            # ticker guarantees one, and every ping recomputes the whole board
            # as well. If both ever stopped, a stale `bad_since` would make this
            # job page sooner than its threshold rather than later — the right
            # direction.
            if bad_since is None:
                bad_since, began, alerted_at, alerted_state = at, now, None, None
                db.set_alert_episode(conn, job.id, bad_since, None)
            self._page(conn, ep, state, bad_since, began, alerted_at,
                       alerted_state, states, transitions, returning, cooled,
                       now, at, intents)
        return intents

    @staticmethod
    def _holding(ep: Episode, about: str) -> bool:
        """Does this OK leave the episode open for want of evidence?

        Two flavours, and they are held on different grounds:

        - ``dest_unverified`` — the destination check could not be made. Only
          holds a ``STALE_DEST``/``BEHIND`` episode, because that episode was a
          statement about the destination. A LATE/FAIL episode is about silence
          or a failed run, and the heartbeat coming back settles that fact on
          its own; holding those broke recovery for every unprobed job.
        - ``damped_failure`` — ``dashboard-probes`` writes its own heartbeat and
          reports ``ok`` while damping a transient probe failure (PR #8), so for
          that one job an ``ok`` run is not evidence of health at all. Holds
          whatever the episode was about, for exactly that reason: the heartbeat
          cannot settle a fact it is currently suppressing.
        """
        if ep.damped_failure:
            return True
        return ep.dest_unverified and about in DEST_DRIVEN_STATES

    def _recompute_pass(self, conn, now: float
                        ) -> tuple[dict[str, str], list[AlertIntent]]:
        """Recompute every declared job inside an open transaction, then run the
        episode/alert pass over the finished batch."""
        states: dict[str, str] = {}
        transitions: dict[str, tuple] = {}
        episodes: list[Episode] = []
        # One query for the whole batch: which probed destinations are currently
        # failing. Used only to judge the self-job's heartbeat (see _holding).
        failing_probes = db.failing_probe_job_ids(
            conn, (j.id for j in self.registry.probed()))
        for job in self.registry:
            state, tr, row, facts = self._recompute_locked(conn, job, now)
            states[job.id] = state
            prev = row.get("state") or "UNKNOWN"
            if tr:
                transitions[job.id] = tr
                log.info("state %s: %s -> %s (%s)", job.id, *tr)
            is_ok = state == "OK"
            episodes.append(Episode(
                job, prev, state, row,
                dest_unverified=is_ok and ok_is_unverified(job, facts, now),
                damped_failure=(is_ok and job.id == SELF_JOB_ID
                                and bool(failing_probes))))
        return states, self._resolve_alerts(conn, episodes, states,
                                            transitions, now)

    def _dispatch(self, intents: list[AlertIntent]) -> None:
        """Send each decided alert, then give back the page for any that did not
        land. ``Notifier.send`` returns False for a 429, a 5xx, a DNS failure or
        a timeout — and the episode had already been stamped, so without this
        one unlucky POST buys permanent silence for the whole episode: the next
        pass short-circuits on ``alerted_at``, and even the recovery is
        suppressed (it only fires for an episode we paged for).

        **A failed RECOVERY is logged and dropped** — accepted, not overlooked.
        Its episode is already closed (``bad_since``/``alerted_at`` NULL), so
        there is no episode to hand it back to, and re-sending it later from an
        in-memory queue risks announcing "→ OK" for a job that has broken again
        in the meantime. Losing it costs a piece of good news, never a page: the
        next real problem opens a fresh episode with an unspent page. Grep
        ``recovery push for`` in the container log if one seems to be missing.
        """
        unsent: list[AlertIntent] = []
        for intent in intents:
            job = intent.job
            sent = False
            try:
                if intent.kind == "recovery":
                    sent = self.notifier.notify_recovery(job.name, job.id,
                                                         intent.state)
                elif intent.kind == "escalation":
                    sent = self.notifier.notify_escalation(
                        job.name, job.id, intent.prev_alerted_state or "",
                        intent.state)
                else:
                    sent = self.notifier.notify_alert(job.name, job.id,
                                                      intent.state,
                                                      job.alert_after_s,
                                                      within_s=intent.within_s)
            except Exception:  # noqa: BLE001 — belt and braces
                log.exception("notifier raised; ignoring")
            if not sent and intent.kind == "recovery" and self.notifier.enabled:
                log.warning("recovery push for %s (%s → OK) did not land; its "
                            "episode is already closed, so it is dropped",
                            job.id, intent.state)
            if intent.kind in ("alert", "escalation") and not sent:
                unsent.append(intent)
        # `notify_alert` also returns False when ntfy is simply switched off;
        # rolling back then would rewrite the row every 60 s for ever, so only a
        # configured-and-failing notifier gets the retry.
        if unsent and self.notifier.enabled:
            self._return_unsent_pages(unsent)

    def _return_unsent_pages(self, unsent: list[AlertIntent]) -> None:
        """Put back what a failed POST had already claimed — the episode's
        ``alerted_at``/``alerted_state`` as they were before it, so the next tick
        retries, **and ``last_paged_at``**, so the undelivered page does not
        start a cooldown. A short follow-up transaction, after dispatch, never
        holding the write lock across an HTTP call.

        Restoring the cooldown stamp is not a nicety. Without it the rollback is
        a half-rollback: the episode gets its page back and then cannot spend it
        for six hours, because a POST that never reached ntfy still looks like a
        page to the rate limiter. One unlucky 429 would buy a whole cooldown of
        silence — the failure this method exists to prevent, moved one field to
        the left. Both halves are undone or neither.

        Only for the SAME episode: if the job has recovered, moved on, or been
        re-stamped since, the page belongs to whatever is there now. The failure
        direction becomes "possibly one duplicate page" (the alert landed but we
        could not tell), which is the correct side to err on.

        **Episode identity is the ONLY test**, deliberately. An earlier version
        skipped any job whose state was no longer alertable, on the reasoning
        that its episode must be over — which is false here: the OK dwell and
        the unverified-OK hold both keep an episode open while the job reads OK.
        A container flapping on `restart: unless-stopped` backoff can easily be
        up for the 5 s an ntfy timeout takes, and the rollback would then be
        skipped for the one episode that most needed it — stamped, undelivered,
        and unable to page again for as long as the outage lasted. A genuinely
        closed episode cannot be mistaken for an open one here: closing clears
        ``bad_since`` to NULL, so the identity check below rejects it anyway.
        """
        conn = self.connect()
        try:
            with transaction(conn):
                for intent in unsent:
                    row = db.job_row(conn, intent.job.id) or {}
                    if (row.get("bad_since") != intent.bad_since
                            or row.get("alerted_at") != intent.alerted_at):
                        continue              # a different episode owns the row
                    log.warning("ntfy push for %s did not land; returning the "
                                "episode's page and its cooldown so the next "
                                "tick retries", intent.job.id)
                    # For a first page the restored pair is (None, None), which
                    # is what this always wrote. For an ESCALATION it is the
                    # pair that page replaced: clearing to NULL instead would
                    # re-arm the episode's FIRST page as well and send it twice,
                    # while restoring the pair leaves the escalation still due —
                    # `alert_severity` sees the worse state again on the next
                    # pass. Delayed, never cancelled, like every other hold.
                    db.set_alert_episode(conn, intent.job.id, intent.bad_since,
                                         intent.prev_alerted_at,
                                         intent.prev_alerted_state)
                    db.set_last_paged_at(conn, intent.job.id,
                                         intent.prev_paged_at)
        except Exception:  # noqa: BLE001 — alerting must never raise into ingest
            log.exception("could not return the page for a failed ntfy push")
        finally:
            conn.close()

    def recompute_all(self, now: float | None = None) -> dict[str, str]:
        """Ticker entry point: recompute every declared job."""
        now = time.time() if now is None else now
        conn = self.connect()
        try:
            with transaction(conn):
                states, intents = self._recompute_pass(conn, now)
        finally:
            conn.close()
        self._dispatch(intents)
        return states

    # -- ingest ------------------------------------------------------------ #

    def record_ping(self, job: Job, payload: dict, source: str = "ping",
                    now: float | None = None) -> str:
        """Record a validated heartbeat and return the job's computed state.

        ``payload`` is the output of :func:`dashboard.ingest.parse_payload`:
        status ∈ {ok, fail, skipped, metric}, optional started_at/finished_at
        (ISO), reason, exit_code, note, metrics. ``metric`` refreshes metrics
        only — it is not a run and never counts as a success.
        """
        now = time.time() if now is None else now
        at = db.to_iso(now)
        conn = self.connect()
        try:
            with transaction(conn):
                metrics = payload.get("metrics") or {}
                if payload["status"] != "metric":
                    db.insert_run(
                        conn, job.id, received_at=at, status=payload["status"],
                        started_at=payload.get("started_at"),
                        finished_at=payload.get("finished_at"),
                        reason=payload.get("reason"),
                        exit_code=payload.get("exit_code"),
                        note=payload.get("note"), metrics=metrics or None,
                        source=source)
                if metrics:
                    db.merge_metrics(conn, job.id, metrics, at)
                # Recompute the whole board so LATE keeps firing for other jobs
                # even if the ticker thread ever dies — and run the SAME episode
                # pass the ticker runs, so a threshold can be crossed on a ping
                # as well as on a tick. These are the two dispatch paths and
                # they must not diverge.
                states, intents = self._recompute_pass(conn, now)
        finally:
            conn.close()
        self._dispatch(intents)
        return states.get(job.id, "UNKNOWN")

    # -- probes ------------------------------------------------------------ #

    def due_probes(self, conn, now: float) -> list[Job]:
        """The probed jobs whose own ``probe.interval_s`` has elapsed since
        their last probe row. A job without ``interval_s`` is probed every
        cycle, i.e. on the global ``PROBE_INTERVAL_S``.

        The clock is the persisted ``probes.probed_at``, not an in-memory
        timer, so a restart does not re-probe a big tree early — the point of a
        long interval is to stop hammering a rate-limited remote.

        Ordered least-recently-probed first: a cycle can run out of its
        wall-clock budget part way down the list (see
        ``Settings.probe_cycle_budget_s``), and this ordering is what stops the
        same slow job at the tail being starved forever.
        """
        due: list[tuple[float, Job]] = []
        for job in self.registry.probed():
            interval = job.probe_interval_s
            last = db.last_probe(conn, job.id)
            ts = db.from_iso(last.get("probed_at")) if last else None
            if interval is not None and ts is not None and now - ts < interval:
                continue
            # Never probed sorts first.
            due.append((-1.0 if ts is None else ts, job))
        due.sort(key=lambda pair: pair[0])      # stable: registry order breaks ties
        return [job for _, job in due]

    def probe_trouble(self, conn, now: float,
                      results: dict | None = None) -> list[dict]:
        """Every probed job whose LATEST probe row is a failure, with the facts
        the self-job's verdict is made from.

        This is read from the ``probes`` table rather than from a counter, which
        is what makes the damping correct and unforgeable:

        - it is **per job**, so a failure on a job probed every 1800 s is not
          erased by the five intervening cycles in which it was not due (the bug
          this replaces: a persistent failure on an interval'd job could never
          reach the threshold, so it never alerted at all);
        - a job's streak clears only when **that job** probes successfully;
        - it survives a restart, like the metric it replaces;
        - the ingest route cannot write the ``probes`` table, so a holder of
          INGEST_TOKEN can no longer suppress (or force) alerting.
        """
        results = results or {}
        out: list[dict] = []
        for job in self.registry.probed():
            last = db.last_probe(conn, job.id)
            if not last or last.get("ok"):
                continue
            error = last.get("error") or "probe failed"
            # For a job probed in this cycle the classification is the one the
            # probe itself made; for an older row, re-derive it from the text.
            fresh = results.get(job.id)
            transient = (fresh.transient if fresh is not None and not fresh.ok
                         else probes.is_transient_error(error))
            ok_row = db.last_ok_probe(conn, job.id)
            # No successful probe ever: measure the silence from the oldest
            # failure we still hold.
            ref_row = ok_row or db.oldest_probe(conn, job.id) or {}
            ref = db.from_iso(ref_row.get("probed_at"))
            out.append({
                "job_id": job.id,
                "error": error,
                "streak": db.probe_fail_streak(conn, job.id, STREAK_SCAN_LIMIT),
                "transient": transient,
                "ever_ok": ok_row is not None,
                "no_success_s": None if ref is None else max(0, int(now - ref)),
                "trip": None,
            })
        return out

    def _classify_trouble(self, trouble: list[dict]) -> None:
        """Decide, per failing job, whether it trips the self-job to FAIL.

        Three ways in, in order of precedence:

        ``hard``    the error is not a quota/timeout — a missing directory or a
                    revoked token is not noise, it is the answer, so it is not
                    damped at all and trips the self-job to FAIL on the FIRST
                    failure (this is the latency the un-damped version had, kept
                    for real errors). **FAIL, not a push**: the state is what
                    arrives at once, and the page then waits out
                    ``dashboard-probes``' own ``alert_after_s`` like every other
                    job's — six hours on the shipped file. Damping decides when
                    the board goes red; the episode rules decide when the phone
                    rings, and nothing here can shorten that.
        ``streak``  ``PROBE_FAIL_THRESHOLD`` consecutive failed probes of this
                    job. This is the damping proper: the measured noise was
                    isolated single failures, never adjacent ones.
        ``silence`` nothing has successfully probed this destination for
                    ``PROBE_NO_SUCCESS_S``. The backstop: damping may delay an
                    alert, it may never cancel one. Without it, failures that
                    alternate with successes could be damped forever.
        """
        threshold = self.settings.effective_fail_threshold
        window = self.settings.effective_no_success_s
        for t in trouble:
            if not t["transient"]:
                t["trip"] = "hard"
            elif t["streak"] >= threshold:
                t["trip"] = "streak"
            elif (window and t["no_success_s"] is not None
                    and t["no_success_s"] > window):
                t["trip"] = "silence"

    def run_probe_cycle(self, now: float | None = None) -> dict[str, probes.ProbeResult]:
        """Probe the jobs that are due, record results, then record a run for
        the dashboard's own ``dashboard-probes`` job and recompute.

        The self-job is reported ``fail`` when any probed job is in a tripped
        failure state (:meth:`_classify_trouble`) — a verdict over the persisted
        per-job probe history, not over this cycle alone. So a cycle in which
        nothing was due neither invents a success nor clears a pending failure;
        it only records the self-heartbeat that keeps the dead-man's switch fed.
        A damped failure records ``ok`` with the error text in
        ``reason``/``note`` and in the probe rows. Recovery stays immediate: one
        successful probe of the offending job clears it.

        Google Drive answers ``rateLimitExceeded`` on a ~1000-object listing
        often enough that alerting on a single failure produced 27 FAIL→OK flips
        in four days (21 failures in 1159 probes, none of them adjacent) while
        nothing was wrong — noise that buried a real backup failure in the same
        window. Never raises.
        """
        now = time.time() if now is None else now
        conn = self.connect()
        try:
            jobs = self.due_probes(conn, now)
        finally:
            conn.close()
        timeout = self.settings.effective_rclone_timeout_s
        budget = self.settings.probe_cycle_budget_s
        results: dict[str, probes.ProbeResult] = {}
        started = _monotonic()
        skipped = 0
        for index, job in enumerate(jobs):
            # Probes are serial, so n × timeout can run well past a cycle. Stop
            # when the budget is spent; the rest stay due and are picked up next
            # cycle (oldest first). Always run at least one, so a cycle can
            # never do nothing at all.
            if index and _monotonic() - started >= budget:
                skipped = len(jobs) - index
                log.warning("probe cycle budget (%ss) spent after %d of %d "
                            "jobs; %d deferred to the next cycle",
                            budget, index, len(jobs), skipped)
                break
            try:
                results[job.id] = probes.probe_job(job, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                log.exception("probe_job %s raised", job.id)
                text = f"{type(exc).__name__}: {exc}"[:300]
                results[job.id] = probes.ProbeResult(
                    ok=False, error=text,
                    transient=probes.is_transient_error(text))
        # Stamp and judge at the END of the cycle: a cycle that spent 900 s
        # listing Drive must not write rows (or compute LATE) against a clock
        # from before it started.
        end = now + max(0.0, _monotonic() - started)
        self.last_cycle_end = end
        at = db.to_iso(end)
        conn = self.connect()
        try:
            with transaction(conn):
                for job_id, res in results.items():
                    db.insert_probe(conn, job_id, probed_at=at, ok=res.ok,
                                    newest_iso=res.newest_iso, count=res.count,
                                    state_sha=res.state_sha,
                                    state_push_epoch=res.state_push_epoch,
                                    error=res.error)
                self_job = self.registry.get(SELF_JOB_ID)
                if self_job is not None:
                    trouble = self.probe_trouble(conn, end, results)
                    self._classify_trouble(trouble)
                    tripped = any(t["trip"] for t in trouble)
                    failed_now = [r for r in results.values() if not r.ok]
                    metrics = {
                        "probed": len(results),
                        "failed": len(failed_now),
                        "failed_transient": sum(1 for r in failed_now
                                                if r.transient),
                        "deferred": skipped,
                        "probe_fail_jobs": len(trouble),
                        "probe_fail_streak": max((t["streak"] for t in trouble),
                                                 default=0),
                    }
                    db.insert_run(
                        conn, self_job.id, received_at=at,
                        status="fail" if tripped else "ok",
                        started_at=db.to_iso(now), finished_at=at,
                        reason=self._probe_reason(
                            len(results), trouble,
                            self.settings.effective_fail_threshold),
                        note=self._probe_note(trouble), metrics=metrics,
                        source="scheduler")
                    db.merge_metrics(conn, self_job.id, metrics, at)
                    db.forget_metrics(conn, self_job.id, LEGACY_METRIC_KEYS)
                elif not self._warned_no_self_job:
                    log.warning("no %r job in jobs.yml — the scheduler's own "
                                "heartbeat is not being recorded", SELF_JOB_ID)
                    self._warned_no_self_job = True
                db.prune(conn)
        finally:
            conn.close()
        try:
            self.recompute_all(end)
        except Exception:  # noqa: BLE001
            log.exception("recompute after probe cycle failed")
        return results

    @staticmethod
    def _probe_note(trouble: list[dict]) -> str | None:
        if not trouble:
            return None
        return ("; ".join(f"{t['job_id']}: {t['error']}"
                          for t in trouble))[:NOTE_MAX]

    @staticmethod
    def _probe_reason(n_probed: int, trouble: list[dict],
                      threshold: int) -> str:
        """The self-job's run reason. "nothing was due" reads differently from
        "everything probed clean" (the two used to be indistinguishable, so a
        registry with no probes at all reported a reassuring "probed" forever);
        a damped cycle says what it is waiting for; and a quota/timeout failure
        is named as transient, so "Drive pushed back" never reads like "the
        destination is wrong"."""
        if not trouble:
            return "probed" if n_probed else "no probes due"
        transient = sum(1 for t in trouble if t["transient"])
        kind = ("transient quota/timeout" if transient == len(trouble)
                else f"{transient} transient of {len(trouble)}" if transient
                else "probe error")
        tripped = [t for t in trouble if t["trip"]]
        if tripped:
            worst = max(tripped, key=lambda t: (t["trip"] == "hard",
                                                t["streak"]))
            if worst["trip"] == "hard":
                detail = f"hard error on {worst['job_id']}, not damped"
            elif worst["trip"] == "streak":
                detail = (f"{worst['streak']} consecutive failed probes of "
                          f"{worst['job_id']}")
            elif worst["ever_ok"]:
                detail = (f"no successful probe of {worst['job_id']} for "
                          f"{worst['no_success_s']}s")
            else:
                detail = (f"no successful probe of {worst['job_id']} ever "
                          f"(failing for {worst['no_success_s']}s)")
            return f"probe-error ({kind}, {detail})"
        worst = max(trouble, key=lambda t: t["streak"])
        return (f"probe-error damped ({kind}, failure {worst['streak']} of "
                f"{threshold} before FAIL)")
