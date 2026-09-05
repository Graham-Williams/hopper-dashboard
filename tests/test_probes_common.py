"""Ping payload builder, env-file parsing, HTTP retry logic (mocked — no network)."""
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import common  # noqa: E402


# --- build_ping -----------------------------------------------------------------
def test_build_ping_minimal_contract_shape():
    body = common.build_ping("ok")
    assert body == {"status": "ok"}


def test_build_ping_full_contract_shape():
    body = common.build_ping(
        "fail", started_at="2026-09-04T03:00:00-04:00", finished_at="2026-09-04T03:00:47-04:00",
        reason="error", exit_code=1, note="boom", metrics={"lag_bytes": 5, "lag_files": 1, "x": "y"},
    )
    assert set(body) == {"status", "started_at", "finished_at", "reason", "exit_code", "note", "metrics"}
    assert body["exit_code"] == 1
    assert body["metrics"] == {"lag_bytes": 5, "lag_files": 1, "x": "y"}


@pytest.mark.parametrize("status", ["ok", "fail", "skipped", "metric"])
def test_build_ping_accepts_all_statuses(status):
    assert common.build_ping(status)["status"] == status


def test_build_ping_rejects_unknown_status():
    with pytest.raises(ValueError):
        common.build_ping("great")


def test_build_ping_truncates_note_to_500():
    body = common.build_ping("ok", note="x" * 900)
    assert len(body["note"]) == 500
    assert body["note"].endswith("…")


def test_build_ping_drops_none_and_empty():
    body = common.build_ping("ok", note=None, reason="", metrics={"a": None})
    assert body == {"status": "ok"}


def test_clean_metrics_types():
    m = common.clean_metrics({"n": 1, "f": 1.5, "b": True, "s": "abc", "bad key!": 2, "long": "z" * 2000, "none": None})
    assert m["n"] == 1 and m["f"] == 1.5 and m["b"] is True and m["s"] == "abc"
    assert "bad_key_" in m
    assert len(m["long"]) == common.METRIC_STR_MAX
    assert "none" not in m


def test_ping_url_and_job_id_validation():
    assert common.ping_url("http://h:8081/", "km-backup") == "http://h:8081/api/v1/ping/km-backup"
    for bad in ("KM", "a_b", "a b", "", "../x"):
        with pytest.raises(ValueError):
            common.validate_job_id(bad)


def test_slug():
    assert common.slug("world backups") == "world_backups"
    assert common.slug("Recordings!") == "recordings"


# --- env file -------------------------------------------------------------------
def test_parse_env_text():
    text = """
# comment
DASHBOARD_URL=http://h:8081
export INGEST_TOKEN="abc def"
QUOTED='single'
TRAILING=value  # trailing comment
BAD LINE
=novalue
1BAD=x
LAST=one
LAST=two
"""
    env = common.parse_env_text(text)
    assert env == {
        "DASHBOARD_URL": "http://h:8081",
        "INGEST_TOKEN": "abc def",
        "QUOTED": "single",
        "TRAILING": "value",
        "LAST": "two",
    }


def test_load_env_file_missing(tmp_path):
    assert common.load_env_file(str(tmp_path / "nope")) == {}


def test_load_config_has_no_default_url(tmp_path, monkeypatch):
    """The box's Tailscale IP is deployment-specific: no URL is baked into the code."""
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    monkeypatch.delenv("INGEST_TOKEN", raising=False)
    p = tmp_path / "env"
    p.write_text("INGEST_TOKEN=t\n")
    cfg = common.load_config(str(p))
    assert "DASHBOARD_URL" not in cfg
    assert cfg["INGEST_TOKEN"] == "t"
    with pytest.raises(common.ProbeError) as ei:
        common.require_dashboard_url(cfg, str(p))
    assert "DASHBOARD_URL is not set" in str(ei.value) and str(p) in str(ei.value)
    with pytest.raises(common.ProbeError):
        common.require_dashboard_url({"DASHBOARD_URL": "h:8081"})
    assert common.require_dashboard_url({"DASHBOARD_URL": " http://h:8081 "}) == "http://h:8081"


