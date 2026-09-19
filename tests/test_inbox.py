"""The Inbox blueprint: auth per endpoint, the per-request body cap, the CSRF
pin on PATCH, the limiters, and the fact that untrusted text never becomes
markup."""

from __future__ import annotations

import json

import pytest

from dashboard import inbox_db
from tests.conftest import PASSWORD, READ_TOKEN

INBOX_TOKEN = "test-inbox-token"
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 4096
M4A = b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 4096


@pytest.fixture
def settings(settings):
    settings.inbox_token = INBOX_TOKEN
    return settings


@pytest.fixture
def bot(read_app):
    """A client with NO session cookie — the shape the Mac worker's curl has.

    It matters that this is a second client: `authed` logs the shared `read`
    client in, and a request carrying a session is authenticated AS a session
    (web.auth_kind resolves the session first), so the machine endpoints would
    refuse it. Which is correct — they are not for humans — but it means a test
    of the machine token cannot reuse the browser's client.
    """
    return read_app.test_client()


def machine(token: str = INBOX_TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def reader() -> dict:
    return {"Authorization": f"Bearer {READ_TOKEN}"}


def post_note(client, text="a spoken bug report", **extra):
    data = {"text": text}
    data.update(extra)
    return client.post("/api/v1/inbox/items", data=data,
                       content_type="multipart/form-data",
                       headers={"Accept": "application/json"})


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def test_unauthenticated_api_gets_json_401_not_a_redirect(read):
    r = read.get("/api/v1/inbox/items")
    assert r.status_code == 401 and r.is_json
    r = read.post("/api/v1/inbox/items", data={"text": "x"})
    assert r.status_code == 401 and r.is_json
    # The HTML board still redirects a signed-out browser to the login page.
    r = read.get("/inbox")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_the_read_token_can_read_the_inbox_but_never_write_it(bot, authed):
    """READ_TOKEN is the credential Hopper's watch carries. It must not become
    a write credential just because the Inbox lives under /api/v1/."""
    created = post_note(authed).get_json()
    assert bot.get("/api/v1/inbox/items", headers=reader()).status_code == 200
    denied = bot.post("/api/v1/inbox/items", data={"text": "written by a token"},
                      content_type="multipart/form-data", headers=reader())
    assert denied.status_code == 401
    denied = bot.patch(f"/api/v1/inbox/items/{created['id']}",
                       json={"reviewed": True}, headers=reader())
    assert denied.status_code == 401


def test_the_inbox_token_is_scoped_to_the_machine_endpoints(bot, authed):
    """INBOX_TOKEN must not act as a second read token for the whole API."""
    item = post_note(authed).get_json()["id"]
    assert bot.get("/api/v1/inbox/transcribe/queue",
                    headers=machine()).status_code == 200
    assert bot.get("/api/v1/inbox/items", headers=machine()).status_code == 401
    assert bot.get("/api/v1/status", headers=machine()).status_code == 401
    assert bot.post("/api/v1/inbox/items", data={"text": "x"},
                     content_type="multipart/form-data",
                     headers=machine()).status_code == 401
    assert bot.patch(f"/api/v1/inbox/items/{item}", json={"reviewed": True},
                      headers=machine()).status_code == 401


#: Every endpoint INBOX_TOKEN can authenticate, as (endpoint, method, path,
#: body-kwargs). Kept in step with `inbox.MACHINE_ENDPOINTS` by
#: `test_the_machine_endpoint_list_is_covered_here`, so adding a machine route
#: without a fail-closed case fails the suite rather than shipping unguarded.
MACHINE_CALLS = [
    ("inbox.audio", "get", "/inbox/audio/{item}", {}),
    ("inbox.transcribe_queue", "get", "/api/v1/inbox/transcribe/queue", {}),
    ("inbox.post_transcript", "post", "/api/v1/inbox/items/{item}/transcript",
     {"json": {"text": "hello", "engine": "whisper"}}),
    ("inbox.post_issues", "post", "/api/v1/inbox/items/{item}/issues",
     {"json": {"repo": "a/b", "number": 1,
               "url": "https://github.com/a/b/issues/1"}}),
    ("inbox.mirror_backlog", "post", "/api/v1/inbox/mirror/backlog",
     {"json": {"complete": True, "items": [{"text": "a thing"}]}}),
]


def test_the_machine_endpoint_list_is_covered_here():
    from dashboard.inbox import MACHINE_ENDPOINTS
    assert {c[0] for c in MACHINE_CALLS} == set(MACHINE_ENDPOINTS)


@pytest.mark.parametrize("endpoint,method,path,body",
                         MACHINE_CALLS,
                         ids=[c[0] for c in MACHINE_CALLS])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer "},
                                     {"Authorization": "Bearer x"}],
                         ids=["none", "empty", "wrong"])
def test_an_empty_inbox_token_fails_closed(settings, registry, notifier,
                                           endpoint, method, path, body,
                                           headers):
    """Same rule INGEST_TOKEN follows: un-provisioned means every machine call
    is refused, never that every machine call is allowed.

    Over ALL FIVE machine endpoints, not just the queue. "Un-provisioned fails
    closed" is a property of the credential, so testing one route proved
    nothing about the other four — and one of those four accepts audio, another
    rewrites transcripts and a third can archive the entire backlog mirror.
    """
    from dashboard import create_app
    settings.inbox_token = ""
    app = create_app("read", settings, registry, notifier)
    # A row with audio, made through the browser side, so the audio route has
    # something real to refuse.
    browser = app.test_client()
    browser.post("/login", data={"password": PASSWORD})
    item = _voice_note(browser)["id"]

    bare = app.test_client()            # no session: the Mac worker's shape
    r = getattr(bare, method)(path.format(item=item), headers=headers,
                              **body)
    # 401 for the JSON API. /inbox/audio/<id> is not under /api/v1/, so the
    # password gate redirects it to the login page instead — different status,
    # same outcome, and the bytes are what actually matter.
    assert r.status_code in (401, 302), (endpoint, r.status_code)
    assert WEBM[:4] not in r.get_data()


