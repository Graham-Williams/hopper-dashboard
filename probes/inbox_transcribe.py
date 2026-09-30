#!/usr/bin/python3
"""Inbox transcription worker — run every 5 minutes by launchd (com.hopper.inbox-transcribe).

Graham talks a bug or an idea into his phone; the browser stores the audio and whatever its
live speech API managed to hear. This worker is the BACKFILL that turns that audio into a real
transcript on the Mac, where an Apple-silicon Whisper runs ~20x realtime, and posts it back.

  GET  /api/v1/inbox/transcribe/queue   → items with audio and no good transcript, oldest first
  GET  /inbox/audio/<id>                → the raw bytes
  POST /api/v1/inbox/items/<id>/transcript  → {"text", "engine", "model", "duration_s"}
                                     ...or  {"failed": true, "error": …} to burn one attempt
  POST /api/v1/ping/inbox-transcribe    → this worker's own heartbeat (the INGEST port)

PHASE TWO — drafting (probes/inbox_draft.py), a no-op unless INBOX_CLAUDE_BIN is set. After
transcription it drafts up to INBOX_DRAFT_LIMIT (5) notes with `claude -p`:

  GET  /api/v1/inbox/draft/queue        → transcribed notes to draft + known_projects
  POST /api/v1/inbox/items/<id>/draft   → {"title","body","project","src_sha","model"}
                                   ...or  {"failed": true, "error", "src_sha"}

The TRANSCRIPT and TITLE go to Anthropic in that phase, plus the optional setup brief
(INBOX_DRAFT_CONTEXT_FILE, read fresh each run). The audio never goes to Anthropic or any speech
service: it lives on the box, on this Mac only while it is being transcribed (a temp file this
worker removes), and add-only in the Google Drive backup, kept there even after Delete or the
retention prune until removed by hand. A systemic drafting failure (claude missing, not logged in, limit, 5xx) fails the heartbeat
and burns no note's attempt; a circuit breaker and a two-run timeout rule sit on top — see
run_drafting and inbox_draft's docstring.

TWO CREDENTIALS, TWO HOSTS, and mixing them up is the most likely mistake here:
  * INBOX_URL  + INBOX_TOKEN   → the PUBLIC host (hub.graham-williams.com), Inbox API. NOT the
    legacy dashboard.* name: it 307s to the Hub, and the client refuses cross-host redirects.
  * DASHBOARD_URL + INGEST_TOKEN → the TAILSCALE-ONLY ingest port (:8081), heartbeat only.
The heartbeat deliberately does NOT ride the public host: "is the Mac running this loop?" must
stay answerable when Cloudflare or the public gate is the thing that is broken.

⚠️ WHISPER IS NOT IMPORTED HERE, EVER. ``probes/`` is stdlib-only and CI compiles + tests it
under /usr/bin/python3 (3.9), which has no mlx. mlx-whisper lives in ANOTHER repo's venv
(INBOX_WHISPER_PYTHON, default ~/code/jjho-fan-almanac/.venv/bin/python) and is reached by
subprocess with a fixed inline script. That cross-repo coupling is a known debt: a dedicated
venv (`python3 -m venv ~/.local/venvs/whisper && pip install mlx-whisper`) is the clean
long-term fix and needs no code change here — just point INBOX_WHISPER_PYTHON at it.

⚠️ THE ffmpeg/PATH TRAP. mlx_whisper's ``load_audio`` shells out to a BARE ``ffmpeg`` resolved
from PATH, and launchd's default PATH (/usr/bin:/bin:/usr/sbin:/sbin) does NOT include
/opt/homebrew/bin where ffmpeg lives. It fails INSIDE load_audio, not at import, so it
presents as "this audio file is corrupt" — and would burn all three of an item's attempts on
an environment problem. The plist injects PATH; this worker ALSO checks up front and aborts
the whole run with a diagnostic naming ffmpeg and PATH rather than failing any item.

Config: ~/.config/hopper-dashboard/env (KEY=VALUE, chmod 600) — the same file the hourly Mac
probe reads. Stdlib only, Python 3.9-clean.

  --dry-run    fetch the queue and print what it WOULD do; downloads nothing, transcribes
               nothing, posts nothing.
  --limit N    cap the items handled in one run (default INBOX_TRANSCRIBE_LIMIT).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import traceback
from typing import Dict, List, Optional, Tuple

# Allow `/usr/bin/python3 probes/inbox_transcribe.py` as well as `python -m probes.inbox_transcribe`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import inbox_draft  # noqa: E402
from probes.common import (  # noqa: E402
    DEFAULT_ENV_FILE,
    DEFAULT_STATE_FILE,
    HTTP_TIMEOUT_S,
    Logger,
    ProbeError,
    api_json,
    api_request,
    build_ping,
    flatten_for_log,
    load_config,
    minimal_env,
    now_iso,
    require_dashboard_url,
    run_cmd,
    send_ping,
    truncate,
)

#: Item ids are uuid4 hex and go straight into a URL path. Validated before interpolation.
ITEM_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: The server's own ceiling (``dashboard.inbox_db.MAX_TEXT``). Truncating to it here is about
#: the TRANSPORT, not that ceiling: ``clean_text`` truncates rather than rejects, so an
#: over-long transcript would be silently clipped server-side anyway. The real wall is the
#: app-wide ``MAX_CONTENT_LENGTH`` of 64 KB (``dashboard.Settings.max_body_bytes``), which
#: rejects the whole request with a 413 — and a rejection on the success path wedges the
#: queue at this item for ever, because it is oldest-first. 20 000 characters of ASCII is
#: 20 KB, but 20 000 characters of non-Latin script is ~60 KB of UTF-8 and lands close to
#: that wall, so keep this comfortably under it rather than raising it to match the server.
MAX_TEXT = 20000

#: A 401/403 is never one item's problem: the token is wrong, revoked, or pointed at the
#: wrong host, and every remaining item would fail the same way — burning all their attempts
#: and marking Graham's voice notes permanently failed. Read off ``ProbeError.status``, NOT
#: off the message: the message embeds up to 200 bytes of the server's own response body, so
#: a 500 whose body merely CONTAINS "HTTP 401" would abort the run and re-create the wedge
#: this abort exists to prevent.
AUTH_FAILURE_STATUSES = frozenset((401, 403))

#: mime → suffix for the temp file. ffmpeg sniffs the content, but a correct extension keeps
#: the failure mode honest and makes a stray temp file identifiable.
AUDIO_SUFFIXES = {
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
}
DEFAULT_SUFFIX = ".audio"

ENGINE = "whisper"
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_WHISPER_PYTHON = os.path.expanduser("~/code/jjho-fan-almanac/.venv/bin/python")

#: One 30 s clip transcribes in ~3 s on this Mac, and the server caps uploads at 8 MB, so this
#: is ~2 orders of magnitude of headroom. It exists to stop a wedged child holding the launchd
#: slot, not to bound normal work.
DEFAULT_TIMEOUT_S = 600
DEFAULT_LIMIT = 10
DEFAULT_LOG = os.path.expanduser("~/Library/Logs/hopper-inbox-transcribe.log")

#: Whisper's own output goes to stdout/stderr in shapes we do not control, so the result is
#: framed by a sentinel and parsed from the LAST occurrence.
SENTINEL = "__HOPPER_TRANSCRIPT__"

#: Run by INBOX_WHISPER_PYTHON, never by this interpreter. argv carries only a
#: server-generated temp path and the configured model id — no transcript, no title, no
#: user text is ever interpolated into a command line or a shell (there is no shell).
WHISPER_SCRIPT = r"""
import json
import sys

