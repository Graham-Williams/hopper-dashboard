import copy

import pytest

from dashboard.registry import (MAX_PROBE_INTERVAL_S, RegistryError, load_registry,
                                parse_registry)
from tests.conftest import EXAMPLE_JOBS, JOBS_DOC


def _doc(**overrides):
    doc = copy.deepcopy(JOBS_DOC)
    # The shared fixture carries `alert_after_s: 0`, and the two alert keys are
    # mutually exclusive by KEY now — so an override that sets `alert` replaces it
    # rather than colliding with it (unless the case is testing exactly that).
    if "alert" in overrides and "alert_after_s" not in overrides:
        doc["jobs"][0].pop("alert_after_s", None)
    doc["jobs"][0].update(overrides)
    return doc


def test_example_file_loads_and_has_required_jobs():
    reg = load_registry(EXAMPLE_JOBS)
    ids = {j.id for j in reg}
    assert {"km-backup", "todoist-points-backup", "box-containers", "mac-probe",
            "pa-backup", "drive-mirror", "minecraft-offload", "taste-twin-publish",
            "jjho-refresh", "baby-pool-sync", "dashboard-probes", "mac-disk",
            "box-disk", "inbox-github-sync"} <= ids
    # The Inbox's mirror is a `worker`, not a `probe`: a probe job on `box`
    # would switch sibling-LATE suppression on for every box job.
    assert reg.get("inbox-github-sync").kind == "worker"
    assert reg.get("inbox-github-sync").deadline_s == 1800
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
    assert len(reg) == 10
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
    ({"alert_after_s": -1}, "non-negative integer"),
    ({"alert_after_s": "86400"}, "non-negative integer"),
    ({"alert_after_s": True}, "non-negative integer"),
    ({"alert": "sometimes"}, "'alert' must be 'never'"),
    ({"alert": False}, "'alert' must be a non-empty string"),
    ({"alert": "never", "alert_after_s": 60}, "mutually exclusive"),
    # Checked on KEY PRESENCE: with an `is not None` test, `alert: never` +
    # `alert_after_s: null` parsed to "never" — silence that reads, in the file,
    # like somebody set a threshold.
    ({"alert": "never", "alert_after_s": None}, "mutually exclusive"),
    ({"alert": None, "alert_after_s": 60}, "mutually exclusive"),
    # A LONE null escaped that check — nothing to be exclusive with — and fell
    # through to whatever an ABSENT key means: `never` on an informational job
    # (silence that reads like a threshold), the 24 h default elsewhere.
    ({"alert_after_s": None}, "present but null"),
    ({"alert": None}, "present but null"),
    # Magnitude was the one hostile value the validator accepted: a threshold of
    # 10**20 seconds is silence, and NOTHING would say so.
    ({"alert_after_s": 10 ** 20}, "over the 2592000 second"),
    ({"alert_after_s": 2_592_001}, "over the 2592000 second"),
])
def test_per_job_errors(overrides, msg):
    with pytest.raises(RegistryError, match=msg):
        parse_registry(_doc(**overrides))


def test_alert_policy_resolution_and_its_conservative_default():
    from dashboard.registry import DEFAULT_ALERT_AFTER_S
    doc = copy.deepcopy(JOBS_DOC)
    for raw in doc["jobs"]:
        raw.pop("alert_after_s", None)
    doc["jobs"][1]["alert_after_s"] = 108000
    doc["jobs"][2]["alert"] = "never"
    doc["jobs"][5]["alert_after_s"] = 60        # `info`: explicit beats informational
    reg = parse_registry(doc)
    # Declares nothing, not informational → 24 h. Never silence.
    assert reg.get("snap").alert_after_s == DEFAULT_ALERT_AFTER_S == 86400
    assert reg.get("snap").alert_never is False and reg.get("snap").alert_source == "default"
    assert reg.get("tree").alert_after_s == 108000
    assert reg.get("tree").alert_source == "alert_after_s"
    assert reg.get("mirror").alert_never is True and reg.get("mirror").alert_source == "alert"
    # `info` is a manual job with no thresholds — informational — but it asked.
    assert reg.get("info").informational and reg.get("info").alert_never is False
    assert reg.get("info").alert_after_s == 60
    # ...and with nothing declared, `informational` finally means what it says.
    doc["jobs"][5].pop("alert_after_s")
    quiet = parse_registry(doc).get("info")
    assert quiet.informational and quiet.alert_never is True
    assert quiet.alert_source == "informational"
    # 0 is legal: "page on the first not-OK recompute" (what the shared fixtures use).
    assert parse_registry(_doc(alert_after_s=0)).get("snap").alert_after_s == 0


