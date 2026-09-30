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

  /* A 409 on the Reviewed tick: show the server's reason, put the box back, reload. */
  {
    const box = el('input', {'class': 'review-box', 'data-id': 'b'.repeat(32)});
    box.checked = true;
    const err2 = el('p', {id: 'capture-error'});
    let reloads2 = 0;
    const win2 = {
      fetch: function () {
        return Promise.resolve({ok: false, status: 409, json: function () {
          return Promise.resolve({error: 'the item changed while saving — reload and try again'}); }});
      },
      setInterval: function () { return 0; }, clearInterval: function () {},
      setTimeout: function (fn) { fn(); return 0; },
      confirm: function () { return false; }, addEventListener: function () {},
      location: {reload: function () { reloads2 += 1; }}
    };
    const doc2 = {
      getElementById: function (i) { return i === 'capture-error' ? err2 : null; },
      querySelectorAll: function (sel) { return sel === '.review-box' ? [box] : []; }
    };
    vm.runInNewContext(SRC, {document: doc2, window: win2, navigator: {}, console: console,
                             Blob: function () {}, FormData: function () {}});
    box.fire('change');
    await settle();
    out.tick409 = {error: err2.textContent, errHidden: err2.hidden, reloads: reloads2,
                   checked: box.checked, disabled: box.disabled};
  }

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


def test_save_patches_the_three_draft_fields_then_reloads(edited):
    saved = edited["saved"]
    assert saved["prevented"] is True
    assert saved["calls"] == 1
    assert saved["url"] == "/api/v1/inbox/items/" + "a" * 32
    assert saved["method"] == "PATCH" and saved["credentials"] == "same-origin"
    assert saved["contentType"] == "application/json"
    assert saved["body"] == {"draft_title": "Fix the wheel",
                             "draft_body": "Line one\nLine two",
                             "draft_project": "km-tracker"}
    assert saved["reloads"] == 1 and saved["formHidden"] is True
    assert edited["emptyProject"] is None


def test_a_failed_save_keeps_the_form_and_the_text_and_says_why(edited):
    failed = edited["failed"]
    assert failed["reloads"] == 0 and failed["formHidden"] is False
    assert failed["title"] == "Kept on failure"
    assert failed["error"].startswith("Could not save that draft") and not failed["errHidden"]
    assert failed["saveDisabled"] is False


def test_a_409_on_the_reviewed_tick_says_why_and_reloads(edited):
    t = edited["tick409"]
    assert t["error"] == "the item changed while saving — reload and try again"
    assert t["errHidden"] is False and t["reloads"] == 1
    assert t["checked"] is False and t["disabled"] is False


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

function world(fetchImpl, to) {
  const btn = el({'class': 'toggle-state', 'data-id': 'c'.repeat(32), 'data-to': to});
  btn.hidden = true;
  const err = el({id: 'capture-error'});
  const calls = [];
  let reloads = 0;
  const win = {
    fetch: function (url, opts) { calls.push({url: url, opts: opts}); return fetchImpl(); },
    setInterval: function () { return 0; }, clearInterval: function () {},
    setTimeout: function (fn) { fn(); return 0; },
    confirm: function () { return false; }, addEventListener: function () {},
    location: {reload: function () { reloads += 1; }}
  };
  const doc = {
    getElementById: function (i) { return i === 'capture-error' ? err : null; },
    querySelectorAll: function (sel) { return sel === '.toggle-state' ? [btn] : []; }
  };
  vm.runInNewContext(SRC, {document: doc, window: win, navigator: {}, console: console,
                           Blob: function () {}, FormData: function () {}});
  return {btn: btn, err: err, calls: calls, reloads: function () { return reloads; }};
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

  /* Delete's confirmation names the Drive copy that Delete does NOT remove. */
  out.confirms = [];
  for (const path of ['audio/2026/09/' + 'd'.repeat(32) + '.*', null]) {
    const del = el({'class': 'delete-item', 'data-id': 'd'.repeat(32)});
    if (path) { del.setAttribute('data-drive-path', path); }
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


def test_close_appears_once_wired_and_patches_the_state_then_reloads(toggled):
    assert toggled["wired"] is True
    close = toggled["close"]
    assert close["calls"] == 1 and close["url"] == "/api/v1/inbox/items/" + "c" * 32
    assert close["method"] == "PATCH" and close["credentials"] == "same-origin"
    assert close["body"] == {"state": "closed"} and close["reloads"] == 1


def test_a_409_on_close_or_reopen_says_why_and_reloads(toggled):
    c = toggled["conflict"]
    assert c["body"] == {"state": "open"}
    assert c["error"] == "the item changed while saving — reload and try again"
    assert c["reloads"] == 1


def test_a_failed_close_says_so_and_gives_the_button_back(toggled):
    o = toggled["offline"]
    assert o["error"].startswith("Could not close that item")
    assert o["reloads"] == 0 and o["disabled"] is False


def test_the_delete_confirmation_names_the_drive_copy_it_leaves(toggled):
    with_audio, without = toggled["confirms"]
    assert len(with_audio) == 1 and len(without) == 1          # confirm only: declined, no fetch
    msg = with_audio[0]
    assert "cannot be undone" in msg and "Google Drive" in msg
    assert "audio/2026/09/" + "d" * 32 + ".*" in msg and "by hand" in msg
    assert "Google Drive" not in without[0] and "cannot be undone" in without[0]
