"""Shared plumbing for the probes: env-file parsing, ping payloads, HTTP with retries,
subprocess wrappers with timeouts, rclone path resolution and the append-only log.

Python 3.9 compatible, stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Contract constants (see DESIGN.md → API contract)
# ---------------------------------------------------------------------------
STATUSES = ("ok", "fail", "skipped", "metric")
JOB_ID_RE = re.compile(r"^[a-z0-9-]+$")
NOTE_MAX = 500
METRIC_STR_MAX = 1000  # metrics strings (e.g. the box-containers running list) are not bounded by the contract; note is

# There is deliberately NO default dashboard URL: the box's Tailscale IP is deployment-specific
# and stays out of the repo. DASHBOARD_URL must come from the env file or the environment.
DEFAULT_ENV_FILE = os.path.expanduser("~/.config/hopper-dashboard/env")
DEFAULT_STATE_FILE = os.path.expanduser("~/.config/hopper-dashboard/state.json")
DEFAULT_LOG_FILE = os.path.expanduser("~/Library/Logs/hopper-dashboard-probe.log")

# launchd runs with a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) that does NOT include Homebrew,
# so rclone is resolved by absolute path first — same list as scripts/backup-personal-assistant.sh.
RCLONE_CANDIDATES = ("/opt/homebrew/bin/rclone", "/usr/local/bin/rclone")

HTTP_TIMEOUT_S = 10
HTTP_RETRIES = 2
RCLONE_TIMEOUT_S = 120


class ProbeError(Exception):
    """A sub-probe failed in a way that should be reported, not crash the run."""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def local_naive_to_iso(ts: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """'2026-09-04 03:00:47' (local wall clock, as the backup log writes) → ISO 8601 with offset."""
    dt = datetime.strptime(ts, fmt)
    return dt.astimezone().isoformat(timespec="seconds")


def epoch_to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Env file (KEY=VALUE, chmod 600). No shell evaluation — plain parsing only.
# ---------------------------------------------------------------------------
def parse_env_text(text: str) -> Dict[str, str]:
    """Parse KEY=VALUE lines. Supports comments, blank lines, an optional ``export`` prefix and
    single/double quoted values. Later keys override earlier ones. Malformed lines are skipped."""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        else:
            # strip a trailing unquoted comment:  KEY=value  # comment
            val = re.split(r"\s+#", val, maxsplit=1)[0].rstrip()
        out[key] = val
    return out


def load_env_file(path: str) -> Dict[str, str]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return parse_env_text(fh.read())


def load_config(env_path: str = DEFAULT_ENV_FILE) -> Dict[str, str]:
    """Env file first, then real process environment overrides (handy for one-off runs)."""
    cfg = load_env_file(env_path)
    for k, v in os.environ.items():
        if k in ("DASHBOARD_URL", "INGEST_TOKEN") or k.startswith("PROBE_"):
            cfg[k] = v
    return cfg


def require_dashboard_url(cfg: Dict[str, str], env_path: str = "") -> str:
    """Return DASHBOARD_URL or raise ProbeError with a clear message (no baked-in default)."""
    url = (cfg.get("DASHBOARD_URL") or "").strip()
    if not url:
        where = " (env file %s)" % env_path if env_path else ""
        raise ProbeError("DASHBOARD_URL is not set%s — e.g. DASHBOARD_URL=http://<box-tailscale-ip>:8081" % where)
    if not url.startswith(("http://", "https://")):
        raise ProbeError("DASHBOARD_URL must start with http:// or https:// (got %r)" % url)
    return url


# ---------------------------------------------------------------------------
# Ping payloads
# ---------------------------------------------------------------------------
def validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise ValueError("invalid job id %r (must match [a-z0-9-]+)" % (job_id,))
    return job_id


def truncate(s: Optional[str], limit: int) -> Optional[str]:
    if s is None:
        return None
    s = str(s)
    if len(s) <= limit:
        return s
    marker = "…"
    return s[: max(0, limit - len(marker))] + marker


def clean_metrics(metrics: Optional[Dict[str, object]]) -> Dict[str, object]:
    """Flat dict; numbers/bools kept, strings truncated, None dropped, anything else stringified."""
    out: Dict[str, object] = {}
    if not metrics:
        return out
    for k, v in metrics.items():
        if v is None:
            continue
        key = re.sub(r"[^A-Za-z0-9_]", "_", str(k))
        if isinstance(v, bool) or isinstance(v, (int, float)):
            out[key] = v
        else:
            out[key] = truncate(str(v), METRIC_STR_MAX)
    return out


def build_ping(
    status: str,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    reason: Optional[str] = None,
    exit_code: Optional[int] = None,
    note: Optional[str] = None,
    metrics: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Build a body for POST /api/v1/ping/<job_id> per the frozen contract. Drops None fields,
    truncates ``note`` to 500 chars and flattens/cleans ``metrics``."""
    if status not in STATUSES:
        raise ValueError("invalid status %r (expected one of %s)" % (status, "|".join(STATUSES)))
    body: Dict[str, object] = {"status": status}
    if started_at:
        body["started_at"] = started_at
    if finished_at:
        body["finished_at"] = finished_at
    if reason:
        body["reason"] = truncate(reason, 100)
    if exit_code is not None:
        body["exit_code"] = int(exit_code)
    if note:
        body["note"] = truncate(note, NOTE_MAX)
    m = clean_metrics(metrics)
    if m:
        body["metrics"] = m
    return body


