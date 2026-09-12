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


def test_timeout_is_configurable_and_defaults_to_the_module_constant(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["timeout"] = kw.get("timeout")
        return _Proc()
    monkeypatch.setattr(subprocess, "run", fake_run)
    run_rclone_lsjson("g:x")
    assert seen["timeout"] == probes.RCLONE_TIMEOUT_S == 240
    run_rclone_lsjson("g:x", 600)
    assert seen["timeout"] == 600
    run_rclone_lsjson("g:x", 0)            # floored, never a zero-second probe
    assert seen["timeout"] == 5
    probe_job(REG.get("snap"), timeout=123)
    assert seen["timeout"] == 123


@pytest.mark.parametrize("text,transient", [
    ("rclone exit 7: googleapi: Error 403: rateLimitExceeded", True),
    ("rclone exit 7: userRateLimitExceeded, userRateLimitExceeded", True),
    ("rclone timed out after 240s", True),
    ("rclone exit 7: Error 429: Too Many Requests", True),
    ("rclone exit 7: Error 503: backendError", True),
    ("rclone exit 3: directory not found", False),
    ("rclone binary not found", False),
    ("rclone returned unparseable JSON", False),
    ("", False),
    (None, False),
])
def test_is_transient_error(text, transient):
    assert probes.is_transient_error(text) is transient


def test_transient_failures_are_labelled_in_the_error_text(monkeypatch):
    """So a quota push-back never reads like "the destination is wrong" on the
    card, in the probe table, or in the self-job's note."""
    monkeypatch.setattr(
        probes, "run_rclone_lsjson",
        lambda p, t=None: (_ for _ in ()).throw(ProbeError("rclone exit 7: rateLimitExceeded")))
    res = probe_job(REG.get("snap"))
    assert res.transient is True
    assert res.error.startswith("transient (Drive quota/timeout): ")
    assert "rateLimitExceeded" in res.error

    monkeypatch.setattr(
        probes, "run_rclone_lsjson",
        lambda p, t=None: (_ for _ in ()).throw(ProbeError("rclone exit 3: directory not found")))
    res = probe_job(REG.get("snap"))
    assert res.transient is False
    assert res.error == "rclone exit 3: directory not found"


def test_a_real_timeout_is_classified_transient(monkeypatch):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("rclone", 240)
    monkeypatch.setattr(subprocess, "run", timeout)
    res = probe_job(REG.get("snap"))
    assert res.ok is False and res.transient is True and "timed out" in res.error


@pytest.mark.parametrize("text,transient", [
    # rclone names the object it was working on; those names come from Drive.
    ('rclone exit 3: directory not found (dir "Backups/503 timeout")', False),
    ("rclone exit 3: directory not found (dir Backups/429-rate limit)", False),
    # …but a real quota error still classifies, object name and all.
    ("rclone exit 1: googleapi: Error 403: userRateLimitExceeded "
     "(dir Backups/km-tracker)", True),
])
def test_object_names_do_not_drive_the_transient_classification(text, transient):
    assert probes.is_transient_error(text) is transient


@pytest.mark.parametrize("text,transient", [
    # REAL rclone stderr: the failing path appears quoted AND bare, so
    # stripping only the quoted copy left `503`/`429` in the text. These are
    # hard errors — revoked permission, wrong path — and must page at once.
    ('rclone exit 1: Failed to lsjson: failed to open directory "503": '
     'open /srv/backups/503/km: permission denied', False),
    ('rclone exit 1: Failed to lsjson: failed to open directory "429": '
     'open /srv/backups/429/km: permission denied', False),
    # The real production path shape: dated snapshots. `20260503` contains
    # `503`; `20260429` contains `429`. Nothing adversarial is needed — the
    # backup trees generate these names every night.
    ("rclone exit 3: directory not found: "
     "gdrive:Backups/km-tracker/daily/km_tracker-20260503-0312.db", False),
    ("rclone exit 1: couldn't list directory: "
     "gdrive:Backups/todoist-points/points-20260429-0315.db: "
     "object not found", False),
    # A path can carry a marker WORD too, not just a status code.
    ("rclone exit 3: directory not found: gdrive:Backups/timeout/20260503",
     False),
    # Genuinely transient — the two shapes actually measured in production —
    # must stay transient.
    ("rclone exit 7: Failed to lsjson: couldn't list directory: googleapi: "
     "Error 403: User Rate Limit Exceeded, userRateLimitExceeded", True),
    ("rclone exit 7: googleapi: Error 403: rateLimitExceeded", True),
    ("rclone timed out after 240s", True),
    ("rclone exit 7: Failed to lsjson: couldn't list directory: Get "
     '"https://www.googleapis.com/drive/v3/files": net/http: request canceled '
     "(Client.Timeout exceeded while awaiting headers)", True),
    # Status codes still classify in their real HTTP spellings.
    ("rclone exit 7: googleapi: Error 429: Too Many Requests", True),
    ("rclone exit 7: failed to list: 503 Service Unavailable", True),
    ("rclone exit 7: googleapi: got HTTP response code 503 with body", True),
])
def test_realistic_rclone_stderr_is_classified_correctly(text, transient):
    assert probes.is_transient_error(text) is transient