def test_the_browser_side_works_with_no_inbox_token(settings, registry, notifier):
    """Why INBOX_TOKEN is not a `${VAR:?}` in compose: the board booting
    matters more than the Inbox booting."""
    from dashboard import create_app
    settings.inbox_token = ""
    client = create_app("read", settings, registry, notifier).test_client()
    client.post("/login", data={"password": PASSWORD})
    assert client.get("/inbox").status_code == 200


def test_a_wrong_machine_token_is_401(bot):
    assert bot.get("/api/v1/inbox/transcribe/queue",
                    headers=machine("nope")).status_code == 401


# --------------------------------------------------------------------------- #
# CSRF pin
# --------------------------------------------------------------------------- #

def _pinned_app(settings, registry, notifier):
    from dashboard import create_app
    settings.app_host = "dash.example.com"
    return create_app("read", settings, registry, notifier)


def _pinned_client(settings, registry, notifier, app=None):
    app = app or _pinned_app(settings, registry, notifier)
    client = app.test_client()
    client.post("/login", data={"password": PASSWORD},
                base_url="https://dash.example.com",
                headers={"Origin": "https://dash.example.com"})
    return client


def test_the_origin_pin_now_covers_patch(settings, registry, notifier):
    """It used to fire on POST alone, so a PATCH route would have arrived with
    no CSRF pin at all. Widened BEFORE the route existed, not after."""
    client = _pinned_client(settings, registry, notifier)
    base = "https://dash.example.com"
    created = client.post("/api/v1/inbox/items", data={"text": "hello"},
                          content_type="multipart/form-data", base_url=base,
                          headers={"Origin": base, "Accept": "application/json"})
    assert created.status_code == 201
    item = created.get_json()["id"]
    # No Origin, no Referer → refused.
    assert client.patch(f"/api/v1/inbox/items/{item}", json={"reviewed": True},
                        base_url=base).status_code == 403
    # A foreign Origin → refused.
    assert client.patch(f"/api/v1/inbox/items/{item}", json={"reviewed": True},
                        base_url=base,
                        headers={"Origin": "https://evil.example"}).status_code == 403
    # The app's own Origin → allowed.
    ok = client.patch(f"/api/v1/inbox/items/{item}", json={"reviewed": True},
                      base_url=base, headers={"Origin": base})
    assert ok.status_code == 200 and ok.get_json()["reviewed"] is True


def test_a_session_plus_a_machine_token_is_still_csrf_pinned(
        settings, registry, notifier):
    """THE test this whole exemption rests on.

    `_host_origin_pin` skips the Origin/Referer check when `auth_kind()` says
    "inbox", and that is sound for exactly one reason: a bearer token is not an
    ambient credential, so a cross-site form post cannot present one. But a
    SESSION cookie is ambient, and the browser attaches it to a cross-site
    request whether or not the page wanted it to.

    So the exemption is only safe while `auth_kind()` resolves the session
    FIRST — before the machine token — because a request carrying both is a
    browser request and must be pinned. Nothing in the code pins that ordering;
    swapping those two `if`s in web.auth_kind turns the exemption into a live
    CSRF bypass, and every other test in this suite would still pass. This is
    that pin.

    The scenario is not theoretical: INBOX_TOKEN is a value Graham could
    plausibly have in a browser extension, a bookmarklet or a devtools snippet
    while logged in to the board.
    """
    app = _pinned_app(settings, registry, notifier)
    client = _pinned_client(settings, registry, notifier, app)
    base = "https://dash.example.com"
    created = client.post("/api/v1/inbox/items", data={"text": "pin me"},
                          content_type="multipart/form-data", base_url=base,
                          headers={"Origin": base, "Accept": "application/json"})
    item = created.get_json()["id"]

    hostile = {"Origin": "https://evil.example", **machine()}
    # A browser route: the session is what authenticates it, so the pin applies.
    assert client.patch(f"/api/v1/inbox/items/{item}", json={"reviewed": True},
                        base_url=base, headers=hostile).status_code == 403
    assert client.delete(f"/api/v1/inbox/items/{item}", base_url=base,
                         headers=hostile).status_code == 403
    assert client.post("/api/v1/inbox/items", data={"text": "x"},
                       content_type="multipart/form-data", base_url=base,
                       headers=hostile).status_code == 403
    # A MACHINE route reached with a session cookie attached is still a browser
    # request, and is still pinned. The exemption may not be bought by adding a
    # valid token to a cross-site post.
    assert client.post(f"/api/v1/inbox/items/{item}/issues",
                       json={"repo": "a/b", "number": 4,
                             "url": "https://github.com/a/b/issues/4"},
                       base_url=base, headers=hostile).status_code == 403
    # ...and none of it landed.
    assert client.get(f"/api/v1/inbox/items", base_url=base,
                      headers={"Accept": "application/json"}
                      ).get_json()["items"][0]["reviewed"] is False


def test_the_two_browser_write_routes_are_not_machine_endpoints(settings):
    """A structural guard, not a behavioural one: if create/patch/delete ever
    appear in MACHINE_ENDPOINTS, `auth_kind` can answer "inbox" for them and
    the origin pin above skips itself — CSRF bypass by one line in a set
    literal, with every behavioural test still green."""
    from dashboard.inbox import MACHINE_ENDPOINTS
    assert "inbox.create_item" not in MACHINE_ENDPOINTS
    assert "inbox.patch_item" not in MACHINE_ENDPOINTS
    assert "inbox.delete_item" not in MACHINE_ENDPOINTS
    # The pin must also cover every method that can change something — it used
    # to fire on POST alone.
    from dashboard.web import MUTATING_METHODS
    assert set(MUTATING_METHODS) >= {"POST", "PUT", "PATCH", "DELETE"}


