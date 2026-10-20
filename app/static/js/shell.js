/* Page shell shared by every page: live connection, job banner, settings dialog, nav. */
(function () {
  'use strict';
  const SWA = globalThis.SWA;
  const { el, $, clear, toast, openDialog, confirmDialog, withBusy, setFieldError } = SWA.ui;
  const fetchJSON = SWA.fetchJSON;

  // ---- Cached /api/system -----------------------------------------------
  let systemPromise = null;
  SWA.getSystem = (refresh) => {
    if (!systemPromise || refresh) {
      systemPromise = fetchJSON('/api/system').catch((e) => { systemPromise = null; throw e; });
    }
    return systemPromise;
  };

  // ---- Connection indicator ---------------------------------------------
  function initConnection() {
    const box = $('#conn-status');
    let timer = null;
    SWA.events.on('connection', (state) => {
      clearTimeout(timer);
      if (state === 'open') { box.hidden = true; return; }
      // Avoid flashing on brief blips.
      timer = setTimeout(() => { box.hidden = SWA.events.connection === 'open'; }, 1500);
    });
  }

  // ---- Global running-job banner ----------------------------------------
  function bannerText(run) {
    const name = run.game_name || `App ${run.appid}`;
    if (run.status === 'waiting_confirm') return `${name} is waiting for your confirmation`;
    if (run.status === 'queued') return `Queued: ${name}`;
    const verb = run.kind === 'metadata' ? 'Refreshing metadata for' : 'Archiving';
    const progress = run.total ? ` — ${run.done}/${run.total}` : run.phase ? ` — ${run.phase}` : '';
    return `${verb} ${name}${progress}`;
  }
  function initBanner() {
    const banner = $('#job-banner');
    const text = $('#job-banner-text');
    const fill = $('#job-banner-fill');
    const link = $('#job-banner-link');
    const onIndex = location.pathname === '/';
    if (onIndex) {
      link.addEventListener('click', (e) => {
        const target = document.getElementById('job');
        if (!target) return;
        e.preventDefault();
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        target.focus({ preventScroll: true });
      });
    }
    SWA.events.on('state', (s) => {
      const run = s.current;
      const show = SWA.isActive(run);
      banner.hidden = !show;
      if (!show) return;
      text.textContent = bannerText(run);
      banner.classList.toggle('warn', run.status === 'waiting_confirm');
      const pct = run.total ? Math.min(100, (run.done / run.total) * 100) : 0;
      fill.style.width = `${pct}%`;
    });
  }

  // ---- Nav highlight ----------------------------------------------------
  function initNav() {
    const update = () => {
      const isHistory = location.pathname === '/' && location.hash === '#history';
      document.querySelectorAll('[data-nav]').forEach((a) => {
        const active = a.dataset.nav === 'history' ? isHistory : location.pathname === '/' && !isHistory;
        if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
      });
    };
    update();
    window.addEventListener('hashchange', update);
  }

  // ---- Settings dialog --------------------------------------------------
  const S = {};
  let clearKey = false;
  let loadedSettings = null;

  function initSettings() {
    const ids = {
      dialog: 'settings-dialog', form: 'settings-form', loading: 'settings-loading', content: 'settings-content',
      error: 'settings-error', apiKey: 'set-api-key', apiKeyClear: 'set-api-key-clear', apiKeyState: 'set-api-key-state',
      backend: 'set-backend', backendHint: 'set-backend-hint', username: 'set-username', httpDirect: 'set-http-direct',
      retries: 'set-retries', stall: 'set-stall', confirm: 'set-confirm', ttl: 'set-ttl', notifyUrls: 'set-notify-urls',
      notifyOn: 'set-notify-on', testNotify: 'set-test-notify', system: 'set-system', save: 'settings-save',
    };
    for (const [k, id] of Object.entries(ids)) S[k] = document.getElementById(id);
    if (!S.dialog) return;

    $('#settings-btn').addEventListener('click', openSettings);
    S.form.addEventListener('submit', (e) => { e.preventDefault(); saveSettings(); });
    S.apiKeyClear.addEventListener('click', async () => {
      const ok = await confirmDialog({
        title: 'Clear the Steam Web API key?',
        message: 'The key is removed when you save settings.',
        confirmLabel: 'Clear key', danger: true,
      });
      if (!ok) return;
      clearKey = true;
      S.apiKey.value = '';
      renderKeyState(false, true);
    });
    S.apiKey.addEventListener('input', () => { if (S.apiKey.value) clearKey = false; });
    S.backend.addEventListener('change', () => { S.backendHint.textContent = ''; });
    S.testNotify.addEventListener('click', () => withBusy(S.testNotify, 'Sending…', async () => {
      if (isDirty()) await putSettings();
      await fetchJSON('/api/settings/test-notification', { method: 'POST' });
      toast('Test notification sent.', 'success');
    }));
  }

  function renderKeyState(set, pendingClear) {
    S.apiKeyState.textContent = pendingClear ? 'Will be cleared' : set ? 'Configured' : 'Not set';
    S.apiKeyState.className = `badge ${pendingClear ? 'badge-warn' : set ? 'badge-done' : 'badge-muted'}`;
    S.apiKey.placeholder = set && !pendingClear ? 'Configured. Paste a new key to replace it' : 'Paste a key to set it';
    S.apiKeyClear.hidden = !set || pendingClear;
  }

  async function openSettings() {
    clearKey = false;
    S.error.hidden = true;
    S.loading.hidden = false;
    S.content.hidden = true;
    S.save.disabled = true;
    openDialog(S.dialog);
    try {
      const [settings, system] = await Promise.all([fetchJSON('/api/settings'), SWA.getSystem(true)]);
      loadedSettings = settings;
      fillSettings(settings, system);
      S.loading.hidden = true;
      S.content.hidden = false;
      S.save.disabled = false;
      S.apiKey.focus();
    } catch (e) {
      S.loading.hidden = true;
      S.error.textContent = `Couldn't load settings: ${e.message}`;
      S.error.hidden = false;
    }
  }

  function fillSettings(s, sys) {
    S.apiKey.value = '';
    renderKeyState(!!s.steam_api_key_set, false);
    const backends = (sys && sys.backends) || {};
    for (const opt of S.backend.options) {
      const info = backends[opt.value];
      const missing = info && !info.available;
      opt.disabled = !!missing;
      opt.textContent = (opt.value === 'steamcmd' ? 'SteamCMD' : 'DepotDownloader') + (missing ? ' (not installed)' : '');
    }
    S.backend.value = s.download_backend || 'steamcmd';
    const current = backends[S.backend.value];
    S.backendHint.textContent = current && !current.available
      ? 'The selected backend is not installed on the server. Pick another one.' : '';
    S.username.value = s.steam_username || '';
    S.httpDirect.checked = !!s.http_direct;
    S.retries.value = s.max_retries ?? '';
    S.stall.value = s.stall_timeout_min ?? '';
    S.confirm.value = s.confirm_timeout_min ?? '';
    S.ttl.value = s.metadata_ttl_hours ?? '';
    S.notifyUrls.value = (s.notify_urls || []).join('\n');
    S.notifyOn.value = s.notify_on || 'all';
    renderSystem(sys);
  }

  function renderSystem(sys) {
    clear(S.system);
    if (!sys) return;
    const backends = Object.entries(sys.backends || {}).map(([name, b]) =>
      el('span', { class: `badge ${b.available ? 'badge-done' : 'badge-muted'}`, title: b.path || '' },
        `${name}: ${b.available ? 'available' : 'missing'}`));
    const rows = [
      ['Version', sys.version || document.body.dataset.version || '–'],
      ['Server time zone', sys.tz || '–'],
      ['Password protection', sys.auth_enabled ? 'Enabled' : 'Disabled'],
      ['Backends', el('span', { class: 'row row-tight' }, backends)],
    ];
    if (sys.disk && sys.disk.total) {
      rows.push(['Downloads volume', `${SWA.ui.formatBytes(sys.disk.free)} free of ${SWA.ui.formatBytes(sys.disk.total)} (${sys.disk.path})`]);
    }
    for (const [k, v] of rows) S.system.append(el('dt', { text: k }), el('dd', null, v));
  }

  function intOrNull(input, min) {
    const v = input.value.trim();
    if (v === '') return { value: null };
    const n = Number(v);
    if (!Number.isInteger(n) || n < min) return { error: `Enter a whole number of at least ${min}.` };
    return { value: n };
  }

  function collectSettings() {
    const body = {};
    let ok = true;
    const nums = [['max_retries', S.retries, 0], ['stall_timeout_min', S.stall, 1],
      ['confirm_timeout_min', S.confirm, 1], ['metadata_ttl_hours', S.ttl, 0]];
    for (const [key, input, min] of nums) {
      const r = intOrNull(input, min);
      setFieldError(input, r.error || '');
      if (r.error) ok = false;
      else if (r.value !== null) body[key] = r.value;
    }
    body.download_backend = S.backend.value;
    body.http_direct = S.httpDirect.checked;
    body.steam_username = S.username.value.trim();
    body.notify_urls = S.notifyUrls.value.split('\n').map((l) => l.trim()).filter(Boolean);
    body.notify_on = S.notifyOn.value;
    if (clearKey) body.steam_api_key = '';
    else if (S.apiKey.value.trim()) body.steam_api_key = S.apiKey.value.trim();
    return ok ? body : null;
  }

  function isDirty() {
    const body = collectSettings();
    if (!body || !loadedSettings) return true;
    if ('steam_api_key' in body) return true;
    return Object.entries(body).some(([k, v]) => JSON.stringify(v) !== JSON.stringify(loadedSettings[k]));
  }

  async function putSettings() {
    const body = collectSettings();
    if (!body) throw new Error('Please fix the highlighted fields.');
    loadedSettings = await fetchJSON('/api/settings', { method: 'PUT', body });
    clearKey = false;
    S.apiKey.value = '';
    renderKeyState(!!loadedSettings.steam_api_key_set, false);
    return loadedSettings;
  }

  async function saveSettings() {
    S.error.hidden = true;
    await withBusy(S.save, 'Saving…', async () => {
      try {
        await putSettings();
      } catch (e) {
        S.error.textContent = e.message;
        S.error.hidden = false;
        return;
      }
      toast('Settings saved.', 'success');
      S.dialog.close('saved');
    });
  }

  // ---- Boot -------------------------------------------------------------
  initConnection();
  initBanner();
  initNav();
  initSettings();
  // Start the stream after page scripts (also deferred) have subscribed.
  document.addEventListener('DOMContentLoaded', () => {
    SWA.ui.showFlash();
    SWA.events.start();
  });
})();
