"""The write-side core shared by the ingest routes and the scheduler.

Everything that mutates the store goes through :class:`Core` so the rules
(record → recompute → transition → notify) live in one place. Each call opens
its own short-lived SQLite connection; WAL + busy_timeout make that safe across
gunicorn threads, the scheduler thread, and the read process.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from . import db, probes
from .config import Settings
from .notify import Notifier
from .registry import Job, Registry
from .state import Facts, compute_state

log = logging.getLogger(__name__)

SELF_JOB_ID = "dashboard-probes"
NOTE_MAX = 500
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

    @staticmethod
    def gather_facts(conn, job: Job) -> Facts:
        row = db.job_row(conn, job.id) or {}
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
                          ) -> tuple[str, tuple | None]:
        """Recompute inside an open transaction. Returns (state, transition)
        where transition is (from, to, reason) or None."""
        facts = self.gather_facts(conn, job)
        state, reason = compute_state(job, facts, now)
        prev = db.set_state(conn, job.id, state, db.to_iso(now), reason)
        if prev is None:
            return state, None
        return state, (prev, state, reason)

    def _machine_probe(self, machine: str) -> Job | None:
        """The ``probe``-kind job that stands for "this machine is reachable"
        (e.g. ``mac-probe``). The dashboard's own probe job never counts."""
        for j in self.registry:
            if (j.kind == "probe" and j.machine == machine
                    and j.id != SELF_JOB_ID):
                return j
        return None

    def _suppressed_offline(self, job: Job, prev: str, state: str,
                            states: dict[str, str],
                            transitions: dict[str, tuple]) -> bool:
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
        its own and alerts normally."""
        probe = self._machine_probe(job.machine)
        if probe is None or probe.id == job.id:
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
        return False

    def _dispatch(self, transitions: list[tuple[Job, tuple]],
                  states: dict[str, str] | None = None) -> None:
        states = states or {}
        by_id = {job.id: tr for job, tr in transitions}
        for job, (prev, state, reason) in transitions:
            log.info("state %s: %s -> %s (%s)", job.id, prev, state, reason)
            if self._suppressed_offline(job, prev, state, states, by_id):
                log.info("alert for %s suppressed: its machine's probe job is "
                         "offline (one alert for the machine instead)", job.id)
                continue
            try:
                self.notifier.notify_transition(job.name, job.id, prev, state,
                                                reason)
            except Exception:  # noqa: BLE001 — belt and braces
                log.exception("notifier raised; ignoring")

    def recompute_all(self, now: float | None = None) -> dict[str, str]:
        """Ticker entry point: recompute every declared job."""
        now = time.time() if now is None else now
        conn = self.connect()
        states: dict[str, str] = {}
        transitions: list[tuple[Job, tuple]] = []
        try:
            with transaction(conn):
                for job in self.registry:
                    state, tr = self._recompute_locked(conn, job, now)
                    states[job.id] = state
                    if tr:
                        transitions.append((job, tr))
        finally:
            conn.close()
        self._dispatch(transitions, states)
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
        transitions: list[tuple[Job, tuple]] = []
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
                # Recompute the whole board so LATE keeps firing for other
                # jobs even if the ticker thread ever dies.
                state = "UNKNOWN"
                states: dict[str, str] = {}
                for j in self.registry:
                    s, tr = self._recompute_locked(conn, j, now)
                    states[j.id] = s
                    if j.id == job.id:
                        state = s
                    if tr:
                        transitions.append((j, tr))
        finally:
            conn.close()
        self._dispatch(transitions, states)
        return state

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
                    damped at all and pages on the first failure (this is the
                    latency the un-damped version had, kept for real errors).
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
