"""Pure state-machine tests over Facts snapshots with a fixed clock."""

from dashboard.db import to_iso
from dashboard.registry import parse_registry
from dashboard.state import (DISK_FIRST_READING_GRACE_S, DISK_METRIC_MAX_AGE_S,
                             Facts, _disk_never_reported_late, compute_state,
                             db_snapshot_stale, dest_info, disk_info, lag_info,
                             running_names)
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


# -- optional box probe on copy trees / manual jobs ---------------------------

def _probed(job_id, path="gdrive-ro:x"):
    import copy
    from dashboard.registry import parse_registry
    from tests.conftest import JOBS_DOC
    doc = copy.deepcopy(JOBS_DOC)
    for j in doc["jobs"]:
        if j["id"] == job_id:
            j["probe"] = {"rclone_path": path}
    return parse_registry(doc).get(job_id)


def test_copy_tree_probe_supplies_newest_and_count():
    job = _probed("tree")
    metrics = {"missing_bytes": 0, "dest_count": 1032}       # Mac reports a count, no newest time
    f = Facts(last_run=run(), last_success=run(), last_metrics=metrics,
              probe=probe(newest_ago=3600, count=1040))
    d = dest_info(job, f, NOW)
    assert d["newest"] == to_iso(NOW - 3600) and d["count"] == 1040 and d["probed_at"]
    assert compute_state(job, f, NOW)[0] == "OK"        # freshness verdict still comes from missing_*


def test_copy_tree_probe_pending_falls_back_to_metrics():
    job = _probed("tree")
    metrics = {"missing_bytes": 0, "dest_count": 1032}
    for pr in (None, probe(newest_ago=10, ok=False, error="rclone exit 3")):
        d = dest_info(job, Facts(last_run=run(), last_success=run(), last_metrics=metrics, probe=pr), NOW)
        assert d["newest"] is None and d["count"] == 1032, pr
    assert d["probe_error"] == "rclone exit 3"


def test_manual_probe_drives_freshness_pill():
    job = _probed("offload")
    f = Facts(last_metrics={"lag_bytes": 0}, probe=probe(newest_ago=15 * 86400, count=9))
    d = dest_info(job, f, NOW)
    assert d["count"] == 9 and d["fresh"] is False      # older than max_age_s (14 d)
    f = Facts(last_metrics={"lag_bytes": 0}, probe=probe(newest_ago=86400, count=9))
    assert dest_info(job, f, NOW)["fresh"] is True


# -- disk --------------------------------------------------------------------

GIB = 1024 ** 3
DISK = REG.get("disk")          # min_free 25 GiB, max_used_pct 90


def disk_facts(free=None, total=None, **extra):
    m = dict(extra)
    if free is not None:
        m["disk_free_bytes"] = free
    if total is not None:
        m["disk_total_bytes"] = total
    return Facts(last_metrics=m, last_metrics_at=to_iso(NOW))


def test_disk_unknown_until_metrics_arrive():
    assert compute_state(DISK, Facts(), NOW) == ("UNKNOWN", "never heard from")
    state, reason = compute_state(DISK, disk_facts(total=400 * GIB), NOW)
    assert state == "UNKNOWN" and reason == "no disk metrics reported yet"


def test_disk_ok_reports_percent_and_free():
    state, reason = compute_state(DISK, disk_facts(free=200 * GIB, total=400 * GIB), NOW)
    assert state == "OK" and reason == "50.0% used, 200.0 GiB free"


def test_disk_behind_below_free_floor():
    # 20 GiB free of 100 GiB: under the 25 GiB floor, but only 80% used — one threshold, not both.
    state, reason = compute_state(DISK, disk_facts(free=20 * GIB, total=100 * GIB), NOW)
    assert state == "BEHIND"
    assert "20.0 GiB free" in reason and "25.0 GiB floor" in reason
    assert "ceiling" not in reason              # only the threshold that tripped is named


def test_disk_behind_over_used_ceiling():
    # 8% free of 500 GiB = 40 GiB, over the 25 GiB floor, but 92% used.
    state, reason = compute_state(DISK, disk_facts(free=40 * GIB, total=500 * GIB), NOW)
    assert state == "BEHIND" and "92.0% used" in reason and "90% ceiling" in reason
    assert "floor" not in reason


def test_disk_behind_names_both_thresholds():
    state, reason = compute_state(DISK, disk_facts(free=2 * GIB, total=400 * GIB), NOW)
    assert state == "BEHIND" and "floor" in reason and "ceiling" in reason


