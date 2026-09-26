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

   1. It NEVER assigns innerHTML and is never handed a JSON blob of transcripts.
      Every row is rendered server-side by Jinja; this script only shows, hides
      and re-labels DOM that already exists. Text it writes goes through
      textContent.
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

  /* ------------------------------------------------------------------ */
  /* Submit                                                             */
  /* ------------------------------------------------------------------ */

  if (form && window.fetch && window.FormData) {
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      fail('');
      var data = new FormData(form);
      if (recordedBlob) {
        data.set('audio', recordedBlob, 'note.' + extensionFor(recordedBlob.type));
        data.set('audio_secs', String(recordedSecs));
      }
      if (!recordedBlob && !String(data.get('text') || '').trim()) {
        fail('Say something or type something first.');
        if (textEl) { textEl.focus(); }
        return;
      }
      if (submitBtn) { submitBtn.disabled = true; }
      status('Saving…');
      window.fetch(form.action, {
        method: 'POST',
        body: data,
        credentials: 'same-origin',
        headers: {'Accept': 'application/json'}
      }).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          if (!response.ok) {
            throw new Error(body.error || ('save failed (' + response.status + ')'));
          }
          /* Say it landed BEFORE reloading. A voice note has no transcript yet
             (nothing leaves this box, so Whisper on the Mac does it on its next
             pass), and "did that even record?" is the question this page has to
             answer out loud. */
          status(recordedBlob
                 ? 'Saved — transcribing… Whisper picks it up within a few minutes.'
                 : 'Saved.');
          /* Reload rather than building a row here: rows are server-rendered,
             and that is the rule that keeps untrusted text out of the DOM by
             any path but Jinja's escaping. The query string (the filters) is
             preserved. */
          window.setTimeout(function () { window.location.reload(); }, 700);
        });
      }).catch(function (err) {
        if (submitBtn) { submitBtn.disabled = false; }
        status('');
        fail(err && err.message ? err.message : 'Could not save — try again.');
      });
    });
  }

  /* ------------------------------------------------------------------ */
  /* Reviewed checkbox                                                  */
  /* ------------------------------------------------------------------ */

  var boxes = document.querySelectorAll('.review-box');
  for (var b = 0; b < boxes.length; b++) {
    (function (box) {
      box.addEventListener('change', function () {
        var id = box.getAttribute('data-id');
        var wanted = box.checked;
        box.disabled = true;
        window.fetch('/api/v1/inbox/items/' + encodeURIComponent(id), {
          method: 'PATCH',
          credentials: 'same-origin',
          headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
          body: JSON.stringify({reviewed: wanted})
        }).then(function (response) {
          if (!response.ok) { throw new Error('save failed'); }
          var row = document.getElementById('item-' + id);
          if (row) { row.setAttribute('data-reviewed', wanted ? '1' : '0'); }
        }).catch(function () {
          /* Put the box back and SAY so. A checkbox that silently un-ticks
             itself on the next page load is how a review decision gets lost. */
          box.checked = !wanted;
          fail('Could not save that review tick — check your connection.');
        }).then(function () {
          box.disabled = false;
        });
      });
    })(boxes[b]);
  }

  /* ------------------------------------------------------------------ */
  /* Delete (the only way a recording of Graham's voice leaves the box)  */
  /* ------------------------------------------------------------------ */

  var deleters = document.querySelectorAll('.delete-item');
  for (var d = 0; d < deleters.length; d++) {
    (function (btn) {
      btn.hidden = false;          // only shown once it actually works
      btn.addEventListener('click', function () {
        var id = btn.getAttribute('data-id');
        /* A confirm step, because this destroys the row AND its audio and
           there is no undo. `confirm` is deliberate: a bespoke modal would be
           more DOM for no more safety. */
        if (!window.confirm('Delete this item and its recording? This cannot ' +
                            'be undone.')) { return; }
        btn.disabled = true;
        window.fetch('/api/v1/inbox/items/' + encodeURIComponent(id), {
          method: 'DELETE',
          credentials: 'same-origin',
          headers: {'Accept': 'application/json'}
        }).then(function (response) {
          if (!response.ok && response.status !== 404) {
            throw new Error('delete failed');
          }
          var row = document.getElementById('item-' + id);
          if (row && row.parentNode) { row.parentNode.removeChild(row); }
        }).catch(function () {
          btn.disabled = false;
          fail('Could not delete that item — check your connection.');
        });
      });
    })(deleters[d]);
  }

  /* ------------------------------------------------------------------ */
  /* Live filtering (over rows that are already on the page)            */
  /* ------------------------------------------------------------------ */

  var rows = document.querySelectorAll('#items .item');
  var q = document.getElementById('filter-q');
  var source = document.getElementById('filter-source');
  var state = document.getElementById('filter-state');
  var awaiting = document.getElementById('filter-awaiting');
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
    if (awaiting && awaiting.value) {
      var key = awaiting.value === 'filing'
        ? 'data-awaiting-filing' : 'data-awaiting-transcription';
      if (row.getAttribute(key) !== '1') { return false; }
    }
    return true;
  }

  function applyFilter() {
    var shown = 0;
    for (var i = 0; i < rows.length; i++) {
      var ok = matches(rows[i]);
      rows[i].hidden = !ok;
      if (ok) { shown++; }
    }
    if (countEl) {
      countEl.textContent = shown === rows.length
        ? ''
        : shown + ' of ' + rows.length + ' shown — press Apply to search every item.';
      countEl.hidden = !countEl.textContent;
    }
  }

  if (rows.length) {
    if (q) { q.addEventListener('input', applyFilter); }
    if (source) { source.addEventListener('change', applyFilter); }
    if (state) { state.addEventListener('change', applyFilter); }
    if (awaiting) { awaiting.addEventListener('change', applyFilter); }
  }
})();
