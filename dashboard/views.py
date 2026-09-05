"""Read-side view models: the ``/api/v1/status`` contract and per-job detail.

The read process never computes or writes state — it reports what the ingest
process stored. The one derived thing it adds is ``summary.computed_at`` (the
newest ``jobs.updated_at``), so a dead scheduler is visible on the board.
"""

from __future__ import annotations

import time

from . import db
from .registry import Job, Registry
from .state import Facts, dest_info, lag_info

HISTORY_LEN = 30


def _facts(conn, job: Job, row: dict | None) -> Facts:
    row = row or {}
    return Facts(
        last_run=db.last_run(conn, job.id),
        last_success=db.last_success(conn, job.id),
        last_metrics=row.get("last_metrics") or {},
        last_metrics_at=row.get("last_metrics_at"),
        probe=db.last_probe(conn, job.id) if job.has_probe else None,
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
        "state_reason": row.get("state_reason"),
        "last_run_status": lr.get("status"),
        "last_run_reason": lr.get("reason"),
        "last_run_note": lr.get("note"),
        "late_means": job.late_means,
        "informational": job.informational,
        "expect": list(job.expect) if job.expect else None,
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