def test_a_machine_post_needs_no_origin(settings, registry, notifier):
    """A bearer token is not an ambient credential — a cross-site form cannot
    set an Authorization header — and the Mac worker's curl sends neither Origin
    nor Referer. Requiring one would make the machine endpoints unreachable."""
    app = _pinned_app(settings, registry, notifier)
    client = _pinned_client(settings, registry, notifier, app)
    base = "https://dash.example.com"
    created = client.post("/api/v1/inbox/items", data={"text": "spoken"},
                          content_type="multipart/form-data", base_url=base,
                          headers={"Origin": base, "Accept": "application/json"})
    item = created.get_json()["id"]
    worker = app.test_client()          # no session, exactly like the Mac's curl
    r = worker.post(f"/api/v1/inbox/items/{item}/issues",
                    json={"repo": "a/b", "number": 3,
                          "url": "https://github.com/a/b/issues/3"},
                    base_url=base, headers=machine())
    assert r.status_code == 201


# --------------------------------------------------------------------------- #
# Body size
# --------------------------------------------------------------------------- #

def test_audio_upload_is_capped_per_request_and_the_global_cap_is_untouched(
        settings, read_app, ingest, authed, read):
    """The create route lifts `request.max_content_length` for itself only. The
    64 KB global cap protects every other route on BOTH roles and is never
    raised — including /api/v1/ping, which is what a compromised heartbeat
    sender would aim at."""
    from dashboard import inbox as inbox_mod
    assert read_app.config["MAX_CONTENT_LENGTH"] == 64 * 1024
    # A 1 MB note sails through the create route...
    big = b"\x1a\x45\xdf\xa3" + b"\x00" * (1024 * 1024)
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "big one", "audio": (io_bytes(big), "x.webm",
                                                       "audio/webm")},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    assert r.status_code == 201, r.get_json()
    # ...and one over the cap is 413 from the route, not a truncated write.
    too_big = b"\x1a\x45\xdf\xa3" + b"\x00" * (settings.inbox_audio_max_bytes + 10)
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "huge", "audio": (io_bytes(too_big), "x.webm",
                                                    "audio/webm")},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    assert r.status_code == 413
    # The ingest ping is still 64 KB-capped.
    from tests.conftest import auth
    r = ingest.post("/api/v1/ping/snap", data=b"x" * (70 * 1024),
                    content_type="application/json", headers=auth())
    assert r.status_code == 413
    # ...and so is every other read-side route.
    r = read.post("/login", data={"password": "x" * (70 * 1024)})
    assert r.status_code == 413
    assert inbox_mod.MULTIPART_OVERHEAD == 64 * 1024


def io_bytes(data: bytes):
    import io
    return io.BytesIO(data)


def test_a_wrong_audio_type_is_415(authed):
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "not audio",
                          "audio": (io_bytes(b"%PDF-1.4" + b"\x00" * 300),
                                    "x.pdf", "application/pdf")},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    assert r.status_code == 415


# --------------------------------------------------------------------------- #
# Create / patch behaviour
# --------------------------------------------------------------------------- #

def test_typed_note_is_typed_and_never_queued_for_transcription(authed):
    item = post_note(authed, text="Remember to bump the rclone client id").get_json()
    assert item["source"] == "typed" and item["transcript_status"] == "typed"
    assert item["has_audio"] is False and item["awaiting_transcription"] is False
    assert item["title"] == "Remember to bump the rclone client id"


@pytest.mark.parametrize("data,mime", [(WEBM, "audio/webm;codecs=opus"),
                                       (M4A, "audio/mp4")])
def test_a_voice_note_is_pending_until_whisper_runs(authed, data, mime):
    """iOS Safari emits mp4 and Chrome/Android webm — both land as `pending`,
    and the Mac's Whisper worker fills them in."""
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "", "audio": (io_bytes(data), "note", mime)},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    item = r.get_json()
    assert r.status_code == 201
    assert item["source"] == "voice" and item["transcript_status"] == "pending"
    assert item["has_audio"] and item["awaiting_transcription"] is True


def test_typed_text_alongside_audio_is_still_pending_not_live(authed):
    """`live` used to mean "the browser speech API transcribed it", and the
    browser speech API streamed the microphone to Google/Apple. It is gone, so
    NOTHING creates a `live` row any more: text next to a recording is
    something Graham typed, and the row is still waiting on Whisper."""
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "the wheel spins twice", "audio_secs": "12.5",
                          "audio": (io_bytes(WEBM), "note", "audio/webm")},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    item = r.get_json()
    assert item["transcript_status"] == "pending" and item["audio_secs"] == 12.5
    assert item["awaiting_transcription"] is True


def test_nothing_in_the_page_reaches_a_third_party_speech_service(read_app):
    """The privacy guarantee, pinned in a test so a future edit has to argue
    with it. `webkitSpeechRecognition` streams the microphone to Google (or
    Apple) for recognition; Graham's ruling was "nothing leaves the box", so
    the only thing that may touch a recording is the upload to this origin.

    Comments are stripped first ON PURPOSE: the file explains at length WHY the
    API is gone, and that explanation is the thing most worth keeping.
    """
    import re
    with open(read_app.static_folder + "/inbox.js", encoding="utf-8") as fh:
        source = fh.read()
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    code = re.sub(r"(?m)//.*$", "", code)
    for banned in ("SpeechRecognition", "speechSynthesis", "getUserMedia({video",
                   "blob:"):
        assert banned not in code, f"{banned} is back in inbox.js"
    # ...and the recording path still exists, so this is not passing by virtue
    # of an empty file.
    assert "getUserMedia" in code and "MediaRecorder" in code


def test_an_empty_submission_is_refused(authed):
    r = post_note(authed, text="")
    assert r.status_code == 400 and "empty" in r.get_json()["error"]


def test_a_bad_project_is_refused(authed):
    # Note the shape that is NOT rejected: `.` and `/` are legal in a project
    # label (it is a label, never a path, and every query is parameterised).
    assert post_note(authed, project="a/b.c-d_e").status_code == 201
    for bad in ("a b", "drop table", "x" * 65, "kmtracker!"):
        r = post_note(authed, project=bad)
        assert r.status_code == 400 and "project" in r.get_json()["error"], bad


