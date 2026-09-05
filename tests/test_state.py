"""Pure state-machine tests over Facts snapshots with a fixed clock."""

from dashboard.db import to_iso
from dashboard.registry import parse_registry
from dashboard.state import (Facts, compute_state, db_snapshot_stale, dest_info,
                             lag_info, running_names)
from tests.conftest import JOBS_DOC

NOW = 1_800_000_000.0
REG = parse_registry(JOBS_DOC)


def run(status="ok", ago=0, **extra):
    return {"status": status, "received_at": to_iso(NOW - ago), **extra}


def probe(newest_ago=None, count=5, sha=None, push_epoch=None, ok=True, error=None):
    return {"ok": ok, "count": count, "state_sha": sha, "state_push_epoch": push_epoch,
            "error": error, "probed_at": to_iso(NOW),
            "newest_iso": to_iso(NOW - newest_ago) if newest_ago is not None else None}


def test_unknown_when_never_heard():
    assert compute_state(REG.get("snap"), Facts(), NOW)[0] == "UNKNOWN"
    assert compute_state(REG.get("offload"), Facts(), NOW)[0] == "UNKNOWN"


def test_ok_within_deadline():
    f = Facts(last_run=run(ago=100), last_success=run(ago=100))
    assert compute_state(REG.get("tree"), f, NOW)[0] == "OK"


def test_late_after_cadence_plus_grace():
    job = REG.get("snap")  # 300 + 300
    f = Facts(last_run=run(ago=599), last_success=run(ago=599))
    assert compute_state(job, f, NOW)[0] == "OK"
    f = Facts(last_run=run(ago=601), last_success=run(ago=601))
    assert compute_state(job, f, NOW)[0] == "LATE"


def test_late_wins_over_stale_fail():
    f = Facts(last_run=run("fail", ago=10_000))
    assert compute_state(REG.get("snap"), f, NOW)[0] == "LATE"


def test_late_uses_late_means_text():
    f = Facts(last_run=run(ago=20_000))
    state, reason = compute_state(REG.get("macprobe"), f, NOW)
    assert state == "LATE" and reason == "Mac offline or asleep"


def test_fail_on_last_run_fail():
    f = Facts(last_run=run("fail", reason="timeout"), last_success=run(ago=400))
    state, reason = compute_state(REG.get("snap"), f, NOW)
    assert state == "FAIL" and "timeout" in reason


def test_skipped_is_healthy():
    f = Facts(last_run=run("skipped"), last_success=run("skipped"))
    assert compute_state(REG.get("snap"), f, NOW)[0] == "OK"


def test_metrics_only_scheduled_job_is_unknown():
    f = Facts(last_metrics={"pending": 0})
    state, reason = compute_state(REG.get("mirror"), f, NOW)
    assert state == "UNKNOWN" and "no heartbeat" in reason


# -- container ---------------------------------------------------------------

def test_container_fail_when_expected_missing():
    job = REG.get("containers")
    f = Facts(last_run=run(), last_metrics={"running": "app-1, other"})
    state, reason = compute_state(job, f, NOW)
    assert state == "FAIL" and "tunnel-1" in reason
    f = Facts(last_run=run(), last_metrics={"running": ["app-1", "tunnel-1", "x"]})
    assert compute_state(job, f, NOW)[0] == "OK"


def test_container_without_running_metric_is_ok_on_heartbeat():
    f = Facts(last_run=run())
    assert compute_state(REG.get("containers"), f, NOW)[0] == "OK"


def test_running_names_parsing():
    assert running_names({"running": "a,b\nc, "}) == {"a", "b", "c"}
    assert running_names({"running": ["a", " b "]}) == {"a", "b"}
    assert running_names({}) is None
    assert running_names({"running": 5}) is None


# -- drive_mirror ------------------------------------------------------------

def test_drive_mirror_behind_when_pending_or_mismatch():
    job = REG.get("mirror")
    assert compute_state(job, Facts(last_run=run(), last_metrics={"pending": 0, "mismatch": 0}), NOW)[0] == "OK"
    assert compute_state(job, Facts(last_run=run(), last_metrics={"pending": 2, "mismatch": 0}), NOW)[0] == "BEHIND"
    assert compute_state(job, Facts(last_run=run(), last_metrics={"pending": 0, "mismatch": 1}), NOW)[0] == "BEHIND"
    d = dest_info(job, Facts(last_run=run(), last_metrics={"pending": 0, "mismatch": 0}), NOW)
    assert d["fresh"] is True


# -- manual ------------------------------------------------------------------

def test_manual_behind_on_bytes():
    job = REG.get("offload")  # max_lag_bytes 1000
    f = Facts(last_metrics={"lag_bytes": 5000})
    state, reason = compute_state(job, f, NOW)
    assert state == "BEHIND" and "bytes" in reason
    f = Facts(last_metrics={"lag_bytes": 10})
    assert compute_state(job, f, NOW)[0] == "OK"


def test_manual_behind_on_age():
    job = REG.get("offload")  # max_age 14d
    f = Facts(last_run=run(ago=15 * 86400), last_success=run(ago=15 * 86400),
              last_metrics={"lag_bytes": 0})
    state, reason = compute_state(job, f, NOW)
    assert state == "BEHIND" and "max age" in reason
    lag = lag_info(job, f, NOW)
    assert lag["behind_on"] == ["age"] and lag["age_s"] == 15 * 86400


