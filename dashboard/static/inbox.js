/* The Inbox page script. Loaded from inbox.html only, with the same per-request
   CSP nonce the inline localizer carries (`script-src 'nonce-…'` has no 'self',
   so an external file without the nonce would simply not run).

   Three rules this file may not break, all of them load-bearing:

   1. It NEVER assigns innerHTML and is never handed a JSON blob of transcripts.
      Every row is rendered server-side by Jinja; this script only shows, hides
      and re-labels DOM that already exists. Text it writes goes through
      textContent.
   2. It NEVER plays audio from a `blob:` URL. That would need `media-src blob:`
      in the CSP, and the CSP is not being loosened for a preview — playback
      happens from /inbox/audio/<id> after the note is saved.
   3. MediaRecorder is the SOURCE OF TRUTH and outranks live speech recognition.
      On iOS Safari the two fight over the microphone; if starting recognition
      throws, or the recorder stops within ~300 ms of recognition starting, the
      live transcript is abandoned for that take and recording continues. The
      row is simply created with transcript_status='pending' and Whisper fills
      it in later. Losing a recording to a nice-to-have is not a trade worth
      making.

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
  var speech = null;
  var speechAbandoned = false;
  var userStopped = false;

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

  /* The iOS rule, in one function: speech recognition is optional and the
     recording is not, so anything that goes wrong with recognition ends
     recognition — never the take. */
  function abandonSpeech(why) {
    if (speech) {
      speech.onresult = null;
      speech.onerror = null;
      speech.onend = null;
      try { speech.stop(); } catch (e) { /* already stopped */ }
      speech = null;
    }
    if (!speechAbandoned) {
      speechAbandoned = true;
      status('Recording… live transcript off (' + why +
             ') — Whisper will transcribe it.');
    }
  }

  function startSpeech() {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      status('Recording… no live transcript in this browser; Whisper will do it.');
      return;
    }
    try {
      speech = new SR();
      speech.continuous = true;
      speech.interimResults = false;
      speech.lang = navigator.language || 'en-US';
      speech.onresult = function (event) {
        if (speechAbandoned || !textEl) { return; }
        var heard = '';
        for (var i = event.resultIndex; i < event.results.length; i++) {
          if (event.results[i].isFinal) { heard += event.results[i][0].transcript; }
        }
        if (!heard) { return; }
        textEl.value = (textEl.value ? textEl.value + ' ' : '') + heard.trim();
      };
      speech.onerror = function (event) {
        abandonSpeech((event && event.error) || 'speech error');
      };
      speech.start();
    } catch (e) {
      abandonSpeech('speech recognition would not start');
      return;
    }
    /* If the recorder has died within ~300 ms of recognition starting, the two
       are fighting over the microphone. Drop recognition and start the recorder
       again on the same open stream, so the take survives. */
    window.setTimeout(function () {
      if (userStopped || !recorder) { return; }
      if (recorder.state === 'recording') { return; }
      abandonSpeech('it interrupted the recorder');
      try {
        recorder = new window.MediaRecorder(stream);
        wireRecorder();
        chunks = [];
        startedAt = Date.now();
        recorder.start();
      } catch (e) {
        status('Recording stopped unexpectedly — please try again, or type it.');
      }
    }, 300);
  }

  function wireRecorder() {
    recorder.ondataavailable = function (event) {
      if (event.data && event.data.size) { chunks.push(event.data); }
    };
    recorder.onstop = function () {
      releaseStream();
      var type = recorder.mimeType || (chunks[0] && chunks[0].type) || 'audio/webm';
      recordedBlob = chunks.length ? new Blob(chunks, {type: type}) : null;
      recordedSecs = startedAt ? Math.round((Date.now() - startedAt) / 100) / 10 : 0;
      setRecording(false);
      if (recordedBlob) {
        /* Deliberately no preview player here: that needs a blob: URL and the
           CSP has no media-src blob:. It is playable from the row as soon as
           it is saved. */
        status('Recorded ' + recordedSecs + 's — add it to the inbox.');
      } else {
        status('Nothing was recorded — try again, or type it instead.');
      }
    };
    recorder.onerror = function () {
      status('The recorder failed — type it instead.');
      setRecording(false);
      releaseStream();
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
    fail('');
    userStopped = false;
    speechAbandoned = false;
    recordedBlob = null;
    status('Asking for the microphone…');
    navigator.mediaDevices.getUserMedia({audio: true}).then(function (granted) {
      stream = granted;
      var mime = preferredMime();
      try {
        recorder = mime ? new window.MediaRecorder(stream, {mimeType: mime})
                        : new window.MediaRecorder(stream);
      } catch (e) {
        recorder = new window.MediaRecorder(stream);
      }
      chunks = [];
      wireRecorder();
      startedAt = Date.now();
      recorder.start();
      setRecording(true);
      status('Recording…');
      /* Recognition starts only AFTER the recorder is running, so the recorder
         has the microphone first. */
      startSpeech();
    }).catch(function (err) {
      /* Never a silently dead button: say what happened and put the cursor
         where the note can still be written. */
      recBtn.disabled = true;
      status('Microphone unavailable (' + ((err && err.name) || 'denied') +
             ') — type it instead.');
      if (textEl) { textEl.focus(); }
    });
  }

  function stopRecording() {
    userStopped = true;
    if (speech) {
      try { speech.stop(); } catch (e) { /* already stopped */ }
      speech = null;
    }
    if (recorder && recorder.state !== 'inactive') {
      recorder.stop();
    } else {
      releaseStream();
      setRecording(false);
    }
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
        if (recorder && recorder.state === 'recording') { stopRecording(); }
        else { startRecording(); }
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
          /* Reload rather than building a row here: rows are server-rendered,
             and that is the rule that keeps untrusted text out of the DOM by
             any path but Jinja's escaping. The query string (the filters) is
             preserved. */
          window.location.reload();
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