def test_patch_validates_and_rejects_unknown_fields(authed):
    item = post_note(authed).get_json()["id"]
    assert authed.patch(f"/api/v1/inbox/items/{item}",
                        json={"nope": 1}).status_code == 400
    assert authed.patch(f"/api/v1/inbox/items/{item}",
                        json={"reviewed": "yes"}).status_code == 400
    assert authed.patch(f"/api/v1/inbox/items/{item}",
                        json={"state": "deleted"}).status_code == 400
    assert authed.patch(f"/api/v1/inbox/items/{item}",
                        json={"title": "   "}).status_code == 400
    assert authed.patch("/api/v1/inbox/items/" + "0" * 32,
                        json={"reviewed": True}).status_code == 404
    ok = authed.patch(f"/api/v1/inbox/items/{item}",
                      json={"reviewed": True, "project": "km-tracker",
                            "state": "closed"})
    body = ok.get_json()
    assert body["reviewed"] and body["project"] == "km-tracker"
    assert body["state"] == "closed" and body["closed_at"]


def test_a_form_post_without_js_redirects_back_to_the_board(authed):
    """Capture has to work with JavaScript off; only the microphone needs it."""
    r = authed.post("/api/v1/inbox/items", data={"text": "typed with no JS"},
                    content_type="multipart/form-data",
                    headers={"Accept": "text/html,application/xhtml+xml"})
    assert r.status_code == 303 and r.headers["Location"].endswith("/inbox")


# --------------------------------------------------------------------------- #
# Rate limits
# --------------------------------------------------------------------------- #

def test_the_create_limiter_returns_429(authed, read_app):
    read_app.extensions["inbox_create_limiter"].max_events = 3
    for _ in range(3):
        assert post_note(authed).status_code == 201
    r = post_note(authed)
    assert r.status_code == 429 and "try again" in r.get_json()["error"]


def test_the_write_limiter_returns_429(authed, read_app):
    item = post_note(authed).get_json()["id"]
    read_app.extensions["inbox_write_limiter"].max_events = 2
    for _ in range(2):
        assert authed.patch(f"/api/v1/inbox/items/{item}",
                            json={"reviewed": True}).status_code == 200
    assert authed.patch(f"/api/v1/inbox/items/{item}",
                        json={"reviewed": False}).status_code == 429


# --------------------------------------------------------------------------- #
# Audio serving
# --------------------------------------------------------------------------- #

def test_audio_is_served_with_no_store_and_nosniff(authed):
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "", "audio": (io_bytes(WEBM), "n", "audio/webm")},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    item = r.get_json()["id"]
    got = authed.get(f"/inbox/audio/{item}")
    assert got.status_code == 200 and got.data == WEBM
    assert got.headers["Cache-Control"] == "private, no-store"
    assert got.headers["X-Content-Type-Options"] == "nosniff"
    assert got.headers["Content-Type"].startswith("audio/webm")
    assert got.headers["Content-Disposition"].startswith("inline")
    assert got.headers["Content-Disposition"].endswith(f'"{item}.webm"')
    # send_file sets its OWN Cache-Control (from max_age) and its own
    # Content-Disposition, so these headers only survive because they are
    # re-asserted AFTER the call. Nothing about a voice note should reach a
    # shared cache, and nosniff is what keeps an unknown container from being
    # re-interpreted by the browser.
    assert "public" not in got.headers["Cache-Control"]
    assert "max-age" not in got.headers["Cache-Control"]


def test_audio_is_streamed_with_range_support(authed):
    """`fh.read()` materialised the whole file per request and, more usefully,
    answered no Range requests — so scrubbing the <audio> element on a phone
    re-downloaded from the start on every seek. `conditional=True` gives both."""
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "", "audio": (io_bytes(WEBM), "n", "audio/webm")},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    item = r.get_json()["id"]
    full = authed.get(f"/inbox/audio/{item}")
    assert full.headers.get("Accept-Ranges") == "bytes"

    part = authed.get(f"/inbox/audio/{item}", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206
    assert part.data == WEBM[10:20]
    assert part.headers["Content-Range"] == f"bytes 10-19/{len(WEBM)}"
    # The security headers are on the partial response too — a range request is
    # the ordinary case for playback, not an edge case.
    assert part.headers["Cache-Control"] == "private, no-store"
    assert part.headers["X-Content-Type-Options"] == "nosniff"


def test_audio_is_404_then_410_after_a_prune(authed, settings, read_app):
    from dashboard import inbox_audio as audio_mod
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "", "audio": (io_bytes(WEBM), "n", "audio/webm")},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    item = r.get_json()["id"]
    assert authed.get("/inbox/audio/" + "0" * 32).status_code == 404
    typed = post_note(authed).get_json()["id"]
    assert authed.get(f"/inbox/audio/{typed}").status_code == 404
    # Make it prunable: whisper transcript + reviewed + old enough.
    conn = inbox_db.connect(settings.inbox_db_path)
    with conn:
        inbox_db.set_transcript(conn, item, text="the wheel spins twice")
        inbox_db.update_item(conn, item, {"reviewed": True})
        conn.execute("UPDATE inbox_items SET created_at='2020-01-01T00:00:00Z'"
                     " WHERE id=?", (item,))
    conn.close()
    assert audio_mod.prune_audio(settings)["pruned"] == 1
    gone = authed.get(f"/inbox/audio/{item}")
    assert gone.status_code == 410 and "pruned" in gone.get_json()["error"]


def test_the_machine_token_can_fetch_audio_but_not_the_board(bot, authed):
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "", "audio": (io_bytes(WEBM), "n", "audio/webm")},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    item = r.get_json()["id"]
    assert bot.get(f"/inbox/audio/{item}", headers=machine()).status_code == 200
    assert bot.get("/inbox", headers=machine()).status_code == 302


# --------------------------------------------------------------------------- #
# Machine endpoints
# --------------------------------------------------------------------------- #

def test_the_transcript_endpoint_fills_a_pending_row(bot, authed):
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "", "audio": (io_bytes(WEBM), "n", "audio/webm")},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    item = r.get_json()["id"]
    assert [i["id"] for i in bot.get("/api/v1/inbox/transcribe/queue",
                                      headers=machine()).get_json()["items"]] == [item]
    done = bot.post(f"/api/v1/inbox/items/{item}/transcript",
                     json={"text": "The km wheel spins twice on iOS.",
                           "engine": "whisper", "model": "large-v3-turbo",
                           "duration_s": 9.5}, headers=machine())
    body = done.get_json()
    assert done.status_code == 200 and body["transcript_status"] == "whisper"
    assert body["title"] == "The km wheel spins twice on iOS."
    assert body["audio_secs"] == 9.5
    assert bot.get("/api/v1/inbox/transcribe/queue",
                    headers=machine()).get_json()["items"] == []