path, model, sentinel = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    import mlx_whisper
except Exception as exc:                      # mlx missing from THIS interpreter
    sys.stderr.write("mlx-whisper unavailable: %s: %s\n" % (type(exc).__name__, exc))
    raise SystemExit(3)
try:
    result = mlx_whisper.transcribe(path, path_or_hf_repo=model, verbose=False)
except Exception as exc:                      # decode failure, bad audio, OOM
    sys.stderr.write("transcribe failed: %s: %s\n" % (type(exc).__name__, exc))
    raise SystemExit(4)
payload = {"text": result.get("text") or "", "language": result.get("language")}
sys.stdout.write("\n" + sentinel + json.dumps(payload) + "\n")
"""

#: Exit codes the inline script above uses. 3 is the machine's problem (no mlx in that
#: interpreter); 4 is this recording's — unless the stderr names ffmpeg, which puts it
#: back in the machine's column. See FFMPEG_MARKERS.
RC_NO_MLX = 3
RC_TRANSCRIBE_FAILED = 4

#: Substrings that mean "ffmpeg could not be run", which is an ENVIRONMENT fault, never the
#: item's. Seen in the wild as a CalledProcessError from subprocess.run(["ffmpeg", ...]).
FFMPEG_MARKERS = ("ffmpeg", "No such file or directory", "Failed to load audio")


class EnvironmentFault(Exception):
    """The Mac cannot transcribe ANYTHING right now (no interpreter, no mlx, no ffmpeg).

    Distinct from an item failing, and the distinction is the point: reporting this as an
    item failure would increment ``transcribe_attempts`` on every queued row and mark all of
    them ``failed`` after three runs — throwing away Graham's voice notes' transcripts
    because a PATH was wrong. An environment fault aborts the run and fails the HEARTBEAT.
    """


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def load_settings(env_path: str) -> Dict[str, str]:
    cfg = load_config(env_path)
    defaults = {
        "INBOX_URL": "",
        "INBOX_TOKEN": "",
        "INBOX_WHISPER_PYTHON": DEFAULT_WHISPER_PYTHON,
        "INBOX_WHISPER_MODEL": DEFAULT_MODEL,
        "INBOX_TRANSCRIBE_LIMIT": str(DEFAULT_LIMIT),
        "INBOX_TRANSCRIBE_TIMEOUT": str(DEFAULT_TIMEOUT_S),
        "INBOX_TRANSCRIBE_LOG": DEFAULT_LOG,
        "INBOX_HTTP_TIMEOUT": str(HTTP_TIMEOUT_S),
        # Drafting (phase two). Empty INBOX_CLAUDE_BIN = drafting is off: a no-op.
        "INBOX_CLAUDE_BIN": "",
        "INBOX_CLAUDE_TOKEN_FILE": "",
        # Optional plain-text brief of Graham's setup, sent with each note as reference data.
        # Empty = none. Missing/unreadable/writable-by-others = none + one log warning.
        "INBOX_DRAFT_CONTEXT_FILE": "",
        "INBOX_DRAFT_MODEL": inbox_draft.DEFAULT_MODEL,
        "INBOX_DRAFT_LIMIT": str(inbox_draft.DEFAULT_LIMIT),
        "INBOX_DRAFT_TIMEOUT": str(inbox_draft.DEFAULT_TIMEOUT_S),
        # Which note timed out LAST run (the two-consecutive-timeouts rule). Next to the
        # hourly probe's state.json.
        "INBOX_DRAFT_STATE": os.path.join(os.path.dirname(DEFAULT_STATE_FILE),
                                          "draft-state.json"),
    }
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    return cfg


def _int(cfg: Dict[str, str], key: str, fallback: int) -> int:
    try:
        return int(str(cfg.get(key, "")).strip())
    except (TypeError, ValueError):
        return fallback


def inbox_base(cfg: Dict[str, str]) -> str:
    url = (cfg.get("INBOX_URL") or "").strip().rstrip("/")
    if not url:
        raise ProbeError("INBOX_URL is not set — e.g. INBOX_URL=https://hub.example.com")
    if not url.startswith(("http://", "https://")):
        raise ProbeError("INBOX_URL must start with http:// or https:// (got %r)" % url)
    return url


# ---------------------------------------------------------------------------
# Environment preflight
# ---------------------------------------------------------------------------
def which(name: str, path: Optional[str] = None) -> Optional[str]:
    """``shutil.which`` restricted to PATH — deliberately NOT falling back to the absolute
    Homebrew location the way ``find_rclone`` does. mlx_whisper resolves a BARE ``ffmpeg``
    from PATH itself, so "the binary exists at /opt/homebrew/bin" is NOT the question; the
    question is whether the CHILD will find it, and only PATH answers that."""
    search = path if path is not None else os.environ.get("PATH", "")
    for d in search.split(os.pathsep):
        if not d:
            continue
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def preflight(cfg: Dict[str, str]) -> None:
    """Raise ``EnvironmentFault`` (never an item failure) if this Mac cannot transcribe."""
    python = cfg["INBOX_WHISPER_PYTHON"]
    if not (os.path.isfile(python) and os.access(python, os.X_OK)):
        raise EnvironmentFault(
            "INBOX_WHISPER_PYTHON %r is not an executable interpreter — mlx-whisper lives in a "
            "venv, not in /usr/bin/python3. Point it at one that has mlx-whisper installed." % python)
    if which("ffmpeg") is None:
        raise EnvironmentFault(
            "ffmpeg is not on PATH (PATH=%r). mlx-whisper shells out to a BARE `ffmpeg` from "
            "PATH inside load_audio, so it is not enough for the binary to exist at "
            "/opt/homebrew/bin — launchd's default PATH omits that directory. Add "
            "PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin to the agent's "
            "EnvironmentVariables (deploy/mac/com.hopper.inbox-transcribe.plist does this)."
            % os.environ.get("PATH", ""))


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------
def fetch_queue(cfg: Dict[str, str], limit: int) -> List[Dict[str, object]]:
    base = inbox_base(cfg)
    url = "%s/api/v1/inbox/transcribe/queue?limit=%d" % (base, max(1, int(limit)))
    doc = api_json(url, cfg["INBOX_TOKEN"], timeout=float(cfg["INBOX_HTTP_TIMEOUT"]))
    items = doc.get("items")
    if not isinstance(items, list):
        raise ProbeError("transcribe queue: 'items' was not a list")
    out: List[Dict[str, object]] = []
    for raw in items:
        if isinstance(raw, dict) and valid_item_id(raw.get("id")):
            out.append(raw)
    return out


def valid_item_id(value: object) -> bool:
    return isinstance(value, str) and bool(ITEM_ID_RE.match(value))


def download_audio(cfg: Dict[str, str], item_id: str) -> Tuple[int, bytes]:
    """``(status, bytes)``. 404/410 come back as a status, not an exception: the audio being
    gone is this ITEM's problem (it should stop being queued), not the API's."""
    if not valid_item_id(item_id):
        raise ProbeError("refusing to fetch a malformed item id")
    url = "%s/inbox/audio/%s" % (inbox_base(cfg), item_id)
    return api_request(url, cfg["INBOX_TOKEN"], accept="*/*",
                       timeout=float(cfg["INBOX_HTTP_TIMEOUT"]))


def post_transcript(cfg: Dict[str, str], item_id: str, body: Dict[str, object]) -> None:
    if not valid_item_id(item_id):
        raise ProbeError("refusing to post to a malformed item id")
    url = "%s/api/v1/inbox/items/%s/transcript" % (inbox_base(cfg), item_id)
    api_json(url, cfg["INBOX_TOKEN"], method="POST", body=body,
             timeout=float(cfg["INBOX_HTTP_TIMEOUT"]))


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------
def parse_result(stdout: str) -> Dict[str, object]:
    idx = stdout.rfind(SENTINEL)
    if idx < 0:
        raise ProbeError("whisper produced no result line")
    tail = stdout[idx + len(SENTINEL):].strip()
    try:
        doc = json.loads(tail.splitlines()[0] if tail else "")
    except (ValueError, IndexError) as e:
        raise ProbeError("whisper result was not JSON (%s)" % e)
    if not isinstance(doc, dict):
        raise ProbeError("whisper result was not an object")
    return doc


def transcribe_file(cfg: Dict[str, str], path: str) -> Tuple[Dict[str, object], float]:
    """Run whisper over ``path``. Raises ``EnvironmentFault`` for a machine-wide problem and
    ``ProbeError`` for this file's problem."""
    argv = [cfg["INBOX_WHISPER_PYTHON"], "-c", WHISPER_SCRIPT,
            path, cfg["INBOX_WHISPER_MODEL"], SENTINEL]
    t0 = time.time()
    # An ALLOWLISTED environment, not this process's. `load_config` overlays INBOX_* from the
    # real environment for one-off runs, so INBOX_TOKEN can be in os.environ — and this child
    # is an interpreter from another repo's venv. It needs PATH (ffmpeg), HOME (the
    # HuggingFace model cache) and TMPDIR; it needs no credential of ours.
    rc, out, err = run_cmd(argv, timeout=float(_int(cfg, "INBOX_TRANSCRIBE_TIMEOUT",
                                                    DEFAULT_TIMEOUT_S)),
                           env=minimal_env())
    took = round(time.time() - t0, 2)
    # Flattened, not raw: whisper's stderr is untrusted child output going into a timestamped
    # log file, where an embedded newline forges a log line and an ESC sequence runs in
    # whoever's terminal is tailing it. Same rule as the server side (commit 54209c3).
    tail = flatten_for_log(err, 400)
    if rc == -2:
        raise EnvironmentFault("could not execute INBOX_WHISPER_PYTHON %r: %s"
                               % (cfg["INBOX_WHISPER_PYTHON"], tail))
    if rc == RC_NO_MLX:
        raise EnvironmentFault("mlx-whisper is not importable by %r: %s"
                               % (cfg["INBOX_WHISPER_PYTHON"], tail))
    if rc != 0 and any(m in tail for m in FFMPEG_MARKERS):
        # The trap, caught by name. Without this it reads as a corrupt recording and burns
        # an attempt per run until the item is marked permanently failed.
        raise EnvironmentFault(
            "whisper could not decode the audio and the error mentions ffmpeg (%s). "
            "mlx-whisper runs a BARE `ffmpeg` from PATH; launchd's PATH omits "
            "/opt/homebrew/bin. Check the agent's PATH before blaming the recording." % tail)
    if rc == -1:
        raise ProbeError("whisper timed out after %ss" % cfg["INBOX_TRANSCRIBE_TIMEOUT"])
    if rc != 0:
        raise ProbeError("whisper exited %d: %s" % (rc, tail))
    return parse_result(out), took


