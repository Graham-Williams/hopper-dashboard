"""Ping payload builder, env-file parsing, HTTP retry logic (mocked — no network)."""
import os
import sys
import time
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import common  # noqa: E402


# --- build_ping -----------------------------------------------------------------
def test_build_ping_minimal_contract_shape():
    body = common.build_ping("ok")
    assert body == {"status": "ok"}


def test_build_ping_full_contract_shape():
    body = common.build_ping(
        "fail", started_at="2026-09-04T03:00:00-04:00", finished_at="2026-09-04T03:00:47-04:00",
        reason="error", exit_code=1, note="boom", metrics={"lag_bytes": 5, "lag_files": 1, "x": "y"},
    )
    assert set(body) == {"status", "started_at", "finished_at", "reason", "exit_code", "note", "metrics"}
    assert body["exit_code"] == 1
    assert body["metrics"] == {"lag_bytes": 5, "lag_files": 1, "x": "y"}


@pytest.mark.parametrize("status", ["ok", "fail", "skipped", "metric"])
def test_build_ping_accepts_all_statuses(status):
    assert common.build_ping(status)["status"] == status


def test_build_ping_rejects_unknown_status():
    with pytest.raises(ValueError):
        common.build_ping("great")


def test_build_ping_truncates_note_to_500():
    body = common.build_ping("ok", note="x" * 900)
    assert len(body["note"]) == 500
    assert body["note"].endswith("…")


def test_build_ping_drops_none_and_empty():
    body = common.build_ping("ok", note=None, reason="", metrics={"a": None})
    assert body == {"status": "ok"}


def test_clean_metrics_types():
    m = common.clean_metrics({"n": 1, "f": 1.5, "b": True, "s": "abc", "bad key!": 2, "long": "z" * 2000, "none": None})
    assert m["n"] == 1 and m["f"] == 1.5 and m["b"] is True and m["s"] == "abc"
    assert "bad_key_" in m
    assert len(m["long"]) == common.METRIC_STR_MAX
    assert "none" not in m


def test_ping_url_and_job_id_validation():
    assert common.ping_url("http://h:8081/", "km-backup") == "http://h:8081/api/v1/ping/km-backup"
    for bad in ("KM", "a_b", "a b", "", "../x"):
        with pytest.raises(ValueError):
            common.validate_job_id(bad)


def test_slug():
    assert common.slug("world backups") == "world_backups"
    assert common.slug("Recordings!") == "recordings"


# --- env file -------------------------------------------------------------------
def test_parse_env_text():
    text = """
# comment
DASHBOARD_URL=http://h:8081
export INGEST_TOKEN="abc def"
QUOTED='single'
TRAILING=value  # trailing comment
BAD LINE
=novalue
1BAD=x
LAST=one
LAST=two
"""
    env = common.parse_env_text(text)
    assert env == {
        "DASHBOARD_URL": "http://h:8081",
        "INGEST_TOKEN": "abc def",
        "QUOTED": "single",
        "TRAILING": "value",
        "LAST": "two",
    }


def test_load_env_file_missing(tmp_path):
    assert common.load_env_file(str(tmp_path / "nope")) == {}


def test_load_config_has_no_default_url(tmp_path, monkeypatch):
    """The box's Tailscale IP is deployment-specific: no URL is baked into the code."""
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    monkeypatch.delenv("INGEST_TOKEN", raising=False)
    p = tmp_path / "env"
    p.write_text("INGEST_TOKEN=t\n")
    cfg = common.load_config(str(p))
    assert "DASHBOARD_URL" not in cfg
    assert cfg["INGEST_TOKEN"] == "t"
    with pytest.raises(common.ProbeError) as ei:
        common.require_dashboard_url(cfg, str(p))
    assert "DASHBOARD_URL is not set" in str(ei.value) and str(p) in str(ei.value)
    with pytest.raises(common.ProbeError):
        common.require_dashboard_url({"DASHBOARD_URL": "h:8081"})
    assert common.require_dashboard_url({"DASHBOARD_URL": " http://h:8081 "}) == "http://h:8081"


