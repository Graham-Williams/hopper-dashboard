"""`static/inbox.js`, actually executed.

Everything the capture panel gets wrong, it gets wrong on a phone: a take lost
to the size cap, a microphone that will not switch off, a stale recorder that
eats the take you just started. None of that is visible to a test that greps the
source, and the rest of this suite has no browser in it.

So this one runs the real file in node against a fake DOM and a fake
MediaRecorder and drives it the way a thumb would. It SKIPS when node is not
installed — it is a sharper version of assertions that also exist in
`test_inbox.py`, never the only thing standing between a bug and production.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

INBOX_JS = pathlib.Path(__file__).resolve().parent.parent / "dashboard" / "static" / "inbox.js"

# --------------------------------------------------------------------------- #
# The harness. Kept here rather than as a checked-in .js file so the whole test
# is one artefact: the fake DOM and the thing it is pretending for cannot drift
# apart in separate files.
# --------------------------------------------------------------------------- #

HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const SRC = fs.readFileSync(process.argv[2], 'utf8');

function el(id) {
  return {
    id: id, hidden: false, disabled: false, textContent: '', value: 0, max: 100,
    _attrs: {}, _listeners: {},
    classList: {toggle: function () {}, add: function () {}, remove: function () {}},
    addEventListener: function (t, fn) {
      (this._listeners[t] = this._listeners[t] || []).push(fn);
    },
    setAttribute: function (k, v) { this._attrs[k] = String(v); },
    getAttribute: function (k) {
      return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null;
    },
    focus: function () {},
    fire: function (t, ev) {
      (this._listeners[t] || []).forEach(function (fn) { fn.call(this, ev || {}); }, this);
    }
  };
}

function world(maxBytes) {
  const els = {};
  ['capture-form', 'capture-text', 'record-btn', 'record-status',
   'capture-error', 'capture-submit', 'record-level'].forEach(function (id) {
    els[id] = el(id);
  });
  els['capture-form'].setAttribute('data-audio-max-bytes', maxBytes);

  const streams = [];
  function makeStream() {
    const tracks = [{stopped: false, stop: function () { this.stopped = true; }}];
    const s = {tracks: tracks, getTracks: function () { return tracks; }};
    streams.push(s);
    return s;
  }

  const recorders = [];
  function FakeRecorder(stream, options) {
    this.stream = stream;
    this.options = options || {};
    this.mimeType = this.options.mimeType || 'audio/webm';
    this.state = 'inactive';
    this.deferStop = false;
    this.throwOnStop = false;
    recorders.push(this);
  }
  FakeRecorder.isTypeSupported = function (t) { return t === 'audio/webm;codecs=opus'; };
  FakeRecorder.prototype.start = function (timeslice) {
    this.timeslice = timeslice;
    this.state = 'recording';
  };
  FakeRecorder.prototype.stop = function () {
    if (this.throwOnStop) { throw new Error('the recorder raced to inactive'); }
    this.state = 'inactive';
    if (!this.deferStop && this.onstop) { this.onstop(); }
  };
  FakeRecorder.prototype.emit = function (size) {
    if (this.ondataavailable) {
      this.ondataavailable({data: {size: size, type: this.mimeType}});
    }
  };

  /* Fake timers: the clock's interval must never keep node alive, and nothing
     under test here depends on it firing. */
  const timers = new Map();
  let tid = 0;
  function FakeFormData() { this.set = function () {}; this.get = function () { return ''; }; }
  const win = {
    isSecureContext: true,
    MediaRecorder: FakeRecorder,
    setInterval: function (fn) { tid += 1; timers.set(tid, fn); return tid; },
    clearInterval: function (id) { timers.delete(id); },
    setTimeout: function () { return 0; },
    fetch: function () { return Promise.resolve({ok: true, json: function () { return Promise.resolve({}); }}); },
    FormData: FakeFormData,
    confirm: function () { return false; },
    addEventListener: function () {},
    location: {reload: function () {}}
  };
  const nav = {mediaDevices: {getUserMedia: function () { return Promise.resolve(makeStream()); }}};
  const doc = {
    getElementById: function (id) { return els[id] || null; },
    querySelectorAll: function () { return []; }
  };
  function FakeBlob(parts, opts) {
    this.parts = parts;
    this.type = (opts || {}).type || '';
    this.size = parts.reduce(function (n, p) { return n + (p.size || 0); }, 0);
  }
  const sandbox = {document: doc, window: win, navigator: nav, Blob: FakeBlob,
                   FormData: FakeFormData, console: console};
  return {
    els: els, streams: streams, recorders: recorders,
    run: function () { vm.runInNewContext(SRC, sandbox); },
    click: function () { els['record-btn'].fire('click'); },
    status: function () { return els['record-status'].textContent; },
    micsOff: function () {
      return streams.every(function (s) {
        return s.tracks.every(function (t) { return t.stopped; });
      });
    }
  };
}

function tick() { return new Promise(function (r) { setImmediate(r); }); }

async function grant() { await tick(); await tick(); }

/* 1. A six-minute ramble must not be lost to the 2 MB cap. */
async function autoStop() {
  const w = world(1000);                 // stop margin works out at 250 bytes
  w.run();
  w.click();
  await grant();
  const rec = w.recorders[0];
  const out = {timeslice: rec.timeslice, bitrate: rec.options.audioBitsPerSecond};
  rec.emit(300);
  rec.emit(300);
  out.stillRecordingAt600 = rec.state === 'recording';
  rec.emit(300);                         // 900 + 250 >= 1000: stop NOW
  out.state = rec.state;
  out.status = w.status();
  out.button = w.els['record-btn'].textContent;
  out.level = {value: w.els['record-level'].value,
               max: w.els['record-level'].max,
               hidden: w.els['record-level'].hidden};
  out.micsOff = w.micsOff();
  return out;
}

/* 2. `stop()` throwing must not leave the microphone live for ever. */
async function stopThrows() {
  const w = world(1000000);
  w.run();
  w.click();
  await grant();
  const rec = w.recorders[0];
  rec.emit(120);
  rec.throwOnStop = true;
  const out = {threw: false};
  try { w.click(); } catch (e) { out.threw = true; }
  out.button = w.els['record-btn'].textContent;
  out.pressed = w.els['record-btn'].getAttribute('aria-pressed');
  out.status = w.status();
  out.micsOff = w.micsOff();
  /* And the page is not wedged: clicking again throws nothing either. */
  try { w.click(); } catch (e) { out.threw = true; }
  return out;
}

/* 3. A superseded recorder must not touch the take that replaced it. */
async function superseded() {
  const w = world(1000000);
  w.run();
  w.click();
  await grant();
  const first = w.recorders[0];
  first.emit(500);
  first.deferStop = true;                // its onstop will arrive late
  w.click();                             // Stop
  w.click();                             // Record again, straight away
  await grant();
  const second = w.recorders[1];
  second.emit(70);
  const before = w.status();
  first.onstop();                        // the stale handler, finally
  return {
    recorders: w.recorders.length,
    firstStreamReleased: w.streams[0].tracks.every(function (t) { return t.stopped; }),
    secondStreamLive: w.streams[1].tracks.every(function (t) { return !t.stopped; }),
    statusUnchanged: before === w.status(),
    status: w.status(),
    button: w.els['record-btn'].textContent,
    secondState: second.state
  };
}

(async function () {
  const out = {};
  out.autoStop = await autoStop();
  out.stopThrows = await stopThrows();
  out.superseded = await superseded();
  process.stdout.write(JSON.stringify(out));
})().catch(function (err) {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    node = shutil.which("node")
    if node is None:                                       # pragma: no cover
        pytest.skip("node is not installed; the source-level assertions in "
                    "test_inbox.py still cover these rules")
    harness = tmp_path_factory.mktemp("js") / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(INBOX_JS)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_recorder_stops_itself_before_the_size_cap_and_keeps_the_take(ran):
    """The finding this file exists for. At 2 MB the cap is two to four minutes
    on iOS Safari's AAC — an ordinary voice note, not an edge case. Unbounded,
    MediaRecorder sailed past it, the upload came back `body too large`, and the
    recording was gone: it lives nowhere but the page.

    So the recorder runs with a timeslice, counts the bytes as they arrive, and
    ends the take itself with everything up to that point saved."""
    out = ran["autoStop"]
    assert out["timeslice"] == 1000, "no timeslice: nothing counts the bytes"
    assert out["bitrate"] == 48000
    assert out["stillRecordingAt600"] is True, "it stopped far too early"
    assert out["state"] == "inactive"
    # It SAYS so, in words that mean something, and the take survives.
    assert "Stopped at the" in out["status"] and "limit" in out["status"]
    assert "saved what was recorded" in out["status"]
    assert "Add to inbox" in out["status"]
    # The button and the microphone both come back.
    assert out["button"] == "● Record" and out["micsOff"] is True
    # ...and the fill bar was tracking it, so the ending is not a surprise.
    assert out["level"] == {"value": 900, "max": 1000, "hidden": False}


def test_a_throwing_stop_still_releases_the_microphone(ran):
    """`stop()` can throw — the state races to `inactive`, or the UA is simply
    odd about it. The exception used to escape the click handler: the stream was
    never released, the button stayed "■ Stop" over a live microphone, and every
    later click threw the same way. The recording is kept too; the chunks are
    already in hand and a UA quirk is no reason to bin them."""
    out = ran["stopThrows"]
    assert out["threw"] is False, "the exception escaped the click handler"
    assert out["micsOff"] is True, "the microphone was left live"
    assert out["button"] == "● Record" and out["pressed"] == "false"
    assert "Recorded" in out["status"] and "Add to inbox" in out["status"]


def test_a_superseded_recorder_never_touches_the_new_take(ran):
    """Stop, then Record again straight away, and the old recorder's `onstop`
    can arrive AFTER the new getUserMedia has resolved. Closing over the
    module-level recorder let it release the brand-new stream — a dead
    microphone — and overwrite the new take with the old one."""
    out = ran["superseded"]
    assert out["recorders"] == 2
    assert out["firstStreamReleased"] is True
    assert out["secondStreamLive"] is True, "the stale handler killed the mic"
    assert out["statusUnchanged"] is True, "the stale take rewrote the UI"
    assert out["secondState"] == "recording" and out["button"] == "■ Stop"