def test_manual_fail_when_last_run_failed():
    f = Facts(last_run=run("fail", reason="rclone exit 1"))
    assert compute_state(REG.get("offload"), f, NOW)[0] == "FAIL"


def test_manual_never_late():
    f = Facts(last_run=run(ago=365 * 86400), last_success=run(ago=365 * 86400))
    assert compute_state(REG.get("info"), f, NOW)[0] == "OK"


def test_informational_manual_never_behind():
    job = REG.get("info")
    f = Facts(last_run=run(ago=400 * 86400), last_success=run(ago=400 * 86400),
              last_metrics={"lag_bytes": 10 ** 15})
    assert compute_state(job, f, NOW)[0] == "OK"
    lag = lag_info(job, f, NOW)
    assert lag is not None and lag["behind"] is False and lag["max_bytes"] is None


def test_lag_info_absent_for_scheduled_without_lag_metrics():
    assert lag_info(REG.get("snap"), Facts(last_run=run()), NOW) is None
    assert lag_info(REG.get("tree"), Facts(last_run=run(), last_metrics={"missing_bytes": 5}), NOW)["bytes"] == 5


# -- db_snapshot / STALE_DEST ------------------------------------------------

def test_snapshot_fresh_newest_is_ok():
    job = REG.get("snap")  # cadence 300 → fresh window 3600
    f = Facts(last_run=run(), last_success=run(), probe=probe(newest_ago=1000))
    assert compute_state(job, f, NOW)[0] == "OK"
    assert dest_info(job, f, NOW)["fresh"] is True


def test_snapshot_old_newest_but_db_unchanged_is_ok_dedup_aware():
    job = REG.get("snap")
    f = Facts(last_run=run(), last_success=run(), last_metrics={"db_sha256": "AB" * 32},
              probe=probe(newest_ago=5 * 86400, sha="ab" * 32))
    state, reason = compute_state(job, f, NOW)
    assert state == "OK" and "unchanged" in reason
    assert dest_info(job, f, NOW)["fresh"] is True


def test_snapshot_old_newest_and_db_changed_is_stale_dest():
    job = REG.get("snap")
    f = Facts(last_run=run(), last_success=run(), last_metrics={"db_sha256": "cd" * 32},
              probe=probe(newest_ago=5 * 86400, sha="ab" * 32))
    state, reason = compute_state(job, f, NOW)
    assert state == "STALE_DEST" and "changed" in reason
    assert dest_info(job, f, NOW)["fresh"] is False


def test_snapshot_stale_via_push_epoch_when_no_sha():
    job = REG.get("snap")
    # Script says it pushed an hour ago, Drive's newest is 5 days old.
    f = Facts(last_run=run(), last_success=run(),
              probe=probe(newest_ago=5 * 86400, push_epoch=int(NOW - 3600)))
    assert compute_state(job, f, NOW)[0] == "STALE_DEST"
    # Script's last push matches Drive → fine.
    f = Facts(last_run=run(), last_success=run(),
              probe=probe(newest_ago=5 * 86400, push_epoch=int(NOW - 5 * 86400)))
    assert compute_state(job, f, NOW)[0] == "OK"


def test_snapshot_without_probe_cannot_judge():
    job = REG.get("snap")
    f = Facts(last_run=run(), last_success=run())
    stale, _ = db_snapshot_stale(job, f, NOW)
    assert stale is None
    assert compute_state(job, f, NOW)[0] == "OK"
    assert dest_info(job, f, NOW)["fresh"] is None


def test_snapshot_failed_probe_surfaces_error_not_stale():
    job = REG.get("snap")
    f = Facts(last_run=run(), last_success=run(),
              probe=probe(ok=False, error="rclone exit 1: boom"))
    assert compute_state(job, f, NOW)[0] == "OK"
    d = dest_info(job, f, NOW)
    assert d["probe_error"] == "rclone exit 1: boom" and d["newest"] is None


def test_fail_beats_stale_dest():
    job = REG.get("snap")
    f = Facts(last_run=run("fail"), last_metrics={"db_sha256": "cd" * 32},
              probe=probe(newest_ago=5 * 86400, sha="ab" * 32))
    assert compute_state(job, f, NOW)[0] == "FAIL"


# -- rclone_copy_tree --------------------------------------------------------

def test_copy_tree_stale_dest_when_ok_but_bytes_missing():
    job = REG.get("tree")
    f = Facts(last_run=run(), last_success=run(), last_metrics={"missing_bytes": 4096})
    state, reason = compute_state(job, f, NOW)
    assert state == "STALE_DEST" and "4096" in reason
    assert dest_info(job, f, NOW)["fresh"] is False
    f = Facts(last_run=run(), last_success=run(), last_metrics={"missing_bytes": 0})
    assert compute_state(job, f, NOW)[0] == "OK"
    assert dest_info(job, f, NOW)["fresh"] is True


def test_dest_from_metrics_for_unprobed_jobs():
    job = REG.get("offload")
    f = Facts(last_metrics={"dest_newest_iso": to_iso(NOW - 100), "dest_count": 7,
                            "lag_bytes": 0})
    d = dest_info(job, f, NOW)
    assert d["count"] == 7 and d["fresh"] is True and d["newest"] == to_iso(NOW - 100)
    f = Facts(last_metrics={"dest_newest_iso": "garbage", "dest_count": "x"})
    d = dest_info(job, f, NOW)
    assert d["newest"] is None and d["count"] is None