def test_the_transcript_endpoint_records_a_failure_and_gives_up(bot, authed):
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "", "audio": (io_bytes(WEBM), "n", "audio/webm")},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    item = r.get_json()["id"]
    for _ in range(3):
        out = bot.post(f"/api/v1/inbox/items/{item}/transcript",
                        json={"failed": True, "error": "ffmpeg could not decode"},
                        headers=machine())
        assert out.status_code == 200
    assert out.get_json()["transcript_status"] == "failed"
    assert bot.get("/api/v1/inbox/transcribe/queue",
                    headers=machine()).get_json()["items"] == []
    # The audio is still there — a failed transcript is not a reason to lose it.
    assert authed.get(f"/inbox/audio/{item}").status_code == 200


def test_the_transcript_endpoint_validates(bot, authed):
    item = post_note(authed).get_json()["id"]
    assert bot.post(f"/api/v1/inbox/items/{item}/transcript", json={},
                     headers=machine()).status_code == 400
    assert bot.post(f"/api/v1/inbox/items/{item}/transcript",
                     json={"text": "x", "engine": "gpt"},
                     headers=machine()).status_code == 400
    assert bot.post("/api/v1/inbox/items/" + "0" * 32 + "/transcript",
                    json={"text": "x"}, headers=machine()).status_code == 404


def test_the_issues_endpoint_is_idempotent_and_validates_the_url(bot, authed):
    item = post_note(authed).get_json()["id"]
    good = {"repo": "Graham-Williams/km-tracker", "number": 95,
            "url": "https://github.com/Graham-Williams/km-tracker/issues/95",
            "title": "Wheel spins twice"}
    first = bot.post(f"/api/v1/inbox/items/{item}/issues", json=good,
                      headers=machine())
    assert first.status_code == 201
    again = bot.post(f"/api/v1/inbox/items/{item}/issues", json=good,
                      headers=machine())
    assert again.status_code == 201
    assert len(again.get_json()["item"]["issues"]) == 1
    # A javascript: href would survive Jinja's escaping and still navigate.
    for bad_url in ("javascript:alert(1)", "http://github.com/a/b/issues/1",
                    "https://evil.example/a/b", "https://github.com/a b"):
        r = bot.post(f"/api/v1/inbox/items/{item}/issues",
                      json={**good, "url": bad_url}, headers=machine())
        assert r.status_code == 400, bad_url
    for bad_repo in ("a", "a/b/c", "../x"):
        r = bot.post(f"/api/v1/inbox/items/{item}/issues",
                      json={**good, "repo": bad_repo}, headers=machine())
        assert r.status_code == 400, bad_repo


def test_filing_an_issue_clears_awaiting_filing(bot, authed):
    item = post_note(authed).get_json()["id"]
    authed.patch(f"/api/v1/inbox/items/{item}", json={"reviewed": True})
    page = authed.get("/api/v1/inbox/items?awaiting=filing").get_json()
    assert [i["id"] for i in page["items"]] == [item]
    bot.post(f"/api/v1/inbox/items/{item}/issues",
              json={"repo": "a/b", "number": 1,
                    "url": "https://github.com/a/b/issues/1"}, headers=machine())
    page = authed.get("/api/v1/inbox/items?awaiting=filing").get_json()
    assert page["items"] == []


def test_the_backlog_mirror_refuses_to_archive_on_an_empty_list(bot, authed):
    payload = {"complete": True, "items": [
        {"key": inbox_db.normalise_backlog_key("Build the thing"),
         "text": "Build the thing\nWhy: because"},
        {"key": inbox_db.normalise_backlog_key("Other thing"),
         "text": "Other thing"}]}
    r = bot.post("/api/v1/inbox/mirror/backlog", json=payload, headers=machine())
    assert r.status_code == 200 and r.get_json() == {
        "synced": 2, "archived": 0, "complete": True}
    # An unreadable file and an emptied backlog look identical here.
    r = bot.post("/api/v1/inbox/mirror/backlog",
                  json={"complete": True, "items": []}, headers=machine())
    assert r.status_code == 400 and "allow_empty" in r.get_json()["error"]
    page = authed.get("/api/v1/inbox/items?source=backlog").get_json()
    assert len(page["items"]) == 2
    # One entry disappears from a COMPLETE sync → archived, never deleted.
    r = bot.post("/api/v1/inbox/mirror/backlog",
                  json={"complete": True, "items": [payload["items"][0]]},
                  headers=machine())
    assert r.get_json() == {"synced": 1, "archived": 1, "complete": True}
    page = authed.get("/api/v1/inbox/items?source=backlog").get_json()
    assert len(page["items"]) == 1
    # A PARTIAL sync archives nothing.
    r = bot.post("/api/v1/inbox/mirror/backlog",
                  json={"complete": False, "items": [payload["items"][0]]},
                  headers=machine())
    assert r.get_json()["archived"] == 0


# --------------------------------------------------------------------------- #
# Untrusted text
# --------------------------------------------------------------------------- #

