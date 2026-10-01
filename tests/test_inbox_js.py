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

function world(maxBytes, opts) {
  opts = opts || {};
  const els = {};
  ['capture-form', 'capture-text', 'record-btn', 'record-status',
   'capture-error', 'capture-submit', 'record-level', 'capture-done'].forEach(function (id) {
    els[id] = el(id);
  });
  els['capture-text'].value = '';
  els['capture-done'].hidden = true;
  /* A note's Close button, to act while a take is still waiting to be added. */
  const toggle = el('toggle');
  toggle.setAttribute('data-id', 'c'.repeat(32));
  toggle.setAttribute('data-to', 'closed');
  let reloads = 0;
  const unload = [];
  const posts = [];
  els['capture-form'].setAttribute('data-audio-max-bytes', maxBytes);
  /* An open Edit-draft form somewhere on the page, with changes not saved yet. */
  const draftField = {value: 'Typed, not saved', defaultValue: 'Machine title'};
  const dirtyEditor = {hidden: false, querySelectorAll: function () { return [draftField]; }};

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
  /* What each POST carried: the fields set on it, and the text as it was when sent. */
  function FakeFormData() {
    const sent = {fields: [], text: els['capture-text'].value};
    posts.push(sent);
    this.set = function (k) { sent.fields.push(k); };
    this.get = function (k) { return k === 'text' ? sent.text : ''; };
  }
  const answer = opts.fetch || function () {
    return Promise.resolve({ok: true, status: 201, json: function () { return Promise.resolve({}); }});
  };
  const win = {
    isSecureContext: true,
    MediaRecorder: FakeRecorder,
    setInterval: function (fn) { tid += 1; timers.set(tid, fn); return tid; },
    clearInterval: function (id) { timers.delete(id); },
    setTimeout: function (fn) { fn(); return 0; },
    fetch: function () { return answer(); },
    FormData: FakeFormData,
    confirm: function () { return false; },
    addEventListener: function (t, fn) { if (t === 'beforeunload') { unload.push(fn); } },
    location: {reload: function () { reloads += 1; }}
  };
  /* opts.holdMic: the permission prompt stays open (getUserMedia never settles). */
  const nav = {mediaDevices: {getUserMedia: function () {
    return opts.holdMic ? new Promise(function () {}) : Promise.resolve(makeStream());
  }}};
  const doc = {
    getElementById: function (id) { return els[id] || null; },
    querySelectorAll: function (sel) {
      if (sel === '.toggle-state') { return [toggle]; }
      if (sel === 'form.draft-edit' && opts.dirtyDraft) { return [dirtyEditor]; }
      return [];
    }
  };
  function FakeBlob(parts, opts) {
    this.parts = parts;
    this.type = (opts || {}).type || '';
    this.size = parts.reduce(function (n, p) { return n + (p.size || 0); }, 0);
  }
  const sandbox = {document: doc, window: win, navigator: nav, Blob: FakeBlob,
                   FormData: FakeFormData, console: console};
  return {
    els: els, streams: streams, recorders: recorders, toggle: toggle, posts: posts,
    reloads: function () { return reloads; },
    run: function () { vm.runInNewContext(SRC, sandbox); },
    click: function () { els['record-btn'].fire('click'); },
    add: function () { els['capture-form'].fire('submit', {preventDefault: function () {}}); },
    leaving: function () {
      const ev = {};
      unload.forEach(function (fn) { fn(ev); });
      return ev.returnValue !== undefined;
    },
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

/* 4. A take recorded but not yet added survives any other action: none of them reloads. */
async function takeThenAct() {
  const w = world(1000000);
  w.run();
  w.click();
  await grant();
  w.recorders[0].emit(500);
  w.click();                             // Stop: the take is held, not yet added
  await grant();
  w.toggle.fire('click');                // Close some other note meanwhile
  for (let i = 0; i < 6; i++) { await tick(); }
  return {reloads: w.reloads(), status: w.status()};
}

async function settle() { for (let i = 0; i < 8; i++) { await tick(); } }

async function heldTake(w) {
  w.run();
  w.click();
  await grant();
  w.recorders[0].emit(500);
  w.click();                             // Stop: held, not yet added
  await grant();
}

function captured(w) {
  return {reloads: w.reloads(), done: w.els['capture-done'].textContent,
          doneHidden: w.els['capture-done'].hidden, error: w.els['capture-error'].textContent,
          addDisabled: w.els['capture-submit'].disabled, text: w.els['capture-text'].value,
          status: w.status(), posts: w.posts.map(function (p) { return p.fields; })};
}

/* 5. Capture save: the one action that reloads, and never over unsaved input. */
async function capture() {
  const out = {};

  /* A held take, nothing else unsaved: saved, then the page reloads. */
  let w = world(1000000);
  await heldTake(w);
  w.add();
  await settle();
  out.heldTake = captured(w);

  /* Text saved while a take is being RECORDED: no reload, the take keeps going. */
  w = world(1000000);
  w.run();
  w.click();
  await grant();
  w.els['capture-text'].value = 'typed while recording';
  w.add();
  await settle();
  out.whileRecording = Object.assign(captured(w), {
    recording: w.recorders[0].state === 'recording', micLive: !w.micsOff()});

  /* ...or while the microphone prompt is still open (a take is STARTING). */
  w = world(1000000, {holdMic: true});
  w.run();
  w.click();
  w.els['capture-text'].value = 'typed while the prompt is open';
  w.add();
  await settle();
  out.whileStarting = captured(w);

  /* ...or with an Edit-draft form open with changes elsewhere on the page. */
  w = world(1000000, {dirtyDraft: true});
  w.run();
  w.els['capture-text'].value = 'a typed note';
  w.add();
  await settle();
  out.dirtyDraft = captured(w);

  /* A take recorded WHILE the save is in flight is not the one that was saved. */
  let land;
  w = world(1000000, {fetch: function () { return new Promise(function (r) { land = r; }); }});
  w.run();
  w.els['capture-text'].value = 'first, a typed note';
  w.add();
  w.click();
  await grant();
  w.recorders[0].emit(500);
  w.click();
  await grant();
  land({ok: true, status: 201, json: function () { return Promise.resolve({}); }});
  await settle();
  out.takeDuringSave = captured(w);

  /* Refused without a JSON reason (a proxy's error page): nothing cleared, Add back. */
  w = world(1000000, {fetch: function () {
    return Promise.resolve({ok: false, status: 413,
                            json: function () { return Promise.reject(new Error('html')); }});
  }});
  await heldTake(w);
  w.add();
  await settle();
  const refusedOnce = captured(w);
  w.add();                               // the take is still held: it is sent again
  await settle();
  out.refused = Object.assign(refusedOnce, {postsAfterRetry: w.posts.length});

  /* Refused WITH the server's reason. */
  w = world(1000000, {fetch: function () {
    return Promise.resolve({ok: false, status: 507, json: function () {
      return Promise.resolve({error: 'the voice-note store is full'}); }});
  }});
  w.run();
  w.els['capture-text'].value = 'a typed note';
  w.add();
  await settle();
  out.refusedWithReason = captured(w);

  /* No answer at all. */
  w = world(1000000, {fetch: function () { return Promise.reject(new TypeError('Failed to fetch')); }});
  w.run();
  w.els['capture-text'].value = 'a typed note';
  w.add();
  await settle();
  out.offline = captured(w);

  return out;
}

/* 6. Leaving the page asks first while anything would be lost. */
async function leaving() {
  const out = {};
  let w = world(1000000);
  w.run();
  out.idle = w.leaving();
  w.click();
  await grant();
  out.recording = w.leaving();
  w.recorders[0].emit(500);
  w.click();
  await grant();
  out.held = w.leaving();
  w = world(1000000, {holdMic: true});
  w.run();
  w.click();
  out.starting = w.leaving();
  w = world(1000000);
  w.run();
  w.els['capture-text'].value = 'half a thought';
  out.text = w.leaving();
  w = world(1000000, {dirtyDraft: true});
  w.run();
  out.dirtyDraft = w.leaving();
  return out;
}

(async function () {
  const out = {};
  out.takeThenAct = await takeThenAct();
  out.capture = await capture();
  out.leaving = await leaving();
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


def test_a_recorded_take_not_yet_added_survives_another_action(ran):
    t = ran["takeThenAct"]
    assert t["reloads"] == 0
    assert t["status"].startswith("Recorded ") and "Add to inbox" in t["status"]


ADDED = "Added — refresh to see it in the list"


def test_a_capture_save_reloads_when_nothing_else_is_unsaved(ran):
    c = ran["capture"]["heldTake"]
    assert c["reloads"] == 1 and c["addDisabled"] is False and c["doneHidden"] is True
    assert c["posts"] == [["audio", "audio_secs"]]
    assert c["status"] == "Saved — transcribing… Whisper picks it up within a few minutes."


def test_a_capture_save_never_reloads_over_a_take_or_an_open_edit(ran):
    for case in ("whileRecording", "whileStarting", "dirtyDraft"):
        c = ran["capture"][case]
        assert c["reloads"] == 0, case
        assert c["done"] == ADDED and c["doneHidden"] is False, case
        assert c["addDisabled"] is False and c["text"] == "", case     # reset; Add is back
        assert c["error"] == "", case
    rec = ran["capture"]["whileRecording"]
    assert rec["recording"] is True and rec["micLive"] is True        # the take carries on
    assert rec["posts"] == [[]]                                       # text only: no audio


def test_a_take_recorded_while_a_save_is_in_flight_is_kept(ran):
    c = ran["capture"]["takeDuringSave"]
    assert c["reloads"] == 0 and c["done"] == ADDED and c["addDisabled"] is False
    assert c["posts"] == [[]]                                         # the save was text only
    assert c["status"].startswith("Recorded ") and "Add to inbox" in c["status"]


def test_a_refused_capture_keeps_everything_and_says_why(ran):
    r = ran["capture"]["refused"]
    assert r["error"] == "Not saved — the server answered 413"
    assert r["reloads"] == 0 and r["addDisabled"] is False and r["doneHidden"] is True
    assert r["status"].startswith("Recorded ")                        # not "Saving…" or blank
    assert r["postsAfterRetry"] == 2                                  # the take was still held
    why = ran["capture"]["refusedWithReason"]
    assert why["error"] == "Not saved — the voice-note store is full"
    assert why["text"] == "a typed note" and why["addDisabled"] is False
    off = ran["capture"]["offline"]
    assert off["error"].startswith("Not saved — check your connection")
    assert off["text"] == "a typed note" and off["addDisabled"] is False
    assert off["reloads"] == 0


def test_leaving_the_page_asks_first_while_anything_would_be_lost(ran):
    assert ran["leaving"] == {"idle": False, "recording": True, "held": True,
                              "starting": True, "text": True, "dirtyDraft": True}


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


# --------------------------------------------------------------------------- #
# Editing a voice note's draft: show, cancel, save, fail — actually executed.
# --------------------------------------------------------------------------- #

EDIT_HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const SRC = fs.readFileSync(process.argv[2], 'utf8');

function el(tag, attrs) {
  const e = {
    tagName: tag, hidden: false, disabled: false, textContent: '', value: '',
    _attrs: Object.assign({}, attrs || {}), _listeners: {}, _children: [],
    focused: false,
    classList: {toggle: function () {}, add: function () {}, remove: function () {}},
    addEventListener: function (t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    setAttribute: function (k, v) { this._attrs[k] = String(v); },
    getAttribute: function (k) {
      return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null;
    },
    focus: function () { this.focused = true; },
    fire: function (t, ev) {
      ev = ev || {};
      ev.preventDefault = ev.preventDefault || function () { ev.prevented = true; };
      (this._listeners[t] || []).forEach(function (fn) { fn.call(this, ev); }, this);
      return ev;
    },
    querySelector: function (sel) {
      for (const c of this._children) {
        if (sel[0] === '.' && (c._attrs['class'] || '').split(' ').indexOf(sel.slice(1)) !== -1) { return c; }
        const m = /^\[name="(.+)"\]$/.exec(sel);
        if (m && c._attrs.name === m[1]) { return c; }
      }
      return null;
    }
  };
  return e;
}

function world(fetchImpl) {
  const id = 'a'.repeat(32);
  const btn = el('button', {'class': 'edit-draft', 'data-id': id});
  btn.hidden = true;
  const form = el('form', {id: 'edit-' + id, 'data-id': id});
  form.hidden = true;
  const title = el('input', {name: 'draft_title'}); title.value = 'Machine title';
  const body = el('textarea', {name: 'draft_body'}); body.value = 'Machine body';
  const project = el('input', {name: 'draft_project'}); project.value = '';
  const save = el('button', {'class': 'edit-save'});
  const cancel = el('button', {'class': 'edit-cancel'});
  form._children = [title, body, project, save, cancel];
  form.resets = 0;
  form.reset = function () { this.resets += 1; title.value = 'Machine title'; body.value = 'Machine body'; project.value = ''; };
  const err = el('p', {id: 'capture-error'});
  const els = {'capture-error': err};
  els['edit-' + id] = form;
  const calls = [];
  let reloads = 0;
  const win = {
    isSecureContext: true,
    fetch: function (url, opts) { calls.push({url: url, opts: opts}); return fetchImpl(url, opts); },
    setInterval: function () { return 0; }, clearInterval: function () {},
    setTimeout: function () { return 0; },
    confirm: function () { return false; }, addEventListener: function () {},
    location: {reload: function () { reloads += 1; }}
  };
  const doc = {
    getElementById: function (i) { return els[i] || null; },
    querySelectorAll: function (sel) { return sel === '.edit-draft' ? [btn] : []; }
  };
  const sandbox = {document: doc, window: win, navigator: {}, console: console,
                   Blob: function () {}, FormData: function () {}};
  vm.runInNewContext(SRC, sandbox);
  return {id: id, btn: btn, form: form, title: title, body: body, project: project,
          save: save, cancel: cancel, err: err, calls: calls,
          reloads: function () { return reloads; }};
}

function tick() { return new Promise(function (r) { setImmediate(r); }); }
async function settle() { for (let i = 0; i < 6; i++) { await tick(); } }

function ok() { return Promise.resolve({ok: true, status: 200, json: function () { return Promise.resolve({}); }}); }
function refused() { return Promise.resolve({ok: false, status: 400, json: function () { return Promise.resolve({error: 'draft_project must be …'}); }}); }

(async function () {
  const out = {};

  let w = world(ok);
  out.initial = {btnHidden: w.btn.hidden, formHidden: w.form.hidden};
  w.btn.fire('click');
  out.opened = {btnHidden: w.btn.hidden, formHidden: w.form.hidden, focused: w.title.focused,
                expanded: w.btn.getAttribute('aria-expanded')};
  w.title.value = 'Typed then abandoned';
  w.cancel.fire('click');
  out.cancelled = {btnHidden: w.btn.hidden, formHidden: w.form.hidden, resets: w.form.resets,
                   title: w.title.value, calls: w.calls.length};

  w.btn.fire('click');
  w.title.value = 'Fix the wheel';
  w.body.value = 'Line one\nLine two';
  w.project.value = '  km-tracker ';
  const ev = w.form.fire('submit');
  await settle();
  const c = w.calls[0];
  out.saved = {prevented: !!ev.prevented, calls: w.calls.length, url: c.url, method: c.opts.method,
               credentials: c.opts.credentials, contentType: c.opts.headers['Content-Type'],
               body: JSON.parse(c.opts.body), reloads: w.reloads(), formHidden: w.form.hidden};

  w = world(ok);
  w.btn.fire('click');
  w.project.value = '';
  w.form.fire('submit');
  await settle();
  out.emptyProject = JSON.parse(w.calls[0].opts.body).draft_project;

  w = world(refused);
  w.btn.fire('click');
  w.title.value = 'Kept on failure';
  w.form.fire('submit');
  await settle();
  out.failed = {reloads: w.reloads(), formHidden: w.form.hidden, title: w.title.value,
                error: w.err.textContent, errHidden: w.err.hidden, saveDisabled: w.save.disabled};

  process.stdout.write(JSON.stringify(out));
})().catch(function (err) {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def edited(tmp_path_factory):
    node = shutil.which("node")
    if node is None:                                       # pragma: no cover
        pytest.skip("node is not installed")
    harness = tmp_path_factory.mktemp("js") / "edit_harness.js"
    harness.write_text(EDIT_HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(INBOX_JS)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_edit_button_appears_only_once_wired_and_opens_the_form(edited):
    assert edited["initial"] == {"btnHidden": False, "formHidden": True}
    assert edited["opened"] == {"btnHidden": True, "formHidden": False,
                                "focused": True, "expanded": "true"}


def test_cancel_closes_resets_and_sends_nothing(edited):
    assert edited["cancelled"] == {"btnHidden": False, "formHidden": True, "resets": 1,
                                   "title": "Machine title", "calls": 0}


def test_save_patches_the_three_draft_fields_without_a_reload(edited):
    saved = edited["saved"]
    assert saved["prevented"] is True
    assert saved["calls"] == 1
    assert saved["url"] == "/api/v1/inbox/items/" + "a" * 32
    assert saved["method"] == "PATCH" and saved["credentials"] == "same-origin"
    assert saved["contentType"] == "application/json"
    assert saved["body"] == {"draft_title": "Fix the wheel",
                             "draft_body": "Line one\nLine two",
                             "draft_project": "km-tracker"}
    assert saved["reloads"] == 0 and saved["formHidden"] is True
    assert edited["emptyProject"] is None


def test_a_failed_save_keeps_the_form_and_the_text_and_says_why(edited):
    failed = edited["failed"]
    assert failed["reloads"] == 0 and failed["formHidden"] is False
    assert failed["title"] == "Kept on failure"
    assert failed["error"] == "Not saved — draft_project must be …" and not failed["errHidden"]
    assert failed["saveDisabled"] is False


# --------------------------------------------------------------------------- #
# Close / Reopen on a note — actually executed.
# --------------------------------------------------------------------------- #

STATE_HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const SRC = fs.readFileSync(process.argv[2], 'utf8');

function el(attrs) {
  return {
    hidden: false, disabled: false, textContent: '', _attrs: Object.assign({}, attrs || {}),
    _listeners: {},
    classList: {toggle: function () {}, add: function () {}, remove: function () {}},
    addEventListener: function (t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    setAttribute: function (k, v) { this._attrs[k] = String(v); },
    getAttribute: function (k) {
      return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null;
    },
    fire: function (t) { (this._listeners[t] || []).forEach(function (fn) { fn.call(this, {}); }, this); }
  };
}

function world(fetchImpl, to, unsaved) {
  const btn = el({'class': 'toggle-state', 'data-id': 'c'.repeat(32), 'data-to': to});
  btn.hidden = true;
  const err = el({id: 'capture-error'});
  const text = el({id: 'capture-text'});
  text.value = (unsaved && unsaved.text) || '';
  /* An open Edit-draft form: its fields carry the server-rendered defaultValue. */
  const field = el({name: 'draft_title'});
  field.defaultValue = 'Machine title';
  field.value = (unsaved && unsaved.draft) || 'Machine title';
  const editForm = el({'class': 'draft-edit'});
  editForm.hidden = !(unsaved && unsaved.draft);
  editForm.querySelectorAll = function () { return [field]; };
  const calls = [];
  let reloads = 0;
  const win = {
    fetch: function (url, opts) { calls.push({url: url, opts: opts}); return fetchImpl(); },
    setInterval: function () { return 0; }, clearInterval: function () {},
    setTimeout: function (fn) { fn(); return 0; },
    confirm: function () { return false; }, addEventListener: function () {},
    location: {reload: function () { reloads += 1; }}
  };
  const byId = {'capture-error': err, 'capture-text': text};
  const doc = {
    getElementById: function (i) { return byId[i] || null; },
    querySelectorAll: function (sel) {
      if (sel === '.toggle-state') { return [btn]; }
      if (sel === 'form.draft-edit') { return [editForm]; }
      return [];
    }
  };
  vm.runInNewContext(SRC, {document: doc, window: win, navigator: {}, console: console,
                           Blob: function () {}, FormData: function () {}});
  return {btn: btn, err: err, calls: calls,
          reloads: function () { return reloads; }};
}

function tick() { return new Promise(function (r) { setImmediate(r); }); }
async function settle() { for (let i = 0; i < 6; i++) { await tick(); } }
function answer(status, body) {
  return function () {
    return Promise.resolve({ok: status < 300, status: status,
                            json: function () { return Promise.resolve(body || {}); }});
  };
}

(async function () {
  const out = {};
  let w = world(answer(200), 'closed');
  out.wired = !w.btn.hidden;
  w.btn.fire('click');
  await settle();
  const c = w.calls[0];
  out.close = {calls: w.calls.length, url: c.url, method: c.opts.method,
               credentials: c.opts.credentials, body: JSON.parse(c.opts.body),
               reloads: w.reloads()};

  w = world(answer(409, {error: 'the item changed while saving — reload and try again'}), 'open');
  w.btn.fire('click');
  await settle();
  out.conflict = {error: w.err.textContent, reloads: w.reloads(), disabled: w.btn.disabled,
                  body: JSON.parse(w.calls[0].opts.body)};

  w = world(function () { return Promise.reject(new Error('offline')); }, 'closed');
  w.btn.fire('click');
  await settle();
  out.offline = {error: w.err.textContent, reloads: w.reloads(), disabled: w.btn.disabled};

  /* A double tap sends ONE request; the button is back once the answer lands (disabled
     only while a request is in flight), so a later tap is a new request. */
  let pending;
  w = world(function () { return new Promise(function (r) { pending = r; }); }, 'closed');
  w.btn.fire('click');
  w.btn.fire('click');
  const inFlight = w.btn.disabled;
  pending({ok: true, status: 200, json: function () { return Promise.resolve({}); }});
  await settle();
  out.doubleTap = {calls: w.calls.length, inFlight: inFlight, disabled: w.btn.disabled};
  w.btn.fire('click');
  out.doubleTap.later = w.calls.length;

  /* Delete's confirmation names the Drive copy that Delete does NOT remove, and only says
     "and its recording" while the note still has one here. */
  out.confirms = [];
  for (const [path, hasAudio] of [['audio/2026/09/' + 'd'.repeat(32) + '.*', true],
                                  [null, false],
                                  ['audio/2026/09/' + 'e'.repeat(32) + '.*', false]]) {
    const del = el({'class': 'delete-item', 'data-id': 'd'.repeat(32)});
    if (path) { del.setAttribute('data-drive-path', path); }
    if (hasAudio) { del.setAttribute('data-has-audio', '1'); }
    const asked = [];
    const win = {
      fetch: function () { asked.push('fetched'); return Promise.resolve({ok: true, status: 200}); },
      setInterval: function () { return 0; }, clearInterval: function () {},
      setTimeout: function () { return 0; },
      confirm: function (msg) { asked.push(msg); return false; }, addEventListener: function () {},
      location: {reload: function () {}}
    };
    const doc = {
      getElementById: function () { return null; },
      querySelectorAll: function (sel) { return sel === '.delete-item' ? [del] : []; }
    };
    vm.runInNewContext(SRC, {document: doc, window: win, navigator: {}, console: console,
                             Blob: function () {}, FormData: function () {}});
    del.fire('click');
    await settle();
    out.confirms.push(asked);
  }

  process.stdout.write(JSON.stringify(out));
})().catch(function (err) {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def toggled(tmp_path_factory):
    node = shutil.which("node")
    if node is None:                                       # pragma: no cover
        pytest.skip("node is not installed")
    harness = tmp_path_factory.mktemp("js") / "state_harness.js"
    harness.write_text(STATE_HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(INBOX_JS)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_close_appears_once_wired_and_patches_the_state_without_a_reload(toggled):
    assert toggled["wired"] is True
    close = toggled["close"]
    assert close["calls"] == 1 and close["url"] == "/api/v1/inbox/items/" + "c" * 32
    assert close["method"] == "PATCH" and close["credentials"] == "same-origin"
    assert close["body"] == {"state": "closed"} and close["reloads"] == 0


def test_a_409_on_close_or_reopen_says_why_and_never_reloads(toggled):
    c = toggled["conflict"]
    assert c["body"] == {"state": "open"}
    assert c["error"] == "Not saved — the item changed while saving — reload and try again"
    assert c["reloads"] == 0 and c["disabled"] is False


def test_a_double_tap_sends_one_request_and_the_button_comes_back(toggled):
    assert toggled["doubleTap"] == {"calls": 1, "inFlight": True, "disabled": False,
                                    "later": 2}


def test_a_failed_close_says_so_and_gives_the_button_back(toggled):
    o = toggled["offline"]
    assert o["error"] == "Not saved — check your connection"
    assert o["reloads"] == 0 and o["disabled"] is False


def test_the_delete_confirmation_names_the_drive_copy_it_leaves(toggled):
    with_audio, without, expired = toggled["confirms"]
    # A note whose recording is already gone here (expired or missing): not "and its
    # recording", but its Drive copy may still exist, so that sentence stays.
    assert len(expired) == 1 and "and its recording" not in expired[0]
    assert expired[0] == ("Delete this note from the Hub? This cannot be undone.\n\n"
                          "Any backed-up copy of the recording stays in Google Drive (audio/2026/"
                          "09/" + "e" * 32 + ".* in the backup folder) until you remove it there "
                          "by hand.")
    assert len(with_audio) == 1 and len(without) == 1          # confirm only: declined, no fetch
    msg = with_audio[0]
    assert "cannot be undone" in msg and "Google Drive" in msg
    assert "audio/2026/09/" + "d" * 32 + ".*" in msg and "by hand" in msg
    assert "Google Drive" not in without[0] and "cannot be undone" in without[0]


# --------------------------------------------------------------------------- #
# In-place updates: no reload after a tick, Close, Reopen, Delete or a draft save.
# A small fake DOM (tree, classList, querySelector with .class/[attr]/:not) and the real
# inbox.js, driven with the server's real response shape.
# --------------------------------------------------------------------------- #

INPLACE_HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const SRC = fs.readFileSync(process.argv[2], 'utf8');

function matchOne(e, sel) {
  let rest = sel;
  const nots = []; rest = rest.replace(/:not\(\.([\w-]+)\)/g, function (_, c) { nots.push(c); return ''; });
  const attrs = []; rest = rest.replace(/\[([\w-]+)="([^"]*)"\]/g, function (_, k, v) { attrs.push([k, v]); return ''; });
  const classes = []; rest = rest.replace(/\.([\w-]+)/g, function (_, c) { classes.push(c); return ''; });
  const tag = rest.trim();
  if (tag && (tag.indexOf(' ') !== -1 || tag[0] === '#')) { return false; }
  if (tag && e.tagName !== tag.toUpperCase()) { return false; }
  const cl = (e._attrs['class'] || '').split(/\s+/);
  if (!classes.every(function (c) { return cl.indexOf(c) !== -1; })) { return false; }
  if (nots.some(function (c) { return cl.indexOf(c) !== -1; })) { return false; }
  return attrs.every(function (kv) { return e.getAttribute(kv[0]) === kv[1]; });
}
function matches(e, sel) { return sel.split(',').some(function (s) { return matchOne(e, s.trim()); }); }

function E(tag, attrs, kids) {
  const e = {
    tagName: tag.toUpperCase(), _attrs: Object.assign({}, attrs || {}), children: [],
    parentNode: null, hidden: !!(attrs && 'hidden' in attrs), disabled: false, checked: false,
    value: '', defaultValue: '', textContent: '', _listeners: {},
    addEventListener: function (t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    fire: function (t, ev) {
      ev = ev || {};
      ev.preventDefault = ev.preventDefault || function () { ev.prevented = true; };
      (this._listeners[t] || []).forEach(function (fn) { fn.call(this, ev); }, this);
      return ev;
    },
    setAttribute: function (k, v) { this._attrs[k] = String(v); },
    getAttribute: function (k) {
      return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null;
    },
    removeAttribute: function (k) { delete this._attrs[k]; },
    removeChild: function (c) { this.children = this.children.filter(function (x) { return x !== c; }); c.parentNode = null; },
    querySelectorAll: function (sel) {
      const out = [];
      (function walk(n) { n.children.forEach(function (c) { if (matches(c, sel)) { out.push(c); } walk(c); }); })(this);
      return out;
    },
    querySelector: function (sel) { return this.querySelectorAll(sel)[0] || null; },
    focus: function () {}
  };
  Object.defineProperty(e, 'classList', {get: function () {
    const self = this;
    const list = function () { return (self._attrs['class'] || '').split(/\s+/).filter(Boolean); };
    return {
      contains: function (c) { return list().indexOf(c) !== -1; },
      add: function (c) { const l = list(); if (l.indexOf(c) === -1) { l.push(c); self._attrs['class'] = l.join(' '); } },
      remove: function (c) { self._attrs['class'] = list().filter(function (x) { return x !== c; }).join(' '); },
      toggle: function (c, on) {
        const has = list().indexOf(c) !== -1;
        const want = on === undefined ? !has : !!on;
        if (want && !has) { self._attrs['class'] = list().concat([c]).join(' '); }
        if (!want && has) { self._attrs['class'] = list().filter(function (x) { return x !== c; }).join(' '); }
        return want;
      }
    };
  }});
  (kids || []).forEach(function (k) { k.parentNode = e; e.children.push(k); });
  return e;
}

const ID = 'a'.repeat(32);
function voiceRow() {
  const H = {hidden: ''};
  return E('li', {id: 'item-' + ID, 'class': 'item item-review state-open', 'data-id': ID,
                  'data-source': 'voice', 'data-state': 'open', 'data-reviewed': '0',
                  'data-needs-review': '1', 'data-awaiting-filing': '0', 'data-project': '',
                  'data-text': 'old'}, [
    E('div', {'class': 'item-head'}, [
      E('span', {'class': 'badge badge-review'}),
      E('span', Object.assign({'class': 'badge badge-filing'}, H)),
      E('span', Object.assign({'class': 'badge badge-closed'}, H)),
      E('span', Object.assign({'class': 'badge badge-project head-project'}, H))]),
    E('h3', {'class': 'item-title'}),
    E('p', Object.assign({'class': 'draft-state draft-pending'}, H)),
    E('p', Object.assign({'class': 'draft-state draft-failed-line'}, H)),
    E('p', Object.assign({'class': 'item-body draft-body'}, H)),
    E('p', Object.assign({'class': 'draft-project-line'}, H), [E('span', {'class': 'draft-project'})]),
    E('div', {'class': 'item-actions'}, [
      E('span', {'class': 'action-group action-start'}, [
        E('button', Object.assign({'class': 'edit-draft', 'data-id': ID}, H)),
        E('label', {'class': 'review'}, [E('input', {'class': 'review-box', 'data-id': ID})])]),
      E('span', {'class': 'action-group action-end'}, [
        E('button', Object.assign({'class': 'toggle-state', 'data-id': ID, 'data-to': 'closed'}, H)),
        E('button', Object.assign({'class': 'toggle-state', 'data-id': ID, 'data-to': 'open'}, H)),
        E('button', Object.assign({'class': 'delete-item', 'data-id': ID}, H))])]),
    E('form', Object.assign({'class': 'draft-edit', id: 'edit-' + ID, 'data-id': ID}, H), [
      E('input', {name: 'draft_title'}), E('textarea', {name: 'draft_body'}),
      E('input', {name: 'draft_project'}),
      E('button', {'class': 'edit-save'}), E('button', {'class': 'edit-cancel'})]),
    E('p', Object.assign({'class': 'row-error'}, H))]);
}

function world(fetchImpl, opts) {
  opts = opts || {};
  const row = voiceRow();
  const list = E('ul', {id: 'items'}, [row]);
  const tile = function (key) { return E('div', {'class': 'tile'}, [E('span', {'data-count': key})]); };
  const root = E('main', {}, [tile('needs_review'), tile('open'), tile('total'),
                              E('span', {'data-plural-of': 'total'}), list,
                              E('textarea', {id: 'capture-text'})]);
  const calls = [];
  const unload = [];
  let reloads = 0;
  const win = {
    fetch: function (url, o) { calls.push({url: url, opts: o}); return fetchImpl(url, o); },
    setInterval: function () { return 0; }, clearInterval: function () {},
    setTimeout: function (fn) { fn(); return 0; },
    confirm: function () { return true; },
    addEventListener: function (t, fn) { if (t === 'beforeunload') { unload.push(fn); } },
    location: {reload: function () { reloads += 1; }}
  };
  const byId = function (id) {
    let hit = null;
    (function walk(n) { n.children.forEach(function (c) { if (!hit && c.getAttribute('id') === id) { hit = c; } walk(c); }); })(root);
    return hit;
  };
  if (opts.captureText) { byId('capture-text').value = opts.captureText; }
  const doc = {getElementById: byId,
               querySelectorAll: function (sel) { return root.querySelectorAll(sel); }};
  vm.runInNewContext(SRC, {document: doc, window: win, navigator: {}, console: console,
                           Blob: function () {}, FormData: function () {}});
  return {row: row, list: list, root: root, calls: calls, unload: unload,
          reloads: function () { return reloads; },
          q: function (sel) { return row.querySelector(sel); },
          tiles: function () {
            return ['needs_review', 'open', 'total'].map(function (k) {
              return root.querySelector('[data-count="' + k + '"]').textContent; });
          }};
}

function tick() { return new Promise(function (r) { setImmediate(r); }); }
async function settle() { for (let i = 0; i < 8; i++) { await tick(); } }
function ok(body) {
  return function () { return Promise.resolve({ok: true, status: 200,
    json: function () { return Promise.resolve(body); }}); };
}
function refused(status, error) {
  return function () { return Promise.resolve({ok: false, status: status,
    json: function () { return Promise.resolve({error: error}); }}); };
}
function item(over) {
  return Object.assign({id: ID, source: 'voice', state: 'open', reviewed: false,
    needs_review: true, awaiting_filing: false, deletable: true, can_tick_reviewed: true,
    title: '(untitled)', project: null, body: 'the wheel sticks',
    draft: {title: 'Fix the sticky wheel', body: 'It sticks.', project: null, status: 'ready',
            edited_at: null},
    counts: {needs_review: 1, open: 2, total: 3}}, over || {});
}

(async function () {
  const out = {};

  /* Wiring reveals the button for the row's state only. */
  let w = world(ok({}));
  out.wired = {close: !w.q('.toggle-state[data-to="closed"]').hidden,
               reopen: !w.q('.toggle-state[data-to="open"]').hidden};

  /* The Reviewed tick: updated in place from the response, no reload. */
  w = world(ok(item({reviewed: true, needs_review: false, awaiting_filing: true,
                     counts: {needs_review: 0, open: 2, total: 3}})));
  const box = w.q('.review-box');
  box.checked = true;
  box.fire('change');
  const inFlight = box.disabled;
  await settle();
  out.tick = {reloads: w.reloads(), inFlight: inFlight, disabled: box.disabled,
              reviewed: w.row.getAttribute('data-reviewed'),
              needsReview: w.row.getAttribute('data-needs-review'),
              filing: w.row.getAttribute('data-awaiting-filing'),
              reviewBadge: w.q('.badge-review').hidden, filingBadge: w.q('.badge-filing').hidden,
              rowClasses: w.row.getAttribute('class'), tiles: w.tiles(),
              reviewTileZero: w.root.querySelector('[data-count="needs_review"]').parentNode
                .classList.contains('zero'),
              error: w.q('.row-error').hidden};

  /* Close: the row's state, badge, buttons and the start group all flip. */
  w = world(ok(item({state: 'closed', needs_review: false,
                     counts: {needs_review: 0, open: 1, total: 3}})));
  w.q('.toggle-state[data-to="closed"]').fire('click');
  await settle();
  out.close = {reloads: w.reloads(), state: w.row.getAttribute('data-state'),
               classes: w.row.getAttribute('class'),
               closedBadge: w.q('.badge-closed').hidden,
               closeHidden: w.q('.toggle-state[data-to="closed"]').hidden,
               reopenHidden: w.q('.toggle-state[data-to="open"]').hidden,
               startGroupHidden: w.q('.action-start').hidden, tiles: w.tiles(),
               enabled: !w.q('.toggle-state[data-to="closed"]').disabled};

  /* A 409 on the tick: the box shows the TRUE (unchanged) state, the reason is inline. */
  w = world(refused(409, 'the item changed while saving — reload and try again'));
  const b2 = w.q('.review-box');
  b2.checked = true;
  b2.fire('change');
  await settle();
  out.tick409 = {reloads: w.reloads(), checked: b2.checked, disabled: b2.disabled,
                 error: w.q('.row-error').textContent, errorHidden: w.q('.row-error').hidden,
                 reviewed: w.row.getAttribute('data-reviewed')};

  /* A network failure on Close: nothing claims to be saved. */
  w = world(function () { return Promise.reject(new Error('offline')); });
  w.q('.toggle-state[data-to="closed"]').fire('click');
  await settle();
  out.closeFailed = {state: w.row.getAttribute('data-state'),
                     error: w.q('.row-error').textContent,
                     enabled: !w.q('.toggle-state[data-to="closed"]').disabled};

  /* Delete: the row goes, the tiles follow. */
  w = world(ok({deleted: ID, had_audio: true, audio_removed: true,
                counts: {needs_review: 0, open: 1, total: 2}}));
  w.q('.delete-item').fire('click');
  await settle();
  out.del = {rows: w.list.children.length, tiles: w.tiles(), reloads: w.reloads()};

  /* A draft save: title, body and the form all updated in place. */
  w = world(ok(item({draft: {title: 'Mend the wheel', body: 'Line one\nLine two',
                             project: 'km-tracker', status: 'ready', edited_at: 'now'}})));
  w.q('.draft-failed-line').hidden = false;          // "Couldn't draft — edit to write one"
  w.q('label.review').hidden = true;                 // nothing to review yet
  w.q('.edit-draft').fire('click');
  const form = w.root.querySelector('.draft-edit');
  form.querySelector('[name="draft_title"]').value = 'Mend the wheel';
  form.fire('submit');
  await settle();
  out.draft = {reloads: w.reloads(), title: w.q('.item-title').textContent,
               body: w.q('.draft-body').textContent, bodyHidden: w.q('.draft-body').hidden,
               project: w.q('.draft-project').textContent,
               projectLineHidden: w.q('.draft-project-line').hidden,
               failedLineHidden: w.q('.draft-failed-line').hidden,
               reviewOffered: !w.q('label.review').hidden,
               formHidden: form.hidden, saveEnabled: !form.querySelector('.edit-save').disabled,
               titleDefault: form.querySelector('[name="draft_title"]').defaultValue,
               text: w.row.getAttribute('data-text')};

  /* A refused draft save keeps the form open with the typing, and says why on the row. */
  w = world(refused(400, 'draft_project must be letters, digits and ._/-'));
  w.q('.edit-draft').fire('click');
  const f2 = w.root.querySelector('.draft-edit');
  f2.querySelector('[name="draft_title"]').value = 'Kept on failure';
  f2.fire('submit');
  await settle();
  out.draftRefused = {formHidden: f2.hidden, title: f2.querySelector('[name="draft_title"]').value,
                      error: w.q('.row-error').textContent,
                      saveEnabled: !f2.querySelector('.edit-save').disabled};

  /* A tick while the editor holds unsaved typing: the row updates, the typing stays. */
  w = world(ok(item({reviewed: true, needs_review: false, awaiting_filing: true})));
  w.q('.edit-draft').fire('click');
  const f3 = w.root.querySelector('.draft-edit');
  f3.querySelector('[name="draft_title"]').value = 'Half-typed title';
  const b3 = w.q('.review-box');
  b3.checked = true;
  b3.fire('change');
  await settle();
  out.tickWhileEditing = {title: f3.querySelector('[name="draft_title"]').value,
                          formHidden: f3.hidden, reviewed: w.row.getAttribute('data-reviewed')};

  /* Reopen: a closed row gets its first group and Close back. */
  w = world(ok(item({state: 'open', counts: {needs_review: 1, open: 2, total: 3}})));
  w.row.setAttribute('data-state', 'closed');
  w.q('.action-start').hidden = true;
  w.q('.badge-closed').hidden = false;
  const reopen = w.q('.toggle-state[data-to="open"]');
  reopen.hidden = false;
  w.q('.toggle-state[data-to="closed"]').hidden = true;
  reopen.fire('click');
  await settle();
  out.reopen = {state: w.row.getAttribute('data-state'), startHidden: w.q('.action-start').hidden,
                closedBadge: w.q('.badge-closed').hidden,
                closeHidden: w.q('.toggle-state[data-to="closed"]').hidden,
                reopenHidden: reopen.hidden, body: JSON.parse(w.calls[0].opts.body)};

  /* A double tap sends ONE request; the control is back once it lands. */
  let resolve;
  w = world(function () { return new Promise(function (r) { resolve = r; }); });
  const t = w.q('.toggle-state[data-to="closed"]');
  t.fire('click');
  t.fire('click');
  resolve({ok: true, status: 200, json: function () { return Promise.resolve(item({state: 'closed'})); }});
  await settle();
  out.doubleTap = {calls: w.calls.length};

  /* beforeunload: prompts only while something would be lost. */
  w = world(ok({}), {captureText: 'half a thought'});
  const ev1 = {}; w.unload.forEach(function (fn) { fn(ev1); });
  w = world(ok({}));
  const ev2 = {}; w.unload.forEach(function (fn) { fn(ev2); });
  out.unload = {withText: ev1.returnValue !== undefined, without: ev2.returnValue !== undefined,
                listeners: w.unload.length};

  process.stdout.write(JSON.stringify(out));
})().catch(function (err) {
  process.stderr.write(String((err && err.stack) || err));
  process.exit(1);
});
"""


@pytest.fixture(scope="module")
def inplace(tmp_path_factory):
    node = shutil.which("node")
    if node is None:                                       # pragma: no cover
        pytest.skip("node is not installed")
    harness = tmp_path_factory.mktemp("js") / "inplace_harness.js"
    harness.write_text(INPLACE_HARNESS, encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(INBOX_JS)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_wiring_reveals_only_the_button_for_the_rows_state(inplace):
    assert inplace["wired"] == {"close": True, "reopen": False}


def test_a_tick_updates_the_row_badges_and_tiles_in_place(inplace):
    t = inplace["tick"]
    assert t["reloads"] == 0 and t["inFlight"] is True and t["disabled"] is False
    assert (t["reviewed"], t["needsReview"], t["filing"]) == ("1", "0", "1")
    assert t["reviewBadge"] is True and t["filingBadge"] is False
    assert "item-filing" in t["rowClasses"] and "item-review" not in t["rowClasses"]
    assert t["tiles"] == ["0", "2", "3"] and t["error"] is True
    assert t["reviewTileZero"] is True


def test_close_flips_state_badge_buttons_and_the_start_group(inplace):
    c = inplace["close"]
    assert c["reloads"] == 0 and c["state"] == "closed" and "state-closed" in c["classes"]
    assert c["closedBadge"] is False and c["closeHidden"] is True and c["reopenHidden"] is False
    assert c["startGroupHidden"] is True and c["tiles"] == ["0", "1", "3"] and c["enabled"]


def test_a_refused_tick_shows_the_true_state_and_says_not_saved(inplace):
    t = inplace["tick409"]
    assert t["reloads"] == 0 and t["checked"] is False and t["disabled"] is False
    assert t["error"] == "Not saved — the item changed while saving — reload and try again"
    assert t["errorHidden"] is False and t["reviewed"] == "0"


def test_a_failed_close_claims_nothing(inplace):
    f = inplace["closeFailed"]
    assert f["state"] == "open" and f["enabled"]
    assert f["error"].startswith("Not saved — ")


def test_delete_removes_the_row_and_updates_the_tiles(inplace):
    assert inplace["del"] == {"rows": 0, "tiles": ["0", "1", "2"], "reloads": 0}


def test_a_draft_save_updates_the_row_in_place(inplace):
    d = inplace["draft"]
    assert d["reloads"] == 0 and d["title"] == "Mend the wheel"
    assert d["body"] == "Line one\nLine two" and d["bodyHidden"] is False
    assert d["project"] == "km-tracker" and d["formHidden"] is True and d["saveEnabled"]
    assert d["titleDefault"] == "Mend the wheel"
    # The draft project differs from the note's (none yet), so its line shows; the failed
    # line goes, and there is now a draft to tick Reviewed on.
    assert d["projectLineHidden"] is False and d["failedLineHidden"] is True
    assert d["reviewOffered"] is True
    assert "mend the wheel" in d["text"] and "line two" in d["text"]   # the live filter


def test_a_refused_draft_save_keeps_the_typing_and_says_why_on_the_row(inplace):
    r = inplace["draftRefused"]
    assert r["formHidden"] is False and r["title"] == "Kept on failure" and r["saveEnabled"]
    assert r["error"] == "Not saved — draft_project must be letters, digits and ._/-"


def test_an_answer_never_overwrites_typing_in_an_open_editor(inplace):
    t = inplace["tickWhileEditing"]
    assert t == {"title": "Half-typed title", "formHidden": False, "reviewed": "1"}


def test_reopen_brings_back_the_first_group_and_close(inplace):
    r = inplace["reopen"]
    assert r["body"] == {"state": "open"} and r["state"] == "open"
    assert r["startHidden"] is False and r["closedBadge"] is True
    assert r["closeHidden"] is False and r["reopenHidden"] is True


def test_a_double_tap_sends_one_request(inplace):
    assert inplace["doubleTap"] == {"calls": 1}


def test_leaving_the_page_prompts_only_while_input_would_be_lost(inplace):
    assert inplace["unload"] == {"withText": True, "without": False, "listeners": 1}