def suffix_for(mime: object) -> str:
    return AUDIO_SUFFIXES.get(str(mime or "").split(";")[0].strip().lower(), DEFAULT_SUFFIX)


def is_auth_failure(err: object) -> bool:
    """True for the one class of API error that is the RUN's problem, not the item's.

    Structural, not textual: only a ProbeError that CARRIES a 401/403 status counts. An error
    whose message happens to quote one does not.
    """
    return getattr(err, "status", None) in AUTH_FAILURE_STATUSES


def report_failure(cfg: Dict[str, str], item_id: str, error: object, log: Logger) -> str:
    """Record this item's failure server-side, burning one of its three attempts.

    Always returns 'failed'. If even the failure report cannot be delivered, that is logged
    and swallowed: the attempt counter simply does not advance this run, and the NEXT item
    still gets its turn. An auth failure is re-raised — the whole run is doomed, and burning
    every queued item's attempts on a revoked token would destroy transcripts.
    """
    detail = flatten_for_log(error, 300)
    log.error("%s: %s" % (item_id, detail))
    try:
        post_transcript(cfg, item_id, {"failed": True, "error": detail})
    except ProbeError as e:
        if is_auth_failure(e):
            raise
        log.error("%s: the failure report could not be delivered either: %s"
                  % (item_id, flatten_for_log(e, 200)))
    return "failed"