# --- state ----------------------------------------------------------------------
def test_state_roundtrip_and_corrupt(tmp_path):
    p = str(tmp_path / "state.json")
    assert common.load_state(p) == {}
    common.save_state(p, {"a": 1})
    assert common.load_state(p) == {"a": 1}
    with open(p, "w") as fh:
        fh.write("{not json")
    assert common.load_state(p) == {}


# --- time -----------------------------------------------------------------------
def test_local_naive_to_iso_has_offset():
    iso = common.local_naive_to_iso("2026-09-04 03:00:47")
    assert iso.startswith("2026-09-04T03:00:47")
    assert iso[-6] in "+-" and iso[-3] == ":"


# --- HTTP with retries (monkeypatched urlopen) ------------------------------------
class _Resp:
    status = 200

    def __init__(self, text=b'{"ok": true, "state": "OK"}'):
        self._t = text

    def read(self):
        return self._t

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_ping_success_sets_headers(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["ct"] = req.get_header("Content-type")
        seen["data"] = req.data
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(common.urllib.request, "urlopen", fake_urlopen)
    code, text = common.send_ping("http://h:8081", "tok", "mac-probe", {"status": "ok"}, timeout=7)
    assert code == 200 and "OK" in text
    assert seen["url"] == "http://h:8081/api/v1/ping/mac-probe"
    assert seen["auth"] == "Bearer tok"
    assert seen["ct"] == "application/json"
    assert seen["data"] == b'{"status": "ok"}'
    assert seen["timeout"] == 7


def test_send_ping_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("connection refused")
        return _Resp()

    monkeypatch.setattr(common.urllib.request, "urlopen", fake_urlopen)
    code, _ = common.send_ping("http://h", "t", "mac-probe", {"status": "ok"}, retries=2, sleep=lambda s: None)
    assert code == 200 and calls["n"] == 3


def test_send_ping_gives_up_after_retries(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(common.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(common.ProbeError) as ei:
        common.send_ping("http://h", "secret-token", "mac-probe", {"status": "ok"}, retries=2, sleep=lambda s: None)
    assert "3 attempts" in str(ei.value)
    assert "secret-token" not in str(ei.value)


def test_send_ping_404_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

    monkeypatch.setattr(common.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(common.ProbeError) as ei:
        common.send_ping("http://h", "t", "typo-job", {"status": "ok"}, retries=2, sleep=lambda s: None)
    assert calls["n"] == 1 and "404" in str(ei.value)


def test_send_ping_5xx_is_retried(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)

    monkeypatch.setattr(common.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(common.ProbeError):
        common.send_ping("http://h", "t", "mac-probe", {"status": "ok"}, retries=2, sleep=lambda s: None)
    assert calls["n"] == 3


# --- run_cmd --------------------------------------------------------------------
def test_run_cmd_missing_binary():
    rc, out, err = common.run_cmd(["/nonexistent/binary"], timeout=5)
    assert rc == -2 and "not found" in err


def test_run_cmd_timeout():
    rc, out, err = common.run_cmd([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
    assert rc == -1 and "timeout" in err


def test_find_rclone_prefers_explicit(tmp_path):
    fake = tmp_path / "rclone"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert common.find_rclone(str(fake)) == str(fake)
    # a bad explicit path falls through to the absolute candidates / PATH — never raises
    found = common.find_rclone(str(tmp_path / "missing"))
    assert found is None or found.endswith("rclone")


def test_logger_writes_line(tmp_path):
    p = tmp_path / "logs" / "probe.log"
    lg = common.Logger(str(p))
    lg.log("hello")
    lg.error("bad")
    lines = p.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(" hello") and lines[1].endswith(" ERROR: bad")
    assert lines[0][:10].count("-") == 2
