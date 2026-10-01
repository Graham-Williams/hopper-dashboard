/* The Inbox page script. Loaded from inbox.html only, with the same per-request
   CSP nonce the inline localizer carries (`script-src 'nonce-…'` has no 'self',
   so an external file without the nonce would simply not run).

   PRIVACY GUARANTEE — the reason this file has no speech recognition in it.
   Recorded audio is uploaded to THIS ORIGIN and nowhere else, and it is
   transcribed LOCALLY by Whisper on Graham's own Mac. Nothing spoken into this
   page is sent to Google, Apple, or any other third party at any point.

   That is a deliberate product decision, not an accident of implementation.
   The page used to run a live transcript through `webkitSpeechRecognition`,
   which on every shipping browser streams the microphone to the vendor's own
   servers for recognition. Graham was told, and chose: "Drop it — nothing
   leaves the box." So a voice note is now always created with
   transcript_status='pending' and the Mac worker (every 5 minutes) fills the
   transcript in. If you are ever tempted to add live transcription back, it has
   to be an on-device engine or it breaks this promise.

   Three rules this file may not break, all of them load-bearing:

   1. It NEVER assigns innerHTML and is never handed a JSON blob of transcripts
      inside the page. Every row is rendered server-side by Jinja, with BOTH states
      of everything an action can change already in it; after a tick, Close, Reopen,
      Delete or a draft save this script updates the row IN PLACE from the server's
      answer: it flips `hidden`, classes and data-* attributes, and writes text
      through textContent. It never builds markup.
   2. It NEVER plays audio from a `blob:` URL. That would need `media-src blob:`
      in the CSP, and the CSP is not being loosened for a preview — playback
      happens from /inbox/audio/<id> after the note is saved.
   3. The microphone is released on EVERY exit path. A stream assigned to a
      variable that is then overwritten can never be stopped again, and a
      recording light that will not go out is the worst thing this page could
      do on a phone. Hence: release before starting, release in the failure
      path, and refuse to start a second time while a permission prompt is
      already open.
   4. A take is NEVER lost to the per-note size cap. The server caps one upload
      at INBOX_AUDIO_MAX_BYTES (2 MB by default, a few minutes of speech), and
      an unbounded MediaRecorder would sail past it: the user talks for six
      minutes, presses “Add to inbox”, gets `body too large`, and the recording
      is gone — it lives nowhere but this page. So the cap is rendered into the
      form as `data-audio-max-bytes`, the recorder runs with a timeslice so the
      bytes are counted as they arrive, and it AUTO-STOPS with a plain message
      just before the cap is reached, keeping everything recorded so far. The
      clock shows measured minutes-remaining and a progress bar shows the fill,
      because “2 MB” tells nobody how long they may talk.

   The page does not talk to Anthropic either. The Mac, not the browser, sends
   the TRANSCRIPT and TITLE (never the audio) to Claude to draft each note; this file
   only lets Graham edit that draft (PATCH, same origin).

   And the page must degrade: with JavaScript off the table, the filters and
   the typed-note form all still work. Only the microphone needs this file. */
