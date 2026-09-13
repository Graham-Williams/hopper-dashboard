"""Read-side view models: the ``/api/v1/status`` contract and per-job detail.

The read process never computes or writes state — it reports what the ingest
process stored. The one derived thing it adds is ``summary.computed_at`` (the
newest ``jobs.updated_at``), so a dead scheduler is visible on the board.
"""

from __future__ import annotations

import time

from . import db
from .registry import Job, Registry
from .services import bad_window_s, cooldown_s
from .state import Facts, dest_info, disk_info, lag_info

HISTORY_LEN = 30


def _facts(conn, job: Job, row: dict | None) -> Facts:
    row = row or {}
    return Facts(
        last_run=db.last_run(conn, job.id),
        last_success=db.last_success(conn, job.id),
        last_metrics=row.get("last_metrics") or {},
        last_metrics_at=row.get("last_metrics_at"),
        probe=db.last_probe(conn, job.id) if job.has_probe else None,
        created_at=row.get("created_at"),
    )


def job_entry(conn, job: Job, row: dict | None, now: float,
              history: list[dict] | None = None) -> dict:
    row = row or {}
    f = _facts(conn, job, row)
    lr = f.last_run or {}
    entry = {
        "id": job.id,
        "name": job.name,
        "machine": job.machine,
        "kind": job.kind,
        "state": row.get("state") or "UNKNOWN",
        "since": row.get("since"),
        "last_run": lr.get("received_at"),
        "last_success": (f.last_success or {}).get("received_at"),
        "cadence_s": job.cadence_s,
        "grace_s": job.grace_s,
        "destination": job.destination,
        "protects": job.protects,
        "method": job.method,
        "lag": lag_info(job, f, now),
        "dest": dest_info(job, f, now),
        "last_metrics": f.last_metrics,
        # Additive (not in the frozen contract, safe to ignore):
        "disk": disk_info(job, f),   # kind: disk only, else null
        "state_reason": row.get("state_reason"),
        "last_run_status": lr.get("status"),
        "last_run_reason": lr.get("reason"),
        "last_run_note": lr.get("note"),
        "late_means": job.late_means,
        # The RESOLVED alert policy plus where this job stands in its current
        # episode, so the policy is inspectable rather than inferred from
        # jobs.yml. `after_s` is null when the job never pages; `source` names
        # what decided it (an explicit key, `alert: never`, `informational`, or
        # the conservative default); `bad_since` non-null means an episode is
        # running (which it can be while the state reads OK — see
        # services._resolve_alerts); `alerted_at` non-null means it was paged.
        # `cooldown_s`/`last_paged_at` are the per-job page rate limit: after a
        # page, the next one for this job is held back for `cooldown_s` —
        # delayed, never cancelled (services.cooldown_s). Both are null for a
        # job that never pages. Reading them together answers the only question
        # worth asking when the phone is quiet but the board is not: is this job
        # unpaged because nothing crossed a threshold, or because it paged
        # recently?
        # `alerted_state` is WHAT that page said, which is not always the state
        # on the card: an episode paged as BEHIND that has since gone FAIL shows
        # `state: FAIL` beside `alerted_state: BEHIND` until the escalation goes
        # out. Without it, "paged 2h ago" next to a red card is unreadable.
        "alert": {
            "after_s": None if job.alert_never else job.alert_after_s,
            "never": job.alert_never,
            "source": job.alert_source,
            "bad_since": row.get("bad_since"),
            "alerted_at": row.get("alerted_at"),
            "alerted_state": row.get("alerted_state"),
            # The accumulator's window: this job also pages after `after_s` of
            # not-OK time (in one state) inside `window_s`, not only after
            # `after_s` unbroken. Exposed so the job page can say both rules out
            # loud rather than describing half the policy.
            "window_s": None if job.alert_never else int(bad_window_s(job)),
            "cooldown_s": None if job.alert_never else int(cooldown_s(job)),
            "last_paged_at": row.get("last_paged_at"),
        },
        "informational": job.informational,
        "expect": list(job.expect) if job.expect else None,
        "never_run": lr == {},
        "created_at": row.get("created_at"),
    }
    if history is not None:
        entry["history"] = [{"status": h["status"], "at": h["received_at"]}
                            for h in history]
    return entry


def build_status(conn, registry: Registry, now: float | None = None,
                 with_history: bool = False) -> dict:
    now = time.time() if now is None else now
    rows = db.all_job_rows(conn)
    histories = db.run_history_all(conn, HISTORY_LEN) if with_history else {}
    jobs = [job_entry(conn, job, rows.get(job.id), now,
                      histories.get(job.id, []) if with_history else None)
            for job in registry]
    summary = {k: 0 for k in ("ok", "late", "fail", "stale_dest", "behind",
                              "unknown")}
    for j in jobs:
        summary[j["state"].lower()] = summary.get(j["state"].lower(), 0) + 1
    summary["total"] = len(jobs)
    computed = [r.get("updated_at") for r in rows.values() if r.get("updated_at")]
    summary["computed_at"] = max(computed) if computed else None
    return {"generated_at": db.to_iso(now), "summary": summary, "jobs": jobs}


def build_job_detail(conn, registry: Registry, job_id: str, limit: int = 100,
                     now: float | None = None) -> dict | None:
    job = registry.get(job_id)
    if job is None:
        return None
    now = time.time() if now is None else now
    row = db.job_row(conn, job.id)
    entry = job_entry(conn, job, row, now, db.recent_runs(conn, job.id, HISTORY_LEN))
    runs = db.recent_runs(conn, job.id, limit)
    changes = db.recent_state_changes(conn, job.id, limit)
    probes = None
    if job.has_probe:
        probes = [db.row_to_dict(r) for r in conn.execute(
            "SELECT * FROM probes WHERE job_id=? ORDER BY probed_at DESC, id DESC"
            " LIMIT ?", (job.id, min(limit, 50))).fetchall()]
    return {"generated_at": db.to_iso(now), "job": entry, "runs": runs,
            "state_changes": changes, "probes": probes}
