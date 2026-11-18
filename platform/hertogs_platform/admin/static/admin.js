// Admin page helpers. Served from the admin origin; the CSP allows only this file.
(function () {
  "use strict";
  var csrf = document.querySelector('meta[name="csrf-token"]').getAttribute("content");
  var pageOwner = document.querySelector('meta[name="admin-owner"]').content;

  // Warn before navigation loses edited forms; token values are never persisted here.
  var editedForms = new Set(), formUpdates = new WeakMap(), submitting = false;
  function formValues(form) {
    return JSON.stringify(Array.from(new FormData(form)));
  }
  function savedValues(form) {
    var copy = form.cloneNode(true);
    HTMLFormElement.prototype.reset.call(copy);
    return formValues(copy);
  }
  function clearFormEdit(form) {
    editedForms.delete(form);
    form.classList.remove('unsaved-form');
    form.removeAttribute('data-stale-form');
    var note = form.querySelector('[data-unsaved-note]');
    if (note) { note.remove(); }
  }
  function markEdited(event) {
    var form = event.target.closest('form');
    if (!form) { return; }
    if (formValues(form) === savedValues(form)) {
      clearFormEdit(form);
      if (formUpdates.has(form)) {
        var latest = formUpdates.get(form);
        formUpdates.delete(form);
        if (latest) { mergeNode(form, latest); }
        else { form.remove(); }
      }
      return;
    }
    editedForms.add(form); form.classList.add('unsaved-form');
    if (!form.querySelector('[data-unsaved-note]')) {
      var note = document.createElement('p'); note.className = 'unsaved-note';
      note.setAttribute('data-unsaved-note', ''); note.textContent = 'Unsaved changes in this form';
      form.appendChild(note);
    }
  }
  document.querySelector('main').addEventListener('input', markEdited);
  document.querySelector('main').addEventListener('change', markEdited);
  window.addEventListener('beforeunload', function (event) {
    if (!submitting && editedForms.size) { event.preventDefault(); event.returnValue = ''; }
  });
  function revealSection() {
    var target = document.getElementById(window.location.hash.slice(1));
    if (!target) { return; }
    for (var parent = target.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS') { parent.open = true; }
    }
  }
  window.addEventListener('hashchange', revealSection); revealSection();

  // Delegation also covers forms added by a live update.
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (event.defaultPrevented) { return; }
    if (form.hasAttribute('data-stale-form')) {
      event.preventDefault();
      toast('This form changed on the server. Copy your edits, then reload before saving.', {level: 'warn'});
      return;
    }
    if (Array.from(editedForms).some(function (other) { return other !== form && other.isConnected; }) &&
        !window.confirm('Submitting this form leaves unsaved changes in another form. Continue and discard those other edits?')) {
      event.preventDefault(); return;
    }
    if (form.hasAttribute('data-confirm-click') && !window.confirm(form.getAttribute('data-confirm-click'))) {
      event.preventDefault(); return;
    }
    if (form.hasAttribute('data-prompt')) {
      var value = window.prompt(form.getAttribute("data-prompt"));
      if (!value) { event.preventDefault(); return; }
      form.querySelector('input[name="' + form.getAttribute("data-prompt-field") + '"]').value = value;
    }
    submitting = true;
  });

  function get(url) {
    return fetch(url, {credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'application/json'}}).then(function (response) {
      if (!response.ok) { throw new Error('HTTP ' + response.status); }
      return response.json();
    });
  }
  function duration(seconds) {
    seconds = Math.max(0, Math.round(seconds || 0));
    if (seconds < 60) { return seconds + ' s'; }
    if (seconds < 3600) { return Math.floor(seconds / 60) + ' min ' + ('0' + (seconds % 60)).slice(-2) + ' s'; }
    return Math.floor(seconds / 3600) + ' h ' + ('0' + Math.floor(seconds % 3600 / 60)).slice(-2) + ' min';
  }
  function updateMeters() {
    // The page's security policy allows no inline styles.
    document.querySelectorAll('.meter span[data-width]').forEach(function (bar) {
    bar.style.width = Math.max(0, Math.min(100, Number(bar.getAttribute('data-width')) || 0)) + '%';
    });
  }
  updateMeters();

  // ---- Popups: small messages bottom right, on every page.
  var toasts = document.getElementById('toasts');
  function toast(text, options) {
    options = options || {};
    if (!toasts) { return null; }
    var box = document.createElement('div'), words = document.createElement('span'), close = document.createElement('button');
    box.className = 'toast ' + (options.level || '');
    words.textContent = text; box.appendChild(words);
    if (options.link) {
      var link = document.createElement('a'); link.href = options.link; link.textContent = options.linkText || 'Open';
      box.appendChild(link);
    }
    if (options.action) {
      var act = document.createElement('button'); act.type = 'button'; act.className = 'small primary';
      act.textContent = options.actionText || 'Reload'; act.addEventListener('click', options.action); box.appendChild(act);
    }
    close.type = 'button'; close.className = 'small'; close.textContent = 'Close'; close.setAttribute('aria-label', 'Close this message');
    close.addEventListener('click', function () { box.remove(); }); box.appendChild(close);
    toasts.appendChild(box);
    while (toasts.children.length > 5) { toasts.firstChild.remove(); }
    // Good news leaves by itself; problems and questions stay until closed.
    if (options.level === 'ok') { window.setTimeout(function () { box.remove(); }, 15000); }
    return box;
  }
  function formAction(form) {
    var action = new URL(form.getAttribute('action'), window.location.href);
    action.searchParams.delete('_csrf');
    return action.pathname + action.search;
  }
  // Merge server-rendered pages while retaining edited values and review tokens.
  function nodeKey(node) {
    if (node.nodeType !== 1) { return ''; }
    if (node.id) { return 'id:' + node.id; }
    if (node.hasAttribute('data-live-key')) { return node.tagName + ':' + node.getAttribute('data-live-key'); }
    if (node.tagName === 'FORM') {
      return 'form:' + formAction(node) + ':' + Array.from(node.querySelectorAll('input[type="hidden"]'))
        .filter(function (field) { return field.name !== 'revision'; })
        .map(function (field) { return field.name + '=' + field.value; }).join('&');
    }
    if (node.matches('.card, details')) {
      var title = node.querySelector('h2, summary');
      if (title) { return node.tagName + ':' + title.textContent; }
    }
    return '';
  }
  function protectedForms(node) {
    return node.nodeType === 1 && Array.from(editedForms).some(function (form) { return node === form || node.contains(form); });
  }
  function setFormConflict(form, latest) {
    formUpdates.set(form, latest);
    form.toggleAttribute('data-stale-form', true);
    var note = form.querySelector('[data-unsaved-note]');
    if (note) { note.textContent = 'This form changed on the server. Undo your edits to load the latest settings, or copy them before reloading.'; }
  }
  function formSignature(form) {
    // Compare saved settings and available choices, independent of app status
    // prose, edge-injected markup, and the user's current selections.
    var fields = Array.from(form.elements, function (field) {
      var options = field.options ? Array.from(field.options, function (option) {
        return [option.value, option.matches(':disabled')];
      }) : null;
      return [field.name, field.type, field.matches(':disabled'), field.required,
        field.getAttribute('value'), options];
    });
    return JSON.stringify([formAction(form), form.method, form.getAttribute('data-confirm-click'), savedValues(form), fields]);
  }
  function mergeNode(current, fresh) {
    if (current.nodeType !== 1) {
      if (current.nodeValue !== fresh.nodeValue) { current.nodeValue = fresh.nodeValue; }
      return;
    }
    if (current.tagName === 'FORM' && editedForms.has(current)) {
      // Include settings without a revision field: saving a log edit must not
      // silently undo protection another tab changed in the meantime.
      if (formSignature(current) !== formSignature(fresh)) {
        setFormConflict(current, fresh);
      } else {
        formUpdates.delete(current);
        current.removeAttribute('data-stale-form');
        current.querySelector('[data-unsaved-note]').textContent = 'Unsaved changes in this form';
        // Only the CSRF token may rotate while the form is edited.
        current.setAttribute('action', fresh.getAttribute('action'));
      }
      return;
    }
    // Its dedicated poller owns the streaming log and status until it completes.
    if (current.hasAttribute('data-live-operation') && current.getAttribute('data-live-operation') === fresh.getAttribute('data-live-operation')) { return; }
    Array.from(current.attributes).forEach(function (attr) {
      if (current.tagName === 'DETAILS' && attr.name === 'open') { return; }
      if (!fresh.hasAttribute(attr.name)) { current.removeAttribute(attr.name); }
    });
    Array.from(fresh.attributes).forEach(function (attr) {
      if (current.tagName === 'DETAILS' && attr.name === 'open') { return; }
      if (current.getAttribute(attr.name) !== attr.value) { current.setAttribute(attr.name, attr.value); }
    });
    var old = Array.from(current.childNodes), used = new Set(), cursor = current.firstChild;
    Array.from(fresh.childNodes).forEach(function (child) {
      var key = nodeKey(child);
      var match = old.find(function (item) {
        return !used.has(item) && item.nodeType === child.nodeType && item.nodeName === child.nodeName &&
          (key ? nodeKey(item) === key : !nodeKey(item));
      });
      if (match) {
        used.add(match);
        if (match !== cursor) { current.insertBefore(match, cursor); }
        mergeNode(match, child); cursor = match.nextSibling;
      } else {
        current.insertBefore(child.cloneNode(true), cursor);
      }
    });
    old.forEach(function (item) {
      if (!used.has(item)) {
        if (protectedForms(item)) {
          Array.from(editedForms).forEach(function (form) { if (item === form || item.contains(form)) { setFormConflict(form, null); } });
        } else { item.remove(); }
      }
    });
    // Attributes alone do not reset the value/checked property of reused controls.
    if (current.matches('input, textarea, select')) {
      if (current.value !== fresh.value) { current.value = fresh.value; }
      if (current.tagName === 'INPUT' && current.checked !== fresh.checked) { current.checked = fresh.checked; }
    }
  }
  var refreshing = false, refreshTimer, refreshFailures = 0, refreshWarning;
  var canRefresh = document.querySelector('main[data-live-page]');
  function refreshPage() {
    if (!canRefresh || refreshing || submitting || document.hidden) { return Promise.resolve(); }
    refreshing = true;
    return fetch(window.location.pathname + window.location.search, {
      credentials: 'same-origin', cache: 'no-store', headers: {'Accept': 'text/html'}
    }).then(function (response) {
      if (!response.ok || response.redirected) { throw new Error('Page unavailable'); }
      return response.text();
    }).then(function (html) {
      if (submitting) { return; }
      var fresh = new DOMParser().parseFromString(html, 'text/html');
      var main = fresh.querySelector('main[data-live-page]'), token = fresh.querySelector('meta[name="csrf-token"]');
      var nextOwner = fresh.querySelector('meta[name="admin-owner"]');
      // A login/error page must never replace an authenticated admin page.
      if (!main || !token || !nextOwner || nextOwner.content !== pageOwner || main.getAttribute('data-live-page') !== canRefresh.getAttribute('data-live-page')) {
        throw new Error('Page unavailable');
      }
      csrf = token.content;
      var x = window.scrollX, y = window.scrollY;
      mergeNode(canRefresh, main); updateMeters(); startLiveOperations();
      window.scrollTo(x, y);
      refreshFailures = 0;
      if (refreshWarning) { refreshWarning.remove(); refreshWarning = null; }
    }).catch(function () {
      refreshFailures += 1;
      if (refreshFailures === 3) { refreshWarning = toast('Live updates are unavailable. Your edits are kept; retrying automatically.', {level: 'warn'}); }
    }).finally(function () { refreshing = false; });
  }
  function scheduleRefresh() {
    window.clearTimeout(refreshTimer);
    refreshPage().finally(function () { refreshTimer = window.setTimeout(scheduleRefresh, document.hidden ? 30000 : 5000); });
  }
  if (canRefresh) {
    refreshTimer = window.setTimeout(scheduleRefresh, 5000);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) { scheduleRefresh(); } });
  }

  var wantsNotifications = false;
  try { wantsNotifications = localStorage.getItem('hertogs-notify') === '1'; } catch (_) {}
  function desktopNotice(text, link) {
    if (!wantsNotifications || !document.hidden || !('Notification' in window) || Notification.permission !== 'granted') { return; }
    try {
      var note = new Notification('MCP Hosting Workspace', {body: text});
      note.onclick = function () { window.focus(); if (link) { window.location.href = link; } };
    } catch (_) {}
  }
  var notifyButton = document.querySelector('[data-enable-notifications]');
  if (notifyButton) {
    var notifyState = document.querySelector('[data-notification-state]');
    var showNotifyState = function () {
      var supported = 'Notification' in window;
      notifyState.textContent = !supported ? 'This browser has no notifications.' :
        Notification.permission === 'denied' ? 'Blocked in the browser settings for this site.' :
        wantsNotifications && Notification.permission === 'granted' ? 'On.' : 'Off.';
      notifyButton.textContent = wantsNotifications && supported && Notification.permission === 'granted' ?
        'Stop Windows notifications' : 'Also show them as Windows notifications';
    };
    notifyButton.addEventListener('click', function () {
      if (!('Notification' in window)) { return; }
      if (wantsNotifications && Notification.permission === 'granted') {
        wantsNotifications = false; try { localStorage.removeItem('hertogs-notify'); } catch (_) {}
        showNotifyState(); return;
      }
      Notification.requestPermission().then(function (permission) {
        wantsNotifications = permission === 'granted';
        try { if (wantsNotifications) { localStorage.setItem('hertogs-notify', '1'); } } catch (_) {}
        showNotifyState();
      });
    });
    showNotifyState();
  }

  // What happened since this tab last looked: finished work, new versions, asks, warnings.
  var noticeCursor = null;
  try { noticeCursor = sessionStorage.getItem('hertogs-notice-cursor'); } catch (_) {}
  var onAppPage = (window.location.pathname.match(/^\/apps\/([^\/]+)$/) || [])[1];
  function pollNotices() {
    get('/api/notices' + (noticeCursor ? '?after=' + encodeURIComponent(noticeCursor) : '')).then(function (data) {
      (data.notices || []).forEach(function (notice) {
        // The live panel on this page already reports its own operation.
        if (notice.finished && document.querySelector('[data-live-operation]') && notice.app === onAppPage) { return; }
        toast(notice.text, {level: notice.level, link: notice.link});
        desktopNotice(notice.text, notice.link);
      });
      if (data.latest !== undefined) {
        noticeCursor = String(data.latest);
        try { sessionStorage.setItem('hertogs-notice-cursor', noticeCursor); } catch (_) {}
      }
    }).catch(function () {}).then(function () {
      window.setTimeout(pollNotices, document.hidden ? 60000 : 20000);
    });
  }
  if (toasts) { window.setTimeout(pollNotices, noticeCursor ? 1500 : 200); }

  // ---- The operation that is running: phase, time and log, every two seconds.
  var observedOperations = new WeakSet();
  function startLiveOperations() {
    document.querySelectorAll('[data-live-operation]').forEach(function (liveOperation) {
    if (observedOperations.has(liveOperation)) { return; }
    observedOperations.add(liveOperation);
    var liveUrl = liveOperation.getAttribute('data-live-operation'), liveLabel = liveOperation.getAttribute('data-live-label');
    var liveLog = liveOperation.querySelector('[data-live-log]'), liveNote = liveOperation.querySelector('[data-live-note]');
    var livePhase = liveOperation.querySelector('[data-live-phase]'), liveDetail = liveOperation.querySelector('[data-live-detail]');
    var liveTime = liveOperation.querySelector('[data-live-time]'), liveOffset = 0, liveFailures = 0;
    var pollLive = function () {
      if (!liveOperation.isConnected || submitting) { return; }
      get(liveUrl + '?offset=' + liveOffset).then(function (data) {
        liveFailures = 0;
        if (data.offset < liveOffset) { liveLog.textContent = ''; }   // the log was shortened: start again
        if (data.text) {
          var follow = liveLog.scrollTop + liveLog.clientHeight >= liveLog.scrollHeight - 30;
          liveLog.appendChild(document.createTextNode(data.text));
          if (follow) { liveLog.scrollTop = liveLog.scrollHeight; }
        }
        liveOffset = data.next_offset || 0;
        var active = data.status === 'queued' || data.status === 'running';
        livePhase.textContent = active ? (data.phase || (data.status === 'queued' ? 'Waiting for its turn' : 'Running')) :
          data.status === 'succeeded' ? 'Finished' : data.status === 'failed' ? 'Failed' + (data.failed_phase ? ' at ' + data.failed_phase : '') : 'Interrupted';
        livePhase.setAttribute('data-state', data.status);
        liveDetail.textContent = active ? (data.phase_detail || '') + (data.phase_seconds !== undefined ? ' (' + duration(data.phase_seconds) + ' in this step)' : '') : (data.error || '');
        liveTime.textContent = duration(data.elapsed_seconds) + ' in total';
        if (active || data.next_offset < data.size) { window.setTimeout(pollLive, active ? 2000 : 200); return; }
        liveNote.textContent = data.log_cut ? 'The log was long: its middle part was left out.' : 'The log is complete.';
        var words = liveLabel + (data.status === 'succeeded' ? ' finished.' : data.status === 'failed' ? ' failed.' : ' was interrupted.');
        toast(words + (data.status === 'succeeded' ? '' : ' Open the operation log for details.'), {level: data.status === 'succeeded' ? 'ok' : 'error'});
        refreshPage();
      }).catch(function () {
        liveFailures += 1;
        liveNote.textContent = 'The status is unavailable for a moment; trying again. Your form edits are kept.';
        window.setTimeout(pollLive, Math.min(15000, 2000 * liveFailures));
      });
    };
    pollLive();
    });
  }
  startLiveOperations();

  // ---- "Measure now" for storage: ask for a scan, then wait for it without blocking the page.
  document.addEventListener('click', function (event) {
    var storageButton = event.target.closest('[data-storage-refresh]');
    if (!storageButton || storageButton.disabled) { return; }
    var storageUrl = '/api/storage?app=' + encodeURIComponent(storageButton.getAttribute('data-storage-refresh'));
    var measured = document.querySelector('[data-storage-measured]'), storageTries = 0;
    var waitForScan = function () {
      get(storageUrl).then(function (data) {
        storageTries += 1;
        if (data.running && storageTries < 90) { window.setTimeout(waitForScan, 2000); return; }
        storageButton.disabled = false; storageButton.textContent = 'Measure now';
          if (data.error) { measured.textContent = (data.measured || 'not yet') + '; measurement failed (' + data.error + ')'; return; }
        var app = data.app || {}, summary = document.querySelector('[data-storage-summary]');
        var bytes = function (value) {
          if (value === null || value === undefined) { return 'not measured'; }
          var unit = 0, units = ['B', 'KB', 'MB', 'GB', 'TB'];
          while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
          return value.toFixed(unit ? 1 : 0) + ' ' + units[unit];
        };
        if (summary) {
          summary.textContent = bytes(app.total_bytes) + (app.budget_bytes ? ' of ' + bytes(app.budget_bytes) + ' budget' : '') +
            (app.percent !== null && app.percent !== undefined ? ' (' + app.percent + '%)' : '');
          summary.className = app.level === 'error' ? 'error-text' : app.level === 'warn' ? 'warn' : '';
        }
        document.querySelectorAll('[data-storage-part]').forEach(function (node) {
          node.textContent = bytes(app[node.getAttribute('data-storage-part')]);
        });
        var meter = document.querySelector('[data-storage-meter]');
        if (meter) {
          meter.hidden = app.percent === null || app.percent === undefined;
          meter.className = 'meter ' + (app.level || '');
          meter.setAttribute('aria-label', app.percent + ' percent of the storage budget');
          meter.querySelector('span').style.width = Math.max(0, Math.min(100, app.percent || 0)) + '%';
        }
        measured.textContent = data.measured_at ? 'just now' : 'not yet';
      }).catch(function () { storageButton.disabled = false; storageButton.textContent = 'Measure now'; });
    };
      storageButton.disabled = true; storageButton.textContent = 'Measuring…'; storageTries = 0;
      fetch(storageUrl, {method: 'POST', credentials: 'same-origin', headers: {'X-CSRF-Token': csrf}})
        .then(function () { window.setTimeout(waitForScan, 1500); })
        .catch(function () { storageButton.disabled = false; storageButton.textContent = 'Measure now'; });
  });

  // Chunked, resumable uploads keep individual requests below tunnel limits.
  var box = document.querySelector("[data-upload-dir]");
  if (!box) { return; }
  var dir = box.getAttribute("data-upload-dir");
  var input = document.getElementById("upload-input");
  var overwrite = document.getElementById("upload-overwrite");
  var progress = document.getElementById("upload-progress");

  function api(method, url, body, isJson) {
    var headers = { "X-CSRF-Token": csrf };
    if (isJson) { headers["Content-Type"] = "application/json"; }
    return fetch(url, { method: method, headers: headers, body: body, credentials: "same-origin" })
      .then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) { throw new Error(data.error || ("HTTP " + response.status)); }
          return data;
        });
      });
  }

  function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  async function sendChunk(id, file, offset, size) {
    for (var attempt = 1; attempt <= 4; attempt++) {
      try {
        var blob = file.slice(offset, Math.min(offset + size, file.size));
        var result = await api("PUT", "/api/uploads/" + id + "?offset=" + offset, blob, false);
        return result.received;
      } catch (error) {
        if (attempt === 4) { throw error; }
        await sleep(1000 * attempt * attempt);
        // The server may have stored the chunk before the connection broke.
        var status = await api("GET", "/api/uploads/" + id, undefined, false);
        if (status.received > offset) { return status.received; }
      }
    }
  }

  function cacheKey(file) {
    return "hertogs-upload:" + JSON.stringify([dir, file.name, file.size, file.lastModified]);
  }
  function remember(key, data) { try { localStorage.setItem(key, JSON.stringify(data)); } catch (_) {} }
  function forget(key) { try { localStorage.removeItem(key); } catch (_) {} }
  async function upload(file) {
    var line = document.createElement("div"), key = cacheKey(file), start, paused = false, cancelled = false;
    progress.appendChild(line);
    var label = document.createElement("span"), pause = document.createElement("button"), cancel = document.createElement("button");
    pause.type = cancel.type = "button"; pause.textContent = "Pause"; cancel.textContent = "Cancel";
    line.append(label, pause, cancel);
    pause.onclick = function () { paused = true; };
    cancel.onclick = async function () {
      paused = true;
      try {
        if (start) { await api("DELETE", "/api/uploads/" + start.upload_id, undefined, false); }
        cancelled = true; forget(key); label.textContent = file.name + ": cancelled";
      } catch (error) { label.textContent = file.name + ": cancellation failed (" + error.message + ")"; }
    };
    label.textContent = file.name + ": starting ";
    try {
      var cached;
      try { cached = JSON.parse(localStorage.getItem(key)); } catch (_) {}
      if (cached) {
        try {
          var status = await api("GET", "/api/uploads/" + cached.upload_id, undefined, false);
          if (status.name === file.name && status.size === file.size && status.dir === dir) {
            start = cached; start.received = status.received;
          }
        } catch (_) { forget(key); }
      }
      if (!start) { start = await api("POST", "/api/uploads", JSON.stringify({ dir: dir, name: file.name, size: file.size }), true); }
      remember(key, start);
      var offset = start.received || 0;
      while (offset < file.size && !paused) {
        offset = await sendChunk(start.upload_id, file, offset, start.chunk_size);
        label.textContent = file.name + ": " + Math.floor((offset / file.size) * 100) + "% ";
      }
      if (cancelled) { return false; }
      if (paused) { label.textContent = file.name + ": paused — select this file again to resume "; return false; }
      await api("POST", "/api/uploads/" + start.upload_id + "/finish", JSON.stringify({ overwrite: overwrite.checked }), true);
      forget(key); label.textContent = file.name + ": done "; pause.remove(); cancel.remove();
      return true;
    } catch (error) {
      if (cancelled) { return false; }
      label.textContent = file.name + ": " + error.message + ". Select this file again to resume. ";
      return false;
    }
  }

  document.getElementById("upload-button").addEventListener("click", async function () {
    var button = this, files = Array.prototype.slice.call(input.files || []), complete = true;
    button.disabled = true;
    try { for (var i = 0; i < files.length; i++) { if (!await upload(files[i])) { complete = false; } } }
    finally { button.disabled = false; }
    if (files.length && complete) { window.location.reload(); }
  });

})();
