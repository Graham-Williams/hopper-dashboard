"""box-disk: statvfs → capacity metrics + ping shape (and the shared disk_free helper)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import common, disk_probe, rclone_check  # noqa: E402


def test_disk_free_shape(tmp_path):
    d = common.disk_free(str(tmp_path))
    assert d["disk_free_bytes"] > 0 and d["disk_total_bytes"] >= d["disk_free_bytes"]
    # Re-exported for the Mac probe, which has always called it through rclone_check.
    assert rclone_check.disk_free is common.disk_free


def test_build_disk_ping_is_metrics_only(tmp_path):
    body = disk_probe.build_disk_ping(str(tmp_path))
    # `metric`, not a run: a disk job is a gauge and must never count as a heartbeat.
    assert body["status"] == "metric" and "started_at" not in body
    m = body["metrics"]
    assert m["disk_total_bytes"] >= m["disk_free_bytes"] > 0
    assert m["disk_path"] == str(tmp_path)


def test_build_disk_ping_unreadable_path_is_a_fail_run(tmp_path):
    """A vanished mount must be visible, not a gauge frozen at yesterday's figure."""
    body = disk_probe.build_disk_ping(str(tmp_path / "gone"))
    assert body["status"] == "fail" and body["reason"] == "error"
    assert "statvfs" in body["note"] and "metrics" not in body


def test_main_dry_run(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("INGEST_TOKEN", raising=False)
    monkeypatch.setenv("DASHBOARD_URL", "http://h:8081")
    rc = disk_probe.main(["--dry-run", "--path", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "/api/v1/ping/box-disk" in out and '"status": "metric"' in out
    assert '"disk_free_bytes"' in out


def test_path_comes_from_env_when_not_given(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DASHBOARD_URL", "http://h:8081")
    monkeypatch.setenv("PROBE_DISK_PATH", str(tmp_path))
    rc = disk_probe.main(["--dry-run"])
    assert rc == 0 and '"disk_path": "%s"' % tmp_path in capsys.readouterr().out


def test_main_without_dashboard_url_is_config_error(monkeypatch, capsys):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    rc = disk_probe.main(["--dry-run"])
    assert rc == 2 and "DASHBOARD_URL is not set" in capsys.readouterr().err


def test_main_without_token_refuses_to_send(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("DASHBOARD_URL", "http://h:8081")
    monkeypatch.delenv("INGEST_TOKEN", raising=False)
    rc = disk_probe.main(["--path", str(tmp_path)])
    assert rc == 2 and "INGEST_TOKEN not set" in capsys.readouterr().err


def test_main_sends_and_reports(monkeypatch, capsys, tmp_path):
    sent = []
    monkeypatch.setenv("DASHBOARD_URL", "http://h:8081")
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    monkeypatch.setattr(disk_probe, "send_ping",
                        lambda url, token, job, body: sent.append((url, token, job, body)) or (200, "{}"))
    rc = disk_probe.main(["--path", str(tmp_path)])
    assert rc == 0 and len(sent) == 1
    url, token, job, body = sent[0]
    assert (url, token, job) == ("http://h:8081", "tok", "box-disk")
    assert body["metrics"]["disk_free_bytes"] > 0
