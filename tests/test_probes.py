import subprocess

import pytest

from dashboard import probes
from dashboard.probes import (ProbeError, probe_job, read_state_dir,
                              run_rclone_lsjson, summarize_listing)
from dashboard.registry import parse_registry
from tests.conftest import JOBS_DOC

REG = parse_registry(JOBS_DOC)


def test_summarize_listing_newest_and_count():
    items = [
        {"Path": "a", "ModTime": "2026-09-01T00:00:00Z", "IsDir": False},
        {"Path": "daily", "ModTime": "2026-09-05T00:00:00Z", "IsDir": True},
        {"Path": "daily/b", "ModTime": "2026-09-03T12:00:00.123456789Z", "IsDir": False},
        {"Path": "c", "ModTime": "2026-09-02T00:00:00+02:00", "IsDir": False},
    ]
    newest, count = summarize_listing(items)
    assert count == 3 and newest == "2026-09-03T12:00:00Z"
    assert summarize_listing([]) == (None, 0)


def test_read_state_dir(tmp_path):
    assert read_state_dir(None) == (None, None)
    assert read_state_dir(str(tmp_path / "missing")) == (None, None)
    (tmp_path / "last_drive.sha256").write_text("AB" * 32 + "\n")
    (tmp_path / "last_drive_push.epoch").write_text("1725000000\n")
    assert read_state_dir(str(tmp_path)) == ("ab" * 32, 1725000000)
    (tmp_path / "last_drive.sha256").write_text("not-a-sha")
    (tmp_path / "last_drive_push.epoch").write_text("soon")
    assert read_state_dir(str(tmp_path)) == (None, None)


class _Proc:
    def __init__(self, code=0, out="[]", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_run_rclone_lsjson_uses_argv_not_shell(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        assert kw.get("shell") is not True
        return _Proc(out='[{"Path":"x","ModTime":"2026-09-01T00:00:00Z"}]')
    monkeypatch.setattr(subprocess, "run", fake_run)
    newest, count = run_rclone_lsjson("gdrive:snap; rm -rf /")
    assert seen["argv"][-1] == "gdrive:snap; rm -rf /"
    assert seen["argv"][0] == "rclone" and "--recursive" in seen["argv"]
    assert count == 1 and newest == "2026-09-01T00:00:00Z"


def test_run_rclone_error_paths(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(code=3, err="x\ndirectory not found"))
    with pytest.raises(ProbeError, match="exit 3: directory not found"):
        run_rclone_lsjson("g:x")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc(out="nope"))
    with pytest.raises(ProbeError, match="unparseable"):
        run_rclone_lsjson("g:x")

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("rclone", 1)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(ProbeError, match="timed out"):
        run_rclone_lsjson("g:x")

    def missing(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ProbeError, match="not found"):
        run_rclone_lsjson("g:x")


def test_probe_job_never_raises(monkeypatch, tmp_path):
    job = REG.get("snap")
    monkeypatch.setattr(probes, "run_rclone_lsjson", lambda p, timeout=0: (_ for _ in ()).throw(ProbeError("boom")))
    res = probe_job(job)
    assert res.ok is False and res.error == "boom"

    def crash(p, timeout=0):
        raise ValueError("weird")
    monkeypatch.setattr(probes, "run_rclone_lsjson", crash)
    res = probe_job(job)
    assert res.ok is False and "ValueError" in res.error

    monkeypatch.setattr(probes, "run_rclone_lsjson", lambda p, timeout=0: ("2026-09-01T00:00:00Z", 9))
    res = probe_job(job)
    assert res.ok and res.count == 9

    assert probe_job(REG.get("tree")).ok is False  # no probe configured