@pytest.mark.parametrize("bad", [
    "Tree copy\r\nX-Injected: yes",          # the header-injection shape
    "Tree\ncopy", "Tree\rcopy",              # a bare LF or CR does it too
    "Tree\x00copy", "Tree\x1b[31mcopy",      # NUL, ESC
    "Tree\x7fcopy",                          # DEL
])
def test_a_control_character_in_a_job_string_is_rejected_at_parse_time(bad):
    """A control character in `job.name` makes that job PERMANENTLY un-pageable.
    The name becomes the ntfy `Title` header; `http.client` refuses a header
    value containing CR/LF, so nothing is ever injected (zero bytes reach the
    socket) — but every POST raises, `send` swallows it and returns False, the
    failed-push rollback hands the page straight back, and the next tick tries
    again: 24 attempts over two simulated hours, `alerted_at` NULL throughout.
    The page is never delivered and never spent, so the recovery can never fire
    either, and the board shows a job that looks armed and cannot speak.

    `.strip()` alone did not catch any of these — it only trims the ends."""
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][1]["name"] = bad
    with pytest.raises(RegistryError, match=r"must not contain control characters"):
        parse_registry(doc)
    # ...and the same for every other free-text field that reaches a log or the page.
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][1]["destination"] = bad            # optional string → _opt_str
    with pytest.raises(RegistryError, match=r"must not contain control characters"):
        parse_registry(doc)
    # A trailing newline from YAML is still fine: it is stripped before the check.
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][1]["name"] = "Tree copy\n"
    assert parse_registry(doc).get("tree").name == "Tree copy"


def test_a_lone_null_alert_key_is_an_error_not_a_resolution():
    """The mutual-exclusion check is on key PRESENCE precisely so an accidental
    silence cannot read like a configured threshold — but a LONE null slipped
    past it, because there was nothing to be exclusive with. `_nonneg_int`
    early-returns None and the job then resolves as if the key were ABSENT: on
    an INFORMATIONAL job that is `alert: never`, i.e. permanent silence sitting
    in the file under a key whose name says a threshold was set. (Elsewhere it
    lands on the 24 h default — the safe direction, and still not what the file
    says.) Present-but-null is a mistake in every case."""
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][5]["alert_after_s"] = None        # `info`: manual, no thresholds
    with pytest.raises(RegistryError, match=r"'alert_after_s' is present but null"):
        parse_registry(doc)
    doc["jobs"][5]["alert_after_s"] = 3600        # the same job, said properly
    assert parse_registry(doc).get("info").alert_after_s == 3600
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][5]["alert"] = None
    with pytest.raises(RegistryError, match=r"'alert' is present but null"):
        parse_registry(doc)


def test_example_file_alert_policy_matches_the_documented_thresholds():
    reg = load_registry(EXAMPLE_JOBS)
    # Only the on-demand manual jobs opt out. A machine's `kind: probe` job must NOT —
    # it is what mutes its siblings, so `never` there is silence for the whole machine.
    assert {j.id for j in reg if j.alert_never} == {
        "minecraft-offload", "taste-twin-publish", "jjho-refresh", "baby-pool-sync"}
    assert all(not j.alert_never for j in reg if j.kind == "probe")
    # 72 h on top of a 15 h LATE deadline: a weekend with the lid shut is silent, a Mac
    # that is gone for ~3.6 days is not.
    assert reg.get("mac-probe").alert_after_s == 259200
    assert reg.get("km-backup").alert_after_s == 86400
    assert reg.get("todoist-points-backup").alert_after_s == 86400
    # 6 h, NOT 30 h. The threshold clock starts when the job goes not-OK, which for
    # this one is already 38 h after the last backup (cadence 86400 + grace 50520), so
    # 108000 shipped a page at 68 h ~ 2.8 days against a stated bar of "missed more
    # than 24 hours". See the TIME-TO-PAGE test below.
    assert reg.get("pa-backup").alert_after_s == 21600
    assert reg.get("box-containers").alert_after_s == 1200
    assert reg.get("dashboard-probes").alert_after_s == 21600
    assert reg.get("drive-mirror").alert_after_s == 86400
    # The disk gauges are 1 h, NOT the 24 h default: DISK_METRIC_MAX_AGE_S (48 h) has
    # already served the "sustained" purpose, and a capacity threshold is a level.
    assert reg.get("box-disk").alert_after_s == reg.get("mac-disk").alert_after_s == 3600
    # Nothing ships with the "page on every blip" setting, and NOTHING is left to the
    # default — a `default` in the shipped file means a job was forgotten.
    assert all(j.alert_never or j.alert_after_s > 0 for j in reg)
    assert [j.id for j in reg if j.alert_source == "default"] == []


