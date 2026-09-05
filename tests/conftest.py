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

# One job per kind + the dashboard's own probe job. Cadences are short so
# LATE arithmetic is easy to reason about in tests.
JOBS_DOC = {
    "jobs": [
        {"id": "snap", "name": "Snap DB", "machine": "box", "kind": "db_snapshot",
         "protects": "a DB", "method": "sqlite backup", "destination": "gdrive:snap",
         "cadence_s": 300, "grace_s": 300,
         "probe": {"rclone_path": "gdrive:snap", "state_dir": "/state/snap"}},
        {"id": "tree", "name": "Tree copy", "machine": "mac", "kind": "rclone_copy_tree",
         "protects": "docs", "method": "rclone copy", "destination": "gdrive:Backups",
         "cadence_s": 86400, "grace_s": 3600},
        {"id": "mirror", "name": "Drive mirror", "machine": "mac", "kind": "drive_mirror",
         "protects": "Documents", "method": "DriveFS", "cadence_s": 3600, "grace_s": 600},
        {"id": "containers", "name": "Containers", "machine": "box", "kind": "container",
         "protects": "apps", "method": "docker ps", "cadence_s": 300, "grace_s": 300,
         "expect": ["app-1", "tunnel-1"]},
        {"id": "offload", "name": "Offload", "machine": "mac", "kind": "manual",
         "protects": "recordings", "method": "rclone copy", "destination": "gdrive:Gremlins",
         "manual": {"max_age_s": 1209600, "max_lag_bytes": 1000}},
        {"id": "info", "name": "Info-only", "machine": "mac", "kind": "manual",
         "protects": "nothing much", "method": "by hand"},
        {"id": "macprobe", "name": "Mac probe", "machine": "mac", "kind": "probe",
         "protects": "visibility", "method": "launchd", "cadence_s": 3600, "grace_s": 7200,
         "late_means": "Mac offline or asleep"},
        {"id": "dashboard-probes", "name": "Probe cycle", "machine": "box", "kind": "probe",
         "protects": "the watcher", "method": "scheduler", "cadence_s": 300, "grace_s": 600},
    ]
}


class RecordingNotifier(Notifier):
    """Real Notifier logic (enabled, should_notify) with the HTTP call captured."""

    def __init__(self, fail: bool = False):
        super().__init__("https://ntfy.example", "topic-secret")
        self.sent: list[tuple[str, str, str]] = []
        self.fail = fail

    def _post(self, title, body, priority):
        if self.fail:
            raise RuntimeError("ntfy down")
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
    return c


@pytest.fixture
def ingest_app(settings, registry, notifier):
    return create_app("ingest", settings, registry, notifier)


@pytest.fixture
def read_app(settings, registry, notifier):
    return create_app("read", settings, registry, notifier)


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
