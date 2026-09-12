"""box-disk: statvfs → capacity metrics + ping shape (and the shared disk_free helper),
plus the systemd wrapper's job of reporting a disk probe that never posted at all."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from probes import common, disk_probe, rclone_check  # noqa: E402

WRAPPER = os.path.join(ROOT, "deploy", "box", "containers_probe.sh")


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


def test_posted_fail_exits_3_so_the_wrapper_does_not_double_report(monkeypatch, capsys, tmp_path):
    """The exit-code contract deploy/box/containers_probe.sh depends on: a delivered
    `fail` run is rc 3, not 1, because the dashboard already has the specific error."""
    monkeypatch.setenv("DASHBOARD_URL", "http://h:8081")
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    monkeypatch.setattr(disk_probe, "send_ping", lambda *a, **k: (200, "{}"))
    assert disk_probe.main(["--path", str(tmp_path / "gone")]) == 3
    assert "fail" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# deploy/box/containers_probe.sh — the unit's exit status is only visible in the
# journal, so a disk probe that cannot even start has to be reported by the wrapper.
# --------------------------------------------------------------------------- #

def _wrapper_env(tmp_path, disk_rc=0, containers_rc=0, **overrides):
    """A stub interpreter with per-probe exit codes + a fake curl that logs its argv."""
    py = tmp_path / "py"
    py.write_text('#!/bin/bash\ncase "$1" in\n  *disk_probe.py) exit %d ;;\n'
                  '  *containers_probe.py) exit %d ;;\nesac\nexit 0\n' % (disk_rc, containers_rc))
    py.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "curl.log"
    curl = bindir / "curl"
    curl.write_text('#!/bin/bash\nprintf "%%s\\n" "$@" >> %s\n' % log)
    curl.chmod(0o755)
    env = dict(os.environ)
    env.update({"PATH": str(bindir) + os.pathsep + env.get("PATH", ""),
                "PYTHON": str(py), "DASHBOARD_URL": "http://h:8081",
                "INGEST_TOKEN": "tok"})
    env.update(overrides)
    return env, log


def _run_wrapper(env, *args):
    return subprocess.run(["bash", WRAPPER] + list(args), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)


def test_wrapper_posts_a_fail_when_the_disk_probe_never_ran(tmp_path):
    env, log = _wrapper_env(tmp_path, disk_rc=2)    # e.g. the file was renamed/moved
    p = _run_wrapper(env)
    assert p.returncode == 2, p.stderr
    logged = log.read_text()
    assert "/api/v1/ping/box-disk" in logged
    assert "result=probe-failed" in logged and "exit=2" in logged
    # The note has to say where to look — the rc itself is only in the journal.
    assert "journalctl -u dashboard-containers.service" in logged


def test_wrapper_stays_quiet_when_the_probe_posted_its_own_failure(tmp_path):
    env, log = _wrapper_env(tmp_path, disk_rc=3)
    p = _run_wrapper(env)
    assert p.returncode == 3, p.stderr
    assert not log.exists()          # no vaguer ping on top of the statvfs error


def test_wrapper_does_not_blame_the_disk_for_a_containers_failure(tmp_path):
    env, log = _wrapper_env(tmp_path, containers_rc=4)
    p = _run_wrapper(env)
    assert p.returncode == 4, p.stderr
    assert not log.exists()


def test_wrapper_reports_nothing_and_still_exits_nonzero_without_credentials(tmp_path):
    env, log = _wrapper_env(tmp_path, disk_rc=1, DASHBOARD_URL="", INGEST_TOKEN="")
    p = _run_wrapper(env)
    assert p.returncode == 1 and not log.exists()
    assert "DASHBOARD_URL/INGEST_TOKEN not set" in p.stderr


def test_wrapper_dry_run_prints_the_failure_ping_instead_of_sending(tmp_path):
    env, log = _wrapper_env(tmp_path, disk_rc=1)
    p = _run_wrapper(env, "--dry-run")
    assert p.returncode == 1 and not log.exists()
    assert "DRY-RUN POST http://h:8081/api/v1/ping/box-disk" in p.stdout
