/* Job fields controller: game combobox, source switching, input resolution,
   validation, and read/write of job settings. Used by the form and the dialog. */
(function () {
  'use strict';
  const SWA = globalThis.SWA;
  const { el, $, $$, clear, debounce, setFieldError } = SWA.ui;
  const fetchJSON = SWA.fetchJSON;
  const D = SWA.DEFAULTS;

  const STEAM_INPUT_RE = /^\d{1,20}$|^https?:\/\/|steamcommunity\.com|store\.steampowered\.com/i;
  const PAGES_RE = /^(all|[1-9]\d*)$/i;

  function splitSortKey(key) {
    key = key || D.sortKey;
    return key.startsWith('trend_') ? ['trend', key.slice(6)] : [key, 'week'];
  }
  function resolveInput(text, signal) {
    return fetchJSON('/api/resolve-input', { method: 'POST', body: { text }, signal });
  }
  SWA.splitSortKey = splitSortKey;

  // ---- Game combobox (ARIA 1.2 combobox + listbox) -----------------------
  class GameCombobox {
    constructor(field, { onResolved } = {}) {
      this.input = $('[data-f="game"]', field);
      this.list = $('[data-combo-list]', field);
      this.spinner = $('[data-combo-spinner]', field);
      this.chip = $('[data-selected]', field);
      this.onResolved = onResolved || (() => {});
      this.selected = null;
      this.results = [];
      this.active = -1;
      this.abort = null;
      this.lookup = debounce(() => this.runLookup(), 250);

      this.input.addEventListener('input', () => this.onInput());
      this.input.addEventListener('keydown', (e) => this.onKey(e));
      this.input.addEventListener('blur', () => setTimeout(() => this.close(), 120));
      this.input.addEventListener('focus', () => { if (this.results.length && !this.selected) this.open(); });
      $('[data-selected-clear]', field).addEventListener('click', () => { this.clear(); this.input.focus(); });
    }

    onInput() {
      if (this.selected) this.setSelected(null); // editing the text drops the old selection
      setFieldError(this.input, '');
      const q = this.input.value.trim();
      if (!q) { this.cancel(); this.results = []; this.close(); return; }
      this.lookup();
    }

    cancel() {
      this.lookup.cancel();
      if (this.abort) this.abort.abort();
      this.abort = null;
      this.spinner.hidden = true;
    }

    async runLookup() {
      const q = this.input.value.trim();
      if (!q) return;
      if (this.abort) this.abort.abort();
      const ctrl = (this.abort = new AbortController());
      this.spinner.hidden = false;
      try {
        if (STEAM_INPUT_RE.test(q)) {
          try {
            const r = await resolveInput(q, ctrl.signal);
            if (ctrl.signal.aborted) return;
            this.close();
            this.onResolved(r);
            return;
          } catch (e) {
            if (e.name === 'AbortError') return;
            if (!/^\d+$/.test(q)) { this.renderMessage(e.message || 'Could not understand that URL.'); return; }
            // A bare number that isn't an ID: fall through to a name search.
          }
        }
        const results = await fetchJSON(`/api/search?q=${encodeURIComponent(q)}`, { signal: ctrl.signal });
        if (ctrl.signal.aborted) return;
        this.renderResults(Array.isArray(results) ? results : []);
      } catch (e) {
        if (e.name !== 'AbortError') this.renderMessage(`Search failed: ${e.message}`);
      } finally {
        if (this.abort === ctrl) { this.abort = null; this.spinner.hidden = true; }
      }
    }

    renderResults(results) {
      this.results = results;
      this.active = -1;
      clear(this.list);
      if (!results.length) { this.renderMessage('No games with Workshop support found.'); return; }
      results.forEach((r, i) => {
        const logo = typeof r.logo === 'string' && /^https:\/\//.test(r.logo)
          ? el('img', { src: r.logo, alt: '', width: 46, height: 17, loading: 'lazy' }) : el('span', { class: 'combo-logo-ph', 'aria-hidden': 'true' });
        if (logo.tagName === 'IMG') logo.addEventListener('error', () => logo.replaceWith(el('span', { class: 'combo-logo-ph', 'aria-hidden': 'true' })), { once: true });
        const li = el('li', {
          id: `${this.list.id}-${i}`, role: 'option', class: 'combo-option', 'aria-selected': 'false',
          on: {
            mousedown: (e) => e.preventDefault(), // keep focus in the input
            click: () => this.choose(i),
          },
        }, logo, el('span', { class: 'combo-name', text: r.name }), el('span', { class: 'combo-id mono', text: r.appid }));
        this.list.append(li);
      });
      this.open();
    }

    renderMessage(text) {
      this.results = [];
      this.active = -1;
      clear(this.list).append(el('li', { class: 'combo-msg', role: 'option', 'aria-disabled': 'true', text }));
      this.open();
    }

    open() {
      if (!this.list.firstChild) return;
      this.list.hidden = false;
      this.input.setAttribute('aria-expanded', 'true');
    }
    close() {
      this.list.hidden = true;
      this.input.setAttribute('aria-expanded', 'false');
      this.input.removeAttribute('aria-activedescendant');
      this.active = -1;
    }
    get isOpen() { return !this.list.hidden; }

    setActive(i) {
      const opts = $$('.combo-option', this.list);
      if (!opts.length) return;
      this.active = (i + opts.length) % opts.length;
      opts.forEach((o, j) => o.setAttribute('aria-selected', j === this.active ? 'true' : 'false'));
      const cur = opts[this.active];
      this.input.setAttribute('aria-activedescendant', cur.id);
      cur.scrollIntoView({ block: 'nearest' });
    }

    onKey(e) {
      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          if (!this.isOpen) this.open();
          this.setActive(this.active + 1);
          break;
        case 'ArrowUp':
          e.preventDefault();
          if (!this.isOpen) this.open();
          this.setActive(this.active - 1);
          break;
        case 'Enter':
          if (this.isOpen && this.active >= 0) { e.preventDefault(); this.choose(this.active); }
          else if (!this.selected && this.input.value.trim()) { e.preventDefault(); this.lookup.cancel(); this.runLookup(); }
          break;
        case 'Escape':
          this.cancel(); // drop a pending lookup so results don't reopen the list
          if (this.isOpen) { e.preventDefault(); e.stopPropagation(); this.close(); }
          break;
        default:
      }
    }

    choose(i) {
      const r = this.results[i];
      if (!r) return;
      this.select({ appid: String(r.appid), name: r.name, logo: r.logo });
    }

    select(game) {
      this.cancel();
      this.input.value = game.name || `App ${game.appid}`;
      this.setSelected(game);
      this.close();
    }

    setSelected(game) {
      this.selected = game;
      this.chip.hidden = !game;
      if (!game) return;
      setFieldError(this.input, '');
      $('[data-selected-name]', this.chip).textContent = game.name || `App ${game.appid}`;
      $('[data-selected-appid]', this.chip).textContent = game.appid;
      const img = $('[data-selected-logo]', this.chip);
      const ok = typeof game.logo === 'string' && /^https:\/\//.test(game.logo);
      img.hidden = !ok;
      img.onerror = () => { img.hidden = true; };
      if (ok) img.src = game.logo; else img.removeAttribute('src');
    }

    clear() {
      this.cancel();
      this.input.value = '';
      this.results = [];
      this.setSelected(null);
      this.close();
    }
  }

  // ---- Job fields --------------------------------------------------------
  class JobFields {
    constructor(container) {
      this.root = container;
      this.f = (name) => $(`[data-f="${name}"]`, container);
      this.radios = (name) => $$(`[data-f="${name}"]`, container);
      this.combo = new GameCombobox($('[data-source-panel="workshop"]', container), { onResolved: (r) => this.applyResolved(r) });
      this.collectionInfo = null;
      this.itemsApp = null;
      this.collectionAbort = null;
      this.itemsAbort = null;

      this.radios('source').forEach((r) => r.addEventListener('change', () => this.syncVisibility()));
      this.f('sort').addEventListener('change', () => this.syncVisibility());
      this.f('pages').addEventListener('input', () => this.validatePages());

      const collection = this.f('collection');
      const lookupCollection = debounce(() => this.resolveCollection(), 600);
      collection.addEventListener('input', () => { this.collectionInfo = null; this.setInfo('collection', ''); if (collection.value.trim()) lookupCollection(); });
      collection.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); lookupCollection.cancel(); this.resolveCollection(); } });
      $('[data-action="resolve-collection"]', container).addEventListener('click', () => { lookupCollection.cancel(); this.resolveCollection(); });

      const lookupItems = debounce(() => this.resolveItems(), 700);
      this.f('items').addEventListener('input', () => { this.itemsApp = null; setFieldError(this.f('items'), ''); lookupItems(); });
      this.reset();
    }

    get source() { return (this.radios('source').find((r) => r.checked) || {}).value || 'workshop'; }
    set source(v) { this.radios('source').forEach((r) => { r.checked = r.value === v; }); this.syncVisibility(); }
    get mode() { return (this.radios('mode').find((r) => r.checked) || {}).value || 'both'; }
    set mode(v) { this.radios('mode').forEach((r) => { r.checked = r.value === v; }); }

    syncVisibility() {
      const src = this.source;
      $$('[data-source-panel]', this.root).forEach((p) => { p.hidden = p.dataset.sourcePanel !== src; });
      $$('[data-workshop-only]', this.root).forEach((p) => { p.hidden = src !== 'workshop'; });
      const range = $('[data-range-wrap]', this.root);
      range.hidden = src !== 'workshop' || this.f('sort').value !== 'trend';
    }

    setInfo(kind, content, tone) {
      const box = $(`[data-${kind}-info]`, this.root);
      clear(box);
      box.className = `resolve-info${tone ? ' ' + tone : ''}`;
      if (content) box.append(content);
    }

    applyResolved(r) {
      if (!r || !r.kind) return;
      if (r.kind === 'app') {
        this.combo.select({ appid: String(r.appid), name: r.name });
      } else if (r.kind === 'collection') {
        this.source = 'collection';
        this.f('collection').value = r.collection_id;
        this.showCollection(r);
        this.combo.clear();
        this.f('collection').focus();
      } else if (r.kind === 'item') {
        this.source = 'items';
        const ta = this.f('items');
        const ids = ta.value.split(/[\s,]+/).filter(Boolean);
        if (!ids.includes(String(r.item_id))) ids.push(String(r.item_id));
        ta.value = ids.join('\n');
        this.itemsApp = { appid: String(r.appid), name: r.name };
        this.showItems(ids.length, r.title);
        this.combo.clear();
        ta.focus();
      }
    }

    showCollection(r) {
      this.collectionInfo = r;
      setFieldError(this.f('collection'), '');
      this.setInfo('collection', el('span', null,
        el('strong', { text: r.title || `Collection ${r.collection_id}` }),
        ` · ${SWA.ui.formatNumber(r.item_count)} item${r.item_count === 1 ? '' : 's'} · ${r.name || 'App ' + r.appid}`), 'ok');
    }

    showItems(count, firstTitle) {
      const app = this.itemsApp;
      this.setInfo('items', el('span', null,
        `${count} item${count === 1 ? '' : 's'}`,
        app ? ` · ${app.name || 'App ' + app.appid}` : '',
        firstTitle && count === 1 ? el('span', { class: 'muted', text: ` · ${firstTitle}` }) : null), 'ok');
    }

    async resolveCollection() {
      const input = this.f('collection');
      const text = input.value.trim();
      if (!text) return;
      if (this.collectionAbort) this.collectionAbort.abort();
      const ctrl = (this.collectionAbort = new AbortController());
      this.setInfo('collection', 'Looking up…', 'muted');
      try {
        const r = await resolveInput(text, ctrl.signal);
        if (ctrl.signal.aborted) return;
        if (r.kind !== 'collection') {
          this.setInfo('collection', '');
          setFieldError(input, r.kind === 'item' ? 'That is a single Workshop item, not a collection. Use "Specific items" instead.' : 'That is a game, not a collection. Use "Whole Workshop" instead.');
          return;
        }
        this.showCollection(r);
      } catch (e) {
        if (e.name === 'AbortError') return;
        this.setInfo('collection', '');
        setFieldError(input, e.message);
      }
    }

    async resolveItems() {
      const ids = this.f('items').value.split(/[\s,]+/).filter(Boolean);
      if (!ids.length) { this.setInfo('items', ''); return; }
      if (this.itemsAbort) this.itemsAbort.abort();
      const ctrl = (this.itemsAbort = new AbortController());
      try {
        const r = await resolveInput(ids[0], ctrl.signal);
        if (ctrl.signal.aborted) return;
        if (r.kind === 'item') this.itemsApp = { appid: String(r.appid), name: r.name };
        this.showItems(ids.length, r.title);
      } catch (e) {
        if (e.name === 'AbortError') return;
        this.showItems(ids.length);
      }
    }

    validatePages() {
      const input = this.f('pages');
      const ok = PAGES_RE.test(input.value.trim());
      setFieldError(input, ok ? '' : 'Enter "all" or a whole number of 1 or more.');
      return ok;
    }

    // Returns the job settings, or null after flagging invalid fields.
    read() {
      const errors = [];
      const flag = (input, msg) => { setFieldError(input, msg); if (msg) errors.push(input); };
      const src = this.source;
      const data = { source_type: src, source_value: '', appid: null, game_name: '' };

      if (src === 'workshop') {
        const g = this.combo.selected;
        flag(this.combo.input, g ? '' : this.combo.input.value.trim()
          ? 'Pick a game from the list (or paste a store URL / App ID).' : 'Search for a game first.');
        if (g) { data.appid = g.appid; data.game_name = g.name || ''; }
      } else if (src === 'collection') {
        const v = this.f('collection').value.trim();
        flag(this.f('collection'), v ? '' : 'Enter a collection URL or ID.');
        data.source_value = this.collectionInfo ? String(this.collectionInfo.collection_id) : v;
        if (this.collectionInfo) { data.appid = String(this.collectionInfo.appid); data.game_name = this.collectionInfo.name || ''; }
      } else {
        const ids = this.f('items').value.split(/[\s,]+/).filter(Boolean);
        flag(this.f('items'), ids.length ? '' : 'Enter at least one Workshop item URL or ID.');
        data.source_value = ids.join('\n');
        if (this.itemsApp) { data.appid = this.itemsApp.appid; data.game_name = this.itemsApp.name || ''; }
      }

      const pagesInput = this.f('pages');
      let pages = pagesInput.value.trim().toLowerCase() || D.pages;
      if (src === 'workshop') {
        if (!this.validatePages()) errors.push(pagesInput);
      } else {
        pages = D.pages;
      }

      const minIn = this.f('delayMin'), maxIn = this.f('delayMax');
      const num = (input, fallback) => {
        const raw = input.value.trim();
        if (raw === '') return fallback;
        const n = Number(raw);
        return Number.isFinite(n) ? n : NaN;
      };
      let dmin = num(minIn, D.delayMin), dmax = num(maxIn, D.delayMax);
      flag(minIn, Number.isNaN(dmin) || dmin < 0 ? 'Must be 0 or more.' : '');
      flag(maxIn, Number.isNaN(dmax) || dmax < 0 ? 'Must be 0 or more.' : '');
      if (dmin > dmax) { [dmin, dmax] = [dmax, dmin]; minIn.value = dmin; maxIn.value = dmax; }

      if (errors.length) { errors[0].focus(); return null; }
      const [main, range] = [this.f('sort').value, this.f('range').value];
      return Object.assign(data, {
        pages,
        sort_key: main === 'trend' ? `trend_${range}` : main,
        delay_min: dmin,
        delay_max: dmax,
        scrape_only: this.mode === 'scrape_only',
        download_only: this.mode === 'download_only',
      });
    }

    // Fill from a SavedJob / Run-like object.
    write(job) {
      job = job || {};
      this.clearErrors();
      this.source = job.source_type || 'workshop';
      this.combo.clear();
      this.collectionInfo = null;
      this.itemsApp = null;
      this.f('collection').value = '';
      this.f('items').value = '';
      this.setInfo('collection', '');
      this.setInfo('items', '');
      const app = job.appid ? { appid: String(job.appid), name: job.game_name || '' } : null;
      if (this.source === 'workshop' && app) this.combo.select(app);
      if (this.source === 'collection') {
        this.f('collection').value = job.source_value || '';
        if (job.source_value) this.resolveCollection();
      }
      if (this.source === 'items') {
        const ids = String(job.source_value || '').split(/[\s,]+/).filter(Boolean);
        this.f('items').value = ids.join('\n');
        this.itemsApp = app;
        if (ids.length) this.showItems(ids.length);
      }
      const [main, range] = splitSortKey(job.sort_key || (job.params && job.params.sort_key));
      this.f('sort').value = main;
      this.f('range').value = range;
      const p = job.params || job;
      this.f('pages').value = p.pages || D.pages;
      this.f('delayMin').value = p.delay_min ?? D.delayMin;
      this.f('delayMax').value = p.delay_max ?? D.delayMax;
      this.mode = p.scrape_only ? 'scrape_only' : p.download_only ? 'download_only' : 'both';
      this.syncVisibility();
    }

    reset() { this.write({}); }

    clearErrors() {
      $$('[aria-invalid="true"]', this.root).forEach((i) => setFieldError(i, ''));
    }
  }

  SWA.JobFields = JobFields;
  SWA.GameCombobox = GameCombobox;
})();
