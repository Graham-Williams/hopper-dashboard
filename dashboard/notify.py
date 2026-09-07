"""ntfy alerts on state transitions.

Disabled when either ``NTFY_URL`` or ``NTFY_TOPIC`` is empty. Never raises into
request handling or the scheduler — a broken ntfy must not break ingest.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

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

    def should_notify(self, from_state: str, to_state: str) -> bool:
        # First sighting of a healthy job is not a "recovery" — don't page for it.
        if from_state == "UNKNOWN" and to_state == "OK":
            return False
        return from_state != to_state

    def notify_transition(self, job_name: str, job_id: str, from_state: str,
                          to_state: str, reason: str | None) -> bool:
        """Dispatch one alert for a transition. Returns True if a POST was
        attempted and succeeded."""
        if not self.enabled or not self.should_notify(from_state, to_state):
            return False
        # Only the transition goes to ntfy.sh (a third party): never the
        # free-text reason, which can carry container names, client-supplied
        # notes or rclone stderr. The reason stays on the board.
        del reason
        title = f"[dashboard] {job_name} → {to_state}"
        body = f"{job_id}: {from_state} → {to_state}"
        priority = "high" if to_state in HIGH_PRIORITY_STATES else "default"
        return self.send(title, body, priority)

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
