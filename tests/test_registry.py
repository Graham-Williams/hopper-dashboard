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
            "jjho-refresh", "baby-pool-sync", "dashboard-probes"} <= ids
    assert "km-tracker-cloudflared-1" in reg.get("box-containers").expect
    assert reg.get("minecraft-offload").max_lag_bytes == 21474836480
    assert reg.get("taste-twin-publish").informational
    assert reg.get("mac-probe").late_means


def test_test_doc_parses():
    reg = parse_registry(JOBS_DOC)
    assert len(reg) == 8
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
