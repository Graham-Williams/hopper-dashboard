import pytest

from dashboard import create_app
from dashboard.config import Settings
from dashboard.db import from_iso, to_iso
from dashboard.humanize import absolute, human_bytes, human_duration, relative
from dashboard.ratelimit import SlidingWindowLimiter


def test_iso_roundtrip_and_parsing():
    assert to_iso(0) == "1970-01-01T00:00:00Z"
    assert from_iso("1970-01-01T00:00:00Z") == 0
    assert from_iso("1970-01-01T01:00:00+01:00") == 0
    assert from_iso("1970-01-01T00:00:00.5") == 0.5
    assert from_iso("") is None and from_iso(None) is None and from_iso("nope") is None


def test_human_bytes():
    assert human_bytes(None) == "—"
    assert human_bytes(512) == "512 B"
    assert human_bytes(21474836480) == "21.5 GB"
    assert human_bytes(1_500_000) == "1.5 MB"
    assert human_bytes("x") == "—"


def test_human_duration_and_relative():
    assert human_duration(45) == "45s"
    assert human_duration(3600) == "1h"
    assert human_duration(90000) == "1d 1h"
    assert relative(None, 100) == "never"
    assert relative(to_iso(100), 100) == "just now"
    assert relative(to_iso(100), 100 + 3600 * 3) == "3h ago"
    assert relative(to_iso(100 + 120), 100) == "in 2m"
    assert absolute(to_iso(0)) == "1970-01-01 00:00:00 UTC"


def test_sliding_window_limiter():
    lim = SlidingWindowLimiter(max_events=2, window_seconds=10)
    assert lim.hit("a", now=0) and lim.hit("a", now=1)
    assert not lim.hit("a", now=2)
    assert lim.is_blocked("a", now=2)
    assert lim.hit("a", now=11)  # oldest aged out
    assert lim.hit("b", now=2)   # other key unaffected
    lim.reset("a")
    assert not lim.is_blocked("a", now=3)


def test_limiter_bounds_tracked_keys():
    lim = SlidingWindowLimiter(max_events=5, window_seconds=1, max_tracked_keys=3)
    for i in range(3):
        lim.hit(f"k{i}", now=0)
    lim.hit("k9", now=5)  # sweep drops the aged-out keys
    assert set(lim._events) == {"k9"}


def test_settings_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PROBE_INTERVAL_S", "42")
    monkeypatch.setenv("NTFY_URL", "https://ntfy.sh/")
    monkeypatch.setenv("APP_HOST", "Dash.Example.COM ")
    s = Settings.from_env()
    assert s.probe_interval_s == 42 and s.ntfy_url == "https://ntfy.sh"
    assert s.app_host == "dash.example.com" and s.db_path.endswith("dashboard.db")
    monkeypatch.setenv("PROBE_INTERVAL_S", "soon")
    with pytest.raises(ValueError, match="PROBE_INTERVAL_S"):
        Settings.from_env()


def test_probe_damping_knobs_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_DATA", str(tmp_path))
    s = Settings.from_env()
    assert s.rclone_timeout_s == 240 and s.probe_fail_threshold == 2
    monkeypatch.setenv("RCLONE_TIMEOUT_S", "600")
    monkeypatch.setenv("PROBE_FAIL_THRESHOLD", "3")
    s = Settings.from_env()
    assert s.rclone_timeout_s == 600 and s.probe_fail_threshold == 3
    monkeypatch.setenv("PROBE_FAIL_THRESHOLD", "twice")
    with pytest.raises(ValueError, match="PROBE_FAIL_THRESHOLD"):
        Settings.from_env()


def test_create_app_rejects_bad_role(settings, registry):
    with pytest.raises(ValueError, match="role"):
        create_app("admin", settings, registry)


def test_create_app_from_env_loads_example_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("JOBS_FILE", "jobs.example.yml")
    monkeypatch.setenv("DASHBOARD_NO_SCHEDULER", "1")
    app = create_app("ingest")
    assert len(app.extensions["registry"]) == 11
    assert app.extensions["scheduler"]._thread is None


def test_scheduler_thread_starts_and_stops(settings, registry, notifier, monkeypatch):
    settings.start_scheduler = True
    app = create_app("ingest", settings, registry, notifier)
    sched = app.extensions["scheduler"]
    assert sched._thread is not None and sched._thread.is_alive()
    sched.stop()
    sched._thread.join(timeout=5)
    assert not sched._thread.is_alive()