def test_disk_threshold_is_strict_at_the_boundary():
    # Exactly ON the floor and exactly ON the ceiling is still OK: both comparisons are strict,
    # so a threshold reads as "alert when worse than this", never "alert at this".
    f = disk_facts(free=25 * GIB, total=250 * GIB)
    assert disk_info(DISK, f)["used_pct"] == 90.0
    assert compute_state(DISK, f, NOW)[0] == "OK"
    assert compute_state(DISK, disk_facts(free=25 * GIB - 1, total=250 * GIB), NOW)[0] == "BEHIND"
    assert compute_state(DISK, disk_facts(free=30 * GIB, total=400 * GIB), NOW)[0] == "BEHIND"


def test_disk_zero_or_missing_total_never_divides_by_zero():
    for total in (0, None, "nan"):
        f = disk_facts(free=100 * GIB, total=total)
        d = disk_info(DISK, f)
        assert d["used_pct"] is None and d["used_bytes"] is None and d["low"] is False
        assert compute_state(DISK, f, NOW) == ("OK", "100.0 GiB free")
    # The free-space floor still works with no total to compare against.
    assert compute_state(DISK, disk_facts(free=1 * GIB, total=0), NOW)[0] == "BEHIND"


def test_disk_has_no_cadence_deadline():
    # Liveness belongs to the machine's probe job, so a reading far older than any
    # cadence is still OK — only the coarse staleness ceiling below applies.
    f = disk_facts(free=200 * GIB, total=400 * GIB)
    f.created_at = to_iso(NOW - 10 * 86400)
    f.last_metrics_at = to_iso(NOW - 6 * 3600)
    assert compute_state(DISK, f, NOW)[0] == "OK"


def disk_fail_run(note="statvfs /data: [Errno 2] No such file or directory", ago=0):
    return {"status": "fail", "reason": "error", "note": note,
            "received_at": to_iso(NOW - ago)}


def test_disk_fail_ping_is_not_masked_by_the_last_good_reading():
    # The probe posted `fail` after its last good reading: the gauge is unreadable,
    # and the stale figure must not keep the card OK (a fail ping that changes
    # nothing is worse than no ping at all — it looks handled).
    f = disk_facts(free=200 * GIB, total=400 * GIB)
    f.last_metrics_at = to_iso(NOW - 7200)
    f.last_run = disk_fail_run(ago=3600)
    state, reason = compute_state(DISK, f, NOW)
    assert state == "FAIL" and "statvfs" in reason


def test_disk_fail_before_any_metrics_arrive_is_fail_not_unknown():
    state, reason = compute_state(DISK, Facts(last_run=disk_fail_run()), NOW)
    assert state == "FAIL" and "statvfs" in reason


def test_disk_fail_is_cleared_by_a_newer_metric_ping():
    # A `metric` ping is not a run, so a failed run stays the newest *runs* row for
    # good; only comparing it against last_metrics_at lets the gauge recover.
    f = disk_facts(free=200 * GIB, total=400 * GIB)     # measured at NOW
    f.last_run = disk_fail_run(ago=3600)
    assert compute_state(DISK, f, NOW)[0] == "OK"


def test_disk_goes_late_when_the_feeder_stops_posting():
    # Partial silence: the disk probe stops running (renamed/moved/interpreter gone)
    # while its machine's probe job keeps reporting OK. Nothing is posted, so the
    # fail branch cannot help — the reading's own age has to be the signal.
    f = disk_facts(free=200 * GIB, total=400 * GIB)
    f.last_metrics_at = to_iso(NOW - 49 * 3600)
    state, reason = compute_state(DISK, f, NOW)
    assert state == "LATE" and "48h" in reason


def test_disk_never_reporting_is_late_once_the_grace_has_passed():
    # The misinstalled-feeder case: the gauge is in jobs.yml but its probe was never
    # deployed (or its --job id is mistyped). UNKNOWN is also the initial stored state,
    # so no transition is recorded and no alert ever fires — the card would read
    # "never heard from" for ever beside a happy box-containers.
    fresh = Facts(created_at=to_iso(NOW - DISK_FIRST_READING_GRACE_S + 60))
    assert compute_state(DISK, fresh, NOW) == ("UNKNOWN", "never heard from")
    old = Facts(created_at=to_iso(NOW - DISK_FIRST_READING_GRACE_S - 60))
    state, reason = compute_state(DISK, old, NOW)
    assert state == "LATE" and "probe deployed" in reason


def test_disk_metrics_without_a_capacity_figure_also_go_late_after_the_grace():
    # Metrics arrived but carry no free-bytes key: same silence, same finding.
    f = disk_facts(total=400 * GIB)
    f.created_at = to_iso(NOW - DISK_FIRST_READING_GRACE_S - 60)
    assert compute_state(DISK, f, NOW)[0] == "LATE"


