"""mac_probe orchestration with every external dependency stubbed: sub-probe isolation, dry-run
never writes state, state only advances after a successful send, heartbeat summarises failures."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import backup_log, mac_probe  # noqa: E402
from probes.common import ProbeError  # noqa: E402


def _env(tmp_path, **extra):
    env = tmp_path / "env"
    lines = ["DASHBOARD_URL=http://h:8081", "INGEST_TOKEN=tok",
             "PROBE_LOG_FILE=%s" % (tmp_path / "probe.log"),
             "PROBE_STATE_FILE=%s" % (tmp_path / "state.json")]
    lines += ["%s=%s" % kv for kv in extra.items()]
    env.write_text("\n".join(lines) + "\n")
    return str(env)


def _stub_probes(monkeypatch, pa=None, mc=None, dm=None):
    entry = backup_log.parse_line("2026-09-04 03:00:47 backup complete")
    monkeypatch.setattr(mac_probe, "probe_pa_backup", pa or (lambda cfg, state, log: ([("pa-backup", {"status": "ok"})], entry)))
    monkeypatch.setattr(mac_probe, "probe_minecraft_offload", mc or (lambda cfg, log: [("minecraft-offload", {"status": "metric", "metrics": {"lag_bytes": 0}})]))
    monkeypatch.setattr(mac_probe, "probe_drive_mirror", dm or (lambda cfg, log: [("drive-mirror", {"status": "metric", "metrics": {"pending": 0}})]))
    return entry


def test_dry_run_prints_all_pings_and_writes_no_state(tmp_path, monkeypatch, capsys):
    _stub_probes(monkeypatch)
    sent = []
    monkeypatch.setattr(mac_probe, "send_ping", lambda *a, **k: sent.append(a) or (200, "{}"))
    rc = mac_probe.main(["--dry-run", "--env", _env(tmp_path), "--quiet"])
    out = capsys.readouterr().out
    assert rc == 0 and not sent
    for job in ("pa-backup", "minecraft-offload", "drive-mirror", "mac-probe"):
        assert "/api/v1/ping/%s" % job in out
    assert not (tmp_path / "state.json").exists()
    assert "run ok" in (tmp_path / "probe.log").read_text()


def test_real_run_sends_and_persists_state(tmp_path, monkeypatch):
    entry = _stub_probes(monkeypatch)
    sent = []

    def fake_send(url, token, job_id, body, timeout=10):
        sent.append((url, token, job_id, body))
        return 200, '{"ok": true, "state": "OK"}'

    monkeypatch.setattr(mac_probe, "send_ping", fake_send)
    rc = mac_probe.main(["--env", _env(tmp_path), "--quiet"])
    assert rc == 0
    assert [s[2] for s in sent] == ["pa-backup", "minecraft-offload", "drive-mirror", "mac-probe"]
    assert all(s[0] == "http://h:8081" and s[1] == "tok" for s in sent)
    hb = sent[-1][3]
    assert hb["status"] == "ok" and hb["metrics"]["subprobes_failed"] == 0 and hb["metrics"]["pings_sent"] == 3
    state = json.loads((tmp_path / "state.json").read_text())
    assert state[backup_log.STATE_KEY]["last_reported_line"] == entry.raw


def test_one_subprobe_failure_does_not_block_others(tmp_path, monkeypatch):
    def boom(cfg, log):
        raise ProbeError("rclone not found")

    _stub_probes(monkeypatch, mc=boom)
    sent = []
    monkeypatch.setattr(mac_probe, "send_ping", lambda u, t, j, b, timeout=10: sent.append((j, b)) or (200, "{}"))
    rc = mac_probe.main(["--env", _env(tmp_path), "--quiet"])
    assert rc == 1
    jobs = [j for j, _ in sent]
    assert jobs == ["pa-backup", "drive-mirror", "mac-probe"]
    hb = sent[-1][1]
    assert hb["status"] == "fail" and "minecraft-offload: rclone not found" in hb["note"]
    assert hb["metrics"]["subprobes_failed"] == 1


def test_unexpected_exception_is_contained(tmp_path, monkeypatch):
    def crash(cfg, log):
        raise KeyError("oops")

    _stub_probes(monkeypatch, dm=crash)
    sent = []
    monkeypatch.setattr(mac_probe, "send_ping", lambda u, t, j, b, timeout=10: sent.append((j, b)) or (200, "{}"))
    rc = mac_probe.main(["--env", _env(tmp_path), "--quiet"])
    assert rc == 1 and sent[-1][1]["status"] == "fail" and "KeyError" in sent[-1][1]["note"]
    assert "ERROR: drive-mirror crashed" in (tmp_path / "probe.log").read_text()


def test_pa_backup_state_not_advanced_when_send_fails(tmp_path, monkeypatch):
    _stub_probes(monkeypatch)

    def flaky_send(url, token, job_id, body, timeout=10):
        if job_id == "pa-backup":
            raise ProbeError("ping pa-backup failed after 3 attempts: refused")
        return 200, "{}"

    monkeypatch.setattr(mac_probe, "send_ping", flaky_send)
    rc = mac_probe.main(["--env", _env(tmp_path), "--quiet"])
    assert rc == 1
    assert not (tmp_path / "state.json").exists()  # will be retried next hour


def test_missing_token_refuses_to_run(tmp_path, monkeypatch):
    monkeypatch.delenv("INGEST_TOKEN", raising=False)
    env = tmp_path / "env"
    env.write_text("DASHBOARD_URL=http://h\nPROBE_LOG_FILE=%s\n" % (tmp_path / "probe.log"))
    rc = mac_probe.main(["--env", str(env), "--quiet"])
    assert rc == 2 and "INGEST_TOKEN missing" in (tmp_path / "probe.log").read_text()


def test_missing_url_refuses_to_run_even_dry(tmp_path, monkeypatch):
    """No default URL in code: an env file without DASHBOARD_URL is a config error, not a silent post to nowhere."""
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    _stub_probes(monkeypatch)
    env = tmp_path / "env"
    env.write_text("INGEST_TOKEN=tok\nPROBE_LOG_FILE=%s\n" % (tmp_path / "probe.log"))
    rc = mac_probe.main(["--dry-run", "--env", str(env), "--quiet"])
    assert rc == 2 and "DASHBOARD_URL is not set" in (tmp_path / "probe.log").read_text()


def test_only_flag_runs_single_subprobe(tmp_path, monkeypatch):
    _stub_probes(monkeypatch)
    sent = []
    monkeypatch.setattr(mac_probe, "send_ping", lambda u, t, j, b, timeout=10: sent.append(j) or (200, "{}"))
    assert mac_probe.main(["--env", _env(tmp_path), "--quiet", "--only", "drive-mirror"]) == 0
    assert sent == ["drive-mirror", "mac-probe"]


# --- the real probe_pa_backup with rclone stubbed ---------------------------------
def test_probe_pa_backup_dedups_and_reports_missing_vs_differ(tmp_path, monkeypatch):
    """All three backup trees are checked; MISSING (never uploaded) and DIFFER (edited since) are
    reported separately and summed; dest size is summed across trees; the log line is deduped."""
    home = tmp_path / "home"
    pa = home / "personal-assistant"; mem = home / ".claude/projects/-Users-graham-personal-assistant/memory"; cc = home / ".claude"
    for d in (pa, mem, cc):
        d.mkdir(parents=True, exist_ok=True)
    (pa / "CLAUDE.md").write_bytes(b"q" * 321)          # differ
    (pa / "new-note.txt").write_bytes(b"n" * 1000)      # missing
    (mem / "MEMORY.md").write_bytes(b"m" * 50)          # missing
    log_file = tmp_path / "hopper-backup.log"
    log_file.write_text("2026-09-03 03:00:00 backup complete\n2026-09-04 03:00:47 backup complete\n")
    cfg = mac_probe.load_settings(_env(tmp_path, PROBE_PA_LOG=str(log_file), PROBE_PA_HOME=str(home), PROBE_RCLONE=sys.executable))
    seen = []

    def fake_check(rclone, src, dst, filters=None, **k):
        seen.append((src, dst, tuple(filters or [])))
        if src.endswith("personal-assistant"):
            return mac_probe.rclone_check.parse_combined("= a\n* CLAUDE.md\n- new-note.txt\n")
        if src.endswith("memory"):
            return mac_probe.rclone_check.parse_combined("- MEMORY.md\n")
        return mac_probe.rclone_check.parse_combined("= settings.json\n= CLAUDE.md\n")

    monkeypatch.setattr(mac_probe.rclone_check, "rclone_check", fake_check)
    monkeypatch.setattr(mac_probe.rclone_check, "rclone_size", lambda r, dst, timeout: (10, 1000) if dst.endswith("personal-assistant") else (5, 500))
    log = mac_probe.Logger(None)

    pings, new_entry = mac_probe.probe_pa_backup(cfg, {}, log)
    assert new_entry is not None and new_entry.raw.endswith("2026-09-04 03:00:47 backup complete")
    assert [p[0] for p in pings] == ["pa-backup", "pa-backup"]
    run, metric = pings[0][1], pings[1][1]
    assert run["status"] == "ok" and run["finished_at"].startswith("2026-09-04T03:00:47")
    assert metric["status"] == "metric"
    m = metric["metrics"]
    assert m["missing_files"] == 2 and m["missing_bytes"] == 1050
    assert m["differ_files"] == 1 and m["differ_bytes"] == 321
    assert m["matched_files"] == 3 and m["trees_checked"] == 3 and m["trees"] == 3
    assert m["missing_files_personal_assistant"] == 1 and m["missing_files_hopper_memory"] == 1 and m["missing_files_claude_config"] == 0
    assert m["dest_count"] == 20 and m["dest_bytes"] == 2000 and m["dest_trees"] == 3
    assert "personal_assistant:new-note.txt" in m["missing_sample"]
    assert "lag_bytes" not in m   # the ambiguous combined figure is gone for this job
    # the three destinations + their filter lists mirror the backup script
    dsts = [d for _, d, _ in seen]
    assert dsts == ["gdrive:Backups/personal-assistant", "gdrive:Backups/hopper-memory", "gdrive:Backups/claude-config"]
    assert seen[0][2] == tuple(mac_probe.rclone_check.PA_BACKUP_FILTERS)
    assert seen[1][2] == () and "- sessions/**" in seen[2][2] and "- *.key" in seen[2][2]

    # second hour: same log line → only the metric ping
    state = backup_log.mark_reported(new_entry, {}, "now")
    pings2, new2 = mac_probe.probe_pa_backup(cfg, state, log)
    assert new2 is None and [p[1]["status"] for p in pings2] == ["metric"]


def test_probe_pa_backup_one_tree_error_is_partial(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "personal-assistant").mkdir(parents=True)
    (home / ".claude").mkdir(parents=True)
    cfg = mac_probe.load_settings(_env(tmp_path, PROBE_PA_LOG=str(tmp_path / "none.log"), PROBE_PA_HOME=str(home), PROBE_RCLONE=sys.executable))

    def fake_check(rclone, src, dst, filters=None, **k):
        if src.endswith(".claude"):
            raise ProbeError("rclone check failed (rc=3): directory not found")
        return mac_probe.rclone_check.parse_combined("= a\n")

    monkeypatch.setattr(mac_probe.rclone_check, "rclone_check", fake_check)
    monkeypatch.setattr(mac_probe.rclone_check, "rclone_size", lambda *a, **k: (1, 1))
    pings, _ = mac_probe.probe_pa_backup(cfg, {}, mac_probe.Logger(None))
    (job, body), = pings
    assert body["status"] == "metric" and body["metrics"]["trees_checked"] == 1 and body["metrics"]["trees_errored"] == 1
    assert body["metrics"]["missing_files"] == 0 and "directory not found" in body["note"]


def test_claude_config_filters_match_backup_script():
    f = mac_probe.rclone_check.CLAUDE_CONFIG_FILTERS
    assert "- sessions/**" in f and "- *.key" in f and "- .DS_Store" in f
    names = [t[0] for t in mac_probe.rclone_check.PA_BACKUP_TREES]
    assert names == ["personal-assistant", "hopper-memory", "claude-config"]


def test_probe_minecraft_offload_per_pair_metrics(tmp_path, monkeypatch):
    base = tmp_path / "mc"
    for d in ("recordings", "world backups"):  # 'replays' deliberately missing locally
        (base / d).mkdir(parents=True)
    (base / "recordings" / "new.mkv").write_bytes(b"m" * 2048)
    cfg = mac_probe.load_settings(_env(tmp_path, PROBE_MC_BASE=str(base), PROBE_MC_SCRIPT="/nonexistent", PROBE_RCLONE=sys.executable, PROBE_DISK_PATH=str(tmp_path)))

    def fake_check(rclone, src, dst, **k):
        assert dst.startswith("gdrive:Gremlins/") and k["min_age"] == "15m"
        return mac_probe.rclone_check.parse_combined("- new.mkv\n" if src.endswith("recordings") else "= world.tgz\n")

    monkeypatch.setattr(mac_probe.rclone_check, "rclone_check", fake_check)
    (job, body), = mac_probe.probe_minecraft_offload(cfg, mac_probe.Logger(None))
    m = body["metrics"]
    assert job == "minecraft-offload" and body["status"] == "metric"
    assert m["lag_bytes"] == 2048 and m["lag_files"] == 1
    assert m["lag_bytes_recordings"] == 2048 and m["lag_files_world_backups"] == 0 and m["lag_bytes_replays"] == 0
    assert m["pairs_checked"] == 2 and m["pairs_errored"] == 0 and m["disk_free_bytes"] > 0


def test_probe_drive_mirror_missing_db_is_fail_ping(tmp_path):
    cfg = mac_probe.load_settings(_env(tmp_path, PROBE_DRIVEFS_DIR=str(tmp_path / "nope")))
    (job, body), = mac_probe.probe_drive_mirror(cfg, mac_probe.Logger(None))
    assert job == "drive-mirror" and body["status"] == "fail" and "no DriveFS mirror db" in body["note"]
    assert body["finished_at"] and body["started_at"]


def test_probe_drive_mirror_read_is_an_ok_run_not_a_metric(tmp_path):
    """Reading the mirror DB IS the check: it must be a real `ok` run (with finished_at) so the
    drive-mirror job can leave UNKNOWN and its dead-man's switch has something to time from."""
    from tests.test_probes_drivefs import make_db
    acct = tmp_path / "DriveFS" / "100000000000000000001"
    acct.mkdir(parents=True)
    make_db(str(acct / "mirror_sqlite.db"), pending=1, mismatched=2)
    cfg = mac_probe.load_settings(_env(tmp_path, PROBE_DRIVEFS_DIR=str(tmp_path / "DriveFS")))
    (job, body), = mac_probe.probe_drive_mirror(cfg, mac_probe.Logger(None))
    assert job == "drive-mirror" and body["status"] == "ok" and body["reason"] == "probed"
    assert body["started_at"] and body["finished_at"]
    assert body["metrics"]["pending"] == 1 and body["metrics"]["mismatch"] == 2
    assert "not caught up" in body["note"]
