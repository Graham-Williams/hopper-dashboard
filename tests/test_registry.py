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


def test_probe_interval_s_is_optional_and_defaults_to_none():
    """None means "every cycle", i.e. the global PROBE_INTERVAL_S."""
    assert parse_registry(JOBS_DOC).get("snap").probe_interval_s is None
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][1]["probe"] = {"rclone_path": "gdrive-ro:Backups",
                               "interval_s": 1800}
    assert parse_registry(doc).get("tree").probe_interval_s == 1800


@pytest.mark.parametrize("bad", [0, -1, "1800", 1800.0, True, None])
def test_probe_interval_s_must_be_a_positive_int(bad):
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][1]["probe"] = {"rclone_path": "gdrive-ro:Backups", "interval_s": bad}
    if bad is None:                       # None is "absent", not an error
        assert parse_registry(doc).get("tree").probe_interval_s is None
        return
    with pytest.raises(RegistryError, match="'interval_s' must be a positive integer"):
        parse_registry(doc)


def test_probe_interval_s_capped_for_db_snapshot():
    """db_snapshot is the only kind whose STALE_DEST verdict reads the probe's
    newest-object time, so its interval must stay well inside the freshness
    window (cadence_s * 12) — otherwise a stale probe row reads as a stale
    destination. cadence_s 300 → window 3600 → cap 1800."""
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][0]["probe"]["interval_s"] = 1800
    assert parse_registry(doc).get("snap").probe_interval_s == 1800
    doc["jobs"][0]["probe"]["interval_s"] = 1801
    with pytest.raises(RegistryError, match=r"'probe.interval_s' must be <= 1800"):
        parse_registry(doc)
    # The cap applies only to db_snapshot: a copy tree may be probed rarely.
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][1]["probe"] = {"rclone_path": "gdrive-ro:Backups",
                               "interval_s": 86400}
    assert parse_registry(doc).get("tree").probe_interval_s == 86400


def test_example_file_probe_intervals_are_documented_for_the_big_trees():
    """pa-backup / minecraft-offload keep their probe blocks COMMENTED OUT (as
    on the box), so the example file must still parse with no probe on them —
    the 1800 s interval lives in the commented template next to them."""
    reg = load_registry(EXAMPLE_JOBS)
    assert not reg.get("pa-backup").has_probe
    assert not reg.get("minecraft-offload").has_probe
    assert [j.id for j in reg.probed()] == ["km-backup", "todoist-points-backup"]
    assert all(j.probe_interval_s is None for j in reg.probed())
    with open(EXAMPLE_JOBS, encoding="utf-8") as fh:
        text = fh.read()
    assert text.count("#   interval_s: 1800") == 2
