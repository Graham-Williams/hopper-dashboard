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

# `owner/repo`, the only shape the unauthenticated GitHub issue mirror can use.
# Validated at STARTUP (not at fetch time) so a typo in the box `.env` is a
# container that refuses to come up naming the bad value, rather than a mirror
# that quietly syncs nothing for ever — the same rule registry.py applies to
# jobs.yml. It also means nothing interpolated into a GitHub URL has ever been
# unvalidated.
# Each half must START with an alphanumeric, which is both GitHub's own rule and
# the thing that matters here: `[\w.-]+/[\w.-]+` happily matches `../x`, and this
# string is interpolated into an api.github.com path — a repo of `..` walks up
# out of `/repos/` and asks GitHub for something else entirely. ASCII classes
# rather than `\w`, which is unicode-aware and would admit homoglyphs.
# `\Z`, NOT `$`: in Python `$` also matches immediately BEFORE a trailing
# newline, so "a/b\n" would pass a `$`-anchored check — and this string is
# interpolated into an api.github.com path and stored as a mirror key.
GITHUB_REPO_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")
# A repo list long enough to blow the mirror's own cadence is a paste accident.
MAX_GITHUB_REPOS = 50

# The characters a GitHub token may contain. Every shape GitHub issues (`ghp_`,
# `github_pat_`, the older 40-hex PATs, an installation token) fits inside this.
#
# Validated at STARTUP, and the reason is not typo-catching: the token goes into
# an `Authorization: Bearer …` header, and `http.client.putheader` rejects an
# illegal header value by raising `ValueError('Invalid header value %r' % value)`
# — with the WHOLE header, token included, inside the message. That message
# becomes `MirrorResponse.error`, which the mirror then both `log.warning`s and
# writes to `inbox_mirror_state.last_error`. So a token with a stray newline or
# space (trivially produced by a copy-paste into `.env`) would put the secret in
# the log and in the database. Refusing it here means the illegal header value
# can never be constructed; `github_mirror._redact` is the second, independent
# defence for the same leak.
GITHUB_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]+\Z")   # \Z, not $ — see above
MAX_GITHUB_TOKEN = 255


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


def _env_github_repos(name: str) -> tuple[str, ...]:
    """Comma/whitespace-separated ``owner/repo`` list, validated here so a bad
    value fails the container at startup instead of at the first sync."""
    raw = os.environ.get(name, "")
    out: list[str] = []
    for part in re.split(r"[,\s]+", raw):
        part = part.strip()
        if not part:
            continue
        if not GITHUB_REPO_RE.match(part):
            raise ValueError(f"{name}: {part!r} is not a valid owner/repo")
        if part not in out:
            out.append(part)
    if len(out) > MAX_GITHUB_REPOS:
        raise ValueError(f"{name}: {len(out)} repos is over the "
                         f"{MAX_GITHUB_REPOS} maximum")
    return tuple(out)


def _env_github_token(name: str) -> str:
    """The token, validated so it can never become an illegal header value.

    The error message names the variable and the RULE, never the value — the
    whole point of this function is that the token does not end up in a string
    that gets logged.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ""
    if len(raw) > MAX_GITHUB_TOKEN:
        raise ValueError(f"{name} is longer than {MAX_GITHUB_TOKEN} characters")
    if not GITHUB_TOKEN_RE.match(raw):
        raise ValueError(f"{name} may only contain letters, digits, '_', '.' "
                         f"and '-' (it becomes an HTTP header value)")
    return raw


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
    # -- Inbox (/inbox) ---------------------------------------------------- #
    # Bearer token for the MACHINE side of the Inbox (the Mac transcription
    # worker and the backlog mirror). Empty = fail closed, exactly like
    # INGEST_TOKEN: every machine endpoint answers 401 and the browser side
    # still works. Deliberately NOT a `${VAR:?}` in compose — the board booting
    # matters more than the Inbox booting.
    inbox_token: str = ""
    # Per-upload cap for one voice note. 2 MB is roughly ten minutes of Opus,
    # which is far more than a spoken bug report and an order of magnitude less
    # than the 8 MB this used to allow — the cap multiplies by the create
    # limiter (30 per 15 min per IP) into how much disk one address can spend.
    # Applied PER REQUEST on the create route only
    # (`request.max_content_length`); the global 64 KB body cap that protects
    # every other route is never raised.
    inbox_audio_max_bytes: int = 2 * 1024 * 1024
    # AGGREGATE cap on the whole audio tree, checked before each upload. The
    # per-note cap alone bounds nothing over time: 30 notes / 15 min / IP at the
    # per-note limit is still gigabytes a day, and `box-disk` only notices once
    # the damage is done. 3 GB is years of real use — a 30 s note is ~60 KB.
    inbox_audio_max_total_bytes: int = 3 * 1024 ** 3
    # Audio is deleted once its transcript is Whisper-quality AND the item has
    # been reviewed AND it is older than this — and UNCONDITIONALLY at twice
    # this age, whatever its state. See inbox_db.prunable_audio: the second rule
    # is the privacy ceiling, and without it this setting reads like a maximum
    # and behaves like a minimum.
    inbox_audio_retention_days: int = 90
    # `owner/repo` list for the unauthenticated GitHub issue mirror. App config,
    # not per-job data, so it lives in .env rather than the gitignored jobs.yml.
    inbox_github_repos: tuple[str, ...] = ()
    # Optional: lifts the unauthenticated 60 req/h rate limit (and would allow
    # private repos). Empty = unauthenticated, which is the supported default.
    inbox_github_token: str = ""
    inbox_github_interval_s: int = 900
    # How often the scheduler sweeps prunable audio + reconciles the file tree
    # against the DB. Hourly: the work is a bounded DELETE plus a directory walk.
    inbox_prune_interval_s: int = 3600
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "dashboard.db")

    @property
    def inbox_db_path(self) -> str:
        """A SEPARATE file from dashboard.db on purpose.

        ``dashboard.db`` keeps exactly one request-path writer (ingest), now
        enforced by ``db.connect_query_only``. The Inbox needs browser-driven
        writes from the multi-worker read role, so those go to their own file,
        coordinated with the scheduler's mirror/prune writes by WAL +
        ``busy_timeout``."""
        return os.path.join(self.data_dir, "inbox.db")

    @property
    def inbox_audio_dir(self) -> str:
        """Voice notes are FILES, never BLOBs: the DB is snapshotted and
        sha256-deduped on every change, and megabytes of per-note audio would
        make every snapshot byte-unique and defeat that dedup entirely."""
        return os.path.join(self.data_dir, "inbox", "audio")

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
            inbox_token=os.environ.get("INBOX_TOKEN", ""),
            inbox_audio_max_bytes=_env_int("INBOX_AUDIO_MAX_BYTES",
                                           2 * 1024 * 1024),
            inbox_audio_max_total_bytes=_env_int("INBOX_AUDIO_MAX_TOTAL_BYTES",
                                                 3 * 1024 ** 3),
            inbox_audio_retention_days=_env_int("INBOX_AUDIO_RETENTION_DAYS", 90),
            inbox_github_repos=_env_github_repos("INBOX_GITHUB_REPOS"),
            inbox_github_token=_env_github_token("INBOX_GITHUB_TOKEN"),
            inbox_github_interval_s=_env_int("INBOX_GITHUB_INTERVAL_S", 900),
            inbox_prune_interval_s=_env_int("INBOX_PRUNE_INTERVAL_S", 3600),
        )