def handle_item(cfg: Dict[str, str], item: Dict[str, object], log: Logger,
                dry_run: bool) -> str:
    """Transcribe one item. Returns 'done' | 'failed' | 'skipped'.

    ⚠️ THE CONTAINMENT RULE, and it is the whole shape of this function: **only an
    ``EnvironmentFault`` or an auth failure may escape.** Everything else — the download, the
    transcription AND the success-path POST — is caught here, reported as this ITEM's failure,
    and the run moves on.

    Why it matters: the queue is oldest-first, so one item that reliably 4xxs (a 413 on an
    over-long transcript, a 400, a persistent 429, an audio body over the read ceiling) used
    to abort the whole run at the same item every five minutes — never burning an attempt,
    never being marked failed, and silently starving EVERY newer voice note behind it. The
    board would not notice for 24 h. An item that cannot be transcribed must cost one attempt,
    not the queue.

    The temp file is removed on every path, including an EnvironmentFault.
    """
    item_id = str(item["id"])
    if dry_run:
        log.log("would transcribe %s (%s, %s bytes)"
                % (item_id, item.get("audio_mime"), item.get("audio_bytes")))
        return "skipped"

    path = None
    try:
        status, raw = download_audio(cfg, item_id)
        if status in (404, 410):
            log.log("%s: audio is gone (HTTP %d) — reporting a permanent failure"
                    % (item_id, status))
            return report_failure(cfg, item_id, "audio unavailable (HTTP %d)" % status, log)
        if not raw:
            return report_failure(cfg, item_id, "audio body was empty", log)

        fd, path = tempfile.mkstemp(prefix="hopper-inbox-",
                                    suffix=suffix_for(item.get("audio_mime")))
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        result, took = transcribe_file(cfg, path)
        text = result.get("text")
        text = text.strip() if isinstance(text, str) else ""
        if not text:
            log.log("%s: whisper returned nothing — reporting a failure" % item_id)
            return report_failure(cfg, item_id, "whisper returned an empty transcript", log)
        # Truncated to the server's own ceiling. Over it the POST is REJECTED, and on the
        # success path that rejection is exactly what used to wedge the queue.
        clipped = len(text) > MAX_TEXT
        body = {"text": truncate(text, MAX_TEXT), "engine": ENGINE,
                "model": cfg["INBOX_WHISPER_MODEL"], "duration_s": took}
        language = result.get("language")
        if isinstance(language, str) and language:
            body["language"] = language[:16]
        post_transcript(cfg, item_id, body)
        log.log("%s: transcribed %d bytes → %d chars in %ss%s"
                % (item_id, len(raw), len(text), took,
                   " (truncated to %d for the server's limit)" % MAX_TEXT if clipped else ""))
        return "done"
    except EnvironmentFault:
        raise                                  # the machine's problem: abort the run
    except ProbeError as e:
        if is_auth_failure(e):
            raise                              # the credential's problem: abort the run
        return report_failure(cfg, item_id, e, log)
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Phase two: drafting (probes/inbox_draft.py)
# ---------------------------------------------------------------------------
def fetch_draft_queue(cfg: Dict[str, str], limit: int) -> Tuple[List[Dict[str, object]],
                                                                List[str]]:
    base = inbox_base(cfg)
    url = "%s/api/v1/inbox/draft/queue?limit=%d" % (base, max(1, int(limit)))
    doc = api_json(url, cfg["INBOX_TOKEN"], timeout=float(cfg["INBOX_HTTP_TIMEOUT"]))
    items = doc.get("items")
    if not isinstance(items, list):
        raise ProbeError("draft queue: 'items' was not a list")
    known = doc.get("known_projects")
    known = [p for p in known if isinstance(p, str)] if isinstance(known, list) else []
    out = [raw for raw in items if isinstance(raw, dict) and valid_item_id(raw.get("id"))
           and isinstance(raw.get("sha"), str)]
    return out, known