def test_a_script_tag_in_a_transcript_is_escaped_in_html_and_json(bot, authed):
    """A transcript is untrusted text from a browser speech API, and a GitHub
    title is untrusted text from a stranger's repo. Neither may become markup,
    an attribute, or a Jinja expression."""
    nasty = '</script><img src=x onerror="alert(1)">{{ 7*7 }}'
    item = post_note(authed, text=nasty).get_json()
    assert item["title"].startswith("</script>")          # stored verbatim
    html = authed.get("/inbox").data.decode()
    # No tag of theirs survives, and no quote of theirs can close an attribute.
    assert "<img" not in html
    assert 'onerror="alert(1)"' not in html
    assert "&lt;/script&gt;" in html and "&lt;img" in html
    # The only </script> in the document is our own single inline script's.
    assert html.count("</script>") == html.count("<script")
    # Jinja renders, it does not re-evaluate: the expression survives verbatim
    # inside the title element. (Asserting "49" is absent from the page is not
    # the check — a uuid4 row id contains "49" about a third of the time.)
    body = html.split('<ul class="items"', 1)[1]
    assert "{{ 7*7 }}" in body
    title = body.split('<h3 class="item-title">', 1)[1].split("</h3>", 1)[0]
    assert "{{ 7*7 }}" in title and "49" not in title
    api = bot.get("/api/v1/inbox/items", headers=reader())
    raw = api.data.decode()
    # Flask 3 no longer HTML-escapes `</script>` inside a JSON body, so the
    # sequence IS present here — and it is harmless for exactly one reason,
    # which is a rule this feature must keep: the JSON is only ever served as
    # `application/json` with nosniff and is NEVER embedded in a <script> tag.
    # (`test_the_board_page_never_embeds_item_json` pins the other half.)
    assert api.headers["Content-Type"].startswith("application/json")
    assert api.headers["X-Content-Type-Options"] == "nosniff"
    assert json.loads(raw)["items"][0]["title"] == item["title"]


def test_the_board_page_never_embeds_item_json(authed):
    """The other half of the rule above: no transcript ever reaches the page
    inside a <script>. Rows are server-rendered; inbox.js only shows and hides
    what is already in the DOM."""
    post_note(authed, text="</script> a spoken note")
    html = authed.get("/inbox").data.decode()
    scripts = html.split("<script")[1:]
    for chunk in scripts:
        inner = chunk.split(">", 1)[1].split("</script>", 1)[0]
        assert "spoken note" not in inner
        assert "items" not in inner or "querySelectorAll" in inner


# --------------------------------------------------------------------------- #
# The page itself (step 6)
# --------------------------------------------------------------------------- #

def _code(path: str) -> str:
    """The file with its comments stripped: these assertions are about what the
    code DOES, and a doc comment that says "never innerHTML" must not be able to
    satisfy a test looking for the absence of innerHTML."""
    import pathlib
    import re as _re
    text = pathlib.Path(path).read_text()
    text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.S)
    return _re.sub(r"^\s*//.*$", "", text, flags=_re.M)


def test_the_board_still_has_exactly_one_script_and_the_inbox_has_two(authed):
    """`{% block scripts %}` is filled by inbox.html and NOTHING else. Two
    existing tests assert the board has exactly one <script>, and the CSP has no
    'self' in script-src — so anything that lands in that block must carry the
    same per-request nonce as the inline localizer."""
    import re
    board = authed.get("/")
    assert board.data.decode().count("<script") == 1
    r = authed.get("/inbox")
    html = r.data.decode()
    nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]{16,})'",
                      r.headers["Content-Security-Policy"]).group(1)
    assert html.count("<script") == 2
    for tag in re.findall(r"<script[^>]*>", html):
        assert f'nonce="{nonce}"' in tag, tag
    assert 'src="/static/inbox.js"' in html
    # The CSP itself is untouched: no 'self', no unsafe-inline, no blob:.
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" not in csp and "'unsafe-inline'" not in csp
    assert "blob:" not in csp and "media-src" not in csp


def test_the_table_renders_every_row_with_js_off(authed, bot):
    """The page must be usable with JavaScript disabled — the script only shows
    and hides rows that are already here."""
    ids = [post_note(authed, text=f"note number {n}").get_json()["id"]
           for n in range(5)]
    bot.post("/api/v1/inbox/mirror/backlog",
             json={"complete": True,
                   "items": [{"key": inbox_db.normalise_backlog_key("mirrored"),
                              "text": "mirrored backlog entry"}]},
             headers=machine())
    html = authed.get("/inbox").data.decode()
    for item in ids:
        assert f'id="item-{item}"' in html
    assert "mirrored backlog entry" in html
    assert html.count('<li class="item') == 6
    # ...and the filters are a plain GET form, so each one is a real URL.
    assert 'method="get"' in html
    filtered = authed.get("/inbox?q=number+3").data.decode()
    assert filtered.count('<li class="item') == 1


def test_the_page_never_plays_audio_from_a_blob_url(authed):
    """A blob: preview would need `media-src blob:` in the CSP. Playback comes
    from /inbox/audio/<id> instead, which costs one round trip and leaves
    `default-src 'self'` exactly as it is."""
    authed.post("/api/v1/inbox/items",
                data={"text": "", "audio": (io_bytes(WEBM), "n", "audio/webm")},
                content_type="multipart/form-data",
                headers={"Accept": "application/json"})
    html = authed.get("/inbox").data.decode()
    assert "/inbox/audio/" in html
    assert "blob:" not in html
    js = _code("dashboard/static/inbox.js")
    assert "createObjectURL" not in js
    # ...and it never builds markup from a string.
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
    assert "document.write" not in js


def test_the_page_is_told_the_size_cap_instead_of_hardcoding_it(authed,
                                                                settings):
    """A take must never be lost to the per-note cap, and at 2 MB that is an
    ordinary six-minute ramble, not a theoretical one: MediaRecorder would run
    on unbounded, the upload would come back `body too large`, and the only
    copy of the recording would be in a page showing an error.

    So the server hands the page its own INBOX_AUDIO_MAX_BYTES and the recorder
    auto-stops under it. Rendered, never hardcoded in the script — otherwise
    changing the setting silently stops the two agreeing, which is the same bug
    with extra steps."""
    html = authed.get("/inbox").data.decode()
    assert f'data-audio-max-bytes="{settings.inbox_audio_max_bytes}"' in html
    # ...and the copy says what the cap MEANS. "Up to 2 MB" is not something a
    # person can act on mid-sentence.
    assert "minutes of speech" in html
    assert "stops itself at the" in html

    js = _code("dashboard/static/inbox.js")
    assert "data-audio-max-bytes" in js
    # A timeslice, or `ondataavailable` fires exactly once — at stop — and
    # there is no running byte count to act on at all.
    assert "recorder.start(TIMESLICE_MS)" in js and "recorder.start()" not in js
    assert "autoStopped" in js and "Stopped at the " in js
    # The size decisions are made on measured bytes, so a UA that ignores the
    # requested bitrate is still stopped in time.
    assert "recordedBytes + stopMargin()" in js
    # Still no innerHTML, still no blob: preview.
    assert "innerHTML" not in js and "createObjectURL" not in js


