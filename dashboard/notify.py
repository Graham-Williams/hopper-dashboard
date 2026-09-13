"""ntfy alerts for sustained problems.

The Notifier is deliberately dumb: it decides nothing about *whether* a job is
worth paging for. :class:`dashboard.services.Core` owns the episode clock and
calls in only when a job has been continuously not-OK past its own threshold
(:meth:`Notifier.notify_alert`) or has come out of an episode it was actually
paged for (:meth:`Notifier.notify_recovery`).

Disabled when either ``NTFY_URL`` or ``NTFY_TOPIC`` is empty. Never raises into
request handling or the scheduler — a broken ntfy must not break ingest.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

from .humanize import human_duration

log = logging.getLogger(__name__)

HIGH_PRIORITY_STATES = ("FAIL", "STALE_DEST")


class Notifier:
    def __init__(self, url: str, topic: str, timeout: float = 5.0):
        self.url = url.rstrip("/")
        self.topic = topic.strip("/ ")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.topic)

    def notify_alert(self, job_name: str, job_id: str, state: str,
                     after_s: int) -> bool:
        """One page for a job that has been not-OK for ``after_s`` seconds.

        ``state`` is the state the episode is *about*, which need not be the
        state the job is in right now: a mid-episode LATE → FAIL does not
        restart the clock, and an episode held open across an OK we could not
        verify is still reported as the problem it started as. Returns True if
        a POST was attempted and succeeded.
        """
        if not self.enabled:
            return False
        # Only the job id, its state and its own configured threshold go to
        # ntfy.sh (a third party): never the free-text reason, which can carry
        # container names, client-supplied notes or rclone stderr. The reason
        # stays on the board.
        title = f"[dashboard] {job_name} → {state}"
        body = (f"{job_id}: {state} for over {human_duration(after_s)}"
                if after_s > 0 else f"{job_id}: {state}")
        priority = "high" if state in HIGH_PRIORITY_STATES else "default"
        return self.send(title, body, priority)

    def notify_recovery(self, job_name: str, job_id: str,
                        from_state: str) -> bool:
        """The end of an episode that was paged for. Never sent for an episode
        Graham was not told about — that check lives in ``Core``."""
        if not self.enabled:
            return False
        return self.send(f"[dashboard] {job_name} → OK",
                         f"{job_id}: {from_state} → OK", "default")

    def send(self, title: str, body: str, priority: str = "default") -> bool:
        if not self.enabled:
            return False
        try:
            return self._post(title, body, priority)
        except Exception as exc:  # noqa: BLE001 — alerts must never raise
            log.warning("ntfy dispatch failed: %s", exc)
            return False

    def _post(self, title: str, body: str, priority: str) -> bool:
        req = urllib.request.Request(
            f"{self.url}/{self.topic}",
            data=body.encode("utf-8"), method="POST",
            headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                     "Priority": priority,
                     "Content-Type": "text/plain; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return 200 <= resp.status < 300
