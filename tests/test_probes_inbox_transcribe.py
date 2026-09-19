"""The Mac transcription worker with HTTP and the whisper subprocess both faked.

Runs under /usr/bin/python3 (3.9) with nothing but the stdlib + pytest, like every other
probe suite — the worker itself must never import mlx, and these tests must never need it.

The behaviour worth pinning here is the one that can silently destroy data: an ENVIRONMENT
fault (no ffmpeg on PATH, no mlx in the interpreter) must abort the run, NOT be reported as
each item's failure. Reporting it per item burns `transcribe_attempts` on every queued row
and marks them permanently `failed` after three runs — losing the transcript of a voice note
because a PATH was wrong.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import inbox_transcribe as it  # noqa: E402
from probes.common import ProbeError  # noqa: E402

ID_A = "a" * 32
ID_B = "b" * 32


def _env(tmp_path, **extra):
    env = tmp_path / "env"
    lines = ["DASHBOARD_URL=http://box:8081", "INGEST_TOKEN=ingest-tok",
             "INBOX_URL=https://dash.example.com", "INBOX_TOKEN=inbox-tok",
             "INBOX_TRANSCRIBE_LOG=%s" % (tmp_path / "worker.log"),
             "INBOX_WHISPER_PYTHON=%s" % (tmp_path / "py"),
             "INBOX_WHISPER_MODEL=mlx-community/whisper-large-v3-turbo"]
    lines += ["%s=%s" % kv for kv in extra.items()]
    env.write_text("\n".join(lines) + "\n")
    return str(env)


class FakeApi:
    """Records every call and serves canned responses keyed by URL suffix."""

    def __init__(self, queue_items, audio=b"RIFFfake", audio_status=200):
        self.queue_items = queue_items
        self.audio = audio
        self.audio_status = audio_status
        self.json_calls = []      # (method, url, body)
        self.raw_calls = []       # (method, url)

    def api_json(self, url, token, method="GET", body=None, **kw):
        assert token == "inbox-tok", "the Inbox API must use INBOX_TOKEN, not INGEST_TOKEN"
        self.json_calls.append((method, url, body))
        if "/transcribe/queue" in url:
            return {"generated_at": "now", "max_attempts": 3, "items": self.queue_items}
        return {"ok": True}

    def api_request(self, url, token, method="GET", **kw):
        assert token == "inbox-tok"
        self.raw_calls.append((method, url))
        return self.audio_status, self.audio

    def transcripts(self):
        return [(u.rsplit("/", 2)[-2], b) for m, u, b in self.json_calls
                if u.endswith("/transcript")]


def _wire(monkeypatch, api, transcribe=None, preflight_ok=True, pings=None):
    monkeypatch.setattr(it, "api_json", api.api_json)
    monkeypatch.setattr(it, "api_request", api.api_request)
    if preflight_ok:
        monkeypatch.setattr(it, "preflight", lambda cfg: None)
    monkeypatch.setattr(it, "transcribe_file",
                        transcribe or (lambda cfg, path: ({"text": " hello world ",
                                                           "language": "en"}, 1.25)))
    sent = pings if pings is not None else []
    monkeypatch.setattr(it, "send_ping",
                        lambda u, t, j, b, timeout=10: sent.append((u, t, j, b)) or (200, "{}"))
    return sent


def _queue(*ids):
    return [{"id": i, "source": "voice", "title": "t", "created_at": "now",
             "transcript_status": "pending", "transcribe_attempts": 0,
             "audio_bytes": 1234, "audio_mime": "audio/webm", "audio_secs": 9.5}
            for i in ids]


# --- the happy path ---------------------------------------------------------------
def test_a_queued_item_is_downloaded_transcribed_and_posted_back(tmp_path, monkeypatch):
    api = FakeApi(_queue(ID_A))
    sent = _wire(monkeypatch, api)
    rc = it.main(["--env", _env(tmp_path), "--quiet"])
    assert rc == 0
    assert api.raw_calls == [("GET", "https://dash.example.com/inbox/audio/" + ID_A)]
    (item_id, body), = api.transcripts()
    assert item_id == ID_A
    # Trimmed, and tagged with the engine + the CONFIGURED model (not a hardcoded one).
    assert body == {"text": "hello world", "engine": "whisper",
                    "model": "mlx-community/whisper-large-v3-turbo",
                    "duration_s": 1.25, "language": "en"}
    # The heartbeat goes to the OTHER host with the OTHER token.
    (url, token, job, hb), = sent
    assert (url, token, job) == ("http://box:8081", "ingest-tok", "inbox-transcribe")
    assert hb["status"] == "ok" and hb["metrics"]["transcribed"] == 1
    assert hb["metrics"]["failed"] == 0 and hb["metrics"]["queued"] == 1


def test_the_queue_url_carries_the_limit_and_the_limit_is_honoured(tmp_path, monkeypatch):
    api = FakeApi(_queue(ID_A, ID_B))
    _wire(monkeypatch, api)
    it.main(["--env", _env(tmp_path), "--quiet", "--limit", "1"])
    assert api.json_calls[0][1].endswith("/api/v1/inbox/transcribe/queue?limit=1")
    assert [c[1] for c in api.raw_calls] == [
        "https://dash.example.com/inbox/audio/" + ID_A]


# --- the data-loss guard ----------------------------------------------------------
def test_a_missing_ffmpeg_aborts_the_run_and_never_burns_an_items_attempts(tmp_path, monkeypatch):
    """The whole reason EnvironmentFault exists. Three runs of "report this item failed"
    would mark every queued voice note permanently un-transcribable."""
    api = FakeApi(_queue(ID_A, ID_B))
    sent = _wire(monkeypatch, api, preflight_ok=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    whisper_python = tmp_path / "py"
    whisper_python.write_text("#!/bin/sh\n")
    whisper_python.chmod(0o755)

    rc = it.main(["--env", _env(tmp_path), "--quiet"])
    assert rc == 1
    assert api.transcripts() == []                 # nothing reported against any item
    assert api.raw_calls == []                     # not even downloaded
    (_, _, _, hb), = sent
    assert hb["status"] == "fail"
    assert "ffmpeg" in hb["note"] and "PATH" in hb["note"]


def test_an_ffmpeg_error_mid_transcription_is_also_environmental(tmp_path, monkeypatch):
    """The trap does not fire at import or at preflight if PATH is right but ffmpeg is
    broken — it surfaces as a decode error deep inside load_audio, which reads exactly like
    a corrupt recording. Classify it by the marker in stderr, not by where it happened."""
    def boom(cfg, path):
        raise it.EnvironmentFault("whisper could not decode; stderr mentions ffmpeg")

    api = FakeApi(_queue(ID_A))
    sent = _wire(monkeypatch, api, transcribe=boom)
    rc = it.main(["--env", _env(tmp_path), "--quiet"])
    assert rc == 1 and api.transcripts() == []
    assert sent[-1][3]["status"] == "fail"


def test_transcribe_file_classifies_by_the_exit_code_and_the_stderr(tmp_path, monkeypatch):
    cfg = {"INBOX_WHISPER_PYTHON": "/bin/true", "INBOX_WHISPER_MODEL": "m",
           "INBOX_TRANSCRIBE_TIMEOUT": "5"}
    cases = {
        (-2, "", "not found: x"): it.EnvironmentFault,          # interpreter missing
        (3, "", "mlx-whisper unavailable"): it.EnvironmentFault,  # mlx missing
        (4, "", "Failed to load audio: ffmpeg"): it.EnvironmentFault,  # the PATH trap
        (-1, "", "timeout after 5s"): ProbeError,               # this file's problem
        (4, "", "RuntimeError: bad frame"): ProbeError,         # this file's problem
        (1, "", "something else"): ProbeError,
    }
    for (rc, out, err), want in cases.items():
        monkeypatch.setattr(it, "run_cmd", lambda a, timeout: (rc, out, err))
        with pytest.raises(want):
            it.transcribe_file(cfg, "/tmp/x.webm")


# --- per-item failures ------------------------------------------------------------
def test_a_bad_recording_is_reported_as_that_items_failure_and_the_run_carries_on(
        tmp_path, monkeypatch):
    calls = {"n": 0}

    def flaky(cfg, path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProbeError("whisper exited 4: RuntimeError")
        return {"text": "second one worked"}, 0.5

    api = FakeApi(_queue(ID_A, ID_B))
    sent = _wire(monkeypatch, api, transcribe=flaky)
    rc = it.main(["--env", _env(tmp_path), "--quiet"])
    assert rc == 0                                  # an item failing is not a run failing
    posted = dict(api.transcripts())
    assert posted[ID_A] == {"failed": True, "error": "whisper exited 4: RuntimeError"}
    assert posted[ID_B]["text"] == "second one worked"
    assert sent[-1][3]["metrics"] == {"queued": 2, "transcribed": 1, "failed": 1,
                                      "skipped": 0,
                                      "duration_s": sent[-1][3]["metrics"]["duration_s"]}


@pytest.mark.parametrize("status", [404, 410])
def test_pruned_audio_is_reported_as_a_failure_so_the_item_leaves_the_queue(
        tmp_path, monkeypatch, status):
    api = FakeApi(_queue(ID_A), audio=b"", audio_status=status)
    _wire(monkeypatch, api)
    assert it.main(["--env", _env(tmp_path), "--quiet"]) == 0
    (_, body), = api.transcripts()
    assert body["failed"] is True and str(status) in body["error"]


def test_an_empty_transcript_is_a_failure_not_an_empty_success(tmp_path, monkeypatch):
    api = FakeApi(_queue(ID_A))
    _wire(monkeypatch, api, transcribe=lambda cfg, path: ({"text": "   "}, 0.1))
    it.main(["--env", _env(tmp_path), "--quiet"])
    (_, body), = api.transcripts()
    assert body["failed"] is True


# --- hygiene ----------------------------------------------------------------------
def test_the_temp_file_is_removed_even_when_transcription_blows_up(tmp_path, monkeypatch):
    seen = {}

    def capture(cfg, path):
        seen["path"] = path
        assert os.path.exists(path) and open(path, "rb").read() == b"RIFFfake"
        raise ProbeError("nope")

    api = FakeApi(_queue(ID_A))
    _wire(monkeypatch, api, transcribe=capture)
    it.main(["--env", _env(tmp_path), "--quiet"])
    assert not os.path.exists(seen["path"])
    assert seen["path"].endswith(".webm")          # suffix follows audio_mime


def test_a_malformed_item_id_never_reaches_a_url(tmp_path, monkeypatch):
    """Ids are interpolated into three different paths. The queue is a trusted endpoint, but
    "trusted" is not a property this worker can verify, so it filters and then re-validates."""
    bad = _queue(ID_A)
    bad[0]["id"] = "../../etc/passwd"
    api = FakeApi(bad + _queue(ID_B))
    _wire(monkeypatch, api)
    it.main(["--env", _env(tmp_path), "--quiet"])
    assert [c[1] for c in api.raw_calls] == [
        "https://dash.example.com/inbox/audio/" + ID_B]
    for fn in (it.download_audio, lambda c, i: it.post_transcript(c, i, {})):
        with pytest.raises(ProbeError):
            fn({"INBOX_URL": "https://d", "INBOX_TOKEN": "t", "INBOX_HTTP_TIMEOUT": "5"},
               "../x")


def test_an_unconfigured_worker_exits_2_and_posts_no_heartbeat(tmp_path, monkeypatch):
    """A job nobody is running must go LATE on the board, not report a healthy run."""
    sent = []
    monkeypatch.setattr(it, "send_ping", lambda *a, **k: sent.append(a) or (200, "{}"))
    monkeypatch.delenv("INBOX_TOKEN", raising=False)
    monkeypatch.delenv("INBOX_URL", raising=False)
    env = tmp_path / "env"
    env.write_text("DASHBOARD_URL=http://box:8081\nINGEST_TOKEN=t\n"
                   "INBOX_TRANSCRIBE_LOG=%s\n" % (tmp_path / "w.log"))
    assert it.main(["--env", str(env), "--quiet"]) == 2
    assert sent == []


def test_dry_run_posts_nothing_anywhere(tmp_path, monkeypatch, capsys):
    api = FakeApi(_queue(ID_A))
    sent = _wire(monkeypatch, api)
    rc = it.main(["--dry-run", "--env", _env(tmp_path), "--quiet"])
    assert rc == 0 and sent == [] and api.raw_calls == [] and api.transcripts() == []
    assert "/api/v1/ping/inbox-transcribe" in capsys.readouterr().out


def test_the_result_is_read_from_the_last_sentinel_not_the_whole_stdout():
    """mlx/whisper print their own noise, and a transcript can itself contain the sentinel
    text — so parse from the LAST marker and take only that line."""
    noise = "Detected language: English\n"
    payload = json.dumps({"text": "the real one", "language": "en"})
    out = noise + "\n" + it.SENTINEL + payload + "\ntrailing\n"
    assert it.parse_result(out)["text"] == "the real one"
    decoy = json.dumps({"text": "decoy"})
    out2 = it.SENTINEL + decoy + "\n" + it.SENTINEL + payload + "\n"
    assert it.parse_result(out2)["text"] == "the real one"
    with pytest.raises(ProbeError):
        it.parse_result("nothing here")


def test_which_only_consults_path_because_that_is_what_the_child_does(tmp_path):
    """`find_rclone`'s absolute-path fallback is the wrong shape here: mlx-whisper resolves a
    BARE `ffmpeg`, so a binary sitting outside PATH does the child no good at all."""
    binary = tmp_path / "bin" / "ffmpeg"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert it.which("ffmpeg", path=str(binary.parent)) == str(binary)
    assert it.which("ffmpeg", path="/nonexistent") is None


def test_suffix_follows_the_mime_and_falls_back_safely():
    assert it.suffix_for("audio/webm") == ".webm"
    assert it.suffix_for("audio/mp4; codecs=mp4a.40.2") == ".m4a"   # what iOS Safari sends
    assert it.suffix_for(None) == it.DEFAULT_SUFFIX
    assert it.suffix_for("application/x-evil") == it.DEFAULT_SUFFIX


def test_the_whisper_argv_is_a_list_with_no_shell_and_no_user_text(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(it, "run_cmd",
                        lambda argv, timeout: captured.update(argv=argv, timeout=timeout)
                        or (0, "\n" + it.SENTINEL + '{"text": "x"}\n', ""))
    cfg = {"INBOX_WHISPER_PYTHON": "/usr/bin/python3", "INBOX_WHISPER_MODEL": "m",
           "INBOX_TRANSCRIBE_TIMEOUT": "42"}
    it.transcribe_file(cfg, "/tmp/clip.webm")
    assert captured["argv"] == ["/usr/bin/python3", "-c", it.WHISPER_SCRIPT,
                                "/tmp/clip.webm", "m", it.SENTINEL]
    assert captured["timeout"] == 42.0


def test_nothing_under_probes_imports_mlx():
    """CI runs `compileall probes/` and these suites under a stock interpreter that has no
    mlx, so a single mlx import anywhere in the package breaks the whole probe stack — not
    just this worker. Asserted with `ast` over every module, because a grep for the text
    would trip over WHISPER_SCRIPT, which is the CHILD interpreter's source, not ours."""
    import ast
    probes_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "probes")
    checked = 0
    for name in sorted(os.listdir(probes_dir)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(probes_dir, name), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=name)
        checked += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for mod in names:
                assert not mod.split(".")[0].startswith("mlx"), "%s imports %s" % (name, mod)
    assert checked >= 8
    assert "import mlx_whisper" in it.WHISPER_SCRIPT      # it lives in the CHILD's source
