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
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick: float | None = None
        self.last_probe: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="dashboard-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def step(self, now: float | None = None) -> None:
        """One iteration: probe if due, then tick. Exposed for tests."""
        now = time.time() if now is None else now
        try:
            if (self.last_probe is None
                    or now - self.last_probe >= self.probe_interval_s):
                self.core.run_probe_cycle(now)
                self.last_probe = now
            else:
                self.core.recompute_all(now)
        except Exception:  # noqa: BLE001
            log.exception("scheduler step failed; continuing")
        finally:
            self.last_tick = now

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