def _fmt_ttp(seconds: float) -> str:
    """The canonical TIME-TO-PAGE string for a number of seconds."""
    if seconds < 3600:
        return "%dm" % round(seconds / 60)
    return ("%.1fh" % (seconds / 3600.0)).replace(".0h", "h")


def test_every_alerting_job_states_its_real_time_to_page():
    """`alert_after_s` is NOT the time to a page, and four of the comments in
    jobs.example.yml used to say it was — wrong by between 15 minutes (the two
    disk gauges aside, `box-containers` reads 20 m and pages at 35 m) and 38
    hours (`pa-backup` read "30 h means a whole night was genuinely missed" and
    paged at 68 h).

    A comment cannot be asserted, so this asserts a NUMBER inside one: every
    job with a threshold carries a `# TIME-TO-PAGE: <n>` line, and the figure is
    recomputed here from that job's own cadence/grace/threshold. Drift one and
    the suite goes red — which is the only reason to believe the other prose
    around it.

    A `disk` gauge has no cadence, so its figure is the CAPACITY-breach one (the
    threshold alone); a gauge nothing feeds is LATE at 48 h and pages a
    threshold after that, which is stated in prose beside it."""
    import re
    reg = load_registry(EXAMPLE_JOBS)
    with open(EXAMPLE_JOBS, encoding="utf-8") as fh:
        text = fh.read()
    blocks = {}
    for m in re.finditer(r"^  - id: ([a-z0-9-]+)$((?:\n(?!  - id:).*)*)", text, re.M):
        blocks[m.group(1)] = m.group(2)
    assert set(blocks) == {j.id for j in reg}
    checked = 0
    for job in reg:
        found = re.search(r"^    # TIME-TO-PAGE: (\S+)", blocks[job.id], re.M)
        if job.alert_never:
            assert found is None, f"{job.id} never pages; it must not claim a time"
            continue
        assert found is not None, f"{job.id} states no TIME-TO-PAGE"
        want = _fmt_ttp((job.deadline_s or 0) + job.alert_after_s)
        assert found.group(1) == want, (
            f"{job.id}: comment says {found.group(1)}, arithmetic says {want} "
            f"(deadline {job.deadline_s} + threshold {job.alert_after_s})")
        checked += 1
    assert checked == 10                                 # every job that can page


def test_the_time_to_page_figures_are_the_ones_graham_was_quoted():
    """The end-to-end numbers themselves, pinned. These are what "how long can
    this be broken before my phone knows?" actually resolves to, and they are
    the figures the deploy notes and DESIGN.md quote — so a threshold edit that
    changes one has to change them everywhere, deliberately."""
    reg = load_registry(EXAMPLE_JOBS)
    assert {j.id: _fmt_ttp((j.deadline_s or 0) + j.alert_after_s)
            for j in reg if not j.alert_never} == {
        "km-backup": "24.3h", "todoist-points-backup": "24.3h",
        "box-containers": "35m", "box-disk": "1h", "dashboard-probes": "6.3h",
        "mac-probe": "87h", "mac-disk": "1h", "pa-backup": "44h",
        "drive-mirror": "39h", "inbox-github-sync": "24.5h"}
    # The bar Graham set was "backups missed more than 24 hours". Both DB
    # snapshots clear it; pa-backup cannot (its 38 h deadline is a hard floor —
    # below it a missed backup is indistinguishable from a sleeping Mac) but it
    # is now the smallest miss the floor allows instead of nearly three days.
    assert reg.get("pa-backup").deadline_s == 136920          # 38 h, the floor
    assert (reg.get("pa-backup").deadline_s
            + reg.get("pa-backup").alert_after_s) < 2 * 86400


def test_the_magnitude_cap_still_admits_every_real_value():
    """The cap must not be so tight it rejects a sane policy: 30 days is well past
    the longest shipped threshold (72 h)."""
    from dashboard.registry import MAX_ALERT_AFTER_S
    reg = load_registry(EXAMPLE_JOBS)
    assert max(j.alert_after_s for j in reg) <= MAX_ALERT_AFTER_S
    assert parse_registry(_doc(alert_after_s=MAX_ALERT_AFTER_S)).get("snap").alert_after_s \
        == MAX_ALERT_AFTER_S


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


