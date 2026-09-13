"""Shared fixtures: an isolated data dir, a small registry covering every kind,
apps for both roles with the scheduler disabled, and a recording notifier."""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# The probe tests (tests/test_probes_*.py) must run under a bare stdlib
# interpreter (macOS /usr/bin/python3, no venv). The app fixtures below need
# Flask/PyYAML, so import them lazily: if the deps are missing, only the app
# fixtures become unusable, and the probe tests still collect and run.
try:
    from dashboard import create_app  # noqa: E402
    from dashboard.config import Settings  # noqa: E402
    from dashboard.notify import Notifier  # noqa: E402
    from dashboard.registry import parse_registry  # noqa: E402
    from dashboard.services import Core  # noqa: E402
except ImportError as _exc:  # pragma: no cover - stdlib-only runs
    create_app = Settings = parse_registry = Core = None  # type: ignore
    Notifier = object  # type: ignore  # lets RecordingNotifier still be defined
    _APP_IMPORT_ERROR = _exc
else:
    _APP_IMPORT_ERROR = None

EXAMPLE_JOBS = os.path.join(ROOT, "jobs.example.yml")

INGEST_TOKEN = "test-ingest-token"
READ_TOKEN = "test-read-token"
PASSWORD = "test-password"

# jobs.created_at drives "never pinged → LATE after cadence+grace". The suites
# use fixed clocks (2027) and the real clock; pinning created_at far in the
# future keeps never-pinged jobs UNKNOWN everywhere except the tests that set
# it explicitly (see tests/test_state.py / test_services.py).
PINNED_CREATED_AT = "2030-03-17T17:46:40Z"


def pin_created_at(settings, iso: str = PINNED_CREATED_AT) -> None:
    from dashboard import db
    conn = db.connect(settings.db_path)
    try:
        with conn:
            conn.execute("UPDATE jobs SET created_at=?", (iso,))
    finally:
        conn.close()

# One job per kind + the dashboard's own probe job. Cadences are short so
# LATE arithmetic is easy to reason about in tests. Appended, not inserted:
# several suites address these by index (doc["jobs"][4] etc.).
#
# Every job here carries `alert_after_s: 0` — "page as soon as it leaves OK",
# still capped at ONE page per episode. That keeps these suites focused on WHICH
# alert is worth sending (above all the machine-offline rule) without every test
# having to wind a 24 h clock forward. The threshold layer itself — the episode
# clock, the flap suppression, the unverified-OK hold, `alert: never`, the
# `informational` resolution — is exercised with realistic values in
# tests/test_alert_thresholds.py. `info` is the one exception: it declares
# nothing, so it exercises the informational → never resolution in place.
ALERT_NOW = {"alert_after_s": 0}
JOBS_DOC = {
    "jobs": [
        {"id": "snap", "name": "Snap DB", "machine": "box", "kind": "db_snapshot",
         "protects": "a DB", "method": "sqlite backup", "destination": "gdrive:snap",
         "cadence_s": 300, "grace_s": 300, **ALERT_NOW,
         "probe": {"rclone_path": "gdrive:snap", "state_dir": "/state/snap"}},
        {"id": "tree", "name": "Tree copy", "machine": "mac", "kind": "rclone_copy_tree",
         "protects": "docs", "method": "rclone copy", "destination": "gdrive:Backups",
         "cadence_s": 86400, "grace_s": 3600, **ALERT_NOW},
        {"id": "mirror", "name": "Drive mirror", "machine": "mac", "kind": "drive_mirror",
         "protects": "Documents", "method": "DriveFS", "cadence_s": 3600, "grace_s": 600,
         **ALERT_NOW},
        {"id": "containers", "name": "Containers", "machine": "box", "kind": "container",
         "protects": "apps", "method": "docker ps", "cadence_s": 300, "grace_s": 300,
         "expect": ["app-1", "tunnel-1"], **ALERT_NOW},
        {"id": "offload", "name": "Offload", "machine": "mac", "kind": "manual",
         "protects": "recordings", "method": "rclone copy", "destination": "gdrive:Gremlins",
         "manual": {"max_age_s": 1209600, "max_lag_bytes": 1000}, **ALERT_NOW},
        {"id": "info", "name": "Info-only", "machine": "mac", "kind": "manual",
         "protects": "nothing much", "method": "by hand"},
        {"id": "macprobe", "name": "Mac probe", "machine": "mac", "kind": "probe",
         "protects": "visibility", "method": "launchd", "cadence_s": 3600, "grace_s": 7200,
         "late_means": "Mac offline or asleep", **ALERT_NOW},
        {"id": "dashboard-probes", "name": "Probe cycle", "machine": "box", "kind": "probe",
         "protects": "the watcher", "method": "scheduler", "cadence_s": 300, "grace_s": 600,
         **ALERT_NOW},
        {"id": "disk", "name": "Mac disk", "machine": "mac", "kind": "disk",
         "protects": "headroom", "method": "statvfs", **ALERT_NOW,
         "disk": {"min_free_bytes": 25 * 1024 ** 3, "max_used_pct": 90}},
    ]
}


class RecordingNotifier(Notifier):
    """Real Notifier logic (enabled, body/priority) with the HTTP call captured.

    Two independent failure modes, because ``Notifier.send`` flattens both to
    False and the retry path has to cope with either: ``fail`` raises (DNS,
    timeout, connection reset) and ``refuse`` returns a non-2xx (429, 503).
    ``attempts`` records every POST tried, ``sent`` only the ones that landed.
    Both override ``_post``, not ``send`` — ``send``'s try/except is part of what
    is under test.
    """

    def __init__(self, fail: bool = False, refuse: bool = False):
        super().__init__("https://ntfy.example", "topic-secret")
        self.sent: list[tuple[str, str, str]] = []
        self.attempts: list[tuple[str, str, str]] = []
        self.fail = fail
        self.refuse = refuse

    def _post(self, title, body, priority):
        self.attempts.append((title, body, priority))
        if self.fail:
            raise RuntimeError("ntfy down")
        if self.refuse:
            return False
        self.sent.append((title, body, priority))
        return True


@pytest.fixture
def registry():
    return parse_registry(JOBS_DOC)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=str(tmp_path / "data"), jobs_file=EXAMPLE_JOBS,
        app_password=PASSWORD, session_secret="s" * 32,
        ingest_token=INGEST_TOKEN, read_token=READ_TOKEN,
        start_scheduler=False, ping_rate_max=20, ping_rate_window_s=60,
        login_rate_max=3, login_rate_window_s=900,
    )


@pytest.fixture
def notifier():
    return RecordingNotifier()


@pytest.fixture
def core(settings, registry, notifier):
    c = Core(settings, registry, notifier)
    c.init_store()
    pin_created_at(settings)
    return c


@pytest.fixture
def ingest_app(settings, registry, notifier):
    app = create_app("ingest", settings, registry, notifier)
    pin_created_at(settings)
    return app


@pytest.fixture
def read_app(settings, registry, notifier):
    app = create_app("read", settings, registry, notifier)
    pin_created_at(settings)
    return app


@pytest.fixture
def ingest(ingest_app):
    return ingest_app.test_client()


@pytest.fixture
def read(read_app):
    return read_app.test_client()


@pytest.fixture
def authed(read):
    read.post("/login", data={"password": PASSWORD})
    return read


def auth(token: str = INGEST_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}
