"""Background scheduler thread (ingest process only, single gunicorn worker).

Two cadences on one thread: a state ticker every ``TICK_INTERVAL_S`` (default
60 s) so LATE fires without traffic, and a destination-probe cycle every
``PROBE_INTERVAL_S`` (default 300 s). The first probe cycle runs shortly after
start so the board is populated within seconds of a deploy. Any exception is
logged and the loop continues — the scheduler must outlive a bad probe.
"""

from __future__ import annotations

import logging
import threading
import time

from .services import Core

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, core: Core, tick_interval_s: int = 60,
                 probe_interval_s: int = 300, initial_delay_s: float = 3.0):
        self.core = core
        self.tick_interval_s = max(5, int(tick_interval_s))
        self.probe_interval_s = max(30, int(probe_interval_s))
        self.initial_delay_s = initial_delay_s
        # Two more cadences, both writing inbox.db and NEITHER inside
        # run_probe_cycle: that cycle is serial, carries a wall-clock budget and
        # can sit for minutes on a blocking rclone listing, so anything sharing
        # it is either starved by the budget or spends it. Floors keep a
        # mistyped env var from turning either into a busy loop.
        self.github_interval_s = max(60, int(core.settings.inbox_github_interval_s))
        self.prune_interval_s = max(300, int(core.settings.inbox_prune_interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick: float | None = None
        self.last_probe: float | None = None
        self.last_github: float | None = None
        self.last_prune: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="dashboard-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def step(self, now: float | None = None) -> None:
        """One iteration: probe if due, then tick, then the Inbox's two jobs.

        Four cadences, four independent try/excepts. The board's own probing
        must not be able to die because GitHub 500'd, and the GitHub sync must
        not be skipped because a probe raised — that is why each of these sits
        in its own block rather than sharing one. Exposed for tests.
        """
        now = time.time() if now is None else now
        try:
            if (self.last_probe is None
                    or now - self.last_probe >= self.probe_interval_s):
                self.core.run_probe_cycle(now)
                # Schedule from the END of the cycle, not its start. Probes are
                # serial and a slow cycle can outlast the interval; measured
                # from the start, the next cycle would already be due the moment
                # this one returned — continuous back-to-back listing of the very
                # remote that just rate-limited us.
                end = self.core.last_cycle_end
                self.last_probe = now if end is None else max(now, end)
            else:
                self.core.recompute_all(now)
        except Exception:  # noqa: BLE001
            log.exception("scheduler step failed; continuing")
        finally:
            self.last_tick = now
        self._run_due(now, "last_github", self.github_interval_s,
                      self.core.sync_inbox_github, "inbox github sync")
        self._run_due(now, "last_prune", self.prune_interval_s,
                      self.core.prune_inbox_audio, "inbox audio prune")

    def _run_due(self, now: float, slot: str, interval_s: int, work,
                 label: str) -> None:
        """Run ``work(now)`` if ``interval_s`` has elapsed, and re-arm from the
        END of the run.

        Re-arming from the end, not the start, for the same reason the probe
        cycle does: a GitHub sync that spent five minutes being rate-limited
        would otherwise be instantly due again on return, which is how a client
        talks itself into a longer ban. Measured with a monotonic delta applied
        to the caller's clock, so a test driving a fixed ``now`` stays
        deterministic.
        """
        last = getattr(self, slot)
        if last is not None and now - last < interval_s:
            return
        started = time.monotonic()
        try:
            work(now)
        except Exception:  # noqa: BLE001
            log.exception("%s failed; continuing", label)
        finally:
            setattr(self, slot, now + max(0.0, time.monotonic() - started))

    def _loop(self) -> None:
        log.info("scheduler started (tick %ss, probes %ss)",
                 self.tick_interval_s, self.probe_interval_s)
        if self._stop.wait(self.initial_delay_s):
            return
        while not self._stop.is_set():
            self.step()
            if self._stop.wait(self.tick_interval_s):
                break
        log.info("scheduler stopped")
