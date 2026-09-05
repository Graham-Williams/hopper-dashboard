"""In-memory per-IP sliding-window limiters.

Process-local (a restart clears them). Two flavours share one implementation:
the login limiter counts *failures* (so a correct password resets the IP), the
ping limiter counts *requests*. Bounded memory even under spoofed client IPs:
keys are truncated to ``KEY_MAX_LEN`` and the number of tracked keys is a hard
cap — when a sweep of expired keys is not enough, the key with the oldest
newest-event is evicted (a burst of fresh keys can therefore forget an old
offender, which is the memory-safe side to err on).
"""

from __future__ import annotations

import threading
import time

KEY_MAX_LEN = 64


def _norm_key(key: str) -> str:
    key = key if isinstance(key, str) else str(key)
    return key[:KEY_MAX_LEN]


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

    def _make_room(self, key: str, now: float) -> None:
        """Enforce ``max_tracked_keys`` before inserting ``key``: sweep expired
        keys first, then evict the stalest keys until there is room."""
        if key in self._events or len(self._events) < self.max_tracked_keys:
            return
        self._sweep(now)
        while self._events and len(self._events) >= self.max_tracked_keys:
            stalest = min(self._events, key=lambda k: max(self._events[k]))
            self._events.pop(stalest, None)

    def is_blocked(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        key = _norm_key(key)
        with self._lock:
            return len(self._prune(key, now)) >= self.max_events

    def record(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        key = _norm_key(key)
        with self._lock:
            self._make_room(key, now)
            self._prune(key, now)
            self._events.setdefault(key, []).append(now)

    def hit(self, key: str, now: float | None = None) -> bool:
        """Record one event and return True if the caller is still allowed."""
        now = time.time() if now is None else now
        key = _norm_key(key)
        with self._lock:
            self._make_room(key, now)
            events = self._prune(key, now)
            if len(events) >= self.max_events:
                return False
            self._events.setdefault(key, []).append(now)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(_norm_key(key), None)

    def tracked(self) -> int:
        with self._lock:
            return len(self._events)


class LoginRateLimiter(SlidingWindowLimiter):
    """Failed-login limiter: after ``max_failures`` failures within
    ``window_seconds`` an IP is blocked until failures age out. Same behaviour
    as the km-tracker / taste-twin / todoist-points / jjho gate."""

    def __init__(self, max_failures: int = 10, window_seconds: int = 900,
                 max_tracked_ips: int = 10000):
        super().__init__(max_failures, window_seconds, max_tracked_ips)

    record_failure = SlidingWindowLimiter.record
