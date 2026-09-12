#!/usr/bin/python3
"""Mac probe for hopper-dashboard — run hourly by launchd (com.hopper.dashboard-probe).

Computes, on the Mac, what only the Mac can see, and POSTs it to the dashboard's Tailscale-only
ingest port:
  pa-backup          last outcome of the nightly backup (log tail, reported once per run) +
                     destination lag for the three trees the script copies (personal-assistant,
                     hopper-memory, claude-config) vs gdrive:Backups/<tree> (rclone check), split into
                     MISSING (never uploaded → stale) and DIFFER (edited since → informational)
  minecraft-offload  bytes/files under ~/minecraft-channel not yet on Drive, per pair + disk free
  mac-disk           free/total bytes of the data volume (the dashboard's disk gauge + thresholds)
  drive-mirror       DriveFS mirror queue/mismatch counts (copied sqlite)
  mac-probe          the probe's own heartbeat: ok, or fail + which sub-probes errored

Each sub-probe is isolated — one failure never blocks the others — and a line is ALWAYS written
to ~/Library/Logs/hopper-dashboard-probe.log on failure (same discipline as the backup script).

Config: ~/.config/hopper-dashboard/env (KEY=VALUE, chmod 600): DASHBOARD_URL, INGEST_TOKEN and
optional PROBE_* overrides (see load_settings). Runs under the stock /usr/bin/python3 (3.9), stdlib only.

  --dry-run   compute everything and PRINT the pings instead of sending; state is not updated.
  --only X    run a single sub-probe (pa-backup | minecraft-offload | drive-mirror) — for debugging.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from typing import Callable, Dict, List, Optional, Tuple

# Allow `/usr/bin/python3 probes/mac_probe.py` (script mode) as well as `python -m probes.mac_probe`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import backup_log, drivefs, rclone_check  # noqa: E402
from probes.common import (  # noqa: E402
    DEFAULT_ENV_FILE,
    DEFAULT_LOG_FILE,
    DEFAULT_STATE_FILE,
    HTTP_TIMEOUT_S,
    RCLONE_TIMEOUT_S,
    Logger,
    ProbeError,
    build_ping,
    find_rclone,
    join_nonempty,
    load_config,
    load_state,
    now_iso,
    require_dashboard_url,
    save_state,
    send_ping,
    slug,
)

Ping = Tuple[str, Dict[str, object]]  # (job_id, body)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def load_settings(env_path: str) -> Dict[str, str]:
    cfg = load_config(env_path)
    home = os.path.expanduser("~")
    defaults = {
        "PROBE_LOG_FILE": DEFAULT_LOG_FILE,
        "PROBE_STATE_FILE": DEFAULT_STATE_FILE,
        "PROBE_RCLONE": "",
        "PROBE_RCLONE_TIMEOUT": str(RCLONE_TIMEOUT_S),
        "PROBE_HTTP_TIMEOUT": str(HTTP_TIMEOUT_S),
        # pa-backup. PROBE_PA_REMOTE is the Backups REMOTE ROOT (the script's $REMOTE); each tree in
        # rclone_check.PA_BACKUP_TREES is checked against <root>/<subpath>. PROBE_PA_HOME lets tests
        # point the tree list at a scratch $HOME.
        "PROBE_PA_LOG": os.path.join(home, "Library/Logs/hopper-backup.log"),
        "PROBE_PA_HOME": home,
        "PROBE_PA_REMOTE": "gdrive:Backups",
        # minecraft-offload
        "PROBE_MC_BASE": os.path.join(home, "minecraft-channel"),
        "PROBE_MC_REMOTE": "gdrive",
        "PROBE_MC_SCRIPT": os.path.join(home, "personal-assistant/scripts/offload-recordings.sh"),
        "PROBE_MC_MIN_AGE": rclone_check.OFFLOAD_MIN_AGE,
        "PROBE_DISK_PATH": "/System/Volumes/Data",
        # drive-mirror
        "PROBE_DRIVEFS_DIR": drivefs.DEFAULT_DRIVEFS_DIR,
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    return cfg


# ---------------------------------------------------------------------------
# Sub-probes. Each returns a list of (job_id, body) pings and may raise ProbeError.
# ---------------------------------------------------------------------------
def probe_pa_backup(cfg: Dict[str, str], state: Dict[str, object], log: Logger) -> Tuple[List[Ping], Optional[backup_log.LogEntry]]:
    pings: List[Ping] = []
    errors: List[str] = []

    # (1) last run outcome from the log tail — reported once per new line
    new_entry: Optional[backup_log.LogEntry] = None
    entry = backup_log.read_last_entry(cfg["PROBE_PA_LOG"])
    if entry is None:
        log.log("pa-backup: log %s missing or empty — no run to report" % cfg["PROBE_PA_LOG"])
    elif backup_log.is_new(entry, state):
        fields = backup_log.entry_to_ping_fields(entry)
        pings.append(("pa-backup", build_ping(**fields)))
        new_entry = entry
    else:
        log.log("pa-backup: last log line already reported (%s)" % entry.raw[:60])

    # (2) destination freshness — every run, for each tree the backup script copies.
    #     MISSING = on the Mac, never reached Drive (→ the dashboard's STALE_DEST).
    #     DIFFER  = on both sides but edited locally since the last nightly copy (informational
    #               lag: a working tree always trails its 03:00 snapshot; NOT stale).
    #     Summed across trees; per-tree breakdown under *_<tree> keys.
    rclone = find_rclone(cfg.get("PROBE_RCLONE") or None)
    if not rclone:
        raise ProbeError("rclone not found (checked /opt/homebrew/bin, /usr/local/bin, PATH)")
    timeout = float(cfg["PROBE_RCLONE_TIMEOUT"])
    remote_root = cfg["PROBE_PA_REMOTE"].rstrip("/")
    home = cfg["PROBE_PA_HOME"]
    totals = {"missing_files": 0, "missing_bytes": 0, "differ_files": 0, "differ_bytes": 0,
              "matched_files": 0, "check_errors": 0, "trees": len(rclone_check.PA_BACKUP_TREES),
              "trees_checked": 0, "trees_errored": 0}
    metrics: Dict[str, object] = {}
    missing_sample: List[str] = []
    dest_count = 0
    dest_bytes = 0
    dest_seen = 0
    for name, rel, sub, filters in rclone_check.PA_BACKUP_TREES:
        src = os.path.join(home, rel)
        dst = "%s/%s" % (remote_root, sub)
        key = slug(name)
        if not os.path.isdir(src):
            log.log("pa-backup: %s not found locally, skipping" % src)
            continue
        try:
            res = rclone_check.rclone_check(rclone, src, dst, filters=filters or None, timeout=timeout)
        except ProbeError as e:
            errors.append(str(e))
            totals["trees_errored"] += 1
            continue
        missing_bytes, _ = rclone_check.sum_local_sizes(src, res.missing)
        differ_bytes, _ = rclone_check.sum_local_sizes(src, res.differ)
        metrics["missing_files_" + key] = len(res.missing)
        metrics["missing_bytes_" + key] = missing_bytes
        metrics["differ_files_" + key] = len(res.differ)
        totals["missing_files"] += len(res.missing)
        totals["missing_bytes"] += missing_bytes
        totals["differ_files"] += len(res.differ)
        totals["differ_bytes"] += differ_bytes
        totals["matched_files"] += len(res.matched)
        totals["check_errors"] += len(res.errors)
        totals["trees_checked"] += 1
        missing_sample.extend("%s:%s" % (key, p) for p in res.missing[:3])
        log.log("pa-backup: %s → %d missing / %d differ / %d matched" % (name, len(res.missing), len(res.differ), len(res.matched)))
        try:
            count, size = rclone_check.rclone_size(rclone, dst, timeout=timeout)
            if count is not None and size is not None:
                dest_count += count
                dest_bytes += size
                dest_seen += 1
        except ProbeError as e:
            errors.append(str(e))
    if totals["trees_checked"]:
        metrics.update(totals)
        if missing_sample:
            metrics["missing_sample"] = ",".join(missing_sample[:6])
    if dest_seen:
        metrics["dest_count"] = dest_count
        metrics["dest_bytes"] = dest_bytes
        metrics["dest_trees"] = dest_seen
    if metrics:
        pings.append(("pa-backup", build_ping("metric", note=join_nonempty(errors) or None, metrics=metrics)))
    if errors and not metrics:
        raise ProbeError(join_nonempty(errors))
    if errors:
        log.error("pa-backup partial: " + join_nonempty(errors))
    return pings, new_entry


def probe_minecraft_offload(cfg: Dict[str, str], log: Logger) -> List[Ping]:
    rclone = find_rclone(cfg.get("PROBE_RCLONE") or None)
    if not rclone:
        raise ProbeError("rclone not found (checked /opt/homebrew/bin, /usr/local/bin, PATH)")
    timeout = float(cfg["PROBE_RCLONE_TIMEOUT"])
    base = cfg["PROBE_MC_BASE"]
    remote = cfg["PROBE_MC_REMOTE"].rstrip(":")
    pairs = rclone_check.load_offload_pairs(cfg.get("PROBE_MC_SCRIPT"))

    metrics: Dict[str, object] = {"lag_bytes": 0, "lag_files": 0, "pairs": len(pairs), "pairs_checked": 0, "pairs_errored": 0}
    errors: List[str] = []
    for name, dst, _stage in pairs:
        src = os.path.join(base, name)
        key = slug(name)
        if not os.path.isdir(src):
            log.log("minecraft-offload: %s not found locally, skipping" % src)
            metrics["lag_bytes_" + key] = 0
            metrics["lag_files_" + key] = 0
            continue
        try:
            # --size-only: these trees are tens of GB of video; an hourly MD5 pass would blow the timeout.
            res = rclone_check.rclone_check(
                rclone, src, "%s:%s" % (remote, dst),
                excludes=rclone_check.OFFLOAD_EXCLUDES, min_age=cfg["PROBE_MC_MIN_AGE"],
                size_only=True, timeout=timeout,
            )
        except ProbeError as e:
            errors.append(str(e))
            metrics["pairs_errored"] = int(metrics["pairs_errored"]) + 1
            continue
        lag_bytes, _ = rclone_check.sum_local_sizes(src, res.lag_paths)
        metrics["lag_bytes_" + key] = lag_bytes
        metrics["lag_files_" + key] = res.lag_files
        metrics["matched_files_" + key] = len(res.matched)
        metrics["lag_bytes"] = int(metrics["lag_bytes"]) + lag_bytes
        metrics["lag_files"] = int(metrics["lag_files"]) + res.lag_files
        metrics["pairs_checked"] = int(metrics["pairs_checked"]) + 1
        log.log("minecraft-offload: %s → %d files / %d bytes behind" % (name, res.lag_files, lag_bytes))

    try:
        metrics.update(rclone_check.disk_free(cfg["PROBE_DISK_PATH"]))
    except OSError as e:
        errors.append("statvfs %s: %s" % (cfg["PROBE_DISK_PATH"], e))

    if int(metrics["pairs_checked"]) == 0 and errors:
        raise ProbeError(join_nonempty(errors))
    note = ("partial: " + join_nonempty(errors)) if errors else None
    if errors:
        log.error("minecraft-offload partial: " + join_nonempty(errors))
    return [("minecraft-offload", build_ping("metric", note=note, metrics=metrics))]


def probe_mac_disk(cfg: Dict[str, str], log: Logger) -> List[Ping]:
    """Capacity of the volume the recordings land on, as its own ``disk`` job so it can carry
    thresholds and alert. ``minecraft-offload`` also reports these two metrics (it has since the
    first release) — that stays as it is: those are a footnote on an offload card, this is the
    gauge. A ``metric`` ping, not a run: a disk job is never LATE, and mac-probe already says
    whether the Mac is awake."""
    try:
        metrics = rclone_check.disk_free(cfg["PROBE_DISK_PATH"])
    except OSError as e:
        raise ProbeError("statvfs %s: %s" % (cfg["PROBE_DISK_PATH"], e))
    metrics["disk_path"] = cfg["PROBE_DISK_PATH"]
    log.log("mac-disk: %s → %d free of %d bytes" % (
        cfg["PROBE_DISK_PATH"], metrics["disk_free_bytes"], metrics["disk_total_bytes"]))
    return [("mac-disk", build_ping("metric", metrics=metrics))]


def probe_drive_mirror(cfg: Dict[str, str], log: Logger) -> List[Ping]:
    """Reading the mirror DB IS the check for this job (there is no separate scheduled task on
    the Mac), so a successful read is an ``ok`` RUN with finished_at — not a metrics-only update.
    Otherwise the job could never leave UNKNOWN/LATE. No DB at all → ``fail`` run."""
    started = now_iso()
    try:
        st = drivefs.probe_drive_mirror(cfg["PROBE_DRIVEFS_DIR"])
    except ProbeError as e:
        # Not an exception of the probe itself: Drive genuinely isn't there → tell the dashboard.
        log.error("drive-mirror: " + str(e))
        return [("drive-mirror", build_ping("fail", reason="error", note=str(e), started_at=started, finished_at=now_iso()))]
    log.log("drive-mirror: pending=%d mismatch=%d roots=%s" % (st.pending, st.mismatch, ",".join(st.roots)))
    note = None if st.caught_up else "not caught up: pending=%d mismatch=%d" % (st.pending, st.mismatch)
    return [("drive-mirror", build_ping("ok", reason="probed", note=note, started_at=started, finished_at=now_iso(), metrics=st.metrics()))]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
SUBPROBES = ("pa-backup", "minecraft-offload", "mac-disk", "drive-mirror")


def deliver(pings: List[Ping], cfg: Dict[str, str], dry_run: bool, log: Logger) -> List[str]:
    """Send (or print) each ping. Returns a list of delivery error strings."""
    errs: List[str] = []
    for job_id, body in pings:
        if dry_run:
            print("DRY-RUN POST %s/api/v1/ping/%s\n  %s" % (cfg["DASHBOARD_URL"], job_id, json.dumps(body, sort_keys=True)))
            continue
        try:
            code, text = send_ping(cfg["DASHBOARD_URL"], cfg["INGEST_TOKEN"], job_id, body,
                                   timeout=float(cfg["PROBE_HTTP_TIMEOUT"]))
            log.log("sent %s %s → HTTP %d %s" % (job_id, body.get("status"), code, text.strip()[:80]))
        except ProbeError as e:
            errs.append(str(e))
            log.error(str(e))
    return errs


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print pings instead of sending; don't update state")
    ap.add_argument("--env", default=os.environ.get("HOPPER_DASHBOARD_ENV", DEFAULT_ENV_FILE), help="env file path")
    ap.add_argument("--only", choices=SUBPROBES, help="run a single sub-probe")
    ap.add_argument("--quiet", action="store_true", help="don't echo log lines to stdout")
    args = ap.parse_args(argv)

    cfg = load_settings(args.env)
    log = Logger(cfg["PROBE_LOG_FILE"], echo=not args.quiet)
    started = now_iso()
    t0 = time.time()

    if not args.dry_run and not cfg.get("INGEST_TOKEN"):
        log.error("INGEST_TOKEN missing (env file %s) — nothing sent" % args.env)
        return 2
    try:
        cfg["DASHBOARD_URL"] = require_dashboard_url(cfg, args.env)
    except ProbeError as e:
        log.error("%s — nothing sent" % e)
        return 2

    state = load_state(cfg["PROBE_STATE_FILE"])
    failures: List[str] = []
    pings_sent = 0
    new_pa_entry: Optional[backup_log.LogEntry] = None
    wanted = [args.only] if args.only else list(SUBPROBES)

    for name in wanted:
        pings: List[Ping] = []
        try:
            if name == "pa-backup":
                pings, new_pa_entry = probe_pa_backup(cfg, state, log)
            elif name == "minecraft-offload":
                pings = probe_minecraft_offload(cfg, log)
            elif name == "mac-disk":
                pings = probe_mac_disk(cfg, log)
            elif name == "drive-mirror":
                pings = probe_drive_mirror(cfg, log)
        except ProbeError as e:
            failures.append("%s: %s" % (name, e))
            log.error("%s: %s" % (name, e))
            continue
        except Exception as e:  # never let one sub-probe kill the run
            failures.append("%s: %s: %s" % (name, type(e).__name__, e))
            log.error("%s crashed: %s\n%s" % (name, e, traceback.format_exc()))
            continue

        errs = deliver(pings, cfg, args.dry_run, log)
        pings_sent += len(pings) - len(errs)
        if errs:
            failures.append("%s delivery: %s" % (name, join_nonempty(errs)))
            # Don't mark the pa-backup line as reported if its ping didn't land → retried next hour.
            if name == "pa-backup":
                new_pa_entry = None

    if new_pa_entry is not None and not args.dry_run:
        state = backup_log.mark_reported(new_pa_entry, state, now_iso())
        try:
            save_state(cfg["PROBE_STATE_FILE"], state)
        except OSError as e:
            failures.append("state: %s" % e)
            log.error("could not save state: %s" % e)

    status = "fail" if failures else "ok"
    heartbeat = build_ping(
        status,
        started_at=started,
        finished_at=now_iso(),
        reason="error" if failures else "pushed",
        note=("sub-probe errors: " + join_nonempty(failures)) if failures else None,
        metrics={
            "subprobes": len(wanted),
            "subprobes_failed": len(failures),
            "pings_sent": pings_sent,
            "duration_s": round(time.time() - t0, 1),
        },
    )
    hb_errs = deliver([("mac-probe", heartbeat)], cfg, args.dry_run, log)
    if hb_errs:
        log.error("mac-probe heartbeat not delivered: " + join_nonempty(hb_errs))

    log.log("run %s in %.1fs (%d sub-probes, %d failed, %d pings)%s" % (
        status, time.time() - t0, len(wanted), len(failures), pings_sent, " [dry-run]" if args.dry_run else ""))
    return 1 if (failures or hb_errs) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:  # last line of defence: always leave a line in the log
        try:
            Logger(DEFAULT_LOG_FILE).error("mac_probe crashed: %s: %s" % (type(e).__name__, e))
        finally:
            traceback.print_exc()
            sys.exit(1)
