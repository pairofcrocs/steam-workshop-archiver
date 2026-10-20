/* Index page: new job form, current job + log, queue, archive, schedules, history. */
(function () {
  'use strict';
  const SWA = globalThis.SWA;
  const {
    el, $, clear, toast, confirmDialog, openDialog, withBusy, debounce, timeEl, statusBadge,
    sourceLabel, formatBytes, formatNumber, formatDuration, setFieldError,
  } = SWA.ui;
  const fetchJSON = SWA.fetchJSON;
  const events = SWA.events;
  const D = SWA.DEFAULTS;

  const PHASE_LABELS = {
    resolving: 'Resolving items', metadata: 'Fetching metadata', downloading: 'Downloading',
    images: 'Caching images', finished: 'Finished',
  };
  const TRIGGER_LABELS = { manual: 'Manual', schedule: 'Scheduled', retry: 'Retry' };
  const HISTORY_PAGE = 25;
  const LOG_MAX_LINES = 2000;

  const gameName = (o) => o.game_name || (o.appid ? `App ${o.appid}` : 'Unknown game');
  const plural = (n, word) => `${formatNumber(n)} ${word}${n === 1 ? '' : 's'}`;

  function btn(label, cls, onClick, attrs) {
    return el('button', Object.assign({ type: 'button', class: `btn ${cls || 'btn-secondary'} btn-sm`, text: label, on: { click: onClick } }, attrs || {}));
  }

  // =========================================================================
  // New job form
  // =========================================================================
  const form = $('#job-form');
  const formFields = new SWA.JobFields(form);

  function runBody(job, extra) {
    const body = {
      kind: 'archive',
      source_type: job.source_type,
      source_value: job.source_value,
      pages: job.pages,
      sort_key: job.sort_key,
      delay_min: job.delay_min,
      delay_max: job.delay_max,
      scrape_only: job.scrape_only,
      download_only: job.download_only,
      fetch_sizes: false,
      force: false,
    };
    if (job.appid) body.appid = job.appid;
    return Object.assign(body, extra || {});
  }

  function announceQueued(run, label) {
    const name = label || gameName(run);
    const s = events.state;
    const ahead = s.current && SWA.isActive(s.current) && s.current.id !== run.id;
    toast(run.status === 'queued' && ahead ? `${name} added to the queue.` : SWA.isActive(run) ? `${name} is starting…` : `${name}: already ${run.status}.`, 'success');
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const job = formFields.read();
    if (!job) return;
    const body = runBody(job, { fetch_sizes: $('#job-fetch-sizes').checked, force: $('#job-force').checked });
    withBusy($('#job-start'), 'Starting…', async () => {
      const run = await fetchJSON('/api/runs', { method: 'POST', body });
      announceQueued(run, job.game_name);
      $('#job').scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });

  $('#job-save').addEventListener('click', () => {
    const job = formFields.read();
    if (!job) return;
    openJobDialog(null, Object.assign({}, job, { schedule_type: 'cron', schedule_value: '0 3 * * *' }));
  });

  // =========================================================================
  // Log viewer: capped DOM, batched per frame, sticky-bottom scrolling
  // =========================================================================
  class LogView {
    constructor(box, jump) {
      this.box = box;
      this.jump = jump;
      this.pending = [];
      this.raf = 0;
      this.forceStick = true;
      box.addEventListener('scroll', () => { if (this.atBottom()) this.jump.hidden = true; });
      jump.addEventListener('click', () => { this.scrollToEnd(); this.box.focus(); });
    }
    atBottom() { return this.box.scrollHeight - this.box.scrollTop - this.box.clientHeight < 32; }
    scrollToEnd() { this.box.scrollTop = this.box.scrollHeight; this.jump.hidden = true; }
    reset(lines) {
      clear(this.box);
      this.pending = (lines || []).slice(-LOG_MAX_LINES);
      this.forceStick = true;
      this.jump.hidden = true;
      this.schedule();
    }
    append(line) {
      this.pending.push(line);
      if (this.pending.length > LOG_MAX_LINES) this.pending.splice(0, this.pending.length - LOG_MAX_LINES);
      this.schedule();
    }
    schedule() { if (!this.raf) this.raf = requestAnimationFrame(() => this.flush()); }
    flush() {
      this.raf = 0;
      if (!this.pending.length) return;
      const stick = this.forceStick || this.atBottom();
      this.forceStick = false;
      const frag = document.createDocumentFragment();
      for (const line of this.pending) frag.append(el('div', { class: `log-line ${classifyLine(line)}`, text: line }));
      this.pending = [];
      this.box.append(frag);
      const excess = this.box.childElementCount - LOG_MAX_LINES;
      for (let i = 0; i < excess; i++) this.box.firstElementChild.remove();
      if (stick) this.scrollToEnd(); else this.jump.hidden = false;
    }
  }

  function classifyLine(line) {
    const m = String(line).toUpperCase();
    if (/\b(ERROR|FATAL|FAILED|TRACEBACK)\b/.test(m)) return 'is-error';
    if (/\bWARN(ING)?\b/.test(m)) return 'is-warn';
    if (/\b(DONE|COMPLETE|SUCCESS|DOWNLOADED|SAVED)\b/.test(m)) return 'is-ok';
    if (/^\s*(STEAMCMD|DEPOTDOWNLOADER)\b/.test(m)) return 'is-dim';
    return '';
  }

  const logView = new LogView($('#job-log'), $('#log-jump'));
  let logRunId = null;

  // =========================================================================
  // Current job panel
  // =========================================================================
  const J = {
    idle: $('#job-idle'), active: $('#job-active'), badge: $('#job-badge-slot'), name: $('#job-name'),
    source: $('#job-source'), phase: $('#job-phase'), times: $('#job-times'), progress: $('#job-progress'),
    fill: $('#job-progress-fill'), counts: $('#job-counts'), hint: $('#job-hint'), error: $('#job-error'),
    cancel: $('#job-cancel'), logLink: $('#job-log-download'), confirmBar: $('#confirm-bar'),
    confirmSummary: $('#confirm-summary'), confirmCountdown: $('#confirm-countdown'),
  };
  let tickTimer = null;

  function renderJob(s) {
    const run = s.current;
    J.idle.hidden = !!run;
    J.active.hidden = !run;
    clear(J.badge);
    if (!run) { stopTicker(); return; }
    J.badge.append(statusBadge(run.status));
    J.name.textContent = (run.kind === 'metadata' ? 'Metadata refresh: ' : '') + gameName(run);
    J.source.textContent = `${sourceLabel(run)} · ${TRIGGER_LABELS[run.trigger] || run.trigger || 'Manual'}`;
    J.phase.textContent = run.status === 'waiting_confirm' ? 'Waiting for confirmation' : PHASE_LABELS[run.phase] || run.phase || '';

    const active = SWA.isActive(run);
    const pct = run.total ? Math.min(100, Math.round((run.done / run.total) * 100)) : (active ? 0 : 100);
    J.progress.classList.toggle('indeterminate', active && !run.total);
    J.progress.setAttribute('aria-valuenow', String(pct));
    J.progress.setAttribute('aria-valuetext', run.total ? `${run.done} of ${run.total} items` : 'Working…');
    J.fill.style.width = `${pct}%`;
    J.progress.dataset.status = run.status;

    renderCounts(run);
    J.hint.hidden = !run.hint;
    J.hint.textContent = run.hint || '';
    J.error.hidden = !run.error;
    J.error.textContent = run.error || '';
    J.cancel.hidden = !active || run.status === 'waiting_confirm'; // the confirm bar has its own cancel
    J.logLink.href = `/api/runs/${encodeURIComponent(run.id)}/log?download=1`;

    renderConfirm(run, s.confirm);
    renderTimes();
    if (active) startTicker(); else stopTicker();
  }

  function renderCounts(run) {
    const total = run.total ? `${formatNumber(run.done)} / ${formatNumber(run.total)}` : formatNumber(run.done || 0);
    const items = [
      ['Processed', total, ''],
      ['Downloaded', run.downloaded, 'ok'],
      ['Updated', run.updated, 'info'],
      ['Skipped', run.skipped, ''],
      ['Failed', run.failed, run.failed ? 'danger' : ''],
      ['Unavailable', run.unavailable, run.unavailable ? 'warn' : ''],
    ];
    clear(J.counts);
    for (const [label, value, tone] of items) {
      J.counts.append(el('div', { class: `stat ${tone ? 'stat-' + tone : ''}` },
        el('span', { class: 'stat-value', text: typeof value === 'number' ? formatNumber(value) : String(value ?? 0) }),
        el('span', { class: 'stat-label', text: label })));
    }
  }

  function renderTimes() {
    const run = events.state.current;
    if (!run) return;
    const now = Date.now() / 1000;
    const parts = [];
    if (run.started_at) {
      const end = run.finished_at || now;
      const elapsed = end - run.started_at;
      parts.push(`Elapsed ${formatDuration(elapsed)}`);
      if (SWA.isActive(run) && run.total && run.done > 0 && run.done < run.total && run.status === 'running') {
        const eta = ((run.total - run.done) * elapsed) / run.done;
        parts.push(`ETA ${formatDuration(eta)}`);
      }
    } else if (run.queued_at) {
      parts.push(`Queued ${SWA.ui.formatRelative(run.queued_at)}`);
    }
    J.times.textContent = parts.join(' · ');
    const c = events.state.confirm;
    if (c && c.timeout_at && !J.confirmBar.hidden) {
      const left = Math.max(0, c.timeout_at - now);
      J.confirmCountdown.textContent = left > 0 ? `Times out in ${formatDuration(left)}` : 'Timed out';
    }
  }
  function startTicker() { if (!tickTimer) tickTimer = setInterval(renderTimes, 1000); }
  function stopTicker() { clearInterval(tickTimer); tickTimer = null; }

  function renderConfirm(run, confirm) {
    const waiting = run.status === 'waiting_confirm';
    J.confirmBar.hidden = !waiting;
    if (!waiting) return;
    const c = confirm && confirm.run_id === run.id ? confirm : null;
    const items = c ? c.items : run.total;
    const bytes = c ? c.bytes : run.bytes_estimate;
    J.confirmSummary.textContent = `Ready to download ${plural(items || 0, 'item')}${bytes ? ` (about ${formatBytes(bytes)})` : ''}.`;
    J.confirmCountdown.textContent = c && c.timeout_at ? '' : 'Waiting for confirmation.';
  }

  $('#confirm-proceed').addEventListener('click', (e) => {
    const run = events.state.current;
    if (!run) return;
    withBusy(e.currentTarget, 'Continuing…', async () => {
      await fetchJSON(`/api/runs/${run.id}/confirm`, { method: 'POST' });
      J.confirmBar.hidden = true;
    });
  });
  $('#confirm-cancel').addEventListener('click', (e) => {
    const run = events.state.current;
    if (run) cancelRun(run, e.currentTarget, false);
  });
  J.cancel.addEventListener('click', (e) => {
    const run = events.state.current;
    if (run) cancelRun(run, e.currentTarget, true);
  });

  async function cancelRun(run, button, ask) {
    if (ask) {
      const ok = await confirmDialog({
        title: `Cancel ${gameName(run)}?`,
        message: run.status === 'queued' ? 'The run is removed from the queue.' : 'The run stops after the current step. Items already downloaded are kept.',
        confirmLabel: 'Cancel run', cancelLabel: 'Keep running', danger: true,
      });
      if (!ok) return;
    }
    await withBusy(button, 'Cancelling…', async () => {
      await fetchJSON(`/api/runs/${run.id}/cancel`, { method: 'POST' });
      toast(run.status === 'queued' ? 'Removed from the queue.' : 'Cancellation requested.', 'info');
    });
  }

  // =========================================================================
  // Queue
  // =========================================================================
  function renderQueue(queue) {
    const section = $('#queue');
    const list = $('#queue-list');
    const queued = queue.filter((r) => r.status === 'queued' || !SWA.TERMINAL.has(r.status));
    section.hidden = !queued.length;
    $('#queue-count').textContent = queued.length ? `(${queued.length})` : '';
    clear(list);
    queued.forEach((run, i) => {
      list.append(el('li', { class: 'list-item' },
        el('div', { class: 'li-main' },
          el('div', { class: 'li-title' }, el('span', { class: 'queue-pos mono', text: `${i + 1}.` }),
            (run.kind === 'metadata' ? 'Metadata refresh: ' : '') + gameName(run)),
          el('div', { class: 'li-sub muted' }, `${sourceLabel(run)} · ${TRIGGER_LABELS[run.trigger] || run.trigger} · queued `, timeEl(run.queued_at))),
        el('div', { class: 'li-actions' },
          btn('Cancel', 'btn-ghost', (e) => cancelRun(run, e.currentTarget, false), { 'aria-label': `Cancel queued run for ${gameName(run)}` }))));
    });
  }

  // =========================================================================
  // Archive + scheduled jobs
  // =========================================================================
  const data = { workshop: null, savedJobs: [], archiveError: null, jobsError: null };
  let scheduleFilter = null;

  async function loadArchive() {
    try { data.workshop = await fetchJSON('/api/workshop'); data.archiveError = null; } catch (e) { data.archiveError = e.message; }
  }
  async function loadSavedJobs() {
    try { data.savedJobs = await fetchJSON('/api/saved-jobs'); data.jobsError = null; } catch (e) { data.jobsError = e.message; }
  }
  async function refreshLists() {
    await Promise.all([loadArchive(), loadSavedJobs()]);
    renderArchive();
    renderSchedules();
    fillHistoryGames();
  }
  const refreshListsSoon = debounce(refreshLists, 500);

  function renderDisk(disk) {
    const box = $('#disk');
    if (!disk || !disk.total) { box.hidden = true; return; }
    box.hidden = false;
    const pct = Math.min(100, Math.round((disk.used / disk.total) * 100));
    $('#disk-label').textContent = `Downloads volume: ${formatBytes(disk.used)} used of ${formatBytes(disk.total)}`;
    $('#disk-free').textContent = `${formatBytes(disk.free)} free`;
    $('#disk-fill').style.width = `${pct}%`;
    const meter = $('#disk-meter');
    meter.setAttribute('aria-valuenow', String(pct));
    meter.setAttribute('aria-valuetext', `${pct}% used, ${formatBytes(disk.free)} free`);
    meter.classList.toggle('warn', pct >= 85);
    meter.classList.toggle('danger', pct >= 95);
  }

  function jobsForGame(game) {
    const ids = new Set(game.saved_job_ids || []);
    return data.savedJobs.filter((j) => ids.has(j.id) || (!game.saved_job_ids && j.appid === game.appid));
  }

  function sortedGames() {
    const games = (data.workshop && data.workshop.games) || [];
    const q = $('#archive-search').value.trim().toLowerCase();
    const sort = $('#archive-sort').value;
    const filtered = q ? games.filter((g) => gameName(g).toLowerCase().includes(q) || String(g.appid).includes(q)) : games.slice();
    const cmp = {
      name: (a, b) => gameName(a).localeCompare(gameName(b)),
      size: (a, b) => (b.total_size || 0) - (a.total_size || 0),
      items: (a, b) => (b.item_count || 0) - (a.item_count || 0),
      recent: (a, b) => ((b.last_run && b.last_run.finished_at) || 0) - ((a.last_run && a.last_run.finished_at) || 0),
    }[sort];
    return filtered.sort(cmp);
  }

  function renderArchive() {
    const list = $('#archive-list');
    list.removeAttribute('aria-busy');
    clear(list);
    if (data.archiveError) {
      list.append(errorItem(`Couldn't load the archive: ${data.archiveError}`, refreshLists));
      return;
    }
    const w = data.workshop || { games: [] };
    renderDisk(w.disk);
    const games = w.games || [];
    $('#archive-totals').textContent = games.length
      ? `${plural(games.length, 'game')} · ${plural(w.total_items || 0, 'item')} · ${formatBytes(w.total_size || 0)}` : '';
    $('#archive-toolbar').hidden = games.length < 2;
    if (!games.length) { list.append(archiveEmptyState()); return; }
    const shown = sortedGames();
    if (!shown.length) { list.append(el('li', { class: 'empty-inline', text: 'No games match that filter.' })); return; }
    for (const g of shown) list.append(archiveRow(g));
  }

  function archiveEmptyState() {
    return el('li', { class: 'empty-state' },
      el('h3', { class: 'empty-title', text: 'Your archive is empty' }),
      el('ol', { class: 'steps' },
        el('li', null, el('strong', { text: 'Pick a source above: ' }), 'search for a game to grab its whole Workshop, or paste a collection or item URL.'),
        el('li', null, el('strong', { text: 'Press Start. ' }), 'Progress and the live log show up under Current job.'),
        el('li', null, el('strong', { text: 'Browse and schedule. ' }), 'Archived games appear here, ready to browse and download. Save a job to keep them up to date automatically.')),
      el('button', { type: 'button', class: 'btn btn-primary btn-sm', text: 'Search for a game', on: { click: () => $('[data-f="game"]', form).focus() } }));
  }

  function archiveRow(g) {
    const jobs = jobsForGame(g);
    const name = gameName(g);
    const sub = el('div', { class: 'li-sub' },
      el('span', { class: 'muted', text: `${plural(g.item_count || 0, 'item')} · ${formatBytes(g.total_size || 0)}` }),
      g.removed_count ? el('span', { class: 'badge badge-warn', title: 'Items removed from the Workshop; local copies kept', text: `${formatNumber(g.removed_count)} removed` }) : null,
      g.last_run ? el('span', { class: 'row row-tight' }, statusBadge(g.last_run.status), g.last_run.finished_at ? timeEl(g.last_run.finished_at) : null) : null);

    const actions = [el('a', { class: 'btn btn-primary btn-sm', href: `/workshop/${encodeURIComponent(g.appid)}`, text: 'Browse', 'aria-label': `Browse ${name}` })];
    if (jobs.length === 1) {
      actions.push(btn('Run saved job', 'btn-secondary', (e) => runSavedJob(jobs[0], e.currentTarget)));
      actions.push(btn('Edit schedule', 'btn-ghost', () => openJobDialog(jobs[0])));
    } else if (jobs.length > 1) {
      actions.push(btn(`Saved jobs (${jobs.length})`, 'btn-secondary', () => filterSchedules(g)));
    } else {
      actions.push(btn('New schedule', 'btn-ghost', () => openJobDialog(null, {
        appid: g.appid, game_name: g.game_name, source_type: 'workshop', schedule_type: 'cron', schedule_value: '0 3 * * *',
      })));
    }
    return el('li', { class: 'list-item' },
      el('div', { class: 'li-main' },
        el('div', { class: 'li-title' },
          el('a', { href: `/workshop/${encodeURIComponent(g.appid)}`, class: 'link-strong', text: name }),
          el('span', { class: 'appid mono', text: g.appid })),
        sub),
      el('div', { class: 'li-actions' }, actions));
  }

  function errorItem(message, retry) {
    return el('li', { class: 'empty-inline error' }, el('span', { text: message }), ' ',
      retry ? el('button', { type: 'button', class: 'btn btn-secondary btn-sm', text: 'Retry', on: { click: retry } }) : null);
  }

  $('#archive-search').addEventListener('input', debounce(renderArchive, 150));
  $('#archive-sort').addEventListener('change', renderArchive);

  // ---- Scheduled jobs ----------------------------------------------------
  function scheduleDescription(job) {
    if (job.schedule_description) return job.schedule_description;
    const v = String(job.schedule_value || '').trim();
    if (job.schedule_type === 'interval') {
      const h = Number(v);
      if (!h) return 'Interval (invalid)';
      return h % 24 === 0 ? `Every ${h / 24 === 1 ? 'day' : `${h / 24} days`}` : `Every ${h} h`;
    }
    if (job.schedule_type === 'cron') {
      const m = v.match(/^(\d{1,2}) (\d{1,2}) \* \* (\*|\d)$/);
      if (m) {
        const time = `${m[2].padStart(2, '0')}:${m[1].padStart(2, '0')}`;
        const days = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
        return m[3] === '*' ? `Daily at ${time}` : `Weekly on ${days[Number(m[3])] || m[3]} at ${time}`;
      }
      return `Cron: ${v}`;
    }
    return 'Manual only';
  }

  function savedJobBody(job, overrides) {
    const keys = ['name', 'appid', 'game_name', 'source_type', 'source_value', 'pages', 'sort_key', 'delay_min',
      'delay_max', 'scrape_only', 'download_only', 'schedule_type', 'schedule_value', 'enabled'];
    const body = {};
    for (const k of keys) if (job[k] !== undefined && job[k] !== null) body[k] = job[k];
    return Object.assign(body, overrides || {});
  }

  function filterSchedules(game) {
    scheduleFilter = game ? { appid: String(game.appid), name: gameName(game) } : null;
    renderSchedules();
    if (game) $('#schedules').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  $('#schedules-filter-clear').addEventListener('click', () => filterSchedules(null));
  $('#schedule-new').addEventListener('click', () => openJobDialog(null, { schedule_type: 'cron', schedule_value: '0 3 * * *' }));

  function renderSchedules() {
    const list = $('#schedules-list');
    list.removeAttribute('aria-busy');
    clear(list);
    $('#schedules-filter').hidden = !scheduleFilter;
    $('#schedules-filter-text').textContent = scheduleFilter ? scheduleFilter.name : '';
    if (data.jobsError) { list.append(errorItem(`Couldn't load scheduled jobs: ${data.jobsError}`, refreshLists)); return; }
    let jobs = data.savedJobs.slice();
    if (scheduleFilter) jobs = jobs.filter((j) => String(j.appid) === scheduleFilter.appid);
    if (!jobs.length) {
      list.append(el('li', { class: 'empty-inline', text: scheduleFilter
        ? 'No scheduled jobs for this game.' : 'No scheduled jobs yet. Use "Save as scheduled job" on the form above to keep an archive up to date automatically.' }));
      return;
    }
    jobs.sort((a, b) => (a.next_run_at || Infinity) - (b.next_run_at || Infinity));
    for (const job of jobs) list.append(scheduleRow(job));
  }

  function scheduleRow(job) {
    const title = job.name || gameName(job);
    const scheduled = job.schedule_type && job.schedule_type !== 'none';
    const toggle = el('input', { type: 'checkbox', role: 'switch', checked: !!job.enabled, 'aria-label': `Enable schedule for ${title}` });
    toggle.addEventListener('change', async () => {
      toggle.disabled = true;
      try {
        await fetchJSON(`/api/saved-jobs/${encodeURIComponent(job.id)}`, { method: 'PUT', body: savedJobBody(job, { enabled: toggle.checked }) });
        job.enabled = toggle.checked;
        toast(`${title}: schedule ${toggle.checked ? 'enabled' : 'paused'}.`, 'success');
        refreshListsSoon();
      } catch (e) {
        toggle.checked = !toggle.checked;
        toast(e.message, 'error');
      } finally {
        toggle.disabled = false;
      }
    });
    const next = !scheduled ? null : job.enabled && job.next_run_at
      ? el('span', null, 'Next ', timeEl(job.next_run_at)) : el('span', { class: 'muted', text: job.enabled ? 'Not scheduled' : 'Paused' });
    return el('li', { class: 'list-item' },
      el('div', { class: 'li-main' },
        el('div', { class: 'li-title' }, el('span', { text: title }), job.name ? el('span', { class: 'muted li-title-sub', text: gameName(job) }) : null),
        el('div', { class: 'li-sub' },
          el('span', { class: 'muted', text: `${sourceLabel(job)} · ${scheduleDescription(job)}` }),
          next,
          el('span', { class: 'row row-tight' }, statusBadge(job.last_status), job.last_run_at ? timeEl(job.last_run_at) : null))),
      el('div', { class: 'li-actions' },
        scheduled ? el('label', { class: 'switch', title: 'Schedule enabled' }, toggle, el('span', { class: 'switch-track', 'aria-hidden': 'true' })) : null,
        btn('Run now', 'btn-secondary', (e) => runSavedJob(job, e.currentTarget)),
        btn('Edit', 'btn-ghost', () => openJobDialog(job)),
        btn('Delete', 'btn-ghost btn-danger-text', () => deleteSavedJob(job), { 'aria-label': `Delete scheduled job ${title}` })));
  }

  async function runSavedJob(job, button) {
    await withBusy(button, 'Queuing…', async () => {
      const run = await fetchJSON(`/api/saved-jobs/${encodeURIComponent(job.id)}/run`, { method: 'POST' });
      announceQueued(run, job.name || gameName(job));
    });
  }

  async function deleteSavedJob(job) {
    const ok = await confirmDialog({
      title: `Delete "${job.name || gameName(job)}"?`,
      message: 'The schedule is removed. Archived files are not touched.',
      confirmLabel: 'Delete job', danger: true,
    });
    if (!ok) return false;
    try {
      await fetchJSON(`/api/saved-jobs/${encodeURIComponent(job.id)}`, { method: 'DELETE' });
      toast('Scheduled job deleted.', 'success');
      refreshListsSoon();
      return true;
    } catch (e) {
      toast(e.message, 'error');
      return false;
    }
  }

  // =========================================================================
  // Saved-job dialog
  // =========================================================================
  const dlg = $('#job-dialog');
  const dlgForm = $('#job-dialog-form');
  const dlgFields = new SWA.JobFields(dlgForm);
  const schedType = $('#dlg-schedule-type');
  const schedValue = $('#dlg-schedule-value');
  const preview = $('#dlg-preview');
  let editingJob = null;
  let previewSeq = 0;
  let previewValid = true;

  function openJobDialog(job, prefill) {
    editingJob = job || null;
    const src = job || prefill || {};
    $('#job-dialog-title').textContent = job ? `Edit "${job.name || gameName(job)}"` : 'New scheduled job';
    $('#dlg-delete').hidden = !job;
    $('#dlg-name').value = src.name || '';
    dlgFields.write(src);
    schedType.value = src.schedule_type || 'none';
    schedValue.value = src.schedule_value || '';
    $('#dlg-enabled').checked = src.enabled !== false;
    setFieldError(schedValue, '');
    syncScheduleUI();
    runPreview();
    openDialog(dlg, { focus: '#dlg-name' });
  }

  function syncScheduleUI() {
    const type = schedType.value;
    const wrap = $('#dlg-schedule-value-wrap');
    wrap.hidden = type === 'none';
    if (type === 'interval') {
      $('#dlg-schedule-value-label').textContent = 'Every (hours)';
      $('#dlg-schedule-hint').textContent = 'e.g. 6, 24 (daily), 168 (weekly)';
      schedValue.placeholder = '24';
      schedValue.inputMode = 'decimal';
    } else if (type === 'cron') {
      $('#dlg-schedule-value-label').textContent = 'Cron expression';
      $('#dlg-schedule-hint').textContent = '5 fields: minute hour day-of-month month day-of-week';
      schedValue.placeholder = '0 3 * * *';
      schedValue.inputMode = 'text';
    }
    $('#dlg-enabled').closest('.field').hidden = type === 'none';
  }

  async function runPreview() {
    const seq = ++previewSeq;
    const type = schedType.value;
    const value = schedValue.value.trim();
    clear(preview);
    preview.className = 'schedule-preview';
    if (type === 'none') {
      previewValid = true;
      preview.append(el('span', { class: 'muted', text: 'No schedule: run it from the list whenever you like.' }));
      return;
    }
    if (!value) { previewValid = false; preview.append(el('span', { class: 'muted', text: 'Enter a schedule to see when it will run.' })); return; }
    preview.append(el('span', { class: 'muted', text: 'Checking…' }));
    try {
      const [res, sys] = await Promise.all([
        fetchJSON('/api/schedule/preview', { method: 'POST', body: { schedule_type: type, schedule_value: value } }),
        SWA.getSystem().catch(() => null),
      ]);
      if (seq !== previewSeq) return;
      clear(preview);
      previewValid = !!res.valid;
      setFieldError(schedValue, res.valid ? '' : (res.error || 'Invalid schedule.'));
      if (!res.valid) { preview.classList.add('is-error'); preview.append(el('span', { text: res.error || 'Invalid schedule.' })); return; }
      preview.append(
        el('strong', { text: res.description || scheduleDescription({ schedule_type: type, schedule_value: value }) }),
        el('span', { class: 'muted', text: 'Next runs:' }),
        el('ul', { class: 'next-runs' }, (res.next_runs || []).slice(0, 3).map((t) => el('li', null, timeEl(t, { relative: false }), el('span', { class: 'muted', text: ` (${SWA.ui.formatRelative(t)})` })))),
        sys && sys.tz ? el('span', { class: 'hint', text: `Shown in your browser's time zone. The server schedules in ${sys.tz}.` }) : null);
    } catch (e) {
      if (seq !== previewSeq) return;
      clear(preview);
      previewValid = true; // let the server decide on save
      preview.append(el('span', { class: 'muted', text: `Preview unavailable: ${e.message}` }));
    }
  }
  const runPreviewSoon = debounce(runPreview, 400);

  schedType.addEventListener('change', () => { syncScheduleUI(); runPreview(); });
  schedValue.addEventListener('input', () => { previewValid = false; runPreviewSoon(); });
  dlgForm.querySelectorAll('[data-preset]').forEach((b) => b.addEventListener('click', () => {
    schedType.value = b.dataset.preset;
    schedValue.value = b.dataset.value;
    syncScheduleUI();
    runPreview();
  }));

  dlgForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const job = dlgFields.read();
    if (!job) return;
    const type = schedType.value;
    if (type !== 'none') {
      runPreviewSoon.cancel();
      await runPreview();
      if (!previewValid) { schedValue.focus(); return; }
    }
    const body = savedJobBody(Object.assign({}, job, {
      name: $('#dlg-name').value.trim(),
      schedule_type: type,
      schedule_value: type === 'none' ? '' : schedValue.value.trim(),
      enabled: type === 'none' ? true : $('#dlg-enabled').checked,
    }));
    if (!body.appid) delete body.appid;
    await withBusy($('#dlg-save'), 'Saving…', async () => {
      const url = editingJob ? `/api/saved-jobs/${encodeURIComponent(editingJob.id)}` : '/api/saved-jobs';
      await fetchJSON(url, { method: editingJob ? 'PUT' : 'POST', body });
      toast(editingJob ? 'Scheduled job updated.' : 'Scheduled job saved.', 'success');
      dlg.close('saved');
      refreshListsSoon();
    });
  });

  $('#dlg-delete').addEventListener('click', async () => {
    if (editingJob && await deleteSavedJob(editingJob)) dlg.close('deleted');
  });

  // =========================================================================
  // History
  // =========================================================================
  const hist = { offset: 0, total: 0, appid: '', seq: 0 };
  const historyBody = $('#history-body');

  function fillHistoryGames() {
    const select = $('#history-game');
    const current = select.value;
    const games = ((data.workshop && data.workshop.games) || []).slice().sort((a, b) => gameName(a).localeCompare(gameName(b)));
    clear(select).append(el('option', { value: '', text: 'All games' }));
    for (const g of games) select.append(el('option', { value: g.appid, text: gameName(g) }));
    if (current && !games.some((g) => String(g.appid) === current)) select.append(el('option', { value: current, text: `App ${current}` }));
    select.value = current;
  }

  async function loadHistory() {
    const seq = ++hist.seq;
    const params = new URLSearchParams({ limit: HISTORY_PAGE, offset: hist.offset });
    if (hist.appid) params.set('appid', hist.appid);
    try {
      const res = await fetchJSON(`/api/runs?${params}`);
      if (seq !== hist.seq) return;
      hist.total = res.total || 0;
      renderHistory(res.runs || []);
    } catch (e) {
      if (seq !== hist.seq) return;
      clear(historyBody).append(el('tr', null, el('td', { colspan: 8, class: 'empty-inline error' },
        `Couldn't load history: ${e.message} `, el('button', { type: 'button', class: 'btn btn-secondary btn-sm', text: 'Retry', on: { click: loadHistory } }))));
    }
  }
  const loadHistorySoon = debounce(() => { if (hist.offset === 0) loadHistory(); }, 1000);

  function resultsSummary(r) {
    const parts = [];
    if (r.downloaded) parts.push(`${r.downloaded} new`);
    if (r.updated) parts.push(`${r.updated} updated`);
    if (r.skipped) parts.push(`${r.skipped} skipped`);
    if (r.failed) parts.push(`${r.failed} failed`);
    if (r.unavailable) parts.push(`${r.unavailable} unavailable`);
    return parts.join(' · ') || (SWA.isActive(r) ? '…' : 'Nothing to do');
  }

  function td(label, ...children) { return el('td', { 'data-label': label }, ...children); }

  function renderHistory(runs) {
    clear(historyBody);
    if (!runs.length) {
      historyBody.append(el('tr', null, el('td', { colspan: 8, class: 'empty-inline', text: hist.appid ? 'No runs for this game yet.' : 'No runs yet.' })));
    }
    for (const r of runs) {
      const terminal = SWA.TERMINAL.has(r.status);
      const duration = r.started_at ? formatDuration((r.finished_at || Date.now() / 1000) - r.started_at) : '–';
      historyBody.append(el('tr', null,
        td('Game', r.appid ? el('a', { href: `/workshop/${encodeURIComponent(r.appid)}`, text: gameName(r) }) : gameName(r),
          r.kind === 'metadata' ? el('span', { class: 'badge badge-muted', text: 'metadata' }) : null),
        td('Source', sourceLabel(r)),
        td('Trigger', TRIGGER_LABELS[r.trigger] || r.trigger || '–'),
        td('Status', statusBadge(r.status)),
        td('Started', r.started_at ? timeEl(r.started_at) : el('span', { class: 'muted', text: 'not started' })),
        td('Duration', duration),
        td('Results', el('span', { class: r.failed ? 'text-danger' : '', title: `${r.done || 0} of ${r.total || 0} processed`, text: resultsSummary(r) })),
        el('td', { class: 'td-actions' }, el('div', { class: 'row row-tight row-end' },
          btn('Log', 'btn-ghost', (e) => openLog(r, e.currentTarget), { 'aria-label': `View log of run ${r.id}` }),
          terminal && r.failed > 0 ? btn('Retry failed', 'btn-secondary', (e) => retryRun(r, e.currentTarget)) : null,
          terminal ? btn('Delete', 'btn-ghost btn-danger-text', () => deleteRun(r), { 'aria-label': `Delete run ${r.id} from history` }) : null))));
    }
    const from = hist.total ? hist.offset + 1 : 0;
    const to = Math.min(hist.offset + HISTORY_PAGE, hist.total);
    $('#history-range').textContent = hist.total ? `${from}–${to} of ${formatNumber(hist.total)}` : '';
    $('#history-prev').disabled = hist.offset === 0;
    $('#history-next').disabled = hist.offset + HISTORY_PAGE >= hist.total;
  }

  $('#history-prev').addEventListener('click', () => { hist.offset = Math.max(0, hist.offset - HISTORY_PAGE); loadHistory(); });
  $('#history-next').addEventListener('click', () => { hist.offset += HISTORY_PAGE; loadHistory(); });
  $('#history-game').addEventListener('change', (e) => { hist.appid = e.target.value; hist.offset = 0; loadHistory(); });

  async function openLog(run, opener) {
    const d = $('#log-dialog');
    const pre = $('#log-dialog-text');
    $('#log-dialog-title').textContent = `Run #${run.id} · ${gameName(run)}`;
    $('#log-dialog-download').href = `/api/runs/${encodeURIComponent(run.id)}/log?download=1`;
    pre.textContent = 'Loading…';
    openDialog(d, { returnFocus: opener });
    try {
      const text = await fetchJSON(`/api/runs/${encodeURIComponent(run.id)}/log`, { headers: { Accept: 'text/plain' } });
      pre.textContent = (typeof text === 'string' ? text : JSON.stringify(text, null, 2)) || '(empty log)';
      pre.scrollTop = pre.scrollHeight;
    } catch (e) {
      pre.textContent = `Couldn't load the log: ${e.message}`;
    }
  }

  async function retryRun(run, button) {
    await withBusy(button, 'Queuing…', async () => {
      const r = await fetchJSON(`/api/runs/${encodeURIComponent(run.id)}/retry`, { method: 'POST' });
      announceQueued(r, `Retry of ${plural(run.failed, 'failed item')}`);
    });
  }

  async function deleteRun(run) {
    const ok = await confirmDialog({
      title: `Delete run #${run.id}?`, message: 'Removes it and its log from history. Archived files are not touched.',
      confirmLabel: 'Delete', danger: true,
    });
    if (!ok) return;
    try {
      await fetchJSON(`/api/runs/${encodeURIComponent(run.id)}`, { method: 'DELETE' });
      toast('Run deleted.', 'success');
      if (historyBody.children.length === 1 && hist.offset > 0) hist.offset -= HISTORY_PAGE;
      loadHistory();
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  // =========================================================================
  // Live events
  // =========================================================================
  let snapshots = 0;
  events.on('state', (s) => { renderJob(s); renderQueue(s.queue); });
  events.on('snapshot', (_msg, s) => {
    logRunId = s.current ? s.current.id : null;
    logView.reset(s.log);
    if (++snapshots > 1) { refreshListsSoon(); loadHistorySoon(); } // reconnected: resync
  });
  events.on('run_started', (msg) => { logRunId = msg.run.id; logView.reset([]); loadHistorySoon(); });
  events.on('run_queued', loadHistorySoon);
  events.on('log', (msg) => { if (msg.run_id === logRunId) logView.append(msg.line); });
  events.on('run_finished', (msg) => {
    const r = msg.run;
    const tone = r.status === 'done' ? 'success' : r.status === 'cancelled' ? 'info' : 'error';
    toast(`${r.kind === 'metadata' ? 'Metadata refresh' : 'Archive run'} for ${gameName(r)}: ${SWA.ui.STATUS_LABELS[r.status] || r.status}.`, tone);
    refreshListsSoon();
    loadHistorySoon();
  });
  events.on('archive_changed', refreshListsSoon);
  events.on('saved_jobs_changed', refreshListsSoon);

  // =========================================================================
  // Boot
  // =========================================================================
  renderJob(events.state);
  Promise.all([refreshLists(), loadHistory()]).then(() => {
    // Content above loads async; re-apply the #history / #job anchor once it has settled.
    const target = location.hash && document.getElementById(location.hash.slice(1));
    if (target) target.scrollIntoView();
  });
})();
