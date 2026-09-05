"""In-memory per-IP sliding-window limiters.

Process-local (a restart clears them). Two flavours share one implementation:
the login limiter counts *failures* (so a correct password resets the IP), the
ping limiter counts *requests*. Bounded memory even under spoofed client IPs.
"""

from __future__ import annotations

import threading
import time


class SlidingWindowLimiter:
    def __init__(self, max_events: int, window_seconds: int,
                 max_tracked_keys: int = 10000):
        self.max_events = max_events
        self.window = window_seconds
        self.max_tracked_keys = max_tracked_keys
        self._events: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> list[float]:
        events = [t for t in self._events.get(key, ()) if now - t < self.window]
        if events:
            self._events[key] = events
        else:
            self._events.pop(key, None)
        return events

    def _sweep(self, now: float) -> None:
        for k in [k for k, ts in self._events.items()
                  if all(now - t >= self.window for t in ts)]:
            self._events.pop(k, None)

    def is_blocked(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            return len(self._prune(key, now)) >= self.max_events

    def record(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            if len(self._events) >= self.max_tracked_keys:
                self._sweep(now)
            self._prune(key, now)
            self._events.setdefault(key, []).append(now)

    def hit(self, key: str, now: float | None = None) -> bool:
        """Record one event and return True if the caller is still allowed."""
        now = time.time() if now is None else now
        with self._lock:
            if len(self._events) >= self.max_tracked_keys:
                self._sweep(now)
            events = self._prune(key, now)
            if len(events) >= self.max_events:
                return False
            self._events.setdefault(key, []).append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)


class LoginRateLimiter(SlidingWindowLimiter):
    """Failed-login limiter: after ``max_failures`` failures within
    ``window_seconds`` an IP is blocked until failures age out. Same behaviour
    as the km-tracker / taste-twin / todoist-points / jjho gate."""

    def __init__(self, max_failures: int = 10, window_seconds: int = 900,
                 max_tracked_ips: int = 10000):
        super().__init__(max_failures, window_seconds, max_tracked_ips)

    record_failure = SlidingWindowLimiter.record
