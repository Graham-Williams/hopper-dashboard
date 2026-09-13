"""ntfy alerts for sustained problems.

The Notifier is deliberately dumb: it decides nothing about *whether* a job is
worth paging for. :class:`dashboard.services.Core` owns the episode clock and
calls in only when a job has been not-OK past its own threshold
(:meth:`Notifier.notify_alert`), when such an episode has got strictly worse
(:meth:`Notifier.notify_escalation`), or when it has come out of an episode it
was actually paged for (:meth:`Notifier.notify_recovery`).

:data:`HIGH_PRIORITY_STATES` is read by ``services.alert_severity`` as well as
by the ``Priority`` header: "would this page be louder than the one already
sent?" is exactly the question the escalation asks, so both answers come from
this one table and cannot drift apart.

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
                     after_s: int, within_s: float | None = None) -> bool:
        """One page for a job that has been not-OK for ``after_s`` seconds.

        ``state`` is the state the episode is *about*, which need not be the
        state the job is in right now: a mid-episode LATE → FAIL does not
        restart the clock, and an episode held open across an OK we could not
        verify is still reported as the problem it started as. Returns True if
        a POST was attempted and succeeded.

        ``within_s`` is set when the page was decided by the accumulator rather
        than by one unbroken run (``services.Core._past_bar``), and the body then
        says so: "``FAIL`` for over 6h in the last 12h" is the truth about an
        intermittent destination, where the plain wording would claim a
        six-hour outage that never happened. Getting this wrong is not
        cosmetic — an alert that overstates what it saw is an alert you learn to
        discount.
        """
        if not self.enabled:
            return False
        # Only the job id, its state and its own configured windows go to
        # ntfy.sh (a third party): never the free-text reason, which can carry
        # container names, client-supplied notes or rclone stderr. The reason
        # stays on the board.
        title = f"[dashboard] {job_name} → {state}"
        if after_s <= 0:
            body = f"{job_id}: {state}"
        elif within_s:
            body = (f"{job_id}: {state} for over {human_duration(after_s)} "
                    f"in the last {human_duration(int(within_s))}")
        else:
            body = f"{job_id}: {state} for over {human_duration(after_s)}"
        return self.send(title, body, self.priority_for(state))

    def notify_escalation(self, job_name: str, job_id: str, from_state: str,
                          to_state: str) -> bool:
        """An episode that already paged has got strictly WORSE.

        Sent at most once per episode (``services.alert_severity``), and it
        carries ``to_state``'s own priority — which is the whole point: the
        defect this fixes was a disk gauge paging "BEHIND for over 1h" at
        default priority and then going silent as it filled and the reading
        failed, so the push that mattered was never sent at any priority.

        Body is the recovery's shape (``job_id: FROM → TO``) because it is the
        same kind of news: a state change worth hearing about, on an episode that
        has already been paged.
        """
        if not self.enabled:
            return False
        return self.send(f"[dashboard] {job_name} → {to_state}",
                         f"{job_id}: {from_state} → {to_state}",
                         self.priority_for(to_state))

    def notify_recovery(self, job_name: str, job_id: str,
                        from_state: str) -> bool:
        """The end of an episode that was paged for. Never sent for an episode
        the operator was not told about — that check lives in ``Core``, which names the
        state that was PAGED rather than the last one the episode passed
        through."""
        if not self.enabled:
            return False
        return self.send(f"[dashboard] {job_name} → OK",
                         f"{job_id}: {from_state} → OK", "default")

    @staticmethod
    def priority_for(state: str) -> str:
        """The ntfy priority a page about ``state`` carries — and, read through
        ``services.alert_severity``, the order an escalation compares."""
        return "high" if state in HIGH_PRIORITY_STATES else "default"

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