def test_the_recorder_teardown_cannot_be_skipped_or_run_by_a_stale_take():
    """Two microphone bugs that only show up on a phone, pinned in the source
    because there is no browser in this suite (the behavioural half lives in
    test_inbox_js.py, which skips without node).

    1. `recorder.stop()` throwing let the exception escape the click handler:
       the stream was never released, the button stayed "■ Stop" with the mic
       live, and every later click threw identically.
    2. A Stop click followed quickly by a Record click whose getUserMedia
       resolved FIRST let the superseded recorder's handlers release the NEW
       stream and overwrite the new take with the old one."""
    import re
    js = _code("dashboard/static/inbox.js")
    assert re.search(r"try\s*\{\s*instance\.stop\(\);", js), \
        "stop() is unguarded again"
    # Every handler a superseded recorder still owns bails out.
    assert js.count("if (instance !== recorder) { return; }") >= 3
    # ...and the previous recorder is orphaned as the new take starts, not when
    # the new recorder is constructed a turn of the event loop later.
    assert re.search(r"recorder = null;\s*recordedBlob = null;", js)


def test_the_page_keeps_mobile_input_sizes_and_tap_targets():
    """iOS Safari auto-zooms the viewport when a focused input is under 16px,
    which throws the page sideways mid-sentence. Load-bearing, not cosmetic."""
    block = _code("dashboard/static/app.css").split("inbox", 1)[1]
    assert "font-size: 16px" in block
    assert "min-height: 44px" in block
    # Rows stack on a phone and go inline at 640px, never one cramped line.
    assert "@media (min-width: 640px)" in block
    assert "flex-direction: column" in block
    assert "overflow-wrap: anywhere" in block
    assert "truncate" not in block and "text-overflow: ellipsis" not in block


def test_the_inbox_is_reachable_from_the_board_nav(authed):
    assert 'href="/inbox"' in authed.get("/").data.decode()


# --------------------------------------------------------------------------- #
# Delete — the one control that destroys something
# --------------------------------------------------------------------------- #
#
# A voice note is a recording of Graham's voice. The automatic prune is a
# SCHEDULE, not a control, and "there is no way to delete it" is not an
# acceptable answer for personal data. These pin the route's auth to exactly
# what `patch_item` has: session only, origin-pinned, rate-limited.

def _voice_note(client):
    return client.post("/api/v1/inbox/items",
                       data={"text": "", "audio": (io_bytes(WEBM), "note",
                                                   "audio/webm")},
                       content_type="multipart/form-data",
                       headers={"Accept": "application/json"}).get_json()


def test_delete_removes_the_row_and_the_recording(authed, settings):
    from dashboard import inbox_audio
    item = _voice_note(authed)
    conn = inbox_db.connect(settings.inbox_db_path)
    try:
        path = inbox_db.get_item(conn, item["id"])["audio_path"]
    finally:
        conn.close()
    assert inbox_audio.open_path(settings.inbox_audio_dir, path) is not None

    r = authed.delete(f"/api/v1/inbox/items/{item['id']}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["deleted"] == item["id"] and body["had_audio"] is True
    assert body["audio_removed"] is True
    # Gone from both halves of the store.
    assert inbox_audio.open_path(settings.inbox_audio_dir, path) is None
    assert authed.get(f"/inbox/audio/{item['id']}").status_code == 404
    conn = inbox_db.connect(settings.inbox_db_path)
    try:
        assert inbox_db.get_item(conn, item["id"]) is None
    finally:
        conn.close()
    # Idempotent-ish: a second delete is an honest 404, not a 500.
    assert authed.delete(f"/api/v1/inbox/items/{item['id']}").status_code == 404


def test_delete_takes_linked_issues_with_it(bot, authed, settings):
    """ON DELETE CASCADE, with foreign_keys=ON. A dangling inbox_issues row
    would make the (repo, number) uniqueness index — the thing that stops the
    GitHub mirror cloning a filed voice note — permanently un-reusable."""
    item = post_note(authed).get_json()["id"]
    r = bot.post(f"/api/v1/inbox/items/{item}/issues",
                 json={"repo": "a/b", "number": 9,
                       "url": "https://github.com/a/b/issues/9"},
                 headers=machine())
    assert r.status_code == 201
    assert authed.delete(f"/api/v1/inbox/items/{item}").status_code == 200
    conn = inbox_db.connect(settings.inbox_db_path)
    try:
        assert inbox_db.linked_issue(conn, "a/b", 9) is None
    finally:
        conn.close()


def test_delete_is_session_only_and_origin_pinned(bot, authed, settings,
                                                  registry, notifier):
    """Same auth as PATCH, and for the same reasons: READ_TOKEN is a read
    credential, INBOX_TOKEN is scoped to the machine endpoints, and a DELETE
    driven by a session cookie is an ambient credential that needs the pin."""
    item = post_note(authed).get_json()["id"]
    assert bot.delete(f"/api/v1/inbox/items/{item}",
                      headers=reader()).status_code == 401
    assert bot.delete(f"/api/v1/inbox/items/{item}",
                      headers=machine()).status_code == 401
    assert bot.delete(f"/api/v1/inbox/items/{item}").status_code == 401
    # ...and it survived all three.
    assert authed.get(f"/api/v1/inbox/items").get_json()["counts"]["total"] == 1

    client = _pinned_client(settings, registry, notifier)
    base = "https://dash.example.com"
    created = client.post("/api/v1/inbox/items", data={"text": "pin me"},
                          content_type="multipart/form-data", base_url=base,
                          headers={"Origin": base, "Accept": "application/json"})
    pinned = created.get_json()["id"]
    assert client.delete(f"/api/v1/inbox/items/{pinned}",
                         base_url=base).status_code == 403
    assert client.delete(f"/api/v1/inbox/items/{pinned}", base_url=base,
                         headers={"Origin": "https://evil.example"}
                         ).status_code == 403
    assert client.delete(f"/api/v1/inbox/items/{pinned}", base_url=base,
                         headers={"Origin": base}).status_code == 200


def test_the_board_offers_a_delete_control_per_row(authed):
    html = authed.get("/inbox").data.decode()
    item = post_note(authed).get_json()["id"]
    html = authed.get("/inbox").data.decode()
    assert f'class="delete-item" data-id="{item}"' in html


# --------------------------------------------------------------------------- #
# Aggregate storage cap
# --------------------------------------------------------------------------- #

def test_the_audio_store_has_an_aggregate_cap_not_just_a_per_note_one(
        authed, settings):
    """The per-note cap bounds ONE upload. The create limiter bounds the rate.
    Neither bounds the total — 30 notes per 15 min per IP at the per-note cap is
    gigabytes a day from a single address, and `box-disk` only pages once the
    disk is already gone."""
    settings.inbox_audio_max_total_bytes = 6000
    clip = b"\x1a\x45\xdf\xa3" + b"\x00" * 2000        # ~2 kB each
    for _ in range(2):
        r = authed.post("/api/v1/inbox/items",
                        data={"text": "", "audio": (io_bytes(clip), "n",
                                                    "audio/webm")},
                        content_type="multipart/form-data",
                        headers={"Accept": "application/json"})
        assert r.status_code == 201
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "", "audio": (io_bytes(clip), "n",
                                                "audio/webm")},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    # 507 Insufficient Storage: the REQUEST is a fine size, the STORE is full.
    assert r.status_code == 507 and "full" in r.get_json()["error"]
    # A typed note still works — the cap is about audio, not about the Inbox.
    assert post_note(authed, text="typed still fine").status_code == 201