def test_disk_grace_does_not_apply_to_other_kinds():
    # The snapshot job has its own cadence-based dead-man's switch; nothing here
    # may change what an unregistered-yet scheduled job reports.
    f = Facts(created_at=to_iso(NOW - 10 * 86400))
    assert _disk_never_reported_late(REG.get("offload"), f, NOW) is False
    assert _disk_never_reported_late(DISK, f, NOW) is True


def test_disk_fail_with_an_undatable_timestamp_still_wins():
    # Fail safe: a corrupt received_at must not resurrect the original bug (a stale
    # good reading reading OK while the probe says the volume is gone).
    f = disk_facts(free=200 * GIB, total=400 * GIB)
    f.last_run = disk_fail_run()
    f.last_run["received_at"] = "garbage"
    assert compute_state(DISK, f, NOW)[0] == "FAIL"


def test_disk_fail_and_reading_in_the_same_second_is_a_fail():
    # to_iso is second-granular, so a tie is reachable in production. It breaks toward
    # over-reporting on purpose: do not flip this comparison to a strict `>`.
    f = disk_facts(free=200 * GIB, total=400 * GIB)     # last_metrics_at == NOW
    f.last_run = disk_fail_run(ago=0)                   # received_at  == NOW
    assert compute_state(DISK, f, NOW)[0] == "FAIL"


def test_disk_reading_with_an_undatable_timestamp_is_late_not_ok():
    # Fail safe the other way: an unparseable last_metrics_at used to disable the
    # staleness ceiling entirely, so a ten-year-old figure read OK.
    f = disk_facts(free=200 * GIB, total=400 * GIB)
    f.last_metrics_at = "garbage"
    assert compute_state(DISK, f, NOW)[0] == "LATE"


def test_disk_negative_capacity_is_unknown_not_a_made_up_percentage():
    # Nothing clamps a negative metric, and arithmetic on one invented
    # "only — free, below the 25.0 GiB floor; 101.2% used" (human_gib renders a
    # negative as an em dash). Corrupt, not small: report it as absent.
    f = disk_facts(free=-5 * GIB, total=400 * GIB)
    d = disk_info(DISK, f)
    assert d["free_bytes"] is None and d["used_pct"] is None and d["low"] is False
    assert compute_state(DISK, f, NOW) == ("UNKNOWN", "no disk metrics reported yet")
    # A negative TOTAL likewise yields no percentage rather than a negative one.
    d2 = disk_info(DISK, disk_facts(free=200 * GIB, total=-400 * GIB))
    assert d2["total_bytes"] is None and d2["used_pct"] is None


def test_disk_staleness_ceiling_clears_a_machine_that_was_off_for_a_day():
    # 24 h would page for a Mac merely switched off overnight and a bit — mac-probe
    # already pages for that at ~15 h. Under the ceiling the gauge stays OK.
    assert DISK_METRIC_MAX_AGE_S == 48 * 3600
    f = disk_facts(free=200 * GIB, total=400 * GIB)
    f.last_metrics_at = to_iso(NOW - 47 * 3600)
    assert compute_state(DISK, f, NOW)[0] == "OK"


def test_disk_staleness_outranks_a_tripped_threshold():
    # A figure nobody has refreshed in two days is not evidence of a full disk.
    f = disk_facts(free=1 * GIB, total=400 * GIB)
    f.last_metrics_at = to_iso(NOW - 72 * 3600)
    assert compute_state(DISK, f, NOW)[0] == "LATE"


def test_disk_info_payload_shape():
    d = disk_info(DISK, disk_facts(free=100 * GIB, total=400 * GIB))
    assert d == {"measured_at": to_iso(NOW), "free_bytes": 100 * GIB,
                 "total_bytes": 400 * GIB, "used_bytes": 300 * GIB, "used_pct": 75.0,
                 "min_free_bytes": 25 * GIB, "max_used_pct": 90, "low": False, "low_on": []}


def test_disk_info_is_none_for_every_other_kind():
    f = disk_facts(free=1, total=2)
    for job_id in ("snap", "tree", "mirror", "containers", "offload", "info", "macprobe"):
        assert disk_info(REG.get(job_id), f) is None, job_id


def test_disk_accepts_the_alternate_metric_spelling():
    f = Facts(last_metrics={"free_bytes": 200 * GIB, "total_bytes": 400 * GIB})
    assert disk_info(DISK, f)["used_pct"] == 50.0


def test_informational_disk_job_never_goes_behind():
    from dashboard.registry import parse_job
    raw = {"id": "d2", "name": "Disk", "machine": "box", "kind": "disk",
           "protects": "space", "method": "statvfs"}
    job = parse_job(raw, 0)
    assert job.informational
    state, reason = compute_state(job, disk_facts(free=1, total=400 * GIB), NOW)
    assert state == "OK" and "free" in reason
