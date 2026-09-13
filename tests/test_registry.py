import copy

import pytest

from dashboard.registry import (RegistryError, load_registry, parse_registry)
from tests.conftest import EXAMPLE_JOBS, JOBS_DOC


def _doc(**overrides):
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][0].update(overrides)
    return doc


def test_example_file_loads_and_has_required_jobs():
    reg = load_registry(EXAMPLE_JOBS)
    ids = {j.id for j in reg}
    assert {"km-backup", "todoist-points-backup", "box-containers", "mac-probe",
            "pa-backup", "drive-mirror", "minecraft-offload", "taste-twin-publish",
            "jjho-refresh", "baby-pool-sync", "dashboard-probes", "mac-disk",
            "box-disk"} <= ids
    assert "km-tracker-cloudflared-1" in reg.get("box-containers").expect
    assert reg.get("minecraft-offload").max_lag_bytes == 21474836480
    assert reg.get("taste-twin-publish").informational
    assert reg.get("mac-probe").late_means
    # 25 GiB / 90% on the Mac (clear-the-staging-copies level);
    # 50 GiB / 85% on the box. A disk job carries no schedule.
    mac_disk = reg.get("mac-disk")
    assert (mac_disk.min_free_bytes, mac_disk.max_used_pct) == (26843545600, 90)
    assert mac_disk.deadline_s is None and not mac_disk.scheduled
    assert not mac_disk.informational
    assert (reg.get("box-disk").min_free_bytes, reg.get("box-disk").max_used_pct) == (53687091200, 85)


def test_test_doc_parses():
    reg = parse_registry(JOBS_DOC)
    assert len(reg) == 9
    assert reg.get("snap").has_probe and reg.get("snap").deadline_s == 600
    assert reg.by_machine()["box"][0].id == "snap"
    assert [j.id for j in reg.probed()] == ["snap"]


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(RegistryError, match="jobs file not found"):
        load_registry(str(tmp_path / "nope.yml"))


def test_bad_yaml(tmp_path):
    p = tmp_path / "jobs.yml"
    p.write_text("jobs: [\n")
    with pytest.raises(RegistryError, match="YAML parse error"):
        load_registry(str(p))


@pytest.mark.parametrize("doc,msg", [
    ({"nope": []}, "'jobs' list"),
    ({"jobs": []}, "non-empty"),
    ({"jobs": ["x"]}, "must be a mapping"),
    ({"jobs": [], "extra": 1}, "unknown top-level"),
])
def test_top_level_errors(doc, msg):
    with pytest.raises(RegistryError, match=msg):
        parse_registry(doc)


@pytest.mark.parametrize("overrides,msg", [
    ({"id": "Bad_ID"}, r"\[a-z0-9-\]"),
    ({"id": "x" * 65}, "longer than 64"),
    ({"machine": "cloud"}, "'machine' must be one of"),
    ({"kind": "cronjob"}, "'kind' must be one of"),
    ({"name": ""}, "'name' is required"),
    ({"protects": None}, "'protects' is required"),
    ({"cadence_s": -5}, "positive integer"),
    ({"cadence_s": None}, "'cadence_s' is required"),
    ({"grace_s": "10"}, "positive integer"),
    ({"probe": {"state_dir": "/x"}}, "probe.rclone_path"),
    ({"probe": {"rclone_path": "g:", "bogus": 1}}, "unknown probe key"),
    ({"probe": None}, "db_snapshot requires probe.rclone_path"),
    ({"expect": ["a"]}, "'expect' is only valid for kind: container"),
    ({"manual": {"max_age_s": 5}}, "only valid for kind: manual"),
    ({"disk": {"min_free_bytes": 5}}, "only valid for kind: disk"),
    ({"typo_field": 1}, "unknown job key"),
])
def test_per_job_errors(overrides, msg):
    with pytest.raises(RegistryError, match=msg):
        parse_registry(_doc(**overrides))


def test_error_names_the_job():
    with pytest.raises(RegistryError, match="'snap'"):
        parse_registry(_doc(cadence_s=0))


def test_duplicate_ids_rejected():
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][1]["id"] = "snap"
    with pytest.raises(RegistryError, match="duplicate job id"):
        parse_registry(doc)


def test_manual_cannot_have_schedule():
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][4]["cadence_s"] = 60
    with pytest.raises(RegistryError, match="manual jobs have no schedule"):
        parse_registry(doc)


