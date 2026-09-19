"""Runtime settings, read once from the environment.

Every knob has an env var so the same image runs on the box (compose `.env`), in
CI, and locally. Tests build :class:`Settings` directly instead of touching the
environment. Nothing here is logged — several fields are secrets.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


MAX_FAIL_THRESHOLD = 10

# `app_host` is operator-configured, but the read role splices it straight into
# a `Location` header for the http->https upgrade, so it must be a BARE
# hostname first — no scheme, no port, no path, no userinfo, no whitespace.
# Without this, `APP_HOST=dashboard.example.test@evil.example` emits a Location
# the browser resolves to `evil.example` (everything before `@` is WHATWG
# userinfo) while the URL still *reads* like this app, `host/evil.net` smuggles
# a path, and an embedded CRLF makes Werkzeug raise on EVERY request — a
# whole-site 500 rather than a logged fail-open.
#
# NOTE \A/\Z with `.fullmatch()`, NEVER ^...$ with `.match()`: in Python "$"
# also matches immediately before a TRAILING NEWLINE, so "evil.net\n" would
# sail through a "^...$" check and reach a response header.
# Per-LABEL pattern (each dot-separated label 1-63 chars, no leading or
# trailing hyphen) — byte-identical to the one in jjho-fan-almanac, so all
# five sibling apps agree on exactly what a hostname is.
_HOSTNAME_RE = re.compile(
    r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_cidrs(name: str) -> tuple[str, ...]:
    """Comma-separated CIDR list; validated so a typo fails at startup, not silently."""
    import ipaddress
    raw = os.environ.get(name, "")
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ipaddress.ip_network(part, strict=False)
        except ValueError as exc:
            raise ValueError(f"{name}: {part!r} is not a valid CIDR") from exc
        out.append(part)
    return tuple(out)


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
    # Hard timeout for one `rclone lsjson` (per job). Big trees page slowly and
    # rclone backs off on Drive rate limits. Env var is deliberately prefixed:
    # bare `RCLONE_TIMEOUT*` names belong to rclone itself (`--timeout`), and a
    # name collision here would silently reconfigure rclone's networking.
    # Read through `effective_rclone_timeout_s`, which clamps it.
    rclone_timeout_s: int = 240
    # Consecutive failed probes OF ONE JOB before the dashboard's own
    # `dashboard-probes` job is reported FAIL. 1 = the old behaviour (alert on
    # the first transient rate limit). Recovery is always immediate. Only
    # transient (quota/timeout) failures are damped at all; a hard error trips
    # on the first failure. Read through `effective_fail_threshold`.
    probe_fail_threshold: int = 2
    # Backstop on the damping: however transient the errors look, a probed job
    # with no SUCCESSFUL probe for this long trips FAIL. 0 disables it.
    # Read through `effective_no_success_s`.
    probe_no_success_s: int = 3600
    # Set False in tests / when another process owns the scheduler.
    start_scheduler: bool = True
    # Per-IP sliding-window limits (count per window seconds).
    ping_rate_max: int = 120
    ping_rate_window_s: int = 60
    login_rate_max: int = 10
    login_rate_window_s: int = 900
    # Global (all clients together) failed-login cap — defeats per-IP limiter
    # evasion via many source addresses / spoofed proxy headers.
    login_global_max: int = 100
    # CIDRs whose CF-Connecting-IP header the READ role trusts (the tunnel
    # container's network). Empty = never trust the header.
    trusted_proxy_cidrs: tuple[str, ...] = ()
    max_body_bytes: int = 64 * 1024
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "dashboard.db")

    @property
    def https_redirect_host(self) -> str:
        """`app_host`, but only when it is safe to paste into a Location header.

        Empty = the http->https redirect is OFF (fail open). Deliberately
        SEPARATE from `app_host` itself: the Host/Origin pin *compares* the
        value and never emits it, so a malformed host must keep failing CLOSED
        there while the redirect fails OPEN here. See `_HOSTNAME_RE`.
        """
        host = (self.app_host or "").strip().lower()
        return host if _HOSTNAME_RE.fullmatch(host) else ""

    @property
    def effective_rclone_timeout_s(self) -> int:
        """One probe can never be allowed to outlast a whole cycle: probes are
        serial, so a timeout above the cycle interval guarantees overrun even
        for a single job."""
        return max(5, min(int(self.rclone_timeout_s), int(self.probe_interval_s)))

    @property
    def effective_fail_threshold(self) -> int:
        """At least 1 (alert eventually) and at most MAX_FAIL_THRESHOLD — a
        typo'd 200 would otherwise switch alerting off for a week."""
        return max(1, min(int(self.probe_fail_threshold), MAX_FAIL_THRESHOLD))

    @property
    def effective_no_success_s(self) -> int:
        """0 = backstop disabled. Otherwise at least one cycle, so it can never
        fire before the job has had a chance to be probed again."""
        window = int(self.probe_no_success_s)
        if window <= 0:
            return 0
        return max(window, int(self.probe_interval_s))

    @property
    def probe_cycle_budget_s(self) -> int:
        """Wall-clock budget for one probe cycle. Probes are serial, so without
        a budget `n_probed × timeout` can run far past the cycle interval (and,
        before the post-cycle clock fix, made the next cycle instantly due —
        back-to-back hammering of a remote that just rate-limited us). One cycle
        interval's worth of probing; whatever is left over stays due and is
        picked up next cycle, oldest-probe-first."""
        return max(int(self.effective_rclone_timeout_s), int(self.probe_interval_s))

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
            app_env=(os.environ.get("APP_ENV", "prod").strip().lower() or "prod"),
            probe_interval_s=_env_int("PROBE_INTERVAL_S", 300),
            tick_interval_s=_env_int("TICK_INTERVAL_S", 60),
            rclone_timeout_s=_env_int("DASHBOARD_RCLONE_TIMEOUT_S", 240),
            probe_fail_threshold=_env_int("PROBE_FAIL_THRESHOLD", 2),
            probe_no_success_s=_env_int("PROBE_NO_SUCCESS_S", 3600),
            start_scheduler=os.environ.get("DASHBOARD_NO_SCHEDULER", "") == "",
            trusted_proxy_cidrs=_env_cidrs("TRUSTED_PROXY_CIDR"),
        )