# --- state ----------------------------------------------------------------------
def test_state_roundtrip_and_corrupt(tmp_path):
    p = str(tmp_path / "state.json")
    assert common.load_state(p) == {}
    common.save_state(p, {"a": 1})
    assert common.load_state(p) == {"a": 1}
    with open(p, "w") as fh:
        fh.write("{not json")
    assert common.load_state(p) == {}


# --- time -----------------------------------------------------------------------
def test_local_naive_to_iso_has_offset():
    iso = common.local_naive_to_iso("2026-09-04 03:00:47")
    assert iso.startswith("2026-09-04T03:00:47")
    assert iso[-6] in "+-" and iso[-3] == ":"


# --- HTTP with retries (monkeypatched opener) -------------------------------------
# The fakes patch ``common._OPENER.open``, NOT ``urllib.request.urlopen``: every request in
# this module now goes through a private opener whose redirect handler refuses to cross an
# origin, precisely so a 3xx can never hand the bearer token to another host. Patching
# urlopen would test a code path that is no longer taken.
class _Resp:
    status = 200

    def __init__(self, text=b'{"ok": true, "state": "OK"}'):
        self._t = text
        self._done = False

    def read(self, _n=None):
        # One-shot: send_ping reads the whole body in one call, api_request reads in chunks
        # until it gets b"" — this satisfies both.
        if self._done:
            return b""
        self._done = True
        return self._t

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_ping_success_sets_headers(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["ct"] = req.get_header("Content-type")
        seen["data"] = req.data
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(common._OPENER, "open", fake_urlopen)
    code, text = common.send_ping("http://h:8081", "tok", "mac-probe", {"status": "ok"}, timeout=7)
    assert code == 200 and "OK" in text
    assert seen["url"] == "http://h:8081/api/v1/ping/mac-probe"
    assert seen["auth"] == "Bearer tok"
    assert seen["ct"] == "application/json"
    assert seen["data"] == b'{"status": "ok"}'
    assert seen["timeout"] == 7


def test_send_ping_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("connection refused")
        return _Resp()

    monkeypatch.setattr(common._OPENER, "open", fake_urlopen)
    code, _ = common.send_ping("http://h", "t", "mac-probe", {"status": "ok"}, retries=2, sleep=lambda s: None)
    assert code == 200 and calls["n"] == 3


def test_send_ping_gives_up_after_retries(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(common._OPENER, "open", fake_urlopen)
    with pytest.raises(common.ProbeError) as ei:
        common.send_ping("http://h", "secret-token", "mac-probe", {"status": "ok"}, retries=2, sleep=lambda s: None)
    assert "3 attempts" in str(ei.value)
    assert "secret-token" not in str(ei.value)


def test_send_ping_404_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

    monkeypatch.setattr(common._OPENER, "open", fake_urlopen)
    with pytest.raises(common.ProbeError) as ei:
        common.send_ping("http://h", "t", "typo-job", {"status": "ok"}, retries=2, sleep=lambda s: None)
    assert calls["n"] == 1 and "404" in str(ei.value)


def test_send_ping_5xx_is_retried(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)

    monkeypatch.setattr(common._OPENER, "open", fake_urlopen)
    with pytest.raises(common.ProbeError):
        common.send_ping("http://h", "t", "mac-probe", {"status": "ok"}, retries=2, sleep=lambda s: None)
    assert calls["n"] == 3


# --- run_cmd --------------------------------------------------------------------
def test_run_cmd_missing_binary():
    rc, out, err = common.run_cmd(["/nonexistent/binary"], timeout=5)
    assert rc == -2 and "not found" in err


def test_run_cmd_timeout():
    rc, out, err = common.run_cmd([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
    assert rc == -1 and "timeout" in err


def test_find_rclone_prefers_explicit(tmp_path):
    fake = tmp_path / "rclone"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    assert common.find_rclone(str(fake)) == str(fake)
    # a bad explicit path falls through to the absolute candidates / PATH — never raises
    found = common.find_rclone(str(tmp_path / "missing"))
    assert found is None or found.endswith("rclone")


def test_logger_writes_line(tmp_path):
    p = tmp_path / "logs" / "probe.log"
    lg = common.Logger(str(p))
    lg.log("hello")
    lg.error("bad")
    lines = p.read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(" hello") and lines[1].endswith(" ERROR: bad")
    assert lines[0][:10].count("-") == 2


# --- the redirect blocker: the bearer token must never leave its origin ------------
# These drive REAL loopback HTTP servers rather than a fake transport, for the same reason
# tests/test_inbox_mirror.py does for the GitHub mirror: the bug being pinned lives in
# urllib's transport (the default opener's redirect handler follows up to ten redirects, to
# any host and any scheme, and copies every header — Authorization included — onto the new
# request). A faked transport cannot catch a transport bug. Hermetic: 127.0.0.1, ephemeral
# port, daemon thread, torn down per test, sub-second.

class _Loopback:
    """A throwaway HTTP server on 127.0.0.1 with a scripted handler."""

    def __init__(self, handle):
        import http.server
        import threading
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):                      # noqa: N802 - stdlib API
                handle(self, outer)

            def do_POST(self):                     # noqa: N802 - stdlib API
                handle(self, outer)

            def log_message(self, *_args):         # keep the test output quiet
                pass

        self.headers_seen = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/" % self.port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def loopback():
    made = []

    def make(handle):
        srv = _Loopback(handle)
        made.append(srv)
        return srv

    yield make
    for srv in made:
        srv.close()


def _ok_recorder(request, server):
    server.headers_seen.append(dict(request.headers))
    request.send_response(200)
    request.send_header("Content-Length", "2")
    request.end_headers()
    request.wfile.write(b"{}")


def _redirect_to(target_url):
    def handler(request, server):
        server.headers_seen.append(dict(request.headers))
        request.send_response(302)
        request.send_header("Location", target_url)
        request.send_header("Content-Length", "0")
        request.end_headers()
    return handler


def test_api_request_never_carries_the_token_across_a_redirect(loopback):
    """THE blocker. INBOX_TOKEN authenticates writes to the Inbox on the PUBLIC host; one 3xx
    from anything able to answer that URL used to hand it to the redirect target verbatim."""
    target = loopback(_ok_recorder)
    source = loopback(_redirect_to(target.url))

    with pytest.raises(common.ProbeError) as ei:
        common.api_request(source.url, "SUPER-SECRET-INBOX-TOKEN",
                           retries=0, sleep=lambda s: None)

    assert "302" in str(ei.value)
    assert target.headers_seen == [], "the redirect target was contacted at all"
    assert "SUPER-SECRET-INBOX-TOKEN" not in str(ei.value)
    # ...and the first hop did get it, so the test is really exercising an authenticated call.
    assert source.headers_seen[0]["Authorization"] == "Bearer SUPER-SECRET-INBOX-TOKEN"


def test_send_ping_never_carries_the_token_across_a_redirect(loopback):
    """Identical defect, identical fix: INGEST_TOKEN is a write credential for every job's
    heartbeat, and ``send_ping`` used the same default opener."""
    target = loopback(_ok_recorder)
    source = loopback(_redirect_to(target.url))

    with pytest.raises(common.ProbeError) as ei:
        common.send_ping(source.url.rstrip("/"), "SUPER-SECRET-INGEST-TOKEN", "mac-probe",
                         {"status": "ok"}, retries=0, sleep=lambda s: None)

    assert "302" in str(ei.value)
    assert target.headers_seen == [], "the redirect target was contacted at all"
    assert "SUPER-SECRET-INGEST-TOKEN" not in str(ei.value)


def test_a_same_origin_redirect_is_still_followed(loopback):
    """Refusing every redirect would also have been defensible; what is NOT acceptable is
    crossing an origin. A same-host, same-scheme hop stays allowed."""
    state = {"n": 0}

    def handler(request, server):
        state["n"] += 1
        if state["n"] == 1:
            server.headers_seen.append(dict(request.headers))
            request.send_response(302)
            request.send_header("Location", "/moved")
            request.send_header("Content-Length", "0")
            request.end_headers()
            return
        _ok_recorder(request, server)

    srv = _Loopback(handler)
    try:
        status, raw = common.api_request(srv.url, "tok", retries=0, sleep=lambda s: None)
    finally:
        srv.close()
    assert status == 200 and raw == b"{}"
    # The token rode along — same origin, so that is the intended behaviour.
    assert srv.headers_seen[-1]["Authorization"] == "Bearer tok"


def test_a_redirect_that_only_changes_the_scheme_is_refused():
    """https → http on the SAME host is still a downgrade that puts the bearer on the wire in
    clear. Checked directly on the handler: the netloc matches and only the scheme differs."""
    req = urllib.request.Request("https://dashboard.example.com/api",
                                 headers={"Authorization": "Bearer t"})
    handler = common._SameOriginRedirects()
    assert handler.redirect_request(req, None, 302, "Found", {},
                                    "http://dashboard.example.com/api") is None


# --- a malformed response must not read as "the Mac is offline" -------------------
def test_a_malformed_http_response_is_retried_not_raised(monkeypatch):
    """``http.client.BadStatusLine``/``LineTooLong`` are ``HTTPException``, NOT ``OSError``,
    so they used to escape the retry loop entirely. In mac_probe that surfaced as the RUN
    failing, and a failing ``mac-probe`` is the machine-offline signal — which suppresses the
    LATE alerts on pa-backup and minecraft-offload. A captive portal must not mute the
    backups."""
    import http.client
    calls = {"n": 0}

    def fake_open(req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise http.client.BadStatusLine("\x16\x03\x01garbage")
        return _Resp()

    monkeypatch.setattr(common._OPENER, "open", fake_open)
    status, raw = common.api_request("http://h/api", "tok", retries=2, sleep=lambda s: None)
    assert status == 200 and calls["n"] == 3


def test_a_persistently_malformed_response_ends_as_a_probe_error(monkeypatch):
    import http.client

    def fake_open(req, timeout):
        raise http.client.LineTooLong("status line")

    monkeypatch.setattr(common._OPENER, "open", fake_open)
    with pytest.raises(common.ProbeError) as ei:
        common.api_request("http://h/api", "secret", retries=1, sleep=lambda s: None)
    assert "2 attempts" in str(ei.value) and "secret" not in str(ei.value)


def test_send_ping_also_survives_a_malformed_response(monkeypatch):
    import http.client

    def fake_open(req, timeout):
        raise http.client.BadStatusLine("nonsense")

    monkeypatch.setattr(common._OPENER, "open", fake_open)
    with pytest.raises(common.ProbeError):
        common.send_ping("http://h", "t", "mac-probe", {"status": "ok"},
                         retries=1, sleep=lambda s: None)


# --- body reads are bounded in BOTH bytes and wall clock --------------------------
class _SlowBody:
    """A body that never ends and never blocks long enough to trip the socket timeout."""

    status = 200

    def __init__(self, chunk=b"x" * 1024):
        self.chunk = chunk

    def read(self, _n=None):
        return self.chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_trickling_response_hits_the_total_deadline(monkeypatch):
    """``timeout=`` is per socket operation, not wall clock. Without a total deadline a server
    dribbling bytes holds the worker for ever — and launchd will not start a second instance
    of the transcription agent while one is stuck."""
    monkeypatch.setattr(common._OPENER, "open", lambda req, timeout: _SlowBody())
    with pytest.raises(common.ProbeError) as ei:
        common.api_request("http://h/audio", "tok", retries=0, max_bytes=10 ** 9,
                           read_deadline_s=0.01, sleep=lambda s: None)
    assert "deadline" in str(ei.value)


class _Trickle:
    """A body that behaves like a real ``HTTPResponse``: ``read(n)`` is BUFFERED and does not
    come back until it has n bytes, while ``read1(n)`` returns whatever one underlying read
    got. A server dripping one byte at a time is the whole attack."""

    status = 200

    def __init__(self, per_read=1, delay=0.01):
        self.per_read = per_read
        self.delay = delay
        self.read_calls = 0
        self.read1_calls = 0

    def read(self, n=None):
        self.read_calls += 1
        n = n or 1
        time.sleep(self.delay * (n / float(self.per_read)))   # fills the WHOLE buffer first
        return b"x" * n

    def read1(self, n=None):
        self.read1_calls += 1
        time.sleep(self.delay)
        return b"x" * self.per_read

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_read_deadline_fires_on_a_dripping_body_not_only_between_full_chunks(monkeypatch):
    """The bug this pins: ``_read_bounded`` checked the clock BETWEEN chunks, but
    ``resp.read(65536)`` on a ``BufferedReader`` blocks until it has a FULL 64 KiB. Against
    the exact attack the deadline exists for — a server sending one byte at a time — the loop
    never came back round and the clock was never consulted. Measured: a 2 s deadline was
    still reading after 25 s at 1 byte/s, and in production (``timeout=10`` → a 300 s
    deadline) the first check would land about 6.8 days in, with launchd refusing to start a
    second transcription agent for the whole of it. ``read1`` is the fix."""
    body = _Trickle(per_read=1, delay=0.01)
    monkeypatch.setattr(common._OPENER, "open", lambda req, timeout: body)
    started = time.monotonic()
    with pytest.raises(common.ProbeError) as ei:
        common.api_request("http://h/audio", "tok", retries=0, max_bytes=10 ** 9,
                           read_deadline_s=0.2, sleep=lambda s: None)
    elapsed = time.monotonic() - started
    assert "deadline" in str(ei.value)
    assert elapsed < 5, "the deadline did not fire while the body was still dripping"
    assert body.read1_calls > 0, "the buffered read() was used; a trickle can outlast it"
    assert body.read_calls == 0


def test_send_ping_bounds_its_response_the_way_api_request_does(monkeypatch):
    """``send_ping`` used to read its body unbounded. The ingest port is on the tailnet and
    answers with a line of JSON, but "it is ours" is not a reason to let a wrong host, a
    captive portal or a runaway hand the probe an unbounded allocation."""
    monkeypatch.setattr(common, "PING_RESPONSE_BYTES", 4096)
    monkeypatch.setattr(common._OPENER, "open", lambda req, timeout: _SlowBody())
    with pytest.raises(common.ProbeError) as ei:
        common.send_ping("http://h", "tok", "job", {}, retries=0, sleep=lambda s: None)
    assert "larger than 4096" in str(ei.value)


def test_a_body_object_without_read1_still_works(monkeypatch):
    """Not every response-like object implements ``read1``; falling back must not break."""
    monkeypatch.setattr(common._OPENER, "open", lambda req, timeout: _Resp(b"{}"))
    status, raw = common.api_request("http://h/x", "tok", retries=0, sleep=lambda s: None)
    assert (status, raw) == (200, b"{}")


def test_an_oversized_body_is_refused_before_it_is_buffered(monkeypatch):
    monkeypatch.setattr(common._OPENER, "open", lambda req, timeout: _SlowBody())
    with pytest.raises(common.ProbeError) as ei:
        common.api_request("http://h/audio", "tok", retries=0, max_bytes=4096,
                           sleep=lambda s: None)
    assert "larger than 4096" in str(ei.value)


# --- child environments and child output ------------------------------------------
def test_minimal_env_keeps_the_tokens_away_from_a_child(monkeypatch):
    monkeypatch.setenv("INBOX_TOKEN", "leak-me")
    monkeypatch.setenv("INGEST_TOKEN", "leak-me-too")
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin")
    env = common.minimal_env({"EXTRA": "1"})
    assert "INBOX_TOKEN" not in env and "INGEST_TOKEN" not in env
    assert env["PATH"] == "/opt/homebrew/bin:/usr/bin"   # ffmpeg must still be findable
    assert env["EXTRA"] == "1"


def test_run_cmd_passes_an_explicit_env_through():
    rc, out, _err = common.run_cmd(
        [sys.executable, "-c", "import os; print(os.environ.get('INBOX_TOKEN', 'absent'))"],
        timeout=10, env=common.minimal_env())
    assert rc == 0 and out.strip() == "absent"


def test_flatten_for_log_cannot_forge_a_log_line():
    forged = "oops\n2026-09-19 03:00:00 run ok in 2s (0 failed)"
    out = common.flatten_for_log(forged)
    assert "\n" not in out and "run ok" in out
    assert "\x1b" not in common.flatten_for_log("\x1b[31mred\x1b[0m")
    assert common.flatten_for_log(None) == ""
    assert len(common.flatten_for_log("x" * 5000, 100)) == 100