def post_draft(cfg: Dict[str, str], item_id: str, body: Dict[str, object]) -> None:
    if not valid_item_id(item_id):
        raise ProbeError("refusing to post to a malformed item id")
    url = "%s/api/v1/inbox/items/%s/draft" % (inbox_base(cfg), item_id)
    api_json(url, cfg["INBOX_TOKEN"], method="POST", body=body,
             timeout=float(cfg["INBOX_HTTP_TIMEOUT"]))


#: Circuit breaker: a run that ENDS with no success and at least this many bad results burns
#: nothing — a fault that looks per-note but hits every note is really systemic. It never
#: stops the batch early (a good note behind two bad ones must still be drafted), and after
#: BREAKER_MAX_TRIPS tripped runs in a row the held notes go STRAIGHT to `failed` (``final``:
#: every remaining attempt burned), so genuinely bad notes show in Needs review after 3 runs
#: instead of blocking the head of the queue.
BREAKER_FAILURES = 2
BREAKER_MAX_TRIPS = 3


def _load_draft_state(path: str) -> Dict[str, object]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_draft_state(path: str, doc: Dict[str, object]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)
    except OSError:
        pass                      # losing it only delays the two-run timeout rule


def run_drafting(cfg: Dict[str, str], log: Logger, dry_run: bool,
                 runner=None) -> Tuple[Dict[str, int], Optional[str]]:
    """Draft up to INBOX_DRAFT_LIMIT transcribed notes. Returns ``(counts, fatal)``.

    A no-op unless INBOX_CLAUDE_BIN is set (and it must be an ABSOLUTE path). ``fatal`` is set
    for a SYSTEMIC failure or a first timeout (drafting stops at once, nothing is burned) and
    for a tripped circuit breaker; either way the caller fails the heartbeat.

    Bad results are HELD while the run has no success yet, and the batch is always finished.
    A success reports the held ones as those notes' failed attempts at once. At the end of the
    batch: no success and at least BREAKER_FAILURES bad results trips the breaker — nothing is
    counted, and the trip is recorded in the state file; on the BREAKER_MAX_TRIPS-th trip in a
    row the held notes are sent straight to ``failed`` (``final``: every remaining attempt
    burned, so they reach Needs review after 3 runs, not 9). Fewer bad results than that with
    no success are counted normally. A 409 (the transcript changed, or Graham edited or
    reviewed it meanwhile) is skipped.
    """
    counts = {"drafted": 0, "draft_failed": 0, "draft_skipped": 0}
    claude_bin = (cfg.get("INBOX_CLAUDE_BIN") or "").strip()
    if not claude_bin:
        return counts, None
    limit = _int(cfg, "INBOX_DRAFT_LIMIT", inbox_draft.DEFAULT_LIMIT)
    if limit <= 0:
        return counts, None
    model = (cfg.get("INBOX_DRAFT_MODEL") or inbox_draft.DEFAULT_MODEL).strip()
    state_path = cfg.get("INBOX_DRAFT_STATE") or ""
    state = _load_draft_state(state_path) if state_path else {}
    last_timeout = state.get("timed_out")
    try:
        trips = max(0, int(state.get("breaker_trips") or 0))
    except (TypeError, ValueError, OverflowError):   # a hand-edited or corrupt state file
        trips = 0
    this_timeout: Optional[str] = None
    held: List[Tuple[str, str, str]] = []          # (item_id, detail, sha) not yet reported
    successes = 0

    def report(item_id: str, detail: str, sha: str, final: bool = False) -> None:
        body = {"failed": True, "error": detail, "src_sha": sha}
        if final:
            body["final"] = True
        try:
            post_draft(cfg, item_id, body)
        except ProbeError as pe:
            if is_auth_failure(pe):
                raise
            log.error("%s: failure report not delivered: %s"
                      % (item_id, flatten_for_log(pe, 200)))

    def flush(final: bool = False) -> None:
        while held:
            report(*held.pop(0), final=final)

    fatal: Optional[str] = None
    try:
        if not os.path.isabs(claude_bin):
            raise inbox_draft.SystemicFailure(
                "INBOX_CLAUDE_BIN %r must be an absolute path" % claude_bin)
        items, known = fetch_draft_queue(cfg, limit)
        log.log("draft queue: %d item(s)%s" % (len(items), " [dry-run]" if dry_run else ""))
        if dry_run:
            for item in items[:limit]:
                log.log("would draft %s (%d chars)"
                        % (item["id"], len(str(item.get("transcript") or ""))))
            counts["draft_skipped"] = len(items[:limit])
            return counts, None
        if items:
            if not (os.path.isfile(claude_bin) and os.access(claude_bin, os.X_OK)):
                raise inbox_draft.SystemicFailure(
                    "INBOX_CLAUDE_BIN %r is not an executable file" % claude_bin)
            token = inbox_draft.read_token(cfg.get("INBOX_CLAUDE_TOKEN_FILE") or "")
            env = inbox_draft.child_env(minimal_env, token)
            timeout = float(_int(cfg, "INBOX_DRAFT_TIMEOUT", inbox_draft.DEFAULT_TIMEOUT_S))
            # Read fresh each run, so an edit takes effect on the next cycle. Never fatal, and
            # neither the brief nor any of it goes into the log or the heartbeat — only the
            # warning (path + reason) and a character count.
            context, warning = inbox_draft.read_context(
                cfg.get("INBOX_DRAFT_CONTEXT_FILE") or "",
                token_path=cfg.get("INBOX_CLAUDE_TOKEN_FILE") or "")
            if warning:
                log.log("warning: %s" % flatten_for_log(warning, 300))
            elif context:
                log.log("setup brief: %d chars" % len(context))
        for item in items[:limit]:
            item_id = str(item["id"])
            bad: Optional[str] = None
            try:
                draft = inbox_draft.draft_one(claude_bin, model, item, known, env,
                                              timeout=timeout, runner=runner,
                                              context=context)
            except inbox_draft.DraftTimeout:
                if last_timeout == item_id:
                    bad = "claude timed out on this note in two consecutive runs"
                else:
                    this_timeout = item_id
                    raise
            except inbox_draft.BadResult as e:
                bad = flatten_for_log(e, 200)
            if bad is not None:
                log.error("%s: draft unusable: %s" % (item_id, bad))
                counts["draft_failed"] += 1
                if successes:
                    report(item_id, bad, item["sha"])
                    continue
                held.append((item_id, bad, item["sha"]))
                continue
            body = dict(draft)
            body.update({"src_sha": item["sha"], "model": model})
            try:
                post_draft(cfg, item_id, body)
            except ProbeError as pe:
                if is_auth_failure(pe):
                    raise
                if getattr(pe, "status", None) == 409 or "HTTP 409" in str(pe):
                    log.log("%s: draft not needed any more (409) — skipped" % item_id)
                    counts["draft_skipped"] += 1
                    continue
                log.error("%s: draft not stored: %s" % (item_id, flatten_for_log(pe, 200)))
                counts["draft_failed"] += 1
                continue
            successes += 1
            counts["drafted"] += 1
            log.log("%s: drafted (%d-char title, %d-char body%s)"
                    % (item_id, len(draft["title"]), len(draft["body"]),
                       ", project %s" % draft["project"] if draft["project"] else ""))
            flush()                # a success proves drafting works: held failures are real
        # The batch is done. No success and >= BREAKER_FAILURES bad results trips the breaker.
        if not successes and len(held) >= BREAKER_FAILURES:
            trips += 1
            if trips >= BREAKER_MAX_TRIPS:
                log.error("circuit breaker tripped %d runs in a row — these notes are "
                          "genuinely bad: marking %d of them failed"
                          % (trips, len(held)))
                trips = 0
                flush(final=True)
            else:
                n = len(held)
                held.clear()
                raise inbox_draft.SystemicFailure(
                    "circuit breaker: %d notes failed and none succeeded this run "
                    "(trip %d of %d) — nothing counted against them"
                    % (n, trips, BREAKER_MAX_TRIPS))
        else:
            trips = 0
            flush()                # a success (or a lone failure) — the failures are real
    except inbox_draft.SystemicFailure as e:
        held.clear()               # a systemic run burns nothing
        fatal = "drafting: %s" % flatten_for_log(e, 300)
        log.error(fatal)
    except ProbeError as e:
        held.clear()
        fatal = "drafting: %s" % flatten_for_log(e, 300)
        log.error(fatal)
    if state_path and not dry_run:
        _save_draft_state(state_path, {"timed_out": this_timeout, "breaker_trips": trips})
    return counts, fatal


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
JOB_ID = "inbox-transcribe"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="list the queue and print what would happen; posts nothing")
    ap.add_argument("--env", default=os.environ.get("HOPPER_DASHBOARD_ENV", DEFAULT_ENV_FILE),
                    help="env file path")
    ap.add_argument("--limit", type=int, default=None, help="max items this run")
    ap.add_argument("--quiet", action="store_true", help="don't echo log lines to stdout")
    args = ap.parse_args(argv)

    cfg = load_settings(args.env)
    log = Logger(cfg["INBOX_TRANSCRIBE_LOG"], echo=not args.quiet)
    started = now_iso()
    t0 = time.time()
    limit = args.limit if args.limit is not None else _int(cfg, "INBOX_TRANSCRIBE_LIMIT",
                                                           DEFAULT_LIMIT)

    # Not configured is not broken: the Inbox may simply not be deployed on this Mac yet. Say
    # so and exit WITHOUT a heartbeat — a job nobody is running should go LATE on the board,
    # not report a healthy run it isn't doing.
    if not (cfg.get("INBOX_TOKEN") or "").strip():
        log.error("INBOX_TOKEN missing (env file %s) — nothing done" % args.env)
        return 2
    try:
        inbox_base(cfg)
    except ProbeError as e:
        log.error("%s — nothing done" % e)
        return 2

    counts = {"done": 0, "failed": 0, "skipped": 0}
    fatal: Optional[str] = None
    queued = 0
    try:
        preflight(cfg)
        items = fetch_queue(cfg, limit)
        queued = len(items)
        log.log("queue: %d item(s)%s" % (queued, " [dry-run]" if args.dry_run else ""))
        for item in items[:limit]:
            counts[handle_item(cfg, item, log, args.dry_run)] += 1
    except EnvironmentFault as e:
        fatal = str(e)
        log.error("environment: %s" % e)
    except ProbeError as e:
        fatal = str(e)
        log.error(str(e))
    except Exception as e:  # never leave the launchd slot without a log line
        fatal = "%s: %s" % (type(e).__name__, e)
        log.error("crashed: %s\n%s" % (e, traceback.format_exc()))

    # Phase two, independent of phase one: a Whisper environment fault (no ffmpeg) must not
    # stop notes that are ALREADY transcribed from being drafted.
    draft_counts = {"drafted": 0, "draft_failed": 0, "draft_skipped": 0}
    try:
        draft_counts, draft_fatal = run_drafting(cfg, log, args.dry_run)
    except Exception as e:  # noqa: BLE001 — same last line of defence as above
        draft_fatal = "drafting crashed: %s: %s" % (type(e).__name__, e)
        log.error("%s\n%s" % (draft_fatal, traceback.format_exc()))
    if draft_fatal:
        fatal = "%s; %s" % (fatal, draft_fatal) if fatal else draft_fatal
    if (cfg.get("INBOX_CLAUDE_BIN") or "").strip():
        log.log("drafted %d (%d failed, %d skipped)"
                % (draft_counts["drafted"], draft_counts["draft_failed"],
                   draft_counts["draft_skipped"]))

    metrics = {"queued": queued, "transcribed": counts["done"],
               "failed": counts["failed"], "skipped": counts["skipped"],
               "duration_s": round(time.time() - t0, 1)}
    if (cfg.get("INBOX_CLAUDE_BIN") or "").strip():
        metrics["drafted"] = draft_counts["drafted"]
        metrics["draft_failed"] = draft_counts["draft_failed"]
    heartbeat = build_ping(
        "fail" if fatal else "ok",
        started_at=started, finished_at=now_iso(),
        reason="error" if fatal else "pushed",
        note=fatal, metrics=metrics)

    if args.dry_run:
        print("DRY-RUN POST %s/api/v1/ping/%s\n  %s"
              % (cfg.get("DASHBOARD_URL", "<DASHBOARD_URL>"), JOB_ID,
                 json.dumps(heartbeat, sort_keys=True)))
        log.log("run %s in %.1fs (%d queued, %d done, %d failed) [dry-run]"
                % ("fail" if fatal else "ok", time.time() - t0, queued,
                   counts["done"], counts["failed"]))
        return 1 if fatal else 0

    # The heartbeat is the ONLY thing that goes to the Tailscale ingest port, with the OTHER
    # token. Its absence is itself a signal, so a missing ingest config is logged loudly.
    hb_err = None
    try:
        url = require_dashboard_url(cfg, args.env)
        if not (cfg.get("INGEST_TOKEN") or "").strip():
            raise ProbeError("INGEST_TOKEN missing (env file %s)" % args.env)
        code, text = send_ping(url, cfg["INGEST_TOKEN"], JOB_ID, heartbeat,
                               timeout=float(cfg.get("PROBE_HTTP_TIMEOUT", HTTP_TIMEOUT_S)))
        log.log("sent %s %s → HTTP %d %s" % (JOB_ID, heartbeat["status"], code,
                                             text.strip()[:80]))
    except ProbeError as e:
        hb_err = str(e)
        log.error("heartbeat not delivered: %s" % e)

    log.log("run %s in %.1fs (%d queued, %d done, %d failed)"
            % ("fail" if fatal else "ok", time.time() - t0, queued,
               counts["done"], counts["failed"]))
    return 1 if (fatal or hb_err) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # last line of defence: always leave a line in a log
        try:
            Logger(DEFAULT_LOG).error("inbox_transcribe crashed: %s: %s"
                                      % (type(exc).__name__, exc))
        finally:
            traceback.print_exc()
            sys.exit(1)