def test_the_aggregate_cap_does_not_stat_the_whole_tree_on_every_upload(
        authed, settings, monkeypatch):
    """Measuring the cap used to mean `os.walk`-ing the audio tree on EVERY
    upload — at the 3 GB cap that is tens of thousands of `stat` calls for a
    number that moves by one file. The rows already know what they are holding,
    so far from the cap the SUM answers it.

    The margin is what keeps that honest, and it is asserted in both
    directions: nowhere near the cap, no walk happens at all; anywhere near it,
    the walk happens and the number it produces is the one that decides."""
    from dashboard import inbox_audio

    walks = []
    real = inbox_audio.tree_bytes
    monkeypatch.setattr(inbox_audio, "tree_bytes",
                        lambda d: (walks.append(d), real(d))[1])

    clip = b"\x1a\x45\xdf\xa3" + b"\x00" * 2000
    assert authed.post("/api/v1/inbox/items",
                       data={"text": "", "audio": (io_bytes(clip), "n",
                                                   "audio/webm")},
                       content_type="multipart/form-data",
                       headers={"Accept": "application/json"}
                       ).status_code == 201
    assert walks == [], "the upload path walked the tree with gigabytes to spare"

    # Now put the cap within the margin. The cheap number must stop being
    # trusted — and it must be the WALK that decides, which is provable because
    # a file no row knows about is invisible to the SUM and fatal to the walk.
    settings.inbox_audio_max_total_bytes = 6000
    import os
    stray = os.path.join(settings.inbox_audio_dir, "2026", "09",
                         "f" * 32 + ".webm")
    os.makedirs(os.path.dirname(stray), exist_ok=True)
    with open(stray, "wb") as fh:
        fh.write(b"\x1a\x45\xdf\xa3" + b"\x00" * 5000)
    r = authed.post("/api/v1/inbox/items",
                    data={"text": "", "audio": (io_bytes(clip), "n",
                                                "audio/webm")},
                    content_type="multipart/form-data",
                    headers={"Accept": "application/json"})
    assert walks, "near the cap the real total must be measured, not guessed"
    assert r.status_code == 507


# --------------------------------------------------------------------------- #
# Log hygiene
# --------------------------------------------------------------------------- #

def test_a_failed_transcript_error_cannot_forge_a_log_line(bot, authed, caplog):
    """`error` is caller-supplied. The caller is the Mac worker, which could
    easily put a fragment of a TRANSCRIPT in it — untrusted text from a
    microphone. Raw, it carries ANSI escapes, C0 control bytes and newlines
    straight into the container log: forged log lines, and a terminal reading
    them does as it is told."""
    item = _voice_note(authed)["id"]
    nasty = ("boom\n2026-09-19 00:00:00 ERROR dashboard.web forged line\r\n"
             "\x1b[2J\x1b]0;pwned\x07\x00tail")
    with caplog.at_level("WARNING"):
        r = bot.post(f"/api/v1/inbox/items/{item}/transcript",
                     json={"failed": True, "error": nasty}, headers=machine())
    assert r.status_code == 200
    logged = [rec.getMessage() for rec in caplog.records
              if "transcription failed" in rec.getMessage()]
    assert len(logged) == 1
    line = logged[0]
    assert "\n" not in line and "\r" not in line
    assert "\x1b" not in line and "\x00" not in line and "\x07" not in line
    # The readable words survive — it is a sanitiser, not a redactor.
    assert "boom" in line and "tail" in line


def test_a_repo_with_a_trailing_newline_is_not_a_valid_repo():
    """Python's `$` also matches immediately BEFORE a trailing newline, so a
    `$`-anchored check accepts "a/b\n" — and this string is interpolated into
    an api.github.com path and stored as a mirror key."""
    from dashboard.config import GITHUB_REPO_RE, GITHUB_TOKEN_RE
    assert GITHUB_REPO_RE.match("Graham-Williams/km-tracker")
    assert not GITHUB_REPO_RE.match("Graham-Williams/km-tracker\n")
    assert not GITHUB_REPO_RE.match("a/b\nc/d")
    assert GITHUB_TOKEN_RE.match("ghp_abc123")
    assert not GITHUB_TOKEN_RE.match("ghp_abc123\n")