(function () {
  'use strict';

  var form = document.getElementById('capture-form');
  var textEl = document.getElementById('capture-text');
  var recBtn = document.getElementById('record-btn');
  var statusEl = document.getElementById('record-status');
  var errEl = document.getElementById('capture-error');
  var submitBtn = document.getElementById('capture-submit');
  var levelEl = document.getElementById('record-level');

  function setText(el, message) {
    if (!el) { return; }
    el.textContent = message || '';
    el.hidden = !message;
  }

  function status(message) {
    if (statusEl) { statusEl.textContent = message || ''; }
  }

  function fail(message) {
    setText(errEl, message);
  }

  /* "Added — refresh to see it in the list": a capture save that could not reload. */
  var doneEl = document.getElementById('capture-done');
  function done(message) {
    setText(doneEl, message);
  }

  /* Everything on this page that is NOT saved anywhere yet: a take being recorded or
     about to be (a permission prompt is open), a take recorded but not yet added, text in
     the capture box, and an open draft editor with changes. Nothing may reload over it:
     a capture save holds back its reload and the beforeunload guard asks first. */
  function unsavedInput() {
    if (starting) { return true; }
    if (recorder && recorder.state === 'recording') { return true; }
    if (recordedBlob) { return true; }
    if (textEl && String(textEl.value || '').trim()) { return true; }
    var editForms = document.querySelectorAll('form.draft-edit');
    for (var f = 0; f < editForms.length; f++) {
      if (!editForms[f].hidden && formIsDirty(editForms[f])) { return true; }
    }
    return false;
  }

  function formIsDirty(formEl) {
    var fields = formEl.querySelectorAll ? formEl.querySelectorAll('input, textarea') : [];
    for (var k = 0; k < fields.length; k++) {
      if (String(fields[k].value || '') !== String(fields[k].defaultValue || '')) {
        return true;
      }
    }
    return false;
  }

  /* ------------------------------------------------------------------ */
  /* Recording                                                          */
  /* ------------------------------------------------------------------ */

  var stream = null;
  var recorder = null;
  var chunks = [];
  var recordedBlob = null;
  var recordedSecs = 0;
  var startedAt = 0;
  /* True from the moment getUserMedia is called until it settles. Without it a
     double-tap (the ordinary way to hit this on a phone) opens a SECOND stream
     whose assignment orphans the first — and an orphaned stream can never be
     stopped, so the microphone stays live for the life of the page. */
  var starting = false;
  var timerId = null;
  /* Bytes accepted so far for THIS take, the biggest chunk seen (the reserve
     for auto-stop is sized off it), whether the cap ended the take, and whether
     the take has already been wrapped up. */
  var recordedBytes = 0;
  var maxChunkBytes = 0;
  var autoStopped = false;
  var finalised = true;

  /* The server's per-note cap, rendered into the form by Jinja so it tracks the
     setting instead of drifting from it. 0 = not available (an old cached page,
     a template change) — in which case nothing auto-stops and the behaviour is
     exactly what it was before. */
  var MAX_BYTES = (function () {
    var raw = form ? parseInt(form.getAttribute('data-audio-max-bytes'), 10) : NaN;
    return (isFinite(raw) && raw > 0) ? raw : 0;
  })();

  /* Speech does not need more, and it makes the cap mean a predictable number
     of minutes rather than whatever the UA felt like. Every size decision below
     is still made on MEASURED bytes, so a UA that ignores this is handled. */
  var AUDIO_BPS = 48000;
  /* Chunk interval. The point is not the chunks, it is that `ondataavailable`
     fires while recording so there is a running byte count to act on at all. */
  var TIMESLICE_MS = 1000;

  /* Both formats the two devices Graham uses actually produce: Chrome/Android
     gives webm/opus, iOS Safari gives mp4/AAC. The server accepts both and the
     Mac's ffmpeg decodes both. An empty string means "let the UA choose", which
     is what iOS wants. */
  var PREFERRED = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4',
                   'audio/ogg;codecs=opus'];
  var EXTENSIONS = {'audio/webm': 'webm', 'audio/mp4': 'm4a',
                    'audio/ogg': 'ogg', 'audio/wav': 'wav'};

  function preferredMime() {
    if (!window.MediaRecorder || !window.MediaRecorder.isTypeSupported) { return ''; }
    for (var i = 0; i < PREFERRED.length; i++) {
      if (window.MediaRecorder.isTypeSupported(PREFERRED[i])) { return PREFERRED[i]; }
    }
    return '';
  }

  function extensionFor(type) {
    var base = String(type || '').split(';')[0].toLowerCase();
    return EXTENSIONS[base] || 'webm';
  }

  function recordingSupported() {
    return !!(window.isSecureContext && navigator.mediaDevices &&
              navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  }

  function releaseStream() {
    if (!stream) { return; }
    var tracks = stream.getTracks ? stream.getTracks() : [];
    for (var i = 0; i < tracks.length; i++) {
      try { tracks[i].stop(); } catch (e) { /* already stopped */ }
    }
    stream = null;
  }

  /* mm:ss. A spinner would prove the script is alive; a clock proves the
     RECORDER is, which is the thing the user is actually anxious about. */
  function clock(secs) {
    var whole = Math.max(0, Math.floor(secs));
    var mins = Math.floor(whole / 60);
    var rest = whole % 60;
    return mins + ':' + (rest < 10 ? '0' : '') + rest;
  }

  function elapsedSecs() {
    return startedAt ? (Date.now() - startedAt) / 1000 : 0;
  }

  /* ---- the size budget ---------------------------------------------- */

  /* What must be left unspent when the decision to stop is taken: the chunk
     currently filling, plus the final one `stop()` flushes, plus a chunk of
     slack. The first chunk carries the container header and is the largest, so
     it is a sound unit to reserve three of. Never more than a quarter of the
     cap, or a tiny cap would make every take stop instantly. */
  function stopMargin() {
    var margin = Math.max(maxChunkBytes * 3, 32768);
    return Math.min(margin, Math.floor(MAX_BYTES / 4));
  }

  function limitLabel() {
    if (!MAX_BYTES) { return 'the size limit'; }
    if (MAX_BYTES < 1048576) { return Math.round(MAX_BYTES / 1024) + ' KB'; }
    return (Math.round(MAX_BYTES / 1048576 * 10) / 10) + ' MB';
  }

  /* “About 4 min left”, from the rate this take is ACTUALLY encoding at —
     never from an assumed bitrate, because iOS Safari's AAC and Chrome's Opus
     are nothing like each other and the honest number is the measured one. */
  function remainingLabel() {
    if (!MAX_BYTES || recordedBytes <= 0) { return ''; }
    var secs = elapsedSecs();
    if (secs < 3) { return ''; }          // too early for the rate to mean much
    var rate = recordedBytes / secs;
    if (rate <= 0) { return ''; }
    var left = (MAX_BYTES - stopMargin() - recordedBytes) / rate;
    if (left <= 0) { return ''; }
    if (left < 60) { return ' · under a minute left'; }
    return ' · about ' + Math.floor(left / 60) + ' min left';
  }

  /* A fill bar, not a byte count: nobody is converting megabytes to minutes in
     their head halfway through a sentence. `.value`/`.max` are properties, so
     this needs no inline style and the CSP stays as strict as it is. */
  function updateLevel() {
    if (!levelEl) { return; }
    if (!MAX_BYTES || recordedBytes <= 0) {
      levelEl.hidden = true;
      return;
    }
    levelEl.max = MAX_BYTES;
    levelEl.value = Math.min(recordedBytes, MAX_BYTES);
    levelEl.hidden = false;
  }

  function stopTimer() {
    if (timerId !== null) {
      window.clearInterval(timerId);
      timerId = null;
    }
  }

  function startTimer() {
    stopTimer();
    status('● Recording… 0:00');
    timerId = window.setInterval(function () {
      status('● Recording… ' + clock(elapsedSecs()) + remainingLabel());
    }, 250);
  }

  /* Wrap a take up: stop the clock, drop the microphone, build the blob and
     say what happened. Called from `onstop` normally — and from the manual
     teardown when `stop()` itself throws, because the chunks are already in
     hand there and throwing them away would lose the take for the sake of a
     UA quirk. Runs at most once per take. */
  function finalise(instance) {
    if (finalised) { return; }
    finalised = true;
    stopTimer();
    releaseStream();
    var type = 'audio/webm';
    try {
      type = (instance && instance.mimeType) || (chunks[0] && chunks[0].type) || type;
    } catch (e) { /* a dead recorder can throw on property access */ }
    recordedBlob = chunks.length ? new Blob(chunks, {type: type}) : null;
    recordedSecs = Math.round(elapsedSecs() * 10) / 10;
    setRecording(false);
    updateLevel();
    /* Deliberately no preview player here: that needs a blob: URL and the CSP
       has no media-src blob:. It is playable from the row as soon as it is
       saved. */
    if (!recordedBlob) {
      status('Nothing was recorded — try again, or type it instead.');
    } else if (autoStopped) {
      status('Stopped at the ' + limitLabel() + ' limit — saved what was ' +
             'recorded (' + clock(recordedSecs) + '). Press “Add to inbox”.');
    } else {
      status('Recorded ' + clock(recordedSecs) + ' — press “Add to inbox” to save it.');
    }
  }

  /* `instance` rather than the module-level `recorder`: a Stop click followed
     quickly by a Record click whose getUserMedia resolves FIRST leaves the old
     recorder's handlers still queued, and a stale one that ran would release
     the brand-new stream (a dead microphone) and overwrite recordedBlob with
     the previous take. A superseded recorder does nothing at all — the take it
     belonged to has already been abandoned, and `startRecording` released its
     stream before asking for the new one. */
  function wireRecorder(instance) {
    instance.ondataavailable = function (event) {
      if (instance !== recorder) { return; }
      if (!event.data || !event.data.size) { return; }
      chunks.push(event.data);
      recordedBytes += event.data.size;
      if (event.data.size > maxChunkBytes) { maxChunkBytes = event.data.size; }
      updateLevel();
      /* The whole point of the timeslice: stop BEFORE the cap rather than
         discovering it at upload time, when the only copy of the recording is
         in a page that is about to show an error. */
      if (MAX_BYTES && !autoStopped &&
          recordedBytes + stopMargin() >= MAX_BYTES) {
        autoStopped = true;
        stopRecording();
      }
    };
    instance.onstop = function () {
      if (instance !== recorder) { return; }
      finalise(instance);
    };
    instance.onerror = function () {
      if (instance !== recorder) { return; }
      finalised = true;                   // nothing usable to wrap up
      stopTimer();
      status('The recorder failed — type it instead.');
      setRecording(false);
      releaseStream();
      updateLevel();
      if (textEl) { textEl.focus(); }
    };
  }

  function setRecording(active) {
    if (!recBtn) { return; }
    recBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    recBtn.textContent = active ? '■ Stop' : '● Record';
    recBtn.classList.toggle('recording', !!active);
  }

  function startRecording() {
    if (starting) { return; }             // a permission prompt is already open
    fail('');
    /* Belt and braces before anything else: if a previous take left a stream
       open (an exception between assignment and start used to do exactly
       that), it is stopped here rather than orphaned by the next assignment. */
    releaseStream();
    stopTimer();
    /* Orphan the previous recorder HERE, not when the new one is constructed.
       Between this click and getUserMedia resolving, the old recorder's
       `onstop` can still fire; while it was still the current one it would
       finalise the ABANDONED take — restoring the old recordedBlob over the
       reset below and overwriting "Asking for the microphone…". With `recorder`
       already null every stale handler bails, and the old stream was released
       on the line above. */
    recorder = null;
    recordedBlob = null;
    recordedSecs = 0;
    recordedBytes = 0;
    maxChunkBytes = 0;
    autoStopped = false;
    finalised = false;
    updateLevel();
    starting = true;
    if (recBtn) { recBtn.disabled = true; }
    status('Asking for the microphone…');
    navigator.mediaDevices.getUserMedia({audio: true}).then(function (granted) {
      starting = false;
      if (recBtn) { recBtn.disabled = false; }
      stream = granted;
      try {
        var mime = preferredMime();
        var options = {audioBitsPerSecond: AUDIO_BPS};
        if (mime) { options.mimeType = mime; }
        try {
          recorder = new window.MediaRecorder(stream, options);
        } catch (e) {
          /* A UA that refuses the options at all still records — it just picks
             its own bitrate, which the measured auto-stop handles anyway. */
          recorder = new window.MediaRecorder(stream);
        }
        chunks = [];
        wireRecorder(recorder);
        startedAt = Date.now();
        /* WITH a timeslice: without one, `ondataavailable` fires exactly once,
           at stop, and there is no running byte count to auto-stop on. */
        recorder.start(TIMESLICE_MS);
      } catch (e) {
        /* The construct-or-start path can throw on its own (an unsupported
           mimeType, a track that died between grant and start). The stream is
           already open at this point, so it MUST be released here or the
           microphone stays live with no way to turn it off. */
        releaseStream();
        recorder = null;
        finalised = true;               // there is no take to wrap up
        setRecording(false);
        updateLevel();
        status('The recorder would not start (' + ((e && e.name) || 'error') +
               ') — type it instead.');
        if (textEl) { textEl.focus(); }
        return;
      }
      setRecording(true);
      startTimer();
    }).catch(function (err) {
      starting = false;
      finalised = true;                 // nothing was ever recorded
      /* Release whatever may have been granted before the failure, always.
         Nothing should be open on this path, but "should" is how a hot
         microphone happens. */
      releaseStream();
      stopTimer();
      setRecording(false);
      var name = (err && err.name) || 'denied';
      /* ONLY a refusal is permanent. NotReadableError (another app holds the
         mic) and AbortError are transient, and disabling the button for them
         used to cost a page reload to recover from. */
      if (name === 'NotAllowedError' || name === 'SecurityError') {
        if (recBtn) { recBtn.disabled = true; }
        status('Microphone permission denied — type it instead.');
      } else {
        if (recBtn) { recBtn.disabled = false; }
        status('Microphone unavailable (' + name + ') — try again, or type it.');
      }
      if (textEl) { textEl.focus(); }
    });
  }

  /* `stop()` is not as safe as it looks: the state can race to `inactive`
     between the check and the call, and UAs have their own quirks. An
     exception escaping the click handler left the microphone live with the
     button still reading “■ Stop”, and every later click threw identically —
     the one failure this page must not have. So it is wrapped, and the fall
     through still wraps the take up from the chunks already in hand. */
  function stopRecording() {
    var instance = recorder;
    if (instance && instance.state !== 'inactive') {
      try {
        instance.stop();                  // onstop finalises the take
        return;
      } catch (e) { /* fall through to the manual teardown */ }
    }
    if (!finalised) {
      finalise(instance);
      return;
    }
    /* Nothing to wrap up (the take is already finished, or never started) —
       but never leave without checking the microphone is off. */
    stopTimer();
    releaseStream();
    setRecording(false);
  }

  if (recBtn) {
    recBtn.hidden = false;
    if (!recordingSupported()) {
      recBtn.disabled = true;
      status(window.isSecureContext
             ? 'This browser has no microphone recorder — type it instead.'
             : 'Recording needs a secure (https) connection — type it instead.');
      if (textEl) { textEl.focus(); }
    } else {
      recBtn.addEventListener('click', function () {
        if (starting) { return; }
        if (recorder && recorder.state === 'recording') { stopRecording(); }
        else { startRecording(); }
      });
      /* If the tab is closed or navigated away mid-take, drop the microphone
         rather than relying on the browser to notice. */
      window.addEventListener('pagehide', function () {
        stopTimer();
        releaseStream();
      });
    }
  }

  /* A refresh, pull-to-refresh, a filter's Apply or closing the tab would silently lose a
     take or typing that lives nowhere but this page: the browser asks first instead. */
  window.addEventListener('beforeunload', function (event) {
    if (!unsavedInput()) { return undefined; }
    if (event.preventDefault) { event.preventDefault(); }
    event.returnValue = '';
    return '';
  });

  /* ------------------------------------------------------------------ */
  /* Submit                                                             */
  /* ------------------------------------------------------------------ */

  /* The answer to any fetch here, read ONCE: {ok, status, body}. A body that is not JSON
     (a proxy's error page) is {}. Rejects only when the request never got an answer. */
  function readAnswer(response) {
    var parsed;
    try { parsed = response.json(); } catch (e) { parsed = null; }
    return Promise.resolve(parsed).catch(function () { return null; }).then(function (body) {
      return {ok: !!response.ok, status: response.status,
              body: (body && typeof body === 'object') ? body : {}};
    });
  }

  /* The reason a save was refused, in the server's words when it gave any. */
  function reasonOf(answer) {
    var error = answer && answer.body && answer.body.error;
    if (typeof error === 'string' && error) { return error; }
    return 'the server answered ' + ((answer && answer.status) || 'with an error');
  }
  var OFFLINE = 'check your connection';

  /* The capture form's own fields, as they were sent. */
  function captureFields() {
    var names = ['text', 'project', 'title'];
    var out = [];
    for (var i = 0; i < names.length; i++) {
      var input = form.querySelector ? form.querySelector('[name="' + names[i] + '"]') : null;
      if (!input && names[i] === 'text') { input = textEl; }
      if (input) { out.push({el: input, value: String(input.value || '')}); }
    }
    return out;
  }

  if (form && window.fetch && window.FormData) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      if (submitBtn && submitBtn.disabled) { return; }   // one request per tap
      fail('');
      done('');
      var data = new FormData(form);
      /* What THIS request carries. A take recorded or text typed while it is in flight
         is not part of it, and must still be here afterwards. */
      var sentBlob = recordedBlob;
      var sentFields = captureFields();
      if (sentBlob) {
        data.set('audio', sentBlob, 'note.' + extensionFor(sentBlob.type));
        data.set('audio_secs', String(recordedSecs));
      }
      if (!sentBlob && !String(data.get('text') || '').trim()) {
        fail('Say something or type something first.');
        if (textEl) { textEl.focus(); }
        return;
      }
      if (submitBtn) { submitBtn.disabled = true; }
      var heldStatus = statusEl ? statusEl.textContent : '';
      status('Saving…');
      function restoreStatus() {
        if (statusEl && statusEl.textContent === 'Saving…') { status(heldStatus); }
      }
      window.fetch(form.action, {
        method: 'POST',
        body: data,
        credentials: 'same-origin',
        headers: {'Accept': 'application/json'}
      }).then(readAnswer).then(function (answer) {
        /* Add is given back on EVERY outcome: it is disabled only while this is in flight. */
        if (submitBtn) { submitBtn.disabled = false; }
        if (!answer.ok) {
          /* Nothing was saved, so nothing is cleared: the take and the text are still
             here for another press of Add. */
          restoreStatus();
          fail('Not saved — ' + reasonOf(answer));
          return;
        }
        added(sentBlob, sentFields);
      }, function () {
        if (submitBtn) { submitBtn.disabled = false; }
        restoreStatus();
        fail('Not saved — ' + OFFLINE + '. The note is still here: press “Add to inbox” ' +
             'to try again.');
      });
    });
  }

  /* A capture save landed. The new row needs server rendering, so this is the ONE action
     that reloads — but never over input that is not saved yet. */
  function added(sentBlob, sentFields) {
    /* What was sent is saved: clear exactly that, and nothing typed or recorded since. */
    if (sentBlob && recordedBlob === sentBlob) {
      recordedBlob = null;
      recordedSecs = 0;
      recordedBytes = 0;
      updateLevel();
    }
    for (var i = 0; i < sentFields.length; i++) {
      if (String(sentFields[i].el.value || '') === sentFields[i].value) {
        sentFields[i].el.value = '';
      }
    }
    /* Say it landed. A voice note has no transcript yet (nothing leaves this box, so
       Whisper on the Mac does it on its next pass), and "did that even record?" is the
       question this page has to answer out loud. */
    if (statusEl && statusEl.textContent === 'Saving…') {
      status(sentBlob
             ? 'Saved — transcribing… Whisper picks it up within a few minutes.'
             : 'Saved.');
    }
    /* Reload rather than building a row here: rows are server-rendered, and that is the
       rule that keeps untrusted text out of the DOM by any path but Jinja's escaping. The
       query string (the filters) is preserved. Checked when the moment comes, so a take
       started in the meantime is not reloaded over either. */
    window.setTimeout(function () {
      if (unsavedInput()) {
        done('Added — refresh to see it in the list');
        return;
      }
      window.location.reload();
    }, 700);
  }

  /* ------------------------------------------------------------------ */
  /* A row, updated in place                                            */
  /* ------------------------------------------------------------------ */

  /* Every row action (the Reviewed tick, Close, Reopen, a draft save, Delete) answers
     with the item's derived fields and fresh `counts`, and the page is updated from that
     answer — never reloaded. The row already holds both states of everything that can
     change (the template renders the one not in force `hidden`), so this only flips
     `hidden`, classes and data-*, and sets textContent. */

  function rowOf(control) {
    var id = control && control.getAttribute ? control.getAttribute('data-id') : null;
    return id ? document.getElementById('item-' + id) : null;
  }

  function each(root, selector, fn) {
    var found = root && root.querySelectorAll ? root.querySelectorAll(selector) : [];
    for (var i = 0; i < found.length; i++) { fn(found[i]); }
  }

  function show(root, selector, visible) {
    each(root, selector, function (node) { node.hidden = !visible; });
  }

  /* "Not saved — <reason>", on the row itself. A row without its error line (an old
     cached page) falls back to the capture panel's. */
  function rowError(row, message) {
    var line = row && row.querySelector ? row.querySelector('.row-error') : null;
    if (line) {
      setText(line, message);
    } else if (message) {
      fail(message);
    }
  }

  /* A control is disabled ONLY while a request is in flight, and while one is, every
     control on that row is: two answers about one row can never land out of order. */
  var ROW_CONTROLS = '.review-box, .toggle-state, .delete-item, .edit-save';
  function setBusy(row, busy, control) {
    each(row, ROW_CONTROLS, function (node) { node.disabled = busy || saveLocked(node); });
    if (control) { control.disabled = busy || saveLocked(control); }
  }

  /* A CLOSED note's editor may stay open (it holds changes not saved yet), but its Save must
     not work: it says "Reopen to save" and stays disabled — through any other request on
     the row — until the note is open again. */
  function saveLocked(node) {
    return node.getAttribute('data-reopen-to-save') !== null;
  }
  function lockSave(save, locked) {
    if (!save) { return; }
    if (locked) {
      if (!saveLocked(save)) {
        save.setAttribute('data-label', save.textContent);
        save.setAttribute('data-reopen-to-save', '1');
      }
      save.textContent = 'Reopen to save';
      save.disabled = true;
    } else if (saveLocked(save)) {
      save.removeAttribute('data-reopen-to-save');
      save.textContent = save.getAttribute('data-label') || 'Save';
      save.disabled = false;
    }
  }

  /* One request about one item. Resolves to readAnswer's {ok, status, body}; rejects
     only when no answer came back at all. */
  function send(id, method, payload) {
    var init = {method: method, credentials: 'same-origin',
                headers: {'Accept': 'application/json'}};
    if (payload !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(payload);
    }
    return window.fetch('/api/v1/inbox/items/' + encodeURIComponent(id), init)
      .then(readAnswer);
  }

  /* Close shows on an open note, Reopen on a closed one; never both. */
  function toggleShown(state, to) {
    return (state === 'open' && to === 'closed') || (state === 'closed' && to === 'open');
  }

  function showToggles(row, state) {
    each(row, '.toggle-state', function (btn) {
      btn.hidden = !toggleShown(state, btn.getAttribute('data-to'));
    });
  }

  /* The tiles: Needs review, Open and the item total, from the same counts() the page
     was rendered with. Answers can land out of order (two rows, two requests), so counts
     are applied only when their stamp (`counts_at`, a decimal string of nanoseconds) is
     newer than the last applied; unstamped counts are applied as they come. */
  var countsAt = '';
  function newerStamp(at) {
    if (typeof at !== 'string' || !/^[0-9]+$/.test(at)) { return true; }
    if (!countsAt) { return true; }
    if (at.length !== countsAt.length) { return at.length > countsAt.length; }
    return at > countsAt;
  }
  function applyCounts(counts, at) {
    if (!counts || typeof counts !== 'object') { return; }
    if (!newerStamp(at)) { return; }
    if (typeof at === 'string' && /^[0-9]+$/.test(at)) { countsAt = at; }
    Object.keys(counts).forEach(function (key) {
      var n = counts[key];
      if (typeof n !== 'number') { return; }
      each(document, '[data-count="' + key + '"]', function (node) {
        node.textContent = String(n);
        var tile = node.parentNode;
        if (tile && tile.classList && tile.classList.contains('tile')) {
          tile.classList.toggle('zero', n === 0);
        }
      });
      each(document, '[data-plural-of="' + key + '"]', function (node) {
        node.textContent = n === 1 ? 'item' : 'items';
      });
    });
  }

  /* One GET of the counts, for an answer that carried none. A failure leaves the tiles as
     they are; a refresh puts them right. */
  function refreshCounts() {
    window.fetch('/api/v1/inbox/counts', {credentials: 'same-origin',
                                          headers: {'Accept': 'application/json'}})
      .then(readAnswer).then(function (answer) {
        if (answer.ok) { applyCounts(answer.body.counts, answer.body.counts_at); }
      }, function () { /* offline: the tiles wait for a refresh */ });
  }

  /* The row, from the item as the server now has it (inbox.item_json). The same rules
     the template renders it with — keep the two in step. */
  function applyItem(row, item) {
    if (!item || typeof item !== 'object') { return; }
    applyCounts(item.counts, item.counts_at);
    if (!row || !item.id) { return; }
    var draft = item.draft || {};
    var voice = item.source === 'voice';
    var closed = item.state === 'closed';
    var needsReview = !!item.needs_review;
    var filing = !needsReview && !!item.awaiting_filing;

    /* data-*: what the live filters read. */
    row.setAttribute('data-state', item.archived_at ? 'archived' : item.state);
    row.setAttribute('data-reviewed', item.reviewed ? '1' : '0');
    row.setAttribute('data-needs-review', needsReview ? '1' : '0');
    row.setAttribute('data-awaiting-filing', item.awaiting_filing ? '1' : '0');
    row.setAttribute('data-awaiting-transcription', item.awaiting_transcription ? '1' : '0');
    row.setAttribute('data-project', item.project || '');
    row.setAttribute('data-text', [item.title || '', item.body || '', item.project || '',
                                   draft.title || '', draft.body || ''].join(' ').toLowerCase());
    row.classList.toggle('item-review', needsReview);
    row.classList.toggle('item-filing', filing);
    row.classList.toggle('state-open', item.state === 'open');
    row.classList.toggle('state-closed', closed);

    /* Badges. */
    show(row, '.badge-review', needsReview);
    show(row, '.badge-filing', filing);
    show(row, '.badge-closed', closed);
    each(row, '.head-project', function (node) {
      node.textContent = item.project || '';
      node.hidden = !item.project;
    });

    /* The title: a voice note shows its draft's until it is reviewed. */
    each(row, '.item-title', function (node) {
      node.textContent = (voice && draft.title && !item.reviewed) ? draft.title : item.title;
    });

    /* The draft and its one status line (the same order as the template). */
    each(row, '.draft-body', function (node) {
      node.textContent = draft.body || '';
      node.hidden = !draft.body;
    });
    each(row, '.draft-project', function (node) { node.textContent = draft.project || ''; });
    show(row, '.draft-project-line', !!draft.project && draft.project !== item.project);
    var noTranscript = row.querySelector('.no-transcript');
    var noTranscriptOn = !!noTranscript && !item.reviewed && !draft.body;
    if (noTranscript) { noTranscript.hidden = !noTranscriptOn; }
    var pendingOn = !noTranscriptOn && draft.status === 'pending' && !item.reviewed;
    show(row, '.draft-pending', pendingOn);
    show(row, '.draft-failed-line', !noTranscriptOn && !pendingOn &&
                                    draft.status === 'failed' && !draft.edited_at);

    /* Controls: a closed note offers only Reopen and Delete. Its first group keeps its slot
       but shows nothing (`dormant`, visibility: hidden), so Reopen lands where Close was. */
    each(row, '.action-start', function (group) {
      group.hidden = false;
      group.classList.toggle('dormant', closed);
    });
    show(row, 'label.review', !!item.can_tick_reviewed);
    each(row, '.review-box', function (box) { box.checked = !!item.reviewed; });
    showToggles(row, item.state);

    /* The editor's fields follow the saved draft, unless they hold changes of Graham's
       that are not saved yet. On a closed note an UNCHANGED editor closes with its group;
       one with changes stays open (unsaved input), with a Save that cannot save. */
    each(row, 'form.draft-edit', function (formEl) {
      var dirty = formIsDirty(formEl);
      lockSave(formEl.querySelector('.edit-save'), closed);
      if (closed && !dirty && !formEl.hidden) {
        formEl.hidden = true;
        each(row, '.edit-draft', function (btn) {
          btn.hidden = false;
          btn.setAttribute('aria-expanded', 'false');
        });
      }
      if (dirty) { return; }
      var values = {draft_title: draft.title, draft_body: draft.body,
                    draft_project: draft.project};
      Object.keys(values).forEach(function (name) {
        var input = formEl.querySelector('[name="' + name + '"]');
        if (input) {
          input.value = values[name] || '';
          input.defaultValue = values[name] || '';
        }
      });
    });
  }

  /* A tick refused because the note has no draft to review (yet): leave no unticked box
     that can only be refused again. The answer carries the row as it is now — "Drafting…",
     or the failed-draft line — and without it the row says a draft is coming, unless it
     already shows why there is none. */
  function nothingToReview(row, item) {
    if (item && item.id) {
      applyItem(row, item);
      return;
    }
    show(row, 'label.review', false);
    var explained = false;
    each(row, '.draft-failed-line, .no-transcript', function (node) {
      if (!node.hidden) { explained = true; }
    });
    if (!explained) { show(row, '.draft-pending', true); }
  }

  /* ------------------------------------------------------------------ */
  /* Reviewed checkbox                                                  */
  /* ------------------------------------------------------------------ */

  each(document, '.review-box', function (box) {
    box.addEventListener('change', function () {
      if (box.disabled) { return; }      // one request per tap
      var row = rowOf(box);
      var wanted = !!box.checked;
      rowError(row, '');
      setBusy(row, true, box);
      send(box.getAttribute('data-id'), 'PATCH', {reviewed: wanted}).then(function (answer) {
        setBusy(row, false, box);
        if (!answer.ok) {
          /* Nothing was written (a 409: a new draft landed, or there is nothing to
             review yet). The box goes back to what is TRUE, and the row says why. */
          box.checked = !wanted;
          if (answer.body.code === 'nothing_to_review') { nothingToReview(row, answer.body.item); }
          rowError(row, 'Not saved — ' + reasonOf(answer));
          return;
        }
        applyItem(row, answer.body);
      }, function () {
        /* Put the box back and SAY so. A checkbox that silently un-ticks itself on the
           next page load is how a review decision gets lost. */
        setBusy(row, false, box);
        box.checked = !wanted;
        rowError(row, 'Not saved — ' + OFFLINE);
      });
    });
  });

  /* ------------------------------------------------------------------ */
  /* Close / Reopen a note                                              */
  /* ------------------------------------------------------------------ */

  /* Both buttons are rendered, hidden (they need this script); the one for the row's
     state is shown here, and an answer swaps them. */
  each(document, '.toggle-state', function (btn) {
    var row = rowOf(btn);
    var state = row ? row.getAttribute('data-state')
                    : (btn.getAttribute('data-to') === 'open' ? 'closed' : 'open');
    var to = btn.getAttribute('data-to') === 'open' ? 'open' : 'closed';
    btn.hidden = !toggleShown(state, to);
    btn.addEventListener('click', function () {
      if (btn.disabled) { return; }      // one request per tap
      rowError(row, '');
      setBusy(row, true, btn);
      send(btn.getAttribute('data-id'), 'PATCH', {state: to}).then(function (answer) {
        setBusy(row, false, btn);
        if (!answer.ok) {
          rowError(row, 'Not saved — ' + reasonOf(answer));
          return;
        }
        applyItem(row, answer.body);
        /* The pressed button is now hidden: keep the focus on the row's other one. */
        if (btn.hidden && row) {
          var other = row.querySelector('.toggle-state[data-to="' +
                                        (to === 'closed' ? 'open' : 'closed') + '"]');
          if (other && !other.hidden && other.focus) { other.focus(); }
        }
      }, function () {
        setBusy(row, false, btn);
        rowError(row, 'Not saved — ' + OFFLINE);
      });
    });
  });

  /* ------------------------------------------------------------------ */
  /* Edit a voice note's draft                                          */
  /* ------------------------------------------------------------------ */

  /* The form is rendered server-side, hidden, inside each voice row. This only
     shows/hides it and PATCHes its three fields; the answer updates the row in place.
     Nothing here builds markup. */
  each(document, '.edit-draft', function (btn) {
    var id = btn.getAttribute('data-id');
    var formEl = document.getElementById('edit-' + id);
    if (!formEl) { return; }
    btn.hidden = false;          // only shown once it actually works
    var cancel = formEl.querySelector('.edit-cancel');
    var save = formEl.querySelector('.edit-save');
    function field(name) {
      var input = formEl.querySelector('[name="' + name + '"]');
      return input ? String(input.value || '') : '';
    }
    function close() {
      formEl.hidden = true;
      btn.hidden = false;
      btn.setAttribute('aria-expanded', 'false');
    }
    btn.addEventListener('click', function () {
      formEl.hidden = false;
      btn.hidden = true;
      btn.setAttribute('aria-expanded', 'true');
      var first = formEl.querySelector('[name="draft_title"]');
      if (first && first.focus) { first.focus(); }
    });
    if (cancel) {
      cancel.addEventListener('click', function () {
        /* Put the fields back to the saved draft, so a cancelled edit cannot be saved
           by accident later. */
        if (formEl.reset) { formEl.reset(); }
        close();
      });
    }
    formEl.addEventListener('submit', function (event) {
      event.preventDefault();
      if (save && save.disabled) { return; }   // one request per tap
      var row = rowOf(btn);
      rowError(row, '');
      var project = field('draft_project').trim();
      setBusy(row, true, save);
      send(id, 'PATCH', {draft_title: field('draft_title'),
                         draft_body: field('draft_body'),
                         draft_project: project || null}).then(function (answer) {
        setBusy(row, false, save);
        if (!answer.ok) {
          /* Keep the form open with what was typed: an edit is never thrown away. */
          rowError(row, 'Not saved — ' + reasonOf(answer));
          return;
        }
        /* Saved: these values are no longer unsaved input. */
        each(formEl, 'input, textarea', function (input) { input.defaultValue = input.value; });
        close();
        applyItem(row, answer.body);
      }, function () {
        setBusy(row, false, save);
        rowError(row, 'Not saved — ' + OFFLINE);
      });
    });
  });

  /* ------------------------------------------------------------------ */
  /* Delete (the only way a recording of Graham's voice leaves the box)  */
  /* ------------------------------------------------------------------ */

  var shownEl = document.getElementById('items-shown');

  each(document, '.delete-item', function (btn) {
    btn.hidden = false;          // only shown once it actually works
    btn.addEventListener('click', function () {
      if (btn.disabled) { return; }      // one request per tap
      var id = btn.getAttribute('data-id');
      var drivePath = btn.getAttribute('data-drive-path');
      /* A confirm step, because this destroys the row AND its audio and
         there is no undo. `confirm` is deliberate: a bespoke modal would be
         more DOM for no more safety. It names what Delete does NOT reach: the
         Drive backup of the recording is add-only. */
      /* "and its recording" only while the note still has one HERE (an expired or
         missing recording is already gone) — but its Drive copy may still exist. */
      var hasAudio = btn.getAttribute('data-has-audio') === '1';
      var question = drivePath
        ? (hasAudio
             ? 'Delete this note and its recording from the Hub? This cannot be undone.' +
               '\n\nA copy of its recording already backed up stays in Google Drive ('
             : 'Delete this note from the Hub? This cannot be undone.' +
               '\n\nAny backed-up copy of the recording stays in Google Drive (') +
          drivePath + ' in the backup folder) until you remove it there by hand.'
        : 'Delete this note? This cannot be undone.';
      if (!window.confirm(question)) { return; }
      var row = rowOf(btn);
      rowError(row, '');
      setBusy(row, true, btn);
      send(id, 'DELETE').then(function (answer) {
        /* 404: it is already gone, which is what was asked for. */
        if (!answer.ok && answer.status !== 404) {
          setBusy(row, false, btn);
          rowError(row, 'Not saved — ' + reasonOf(answer));
          return;
        }
        if (row && row.parentNode) { row.parentNode.removeChild(row); }
        if (answer.body.counts) {
          applyCounts(answer.body.counts, answer.body.counts_at);
        } else {
          refreshCounts();
        }
        if (shownEl) {
          shownEl.textContent = String(Math.max(0, (parseInt(shownEl.textContent, 10) || 1) - 1));
        }
        recount();
      }, function () {
        setBusy(row, false, btn);
        rowError(row, 'Not saved — ' + OFFLINE);
      });
    });
  });

  /* ------------------------------------------------------------------ */
  /* Live filtering (over rows that are already on the page)            */
  /* ------------------------------------------------------------------ */

  /* Read afresh each time: Delete removes rows. */
  function allRows() { return document.querySelectorAll('#items .item'); }
  var q = document.getElementById('filter-q');
  var source = document.getElementById('filter-source');
  var state = document.getElementById('filter-state');
  var review = document.getElementById('filter-review');
  var countEl = document.getElementById('filter-count');

  function matches(row) {
    var needle = q ? q.value.trim().toLowerCase() : '';
    if (needle && (row.getAttribute('data-text') || '').indexOf(needle) === -1) {
      return false;
    }
    if (source && source.value && row.getAttribute('data-source') !== source.value) {
      return false;
    }
    if (state && state.value && row.getAttribute('data-state') !== state.value) {
      return false;
    }
    if (review && review.checked && row.getAttribute('data-needs-review') !== '1') {
      return false;
    }
    return true;
  }

  /* "N of M shown", from the rows as they stand. A row an action just changed is NOT
     re-filtered away: it stays in view, so the change can be undone from it. */
  function recount() {
    if (!countEl) { return; }
    var rows = allRows();
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {
      if (!rows[i].hidden) { shown++; }
    }
    countEl.textContent = shown === rows.length
      ? ''
      : shown + ' of ' + rows.length + ' shown — press Apply to search every item.';
    countEl.hidden = !countEl.textContent;
  }

  function applyFilter() {
    var rows = allRows();
    for (var i = 0; i < rows.length; i++) {
      rows[i].hidden = !matches(rows[i]);
    }
    recount();
  }

  if (allRows().length) {
    if (q) { q.addEventListener('input', applyFilter); }
    if (source) { source.addEventListener('change', applyFilter); }
    if (state) { state.addEventListener('change', applyFilter); }
    if (review) { review.addEventListener('change', applyFilter); }
  }
})();
