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

    def _dispatch(self, transitions: list[tuple[Job, tuple]]) -> None:
        for job, (prev, state, reason) in transitions:
            log.info("state %s: %s -> %s (%s)", job.id, prev, state, reason)
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
        self._dispatch(transitions)
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
                for j in self.registry:
                    s, tr = self._recompute_locked(conn, j, now)
                    if j.id == job.id:
                        state = s
                    if tr:
                        transitions.append((j, tr))
        finally:
            conn.close()
        self._dispatch(transitions)
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
