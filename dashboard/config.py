"""Runtime settings, read once from the environment.

Every knob has an env var so the same image runs on the box (compose `.env`), in
CI, and locally. Tests build :class:`Settings` directly instead of touching the
environment. Nothing here is logged — several fields are secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass
class Settings:
    data_dir: str = "/app/data"
    jobs_file: str = "/app/jobs.yml"
    app_password: str = ""
    session_secret: str = ""
    ingest_token: str = ""
    read_token: str = ""
    ntfy_url: str = ""
    ntfy_topic: str = ""
    app_host: str = ""
    app_env: str = "prod"
    probe_interval_s: int = 300
    tick_interval_s: int = 60
    # Set False in tests / when another process owns the scheduler.
    start_scheduler: bool = True
    # Per-IP sliding-window limits (count per window seconds).
    ping_rate_max: int = 120
    ping_rate_window_s: int = 60
    login_rate_max: int = 10
    login_rate_window_s: int = 900
    max_body_bytes: int = 64 * 1024
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "dashboard.db")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=os.environ.get("DASHBOARD_DATA", "/app/data"),
            jobs_file=os.environ.get("JOBS_FILE", "/app/jobs.yml"),
            app_password=os.environ.get("APP_PASSWORD", ""),
            session_secret=(os.environ.get("SESSION_SECRET", "")
                            or os.environ.get("SECRET_KEY", "")),
            ingest_token=os.environ.get("INGEST_TOKEN", ""),
            read_token=os.environ.get("READ_TOKEN", ""),
            ntfy_url=os.environ.get("NTFY_URL", "").rstrip("/"),
            ntfy_topic=os.environ.get("NTFY_TOPIC", "").strip(),
            app_host=os.environ.get("APP_HOST", "").strip().lower(),
            app_env=os.environ.get("APP_ENV", "prod"),
            probe_interval_s=_env_int("PROBE_INTERVAL_S", 300),
            tick_interval_s=_env_int("TICK_INTERVAL_S", 60),
            start_scheduler=os.environ.get("DASHBOARD_NO_SCHEDULER", "") == "",
        )
