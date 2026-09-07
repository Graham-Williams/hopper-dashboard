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

    def run_probe_cycle(self, now: float | None = None) -> dict[str, probes.ProbeResult]:
        """Probe every probed job, record results, then record a run for the
        dashboard's own ``dashboard-probes`` job (ok unless a probe errored)
        and recompute. Never raises."""
        now = time.time() if now is None else now
        results: dict[str, probes.ProbeResult] = {}
        for job in self.registry.probed():
            try:
                results[job.id] = probes.probe_job(job)
            except Exception as exc:  # noqa: BLE001
                log.exception("probe_job %s raised", job.id)
                results[job.id] = probes.ProbeResult(
                    ok=False, error=f"{type(exc).__name__}: {exc}"[:300])
        at = db.to_iso(now)
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
                    failures = [f"{jid}: {r.error}" for jid, r in results.items()
                                if not r.ok]
                    db.insert_run(
                        conn, self_job.id, received_at=at,
                        status="fail" if failures else "ok",
                        started_at=at, finished_at=at,
                        reason="probe-error" if failures else "probed",
                        note=("; ".join(failures))[:NOTE_MAX] if failures else None,
                        metrics={"probed": len(results),
                                 "failed": len(failures)},
                        source="scheduler")
                    db.merge_metrics(conn, self_job.id,
                                     {"probed": len(results),
                                      "failed": len(failures)}, at)
                elif not self._warned_no_self_job:
                    log.warning("no %r job in jobs.yml — the scheduler's own "
                                "heartbeat is not being recorded", SELF_JOB_ID)
                    self._warned_no_self_job = True
                db.prune(conn)
        finally:
            conn.close()
        try:
            self.recompute_all(now)
        except Exception:  # noqa: BLE001
            log.exception("recompute after probe cycle failed")
        return results
