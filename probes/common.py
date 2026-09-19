"""Shared plumbing for the probes: env-file parsing, ping payloads, HTTP with retries,
subprocess wrappers with timeouts, rclone path resolution and the append-only log.

Python 3.9 compatible, stdlib only.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
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
#: Cap on a heartbeat-ingest response body. The endpoint answers with a line of JSON; anything
#: approaching this is a wrong host or a captive portal, not the dashboard.
PING_RESPONSE_BYTES = 256 * 1024


class ProbeError(Exception):
    """A sub-probe failed in a way that should be reported, not crash the run.

    ``status`` carries the HTTP status code when the failure WAS an HTTP status — so a caller
    can ask "was this a 401?" without pattern-matching the message. That distinction is
    load-bearing: the message embeds up to 200 bytes of the server's own response body, so a
    500 whose body happens to contain the string "HTTP 401" would otherwise be read as an
    auth failure and abort a whole run (see probes/inbox_transcribe.is_auth_failure).
    """

    def __init__(self, *args, **kwargs):
        self.status = kwargs.pop("status", None)
        super().__init__(*args, **kwargs)


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
    """Env file first, then real process environment overrides (handy for one-off runs).

    ``INBOX_*`` is in the overlay list alongside ``PROBE_*`` so the Inbox worker and the
    backlog sub-probe can be driven from the environment in a one-off run without editing
    the 0600 env file — same convenience the other probes already had."""
    cfg = load_env_file(env_path)
    for k, v in os.environ.items():
        if k in ("DASHBOARD_URL", "INGEST_TOKEN") or k.startswith(("PROBE_", "INBOX_")):
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


#: C0 controls, DEL, and the C1 range — everything that renders as garbage, moves a cursor,
#: or (for ESC) makes a terminal reading the log do as it is told.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def flatten_for_log(value: object, limit: int = NOTE_MAX) -> str:
    """One line, no control bytes, bounded — for anything a CHILD or a REMOTE produced.

    Whisper's stderr and an API's error body are untrusted text that goes straight into a
    timestamped log file. Left intact, an embedded newline forges a whole log line ("…
    2026-09-19 03:00:00 run ok in 2s") and an ESC sequence executes in whoever's terminal
    tails it. The server side flattens the same way (``dashboard/inbox_db.clean_text`` +
    newline flattening, commit 54209c3); this is that rule applied to the Mac's log.
    """
    text = "" if value is None else str(value)
    text = _CONTROL_CHARS_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return truncate(text, limit) or ""


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
#
# ⚠️ EVERY request below carries `Authorization: Bearer <token>` — INGEST_TOKEN to the box's
# Tailscale ingest port, INBOX_TOKEN to the public host. `urllib.request.urlopen` uses a
# module-global DEFAULT opener whose `HTTPRedirectHandler` follows up to TEN redirects, to
# ANY host and ANY scheme, and `redirect_request` copies every header except
# content-length/content-type onto the new request — the Authorization header included.
# /usr/bin/python3 here is 3.9, whose stdlib has no cross-origin Authorization stripping at
# all. So one 3xx from anything able to answer on these URLs (a hijacked DNS answer, a
# misconfigured edge, a sibling container) hands the bearer token to a third party.
#
# The same defect was found and fixed in `dashboard/github_mirror.py` (`_GitHubOnlyRedirects`
# + `_OPENER`); this is the same fix, and every request in this module goes through it.
# ---------------------------------------------------------------------------
class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect ONLY when scheme AND host:port are unchanged.

    Returning ``None`` is the documented mechanism: CPython then re-raises the original 3xx
    as an ``HTTPError``, which lands on the ordinary error path below — a 302 is reported as
    "rejected: HTTP 302" rather than chased.

    Neither endpoint this module talks to redirects in normal operation, so refusing outright
    would also have been defensible. Same-origin is chosen because it keeps a harmless local
    redirect (a trailing slash, an http→http path move on the SAME host) working while making
    the thing that actually matters impossible: the token cannot reach a different origin,
    and cannot be downgraded to http on the same one either (the scheme is compared too).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (new.scheme.lower(), new.netloc.lower()) != (old.scheme.lower(), old.netloc.lower()):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


#: Built once, used explicitly — deliberately NOT installed globally, so nothing else in the
#: process changes behaviour. ``build_opener`` swaps our subclass in for the default
#: redirect handler rather than adding a second one.
_OPENER = urllib.request.build_opener(_SameOriginRedirects)

#: ``http.client`` raises these for a malformed response (garbage on the wire, a too-long
#: status line, a chunked-encoding error). They are NOT ``OSError``, so before this they
#: escaped the retry loop entirely and surfaced as an unhandled exception in the caller —
#: which, for the Mac probe, means the whole run reports `fail` and the dashboard reads the
#: machine as offline. A malformed response is exactly the transient the retries exist for.
_RETRYABLE_TRANSPORT = (urllib.error.URLError, http.client.HTTPException, OSError)


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
            with _OPENER.open(req, timeout=timeout) as resp:
                # Bounded exactly like api_request's: the ingest port is on the tailnet and
                # answers with a few dozen bytes of JSON, but "it is ours" is not a reason to
                # let an unbounded read allocate without limit or hold the run for ever.
                return resp.status, _read_bounded(
                    resp, PING_RESPONSE_BYTES,
                    max(30.0, float(timeout) * READ_DEADLINE_FACTOR),
                ).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 429 or e.code >= 500:
                last_err = "HTTP %d %s" % (e.code, text[:120])
            else:
                raise ProbeError("ping %s rejected: HTTP %d %s" % (job_id, e.code, text[:200]),
                                 status=e.code)
        except _RETRYABLE_TRANSPORT as e:  # includes socket.timeout and a malformed response
            last_err = "%s" % (getattr(e, "reason", e) or e.__class__.__name__,)
        if attempt < retries:
            sleep(1.5 * (attempt + 1))
    raise ProbeError("ping %s failed after %d attempts: %s" % (job_id, retries + 1, last_err))


# ---------------------------------------------------------------------------
# Generic bearer-authenticated HTTP (the Inbox API on the PUBLIC host)
#
# ``send_ping`` above talks to the Tailscale-only ingest port with INGEST_TOKEN. The Inbox
# machine endpoints are a DIFFERENT credential on a DIFFERENT host: INBOX_TOKEN against
# https://<public host>. Two rules from that endpoint contract are structural here, not
# incidental:
#   * NO cookie jar. ``urllib.request.urlopen`` keeps no cookies unless an opener installs a
#     HTTPCookieProcessor, and none is installed anywhere in this package. That matters because
#     a session cookie OUTRANKS the bearer on the server — a request carrying both is refused.
#   * NO Origin/Referer. The server's CSRF pin exempts bearer-authenticated calls precisely
#     because a bearer is not an ambient credential; sending an Origin here would be a 403 on
#     prod (where APP_HOST is set) that no test would catch.
# Neither the token nor the Authorization header ever appears in a raised message.
# ---------------------------------------------------------------------------
API_USER_AGENT = "hopper-dashboard-probe/1"
#: Refuse to buffer a response larger than this. A runaway or wrong endpoint must not be able
#: to make the worker allocate unboundedly; audio is capped server-side well below it.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
#: Body read chunk. Small enough that the wall-clock deadline below is checked often.
_READ_CHUNK = 64 * 1024
#: ``timeout=`` on urlopen is PER SOCKET OPERATION, not wall clock: a server that dribbles a
#: byte every 9 s never trips a 10 s timeout and can hold the worker for ever — and launchd
#: will not start a second instance of the transcription agent while one is stuck. So the
#: body read also carries a TOTAL deadline, this multiple of the socket timeout (10 s → 300 s,
#: two orders of magnitude more than an 8 MB download needs on any real link).
READ_DEADLINE_FACTOR = 30


def api_request(
    url: str,
    token: str,
    method: str = "GET",
    body: Optional[Dict[str, object]] = None,
    timeout: float = HTTP_TIMEOUT_S,
    retries: int = HTTP_RETRIES,
    accept: str = "application/json",
    max_bytes: int = MAX_RESPONSE_BYTES,
    read_deadline_s: Optional[float] = None,
    sleep=time.sleep,
) -> Tuple[int, bytes]:
    """Bearer-authenticated request returning ``(status, raw_body)``.

    Retries the same cases ``send_ping`` does (network error, 5xx, 429) and raises
    ``ProbeError`` on a non-retryable 4xx or once the retries are spent. A 404/410 is
    *returned*, not raised, when ``method`` is GET — callers need to tell "this one item's
    audio is gone" apart from "the API is broken", and only they know which is which.
    """
    if not url.startswith(("http://", "https://")):
        raise ProbeError("refusing a non-http(s) URL")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": "Bearer " + token, "Accept": accept,
               "User-Agent": API_USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    last_err = "unknown"
    deadline_s = (float(read_deadline_s) if read_deadline_s is not None
                  else max(30.0, float(timeout) * READ_DEADLINE_FACTOR))
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return resp.status, _read_bounded(resp, max_bytes, deadline_s)
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read(2000).decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in (404, 410) and method == "GET":
                return e.code, text.encode("utf-8")
            if e.code == 429 or e.code >= 500:
                last_err = "HTTP %d %s" % (e.code, text[:120])
            else:
                raise ProbeError("%s %s rejected: HTTP %d %s" % (
                    method, _redact_url(url), e.code, text[:200]), status=e.code)
        except _RETRYABLE_TRANSPORT as e:
            last_err = "%s" % (getattr(e, "reason", e) or e.__class__.__name__,)
        if attempt < retries:
            sleep(1.5 * (attempt + 1))
    raise ProbeError("%s %s failed after %d attempts: %s" % (
        method, _redact_url(url), retries + 1, last_err))


def _read_bounded(resp, max_bytes: int, deadline_s: float) -> bytes:
    """Read a response body under BOTH a size cap and a wall-clock deadline.

    ``resp.read(n)`` in one call would honour the size cap but not the clock: the socket
    timeout only bounds how long a SINGLE recv may block, so a server trickling bytes just
    under it holds the reader indefinitely. Reading in chunks lets the total elapsed time be
    checked, and a breach raises ``ProbeError`` (not an ``OSError``), so it is reported rather
    than retried — a server that is slow on purpose would be slow on the retry too.

    ⚠️ ``read1``, NOT ``read``, and that is the whole point. ``HTTPResponse`` is a
    ``BufferedIOBase``: ``resp.read(65536)`` blocks until it has a FULL 65536 bytes (or EOF),
    so against the exact attack this deadline exists for — a server dripping one byte at a
    time — the loop never comes back around and the clock is never consulted. Measured: with
    ``read``, a 2 s deadline was still reading after 25 s at 1 byte/s, and in production
    (``timeout=10`` → a 300 s deadline) the FIRST check would land ~6.8 days in, with launchd
    refusing to start a second transcription agent the whole time. ``read1`` issues at most
    one underlying recv and returns what it got, so every trickled byte re-checks the clock.
    (``or resp.read`` keeps hand-rolled test doubles, which only implement ``read``, working.)
    """
    end = time.monotonic() + deadline_s
    chunks: List[bytes] = []
    total = 0
    read = getattr(resp, "read1", None) or resp.read
    while True:
        chunk = read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ProbeError("response larger than %d bytes" % max_bytes)
        chunks.append(chunk)
        if time.monotonic() > end:
            raise ProbeError("response body exceeded the %.0fs total read deadline "
                             "(%d bytes so far)" % (deadline_s, total))
    return b"".join(chunks)


def _redact_url(url: str) -> str:
    """Path only — never echo a query string or userinfo into a log line."""
    return re.sub(r"\?.*$", "", url)


def api_json(
    url: str,
    token: str,
    method: str = "GET",
    body: Optional[Dict[str, object]] = None,
    timeout: float = HTTP_TIMEOUT_S,
    retries: int = HTTP_RETRIES,
    sleep=time.sleep,
) -> Dict[str, object]:
    """``api_request`` + a JSON object, or ``ProbeError`` if the body is not one."""
    status, raw = api_request(url, token, method=method, body=body, timeout=timeout,
                              retries=retries, sleep=sleep)
    if status in (404, 410):
        raise ProbeError("%s %s: HTTP %d" % (method, _redact_url(url), status), status=status)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ProbeError("%s %s: response was not JSON (%s)" % (method, _redact_url(url), e))
    if not isinstance(doc, dict):
        raise ProbeError("%s %s: expected a JSON object" % (method, _redact_url(url)))
    return doc


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


#: Everything a child needs to find its binaries, its caches and its certificates — and
#: nothing else. Used to build a minimal environment for children that have no business
#: seeing this process's secrets (see ``minimal_env``).
ENV_PASSTHROUGH = (
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "XDG_CACHE_HOME", "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE",
)


def minimal_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """A child environment built from an ALLOWLIST of this process's env, plus ``extra``.

    ``load_config`` deliberately overlays ``INBOX_*`` (and ``INGEST_TOKEN``) from the real
    environment so a one-off run can be driven without editing the 0600 env file. That
    convenience means ``INBOX_TOKEN`` can be sitting in ``os.environ`` — and a child spawned
    with the default ``env=None`` inherits all of it. The Whisper child is an interpreter from
    ANOTHER repo's venv; it has no business being handed a credential for the Inbox API.
    """
    env = {k: os.environ[k] for k in ENV_PASSTHROUGH if k in os.environ}
    if extra:
        env.update(extra)
    return env


def run_cmd(argv: List[str], timeout: float, cwd: Optional[str] = None,
            env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    """Run a command with a hard timeout. Returns (rc, stdout, stderr); rc=-1 on timeout.

    ``env=None`` inherits this process's environment (right for rclone and docker, which need
    their own config); pass ``minimal_env()`` for a child that must not see our secrets.
    """
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
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