def test_container_requires_expect_list():
    doc = copy.deepcopy(JOBS_DOC)
    del doc["jobs"][3]["expect"]
    with pytest.raises(RegistryError, match="non-empty 'expect' list"):
        parse_registry(doc)
    doc["jobs"][3]["expect"] = ["a", "a"]
    with pytest.raises(RegistryError, match="duplicate names"):
        parse_registry(doc)


def test_copy_tree_requires_destination():
    doc = copy.deepcopy(JOBS_DOC)
    del doc["jobs"][1]["destination"]
    with pytest.raises(RegistryError, match="requires 'destination'"):
        parse_registry(doc)


def test_probe_block_only_for_probeable_kinds():
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][2]["probe"] = {"rclone_path": "g:x"}
    with pytest.raises(RegistryError, match="cannot have a 'probe' block"):
        parse_registry(doc)


def test_probe_block_optional_on_copy_tree_and_manual():
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][1]["probe"] = {"rclone_path": "gdrive-ro:Backups"}     # rclone_copy_tree
    doc["jobs"][4]["probe"] = {"rclone_path": "gdrive-ro:Gremlins"}    # manual
    reg = parse_registry(doc)
    assert reg.get("tree").has_probe and reg.get("offload").has_probe
    assert [j.id for j in reg.probed()] == ["snap", "tree", "offload"]
    # Still optional: the same jobs without a probe block parse as before.
    assert not parse_registry(JOBS_DOC).get("tree").has_probe


@pytest.mark.parametrize("idx", [2, 3, 6])  # drive_mirror, container, probe
def test_probe_block_rejected_on_other_kinds(idx):
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][idx]["probe"] = {"rclone_path": "g:x"}
    with pytest.raises(RegistryError, match="cannot have a 'probe' block"):
        parse_registry(doc)


# -- disk --------------------------------------------------------------------

DISK_IDX = 8  # the disk job in JOBS_DOC


def _disk_doc(block):
    doc = copy.deepcopy(JOBS_DOC)
    if block is None:
        doc["jobs"][DISK_IDX].pop("disk")
    else:
        doc["jobs"][DISK_IDX]["disk"] = block
    return doc


def test_disk_thresholds_parse():
    job = parse_registry(JOBS_DOC).get("disk")
    assert job.kind == "disk" and job.min_free_bytes == 25 * 1024 ** 3
    assert job.max_used_pct == 90 and not job.informational
    # Not scheduled, not probeable: no dead-man's switch and no destination listing.
    assert not job.scheduled and job.deadline_s is None
    assert job.id not in [j.id for j in parse_registry(JOBS_DOC).probed()]


def test_disk_without_thresholds_is_informational():
    job = parse_registry(_disk_doc(None)).get("disk")
    assert job.informational and job.min_free_bytes is None and job.max_used_pct is None
    job = parse_registry(_disk_doc({})).get("disk")
    assert job.informational


def test_disk_one_threshold_is_enough_to_alert():
    assert not parse_registry(_disk_doc({"max_used_pct": 90})).get("disk").informational
    assert not parse_registry(_disk_doc({"min_free_bytes": 1})).get("disk").informational


@pytest.mark.parametrize("block,msg", [
    ({"min_free_bytes": 1, "bogus": 2}, "unknown disk key"),
    ({"min_free_bytes": 0}, "positive integer"),
    ({"min_free_bytes": "25G"}, "positive integer"),
    ({"max_used_pct": 101}, "percentage"),
    ({"max_used_pct": 26843545600}, "percentage"),   # bytes pasted into the wrong field
])
def test_disk_block_errors(block, msg):
    with pytest.raises(RegistryError, match=msg):
        parse_registry(_disk_doc(block))


def test_disk_is_not_a_mapping():
    with pytest.raises(RegistryError, match="'disk' must be a mapping"):
        parse_registry(_disk_doc(["25G"]))


@pytest.mark.parametrize("idx", [0, 1, 4, 6])  # db_snapshot, copy tree, manual, probe
def test_disk_block_rejected_on_other_kinds(idx):
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][idx]["disk"] = {"max_used_pct": 90}
    with pytest.raises(RegistryError, match="only valid for kind: disk"):
        parse_registry(doc)


def test_disk_job_has_no_schedule_or_probe():
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][DISK_IDX]["cadence_s"] = 300
    with pytest.raises(RegistryError, match="disk jobs have no schedule"):
        parse_registry(doc)
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][DISK_IDX]["probe"] = {"rclone_path": "g:x"}
    with pytest.raises(RegistryError, match="cannot have a 'probe' block"):
        parse_registry(doc)
