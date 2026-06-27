(function () {
  'use strict';

  // ── Audio tracking ──────────────────────────────────────────────────────
  var refAudioEl    = document.getElementById('ref-audio');
  var tarAudioEl    = document.getElementById('tar-audio');
  var refPlayedEl   = document.getElementById('ref-audio-played');
  var tarPlayedEl   = document.getElementById('target-audio-played');

  if (refAudioEl && refPlayedEl) {
    refAudioEl.addEventListener('ended', function () {
      refPlayedEl.value = 'true';
    });
  }

  if (tarAudioEl && tarPlayedEl) {
    tarAudioEl.addEventListener('ended', function () {
      tarPlayedEl.value = 'true';
    });
  }

  // Blur audio elements as soon as they start playing, so keyboard focus
  // leaves the shadow-DOM play button.  Otherwise number-key shortcuts get
  // intercepted by the browser's native media-control handling.
  [refAudioEl, tarAudioEl].forEach(function (el) {
    if (!el) return;
    el.addEventListener('play', function () {
      el.blur();
    });
  });

  // ── Client-side validation ──────────────────────────────────────────────
  var form = document.getElementById('test-form');

  function showError(msg) {
    var el = document.getElementById('client-error');
    if (!el) {
      el = document.createElement('div');
      el.id = 'client-error';
      el.className = 'error-message';
      // Insert before the first child of the form
      if (form) form.insertBefore(el, form.firstChild);
    }
    el.textContent = msg;
    el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  function validate() {
    if (tarPlayedEl && tarPlayedEl.value !== 'true') {
      showError('Please finish listening to the audio before submitting.');
      return false;
    }
    if (refPlayedEl && refPlayedEl.value !== 'true') {
      showError('Please finish listening to both audio samples before submitting.');
      return false;
    }
    if (form && !form.querySelector('input[name="score"]:checked')) {
      showError('Please select a score before submitting.');
      return false;
    }
    var editingGroup = form && form.querySelector('input[name="editing_score"]');
    if (editingGroup && !form.querySelector('input[name="editing_score"]:checked')) {
      showError('Please select an editing effect score before submitting.');
      return false;
    }
    return true;
  }

  if (form) {
    form.addEventListener('submit', function (e) {
      if (!validate()) e.preventDefault();
    });
  }

  // ── Keyboard shortcuts ──────────────────────────────────────────────────
  // Capture phase so we intercept BEFORE the audio element's shadow DOM
  // (play button, etc.) can eat the keypress.  stopPropagation prevents
  // the event from ever reaching the shadow DOM when we handle it here.
  document.addEventListener('keydown', function (e) {
    var tag = document.activeElement.tagName;
    var mod = e.ctrlKey || e.metaKey;

    // Ctrl/Cmd+Shift+, — play first audio (Sample A)
    // Ctrl/Cmd+Shift+. — play second audio (Sample B)
    // Fall back to the other audio element on no-reference pages.
    // Shift avoids browser conflicts (e.g. Chrome Cmd+, = settings).
    if (mod && e.shiftKey && !e.altKey) {
      if (e.key === ',') {
        var audio = refAudioEl || tarAudioEl;
        if (audio) {
          e.preventDefault();
          e.stopPropagation();
          audio.play();
          return;
        }
      }
      if (e.key === '.') {
        var audio = tarAudioEl || refAudioEl;
        if (audio) {
          e.preventDefault();
          e.stopPropagation();
          audio.play();
          return;
        }
      }
    }

    // Enter submits
    if (e.key === 'Enter' && form && tag !== 'BUTTON') {
      e.preventDefault();
      e.stopPropagation();
      form.requestSubmit();
      return;
    }

    // Digit keys 1-9 select score options by position
    var digit = parseInt(e.key, 10);
    if (!isNaN(digit) && digit >= 1 && form && tag !== 'INPUT' && tag !== 'TEXTAREA') {
      var groups = form.querySelectorAll('.score-options');
      // Target the first group with no selection; fall back to the first group
      var target = null;
      for (var g = 0; g < groups.length; g++) {
        if (!groups[g].querySelector('input[type="radio"]:checked')) {
          target = groups[g];
          break;
        }
      }
      if (!target && groups.length) target = groups[0];
      if (!target) return;
      var radios = target.querySelectorAll('input[type="radio"]');
      var idx = digit - 1;
      if (idx < radios.length) {
        e.preventDefault();
        e.stopPropagation();
        radios[idx].checked = true;
        radios[idx].dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
  }, {capture: true});

  // ── Audio prefetch ──────────────────────────────────────────────────────
  function prefetchAudio() {
    var state = window.SESSION_STATE;
    if (!state || !state.prefetch_urls) return;
    state.prefetch_urls.forEach(function (url) {
      var a = new Audio();
      a.preload = 'auto';
      a.src = url;
    });
  }

  // ── localStorage cache (for network-resilient resume) ───────────────────
  function saveSession() {
    var state = window.SESSION_STATE;
    if (!state || !state.session_id) return;
    try {
      localStorage.setItem('mos_session', JSON.stringify({
        session_id: state.session_id,
        current_page: state.current_page,
        ts: Date.now()
      }));
    } catch (e) { /* storage full or private mode */ }
  }

  async function tryRestore() {
    var raw;
    try { raw = localStorage.getItem('mos_session'); } catch (e) { return; }
    if (!raw) return;

    var data;
    try { data = JSON.parse(raw); } catch (e) { return; }
    if (!data || !data.session_id) return;

    // Session is stale if older than 7 days
    if (Date.now() - (data.ts || 0) > 7 * 24 * 3600 * 1000) {
      try { localStorage.removeItem('mos_session'); } catch (e) {}
      return;
    }

    try {
      var resp = await fetch('/api/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: data.session_id })
      });
      if (resp.ok) {
        var result = await resp.json();
        window.location.href = result.redirect || '/test';
      } else {
        try { localStorage.removeItem('mos_session'); } catch (e) {}
      }
    } catch (e) { /* network error — stay on login page */ }
  }

  // ── Page-specific init ──────────────────────────────────────────────────
  var path = window.location.pathname;

  if (path === '/' || path === '') {
    // Login page: try to resume an in-progress session
    tryRestore();
  } else if (path === '/complete') {
    try { localStorage.removeItem('mos_session'); } catch (e) {}
  } else {
    // Test page: save state and kick off prefetch
    saveSession();
    prefetchAudio();
  }

})();