def test_probe_interval_s_has_a_magnitude_cap_on_every_kind():
    """The semantic cap above is db_snapshot-only, so `rclone_copy_tree` and
    `manual` — the two kinds DEPLOY.md §1d has the operator hand-edit
    (`pa-backup`, `minecraft-offload`) — had no magnitude bound at all:
    `interval_s: 18000000` parsed fine and meant 208 days between probes,
    invisibly (a cycle with nothing due still records `dashboard-probes` as ok).
    A day is the ceiling, and 86400 itself stays legal."""
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][1]["probe"] = {"rclone_path": "gdrive-ro:Backups",
                               "interval_s": MAX_PROBE_INTERVAL_S}
    assert parse_registry(doc).get("tree").probe_interval_s == MAX_PROBE_INTERVAL_S
    doc["jobs"][1]["probe"]["interval_s"] = MAX_PROBE_INTERVAL_S + 1
    with pytest.raises(RegistryError, match=r"over the 86400 second maximum"):
        parse_registry(doc)
    doc["jobs"][1]["probe"]["interval_s"] = 18_000_000        # the 208-day typo
    with pytest.raises(RegistryError, match=r"over the 86400 second maximum"):
        parse_registry(doc)
    # ...and on a manual job, the other kind that may carry a probe block.
    doc = copy.deepcopy(JOBS_DOC)
    doc["jobs"][4]["probe"] = {"rclone_path": "gdrive-ro:Gremlins",
                               "interval_s": 10 ** 20}
    with pytest.raises(RegistryError, match=r"over the 86400 second maximum"):
        parse_registry(doc)


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


# --------------------------------------------------------------------------- #
# kind: worker  (the Inbox's background loops)
# --------------------------------------------------------------------------- #

WORKER_IDX = 9


def test_worker_is_a_scheduled_kind_with_no_extras():
    """A `worker` is "did this background loop run?" and nothing else: it needs a
    cadence + grace (so silence is LATE), and it may carry none of the blocks
    that belong to a kind with a destination."""
    from dashboard.registry import PROBEABLE_KINDS, SCHEDULED_KINDS
    assert "worker" in SCHEDULED_KINDS and "worker" not in PROBEABLE_KINDS
    job = parse_registry(JOBS_DOC).get("worker")
    assert job.kind == "worker" and job.scheduled
    assert job.deadline_s == 1800 and not job.has_probe
    assert not job.informational and job.max_age_s is None


@pytest.mark.parametrize("overrides,msg", [
    ({"cadence_s": None}, "'cadence_s' is required"),
    ({"grace_s": None}, "'grace_s' is required"),
    ({"probe": {"rclone_path": "gdrive:x"}}, "cannot have a 'probe' block"),
    ({"manual": {"max_age_s": 60}}, "'manual' block is only valid"),
    ({"disk": {"max_used_pct": 90}}, "'disk' block is only valid"),
    ({"expect": ["a"]}, "'expect' is only valid"),
])
def test_worker_rejects_blocks_that_are_not_its_own(overrides, msg):
    doc = copy.deepcopy(JOBS_DOC)
    for key, value in overrides.items():
        if value is None:
            doc["jobs"][WORKER_IDX].pop(key, None)
        else:
            doc["jobs"][WORKER_IDX][key] = value
    with pytest.raises(RegistryError, match=msg):
        parse_registry(doc)


def test_exactly_one_non_self_probe_job_per_machine():
    """PERMANENT GUARD. `services._machine_probe` returns the FIRST `kind: probe`
    job for a machine, and the whole machine-offline rule hangs off it:

    - a SECOND probe job on `mac` makes which job stands for "the Mac is
      reachable" depend on the ORDER of jobs.yml, silently;
    - the FIRST probe job on `box` switches sibling-LATE suppression on for every
      box job at once — box jobs are currently never suppressed, which is why a
      box outage pages per job.

    Both are one-line edits with fleet-wide alerting consequences and neither
    would fail anything else. That is why the Inbox's loops are `kind: worker`.
    """
    from dashboard.services import SELF_JOB_ID
    reg = load_registry(EXAMPLE_JOBS)
    per_machine = {}
    for job in reg:
        if job.kind == "probe" and job.id != SELF_JOB_ID:
            per_machine.setdefault(job.machine, []).append(job.id)
    assert per_machine == {"mac": ["mac-probe"]}, per_machine