def ping_url(base_url: str, job_id: str) -> str:
    return base_url.rstrip("/") + "/api/v1/ping/" + validate_job_id(job_id)


# ---------------------------------------------------------------------------
# HTTP (urllib; 10 s timeout; 2 retries on network errors / 5xx / 429)
# ---------------------------------------------------------------------------
def send_ping(
    base_url: str,
    token: str,
    job_id: str,
    body: Dict[str, object],
    timeout: float = HTTP_TIMEOUT_S,
    retries: int = HTTP_RETRIES,
    sleep=time.sleep,
) -> Tuple[int, str]:
    """POST the JSON body. Returns (http_status, response_text). Raises ProbeError after the
    retries are exhausted or on a non-retryable 4xx. Never includes the token in messages."""
    url = ping_url(base_url, job_id)
    data = json.dumps(body).encode("utf-8")
    last_err = "unknown"
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "User-Agent": "hopper-dashboard-probe/1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 429 or e.code >= 500:
                last_err = "HTTP %d %s" % (e.code, text[:120])
            else:
                raise ProbeError("ping %s rejected: HTTP %d %s" % (job_id, e.code, text[:200]))
        except (urllib.error.URLError, OSError) as e:  # includes socket.timeout
            last_err = "%s" % (getattr(e, "reason", e),)
        if attempt < retries:
            sleep(1.5 * (attempt + 1))
    raise ProbeError("ping %s failed after %d attempts: %s" % (job_id, retries + 1, last_err))


# ---------------------------------------------------------------------------
# Subprocess + rclone
# ---------------------------------------------------------------------------
def find_rclone(explicit: Optional[str] = None) -> Optional[str]:
    cands: List[str] = []
    if explicit:
        cands.append(explicit)
    cands.extend(RCLONE_CANDIDATES)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d:
            cands.append(os.path.join(d, "rclone"))
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def run_cmd(argv: List[str], timeout: float, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Run a command with a hard timeout. Returns (rc, stdout, stderr); rc=-1 on timeout."""
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else ""
        return -1, out, "timeout after %ss" % timeout
    except FileNotFoundError as e:
        return -2, "", "not found: %s" % e
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Logging: append-only, one timestamped line per event, mirrors the backup script's discipline
# ("always write a line on failure").
# ---------------------------------------------------------------------------
class Logger:
    def __init__(self, path: Optional[str], echo: bool = False):
        self.path = path
        self.echo = echo
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)

    def log(self, msg: str) -> None:
        line = "%s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                print(line, file=sys.stderr)
        if self.echo:
            print(line)

    def error(self, msg: str) -> None:
        self.log("ERROR: " + msg)


# ---------------------------------------------------------------------------
# Small state file (JSON) with atomic writes
# ---------------------------------------------------------------------------
def load_state(path: str) -> Dict[str, object]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: Dict[str, object]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def disk_free(path: str) -> Dict[str, int]:
    """``statvfs`` capacity for the filesystem holding ``path``, as the two metric keys the
    dashboard's ``disk`` kind reads.

    ``f_bavail`` — the space actually available to a non-root user — is the deliberate choice:
    it is the number that matters when a recording can no longer be written.

    **The free BYTES match ``df``; the PERCENTAGE does not, and that is not a bug.** Measured on
    macOS/APFS: ``statvfs`` hands Python ``f_bfree == f_bavail`` (94.23 GiB, exactly ``df``'s
    Avail), while ``df`` gets a much larger ``f_bfree`` from ``statfs`` (~139 GiB) and computes
    its own percentage from that, so it printed 78% where the dashboard says 79.5%. The ~45 GiB
    gap is the OS's own accounting (purgeable and similar), NOT a reserve this code can see or
    subtract. The dashboard's figure is simply ``(total - available) / total``; expect it to read
    a point or two off ``df`` on the same filesystem, and do not "fix" the difference."""
    st = os.statvfs(path)
    return {
        "disk_free_bytes": st.f_bavail * st.f_frsize,
        "disk_total_bytes": st.f_blocks * st.f_frsize,
    }


def slug(name: str) -> str:
    """'world backups' → 'world_backups' (for flat metric keys)."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def join_nonempty(parts: Iterable[Optional[str]], sep: str = "; ") -> str:
    return sep.join(p for p in parts if p)
