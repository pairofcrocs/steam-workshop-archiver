/* Workshop browser: summary, filters synced to the URL, paged grid with
   infinite scroll, bulk selection, item detail dialog, metadata refresh. */
(function () {
  'use strict';
  const SWA = globalThis.SWA;
  const {
    el, $, $$, clear, toast, confirmDialog, openDialog, withBusy, debounce, formatBytes, formatNumber,
    formatDate, formatDateTime, flash,
  } = SWA.ui;
  const fetchJSON = SWA.fetchJSON;
  const events = SWA.events;
  const D = SWA.DEFAULTS;

  const APPID = document.body.dataset.appid;
  const API = `/api/workshop/${encodeURIComponent(APPID)}`;
  const DEFAULT_SORT = 'updated_desc';
  const FILTER_LABELS = { outdated: 'Outdated', removed: 'Removed from Workshop', no_metadata: 'Missing metadata' };
  const FILTER_COUNTS = { outdated: 'outdated_count', removed: 'removed_count', no_metadata: 'no_metadata_count' };

  const st = { q: '', tags: [], tagMode: 'all', sort: DEFAULT_SORT, filter: '', item: null };
  const grid = { items: [], total: 0, offset: 0, loading: false, gen: 0, error: null, loadedOnce: false };
  const cards = new Map(); // item id -> card element
  const sel = { on: false, ids: new Set(), last: -1 };
  let summary = null;

  const E = {
    title: $('#ws-title'), stats: $('#ws-stats'), q: $('#ws-q'), sort: $('#ws-sort'), filter: $('#ws-filter'),
    tagsWrap: $('#tags-wrap'), tagList: $('#tag-list'), tagsMore: $('#tags-more'), tagsClear: $('#tags-clear'),
    grid: $('#grid'), status: $('#grid-status'), sentinel: $('#sentinel'), count: $('#ws-count'), stale: $('#ws-stale'),
    selectToggle: $('#select-toggle'), bulk: $('#bulk-bar'), bulkCount: $('#bulk-count'), bulkNote: $('#bulk-note'),
    bulkZip: $('#bulk-zip'), bulkRedl: $('#bulk-redownload'), bulkDelete: $('#bulk-delete'),
    metaBtn: $('#meta-refresh'), metaStatus: $('#meta-status'),
  };

  const gameLabel = () => (summary && summary.game_name) || `App ${APPID}`;
  const plural = (n, w) => `${formatNumber(n)} ${w}${n === 1 ? '' : 's'}`;
  const hasFilters = () => !!(st.q || st.tags.length || st.filter);
  const previewUrl = (id) => `/workshop-image/${encodeURIComponent(APPID)}/${encodeURIComponent(id)}`;
  const downloadUrl = (id) => `/workshop-download/${encodeURIComponent(APPID)}/${encodeURIComponent(id)}`;

  // =========================================================================
  // URL state
  // =========================================================================
  function readURL() {
    const p = new URLSearchParams(location.search);
    st.q = p.get('q') || '';
    st.tags = (p.get('tags') || '').split(',').map((t) => t.trim()).filter(Boolean);
    st.tagMode = p.get('tag_mode') === 'any' ? 'any' : 'all';
    st.sort = p.get('sort') || DEFAULT_SORT;
    st.filter = FILTER_LABELS[p.get('filter')] ? p.get('filter') : '';
    st.item = p.get('item') || null;
  }
  function writeURL() {
    const p = new URLSearchParams();
    if (st.q) p.set('q', st.q);
    if (st.tags.length) p.set('tags', st.tags.join(','));
    if (st.tagMode !== 'all') p.set('tag_mode', st.tagMode);
    if (st.sort !== DEFAULT_SORT) p.set('sort', st.sort);
    if (st.filter) p.set('filter', st.filter);
    if (st.item) p.set('item', st.item);
    const qs = p.toString();
    history.replaceState(null, '', location.pathname + (qs ? `?${qs}` : ''));
  }
  function syncControls() {
    E.q.value = st.q;
    E.sort.value = st.sort;
    if (E.sort.value !== st.sort) { st.sort = DEFAULT_SORT; E.sort.value = DEFAULT_SORT; }
    E.filter.value = st.filter;
    $$('input[name="tag-mode"]').forEach((r) => { r.checked = r.value === st.tagMode; });
  }

  // =========================================================================
  // Summary, header, hero
  // =========================================================================
  async function loadSummary() {
    try {
      summary = await fetchJSON(API);
    } catch (e) {
      if (!summary) {
        E.title.textContent = e.status === 404 ? 'Not archived' : `App ${APPID}`;
        E.stats.textContent = e.status === 404 ? 'Nothing has been archived for this game yet.' : `Couldn't load summary: ${e.message}`;
      }
      return;
    }
    E.title.textContent = gameLabel();
    document.title = `${gameLabel()} · Workshop Browser`;
    const bits = [plural(summary.item_count || 0, 'item'), formatBytes(summary.total_size || 0)];
    if (summary.outdated_count) bits.push(`${formatNumber(summary.outdated_count)} outdated`);
    if (summary.removed_count) bits.push(`${formatNumber(summary.removed_count)} removed`);
    E.stats.textContent = bits.join(' · ');
    for (const opt of E.filter.options) {
      const key = FILTER_COUNTS[opt.value];
      if (key) opt.textContent = `${FILTER_LABELS[opt.value]} (${formatNumber(summary[key] || 0)})`;
    }
    renderTags();
  }

  function loadHero() {
    const base = `https://cdn.akamai.steamstatic.com/steam/apps/${encodeURIComponent(APPID)}`;
    const hero = $('#hero');
    const probe = (src, onOk) => { const img = new Image(); img.onload = () => onOk(src); img.src = src; };
    probe(`${base}/library_hero.jpg`, (src) => { $('#hero-bg').style.backgroundImage = `url("${src}")`; hero.hidden = false; });
    probe(`${base}/logo.png`, (src) => { const logo = $('#hero-logo'); logo.src = src; logo.hidden = false; hero.hidden = false; });
  }

  // =========================================================================
  // Tags
  // =========================================================================
  function renderTags() {
    const tags = (summary && summary.tags) || [];
    E.tagsWrap.hidden = !tags.length;
    clear(E.tagList);
    if (!tags.length) return;
    const active = new Set(st.tags);
    const ordered = tags.slice().sort((a, b) =>
      (active.has(b.tag) - active.has(a.tag)) || (b.count - a.count) || a.tag.localeCompare(b.tag));
    for (const t of ordered) {
      E.tagList.append(el('button', {
        type: 'button', class: 'tag', 'aria-pressed': active.has(t.tag) ? 'true' : 'false',
        on: { click: () => toggleTag(t.tag) },
      }, el('span', { text: t.tag }), el('span', { class: 'tag-count', text: formatNumber(t.count) })));
    }
    E.tagsClear.hidden = !st.tags.length;
    E.tagsMore.dataset.total = tags.length;
    updateTagsOverflow();
  }

  function updateTagsOverflow() {
    const expanded = E.tagsMore.getAttribute('aria-expanded') === 'true';
    E.tagList.classList.add('collapsed');
    const overflowing = E.tagList.scrollHeight > E.tagList.clientHeight + 2;
    E.tagList.classList.toggle('collapsed', !expanded);
    E.tagsMore.hidden = !overflowing && !expanded;
    E.tagsMore.textContent = expanded ? 'Show fewer tags' : `Show all ${E.tagsMore.dataset.total} tags`;
  }

  E.tagsMore.addEventListener('click', () => {
    const expanded = E.tagsMore.getAttribute('aria-expanded') === 'true';
    E.tagsMore.setAttribute('aria-expanded', expanded ? 'false' : 'true');
    updateTagsOverflow();
  });
  window.addEventListener('resize', debounce(updateTagsOverflow, 150));

  function toggleTag(tag) {
    st.tags = st.tags.includes(tag) ? st.tags.filter((t) => t !== tag) : [...st.tags, tag];
    renderTags();
    applyFilters();
  }
  E.tagsClear.addEventListener('click', () => { st.tags = []; renderTags(); applyFilters(); });

  // =========================================================================
  // Filters
  // =========================================================================
  function applyFilters() {
    writeURL();
    loadPage(true);
  }
  E.q.addEventListener('input', debounce(() => { st.q = E.q.value.trim(); applyFilters(); }, 250));
  E.sort.addEventListener('change', () => { st.sort = E.sort.value; applyFilters(); });
  E.filter.addEventListener('change', () => { st.filter = E.filter.value; applyFilters(); });
  $$('input[name="tag-mode"]').forEach((r) => r.addEventListener('change', () => {
    st.tagMode = r.value;
    if (st.tags.length > 1) applyFilters(); else writeURL();
  }));

  function clearFilters() {
    st.q = ''; st.tags = []; st.filter = '';
    syncControls();
    renderTags();
    applyFilters();
  }

  // =========================================================================
  // Grid: server paging + infinite scroll
  // =========================================================================
  async function loadPage(reset) {
    if (reset) {
      grid.gen++;
      grid.items = [];
      grid.offset = 0;
      grid.total = 0;
      grid.error = null;
      grid.loading = false;
      cards.clear();
      clear(E.grid);
      sel.last = -1;
    }
    if (grid.loading || (grid.loadedOnce && !reset && grid.offset >= grid.total)) return;
    const gen = grid.gen;
    if (reset) E.stale.hidden = true; // any full reload clears the pill
    grid.loading = true;
    renderStatus();
    const params = new URLSearchParams({ offset: grid.offset, limit: D.pageSize, sort: st.sort });
    if (st.q) params.set('q', st.q);
    if (st.tags.length) { params.set('tags', st.tags.join(',')); params.set('tag_mode', st.tagMode); }
    if (st.filter) params.set('filter', st.filter);
    try {
      const res = await fetchJSON(`${API}/items?${params}`);
      if (gen !== grid.gen) return;
      const items = res.items || [];
      grid.total = res.total || 0;
      grid.offset += items.length;
      grid.loadedOnce = true;
      if (!items.length) grid.offset = grid.total; // guard against a short server page
      appendCards(items);
    } catch (e) {
      if (gen !== grid.gen) return;
      grid.error = e.message;
    } finally {
      if (gen === grid.gen) {
        grid.loading = false;
        E.grid.removeAttribute('aria-busy');
        renderStatus();
        maybeLoadMore();
      }
    }
  }

  function appendCards(items) {
    const frag = document.createDocumentFragment();
    for (const item of items) {
      if (cards.has(item.id)) continue;
      grid.items.push(item);
      const card = buildCard(item);
      cards.set(item.id, card);
      frag.append(card);
    }
    E.grid.append(frag);
    updateBulk();
    if (st.item && dlg.open) updateItemNav();
  }

  function maybeLoadMore() {
    if (grid.loading || grid.error || grid.offset >= grid.total) return;
    const r = E.sentinel.getBoundingClientRect();
    if (r.top < window.innerHeight + 800) loadPage(false);
  }
  if ('IntersectionObserver' in window) {
    new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) maybeLoadMore();
    }, { rootMargin: '800px 0px' }).observe(E.sentinel);
  }
  // Fallback: card heights settle late (content-visibility), so also check on scroll.
  let scrollPending = false;
  window.addEventListener('scroll', () => {
    if (scrollPending) return;
    scrollPending = true;
    setTimeout(() => { scrollPending = false; maybeLoadMore(); }, 200);
  }, { passive: true });

  function renderStatus() {
    clear(E.status);
    const shown = grid.items.length;
    E.count.textContent = grid.loadedOnce
      ? (hasFilters() ? `${plural(grid.total, 'matching item')}` : `${plural(grid.total, 'item')}`) + (shown < grid.total ? ` · showing ${formatNumber(shown)}` : '')
      : '';
    if (grid.loading) {
      E.status.append(el('div', { class: 'loading-row' }, el('span', { class: 'spinner', 'aria-hidden': 'true' }), shown ? 'Loading more…' : 'Loading items…'));
      return;
    }
    if (grid.error) {
      E.status.append(el('div', { class: 'empty-state compact error' },
        el('p', { text: `Couldn't load items: ${grid.error}` }),
        el('button', { type: 'button', class: 'btn btn-secondary btn-sm', text: 'Retry', on: { click: () => { grid.error = null; loadPage(!grid.items.length); } } })));
      return;
    }
    if (grid.loadedOnce && !grid.total) {
      E.status.append(hasFilters()
        ? el('div', { class: 'empty-state compact' }, el('p', { text: 'No items match these filters.' }),
          el('button', { type: 'button', class: 'btn btn-secondary btn-sm', text: 'Clear filters', on: { click: clearFilters } }))
        : el('div', { class: 'empty-state compact' }, el('p', { text: 'No items are archived for this game yet.' }),
          el('a', { class: 'btn btn-primary btn-sm', href: '/', text: 'Start an archive job' })));
    }
  }

  function buildCard(item) {
    const media = el('span', { class: 'card-media' });
    if (item.has_preview) {
      const img = el('img', { src: previewUrl(item.id), alt: '', width: 320, height: 180, loading: 'lazy', decoding: 'async' });
      img.addEventListener('error', () => img.replaceWith(placeholder(item)), { once: true });
      media.append(img);
    } else {
      media.append(placeholder(item));
    }
    const badges = el('span', { class: 'card-badges' },
      item.removed ? el('span', { class: 'badge badge-danger', text: 'Removed from Workshop' }) : null,
      item.outdated ? el('span', { class: 'badge badge-warn', text: 'Update available' }) : null,
      item.is_bin ? el('span', { class: 'badge badge-muted', text: '.bin' }) : null);
    const tags = item.tags || [];
    const body = el('span', { class: 'card-body' },
      el('span', { class: 'card-title', text: item.title || `Item ${item.id}` }),
      tags.length ? el('span', { class: 'chips chips-sm' },
        tags.slice(0, 3).map((t) => el('span', { class: 'chip chip-sm', text: t })),
        tags.length > 3 ? el('span', { class: 'chip chip-sm chip-more', text: `+${tags.length - 3}` }) : null) : null,
      item.excerpt ? el('span', { class: 'card-excerpt', text: item.excerpt }) : null,
      el('span', { class: 'card-meta muted' },
        el('span', { text: formatBytes(item.size) }),
        item.time_updated ? el('span', { title: formatDateTime(item.time_updated), text: `Updated ${formatDate(item.time_updated)}` }) : null));

    const open = el('button', { type: 'button', class: 'card-open', 'aria-haspopup': 'dialog' }, media, badges, body);
    const check = el('input', { type: 'checkbox', class: 'card-check', 'aria-label': `Select ${item.title || item.id}`, tabindex: sel.on ? null : '-1' });
    check.checked = sel.ids.has(item.id);
    const card = el('div', { class: 'card', role: 'listitem', dataset: { id: item.id } }, open, check);
    card.classList.toggle('selected', check.checked);

    open.addEventListener('click', (e) => {
      if (sel.on) toggleSelect(item.id, !sel.ids.has(item.id), e.shiftKey);
      else openItem(item.id);
    });
    check.addEventListener('click', (e) => toggleSelect(item.id, check.checked, e.shiftKey));
    return card;
  }

  function placeholder(item) {
    return el('span', { class: 'card-ph', 'aria-hidden': 'true', text: item.is_bin ? '.bin' : '◇' });
  }

  // =========================================================================
  // Bulk selection
  // =========================================================================
  function setSelectMode(on) {
    sel.on = on;
    E.grid.classList.toggle('selecting', on);
    E.selectToggle.setAttribute('aria-pressed', on ? 'true' : 'false');
    E.selectToggle.textContent = on ? 'Done selecting' : 'Select';
    if (!on) sel.ids.clear();
    sel.last = -1;
    for (const [id, card] of cards) syncCard(id, card);
    E.bulk.hidden = !on;
    updateBulk();
  }
  E.selectToggle.addEventListener('click', () => setSelectMode(!sel.on));

  function syncCard(id, card) {
    const check = card.querySelector('.card-check');
    const on = sel.ids.has(id);
    check.checked = on;
    check.tabIndex = sel.on ? 0 : -1;
    card.classList.toggle('selected', on);
  }

  function toggleSelect(id, on, range) {
    const idx = grid.items.findIndex((i) => i.id === id);
    const ids = range && sel.last >= 0 && idx >= 0
      ? grid.items.slice(Math.min(sel.last, idx), Math.max(sel.last, idx) + 1).map((i) => i.id) : [id];
    for (const x of ids) { if (on) sel.ids.add(x); else sel.ids.delete(x); syncCard(x, cards.get(x)); }
    sel.last = idx;
    if (!sel.on) setSelectMode(true);
    updateBulk();
  }

  function updateBulk() {
    const n = sel.ids.size;
    E.bulkCount.textContent = `${formatNumber(n)} selected`;
    E.bulkRedl.disabled = !n;
    E.bulkDelete.disabled = !n;
    E.bulkZip.disabled = !n || n > D.zipMax;
    E.bulkNote.hidden = n <= D.zipMax;
    E.bulkNote.textContent = n > D.zipMax ? `A .zip can hold at most ${D.zipMax} items. Deselect ${formatNumber(n - D.zipMax)} to download.` : '';
  }

  $('#bulk-all').addEventListener('click', () => {
    for (const i of grid.items) sel.ids.add(i.id);
    for (const [id, card] of cards) syncCard(id, card);
    updateBulk();
  });
  $('#bulk-none').addEventListener('click', () => {
    sel.ids.clear();
    for (const [id, card] of cards) syncCard(id, card);
    updateBulk();
  });
  E.bulkZip.addEventListener('click', () => {
    const ids = [...sel.ids];
    if (!ids.length || ids.length > D.zipMax) return;
    triggerDownload(`${API}/zip?ids=${ids.map(encodeURIComponent).join(',')}`);
    toast(`Preparing a .zip of ${plural(ids.length, 'item')}…`, 'info');
  });
  E.bulkRedl.addEventListener('click', () => redownload([...sel.ids], E.bulkRedl));
  E.bulkDelete.addEventListener('click', () => deleteItems([...sel.ids], E.bulkDelete));

  function triggerDownload(url) {
    const a = el('a', { href: url, download: '', hidden: true });
    document.body.append(a);
    a.click();
    a.remove();
  }

  async function redownload(ids, button) {
    if (!ids.length) return;
    await withBusy(button, 'Queuing…', async () => {
      await fetchJSON('/api/runs', {
        method: 'POST',
        body: {
          kind: 'archive', appid: APPID, source_type: 'items', source_value: ids.join('\n'),
          pages: D.pages, sort_key: D.sortKey, delay_min: D.delayMin, delay_max: D.delayMax,
          scrape_only: false, download_only: false, fetch_sizes: false, force: true,
        },
      });
      toast(`Re-download of ${plural(ids.length, 'item')} queued.`, 'success');
    });
  }

  async function deleteItems(ids, button) {
    if (!ids.length) return false;
    const one = ids.length === 1 ? grid.items.find((i) => i.id === ids[0]) : null;
    const ok = await confirmDialog({
      title: one ? `Delete "${one.title || one.id}"?` : `Delete ${plural(ids.length, 'item')}?`,
      message: 'The downloaded files are permanently removed from the archive.',
      confirmLabel: 'Delete', danger: true,
    });
    if (!ok) return false;
    let deleted = false;
    await withBusy(button, 'Deleting…', async () => {
      if (ids.length === 1) await fetchJSON(`${API}/items/${encodeURIComponent(ids[0])}`, { method: 'DELETE' });
      else await fetchJSON(`${API}/items/delete`, { method: 'POST', body: { ids } });
      removeItems(ids);
      toast(`Deleted ${plural(ids.length, 'item')}.`, 'success');
      deleted = true;
    });
    return deleted;
  }

  // Remove cards in place without resetting the grid or scroll position.
  function removeItems(ids) {
    const gone = new Set(ids);
    let removed = 0;
    for (const id of gone) {
      const card = cards.get(id);
      if (card) { card.remove(); cards.delete(id); removed++; }
      sel.ids.delete(id);
    }
    grid.items = grid.items.filter((i) => !gone.has(i.id));
    grid.total = Math.max(0, grid.total - removed);
    grid.offset = Math.max(0, grid.offset - removed);
    sel.last = -1;
    updateBulk();
    renderStatus();
    loadSummary();
    maybeLoadMore();
  }

  // =========================================================================
  // Item detail dialog
  // =========================================================================
  const dlg = $('#item-dialog');
  const I = {
    banner: $('#item-banner'), title: $('#item-title'), notices: $('#item-notices'), tags: $('#item-tags'),
    meta: $('#item-meta'), download: $('#item-download'), steam: $('#item-steam'), redl: $('#item-redownload'),
    del: $('#item-delete'), desc: $('#item-desc'), prev: $('#item-prev'), next: $('#item-next'), body: $('#item-body'),
  };
  let detailSeq = 0;

  function openItem(id) {
    st.item = String(id);
    writeURL();
    const base = grid.items.find((i) => i.id === st.item) || { id: st.item };
    renderDetail(base, true);
    if (!dlg.open) {
      const card = cards.get(st.item);
      openDialog(dlg, { returnFocus: card ? card.querySelector('.card-open') : null });
    }
    fetchDetail(st.item);
  }

  async function fetchDetail(id) {
    const seq = ++detailSeq;
    try {
      const item = await fetchJSON(`${API}/items/${encodeURIComponent(id)}`);
      if (seq !== detailSeq) return;
      renderDetail(Object.assign({ id }, item), false);
    } catch (e) {
      if (seq !== detailSeq) return;
      clear(I.desc).append(el('p', { class: 'text-danger', text: e.status === 404 ? 'This item is no longer in the archive.' : `Couldn't load details: ${e.message}` }));
    }
  }

  function renderDetail(item, loading) {
    I.title.textContent = item.title || `Item ${item.id}`;
    clear(I.banner);
    if (item.has_preview) I.banner.append(el('img', { src: previewUrl(item.id), alt: '', decoding: 'async' }));
    I.banner.hidden = !item.has_preview;

    clear(I.notices);
    if (item.removed) I.notices.append(el('p', { class: 'callout callout-danger', text: 'Removed from the Steam Workshop (or made private). Your local copy is kept.' }));
    if (item.outdated) I.notices.append(el('p', { class: 'callout callout-warn', text: 'A newer version is on the Workshop. Re-download to update your copy.' }));
    if (!loading && item.has_metadata === false) I.notices.append(el('p', { class: 'callout callout-info', text: 'Metadata hasn\'t been fetched for this item yet. Use "Refresh metadata" to load its description and tags.' }));

    clear(I.tags).append(...(item.tags || []).map((t) => el('span', { class: 'chip', text: t })));

    const rows = [
      ['Size', formatBytes(item.file_size || item.size)],
      ['Updated', item.time_updated ? formatDateTime(item.time_updated) : null],
      ['Created', item.time_created ? formatDateTime(item.time_created) : null],
      ['Downloaded', item.downloaded_at ? formatDateTime(item.downloaded_at) : null],
      ['Creator', item.creator ? String(item.creator) : null],
      ['Item ID', String(item.id)],
    ];
    clear(I.meta);
    for (const [k, v] of rows) if (v) I.meta.append(el('div', null, el('dt', { text: k }), el('dd', { text: v })));

    I.download.href = downloadUrl(item.id);
    I.download.textContent = item.is_bin ? 'Download (.bin)' : 'Download (.zip)';
    I.steam.href = /^https:\/\/(steamcommunity\.com|store\.steampowered\.com)\//.test(item.steam_url || '')
      ? item.steam_url : `https://steamcommunity.com/sharedfiles/filedetails/?id=${encodeURIComponent(item.id)}`;

    clear(I.desc);
    if (loading) I.desc.append(el('div', { class: 'loading-row' }, el('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Loading details…'));
    else if (item.description) I.desc.append(globalThis.renderBBCode(item.description, { imageMap: item.desc_image_map || {} }));
    else I.desc.append(el('p', { class: 'muted', text: item.has_metadata === false ? 'No description yet.' : 'No description.' }));

    updateItemNav();
    if (loading) I.body.scrollTop = 0;
  }

  function updateItemNav() {
    const idx = grid.items.findIndex((i) => i.id === st.item);
    I.prev.disabled = idx <= 0;
    I.next.disabled = idx < 0 || (idx >= grid.items.length - 1 && grid.offset >= grid.total);
  }

  async function step(delta) {
    let idx = grid.items.findIndex((i) => i.id === st.item);
    if (idx < 0) return;
    if (delta > 0 && idx >= grid.items.length - 1 && grid.offset < grid.total) {
      await loadPage(false);
      idx = grid.items.findIndex((i) => i.id === st.item);
    }
    const target = grid.items[idx + delta];
    if (!target) return;
    openItem(target.id);
    dlg._returnFocus = cards.get(target.id) ? cards.get(target.id).querySelector('.card-open') : dlg._returnFocus;
    const card = cards.get(target.id);
    if (card) card.scrollIntoView({ block: 'nearest' });
  }

  I.prev.addEventListener('click', () => step(-1));
  I.next.addEventListener('click', () => step(1));
  dlg.addEventListener('keydown', (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey || /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1); }
    if (e.key === 'ArrowRight') { e.preventDefault(); step(1); }
  });
  dlg.addEventListener('close', () => { st.item = null; detailSeq++; writeURL(); });

  I.redl.addEventListener('click', () => st.item && redownload([st.item], I.redl));
  I.del.addEventListener('click', async () => {
    const id = st.item;
    if (!id) return;
    const idx = grid.items.findIndex((i) => i.id === id);
    if (!(await deleteItems([id], I.del))) return;
    const next = grid.items[idx] || grid.items[idx - 1];
    if (next) openItem(next.id); else dlg.close();
  });

  // =========================================================================
  // Metadata refresh (tracked through the live events stream)
  // =========================================================================
  let metaRunId = null;
  let staleMutedUntil = 0; // ignore archive_changed caused by a metadata run we just reloaded for

  function findMetaRun(s) {
    const mine = (r) => r && r.kind === 'metadata' && String(r.appid) === String(APPID) && SWA.isActive(r);
    if (mine(s.current)) return s.current;
    return s.queue.find(mine) || null;
  }

  function renderMeta(s) {
    const run = findMetaRun(s);
    if (run) metaRunId = run.id;
    E.metaBtn.disabled = !!run;
    if (!run) { E.metaStatus.textContent = ''; return; }
    if (run.status === 'queued') {
      const pos = s.queue.findIndex((r) => r.id === run.id) + 1;
      E.metaStatus.textContent = `Metadata refresh queued${pos ? ` (#${pos})` : ''}`;
    } else {
      E.metaStatus.textContent = `Refreshing metadata${run.total ? ` ${run.done}/${run.total}` : '…'}`;
    }
  }

  E.metaBtn.addEventListener('click', () => withBusy(E.metaBtn, 'Queuing…', async () => {
    const run = await fetchJSON(`${API}/refresh-metadata`, { method: 'POST' });
    metaRunId = run.id;
    toast('Metadata refresh queued. The grid reloads when it finishes.', 'success');
    E.metaStatus.textContent = 'Metadata refresh queued';
    E.metaBtn.disabled = true;
  }).then(() => renderMeta(events.state)));

  events.on('state', renderMeta);
  events.on('run_finished', (msg) => {
    const r = msg.run;
    if (String(r.appid) !== String(APPID)) return;
    if (r.kind === 'metadata' && (metaRunId === null || r.id === metaRunId)) {
      metaRunId = null;
      staleMutedUntil = Date.now() + 10000;
      toast(`Metadata refresh ${r.status === 'done' ? 'finished' : SWA.ui.STATUS_LABELS[r.status] || r.status}.`, r.status === 'done' ? 'success' : 'error');
      loadSummary();
      loadPage(true);
      if (st.item && dlg.open) fetchDetail(st.item);
    }
  });
  events.on('archive_changed', (msg) => {
    if (String(msg.appid) !== String(APPID)) return;
    loadSummary();
    if (metaRunId === null && grid.loadedOnce && Date.now() > staleMutedUntil) E.stale.hidden = false;
  });
  E.stale.addEventListener('click', () => loadPage(true));

  // =========================================================================
  // Delete whole archive
  // =========================================================================
  $('#archive-delete').addEventListener('click', async (e) => {
    const button = e.currentTarget;
    const name = gameLabel();
    const res = await confirmDialog({
      title: `Delete the ${name} archive?`,
      message: `All ${summary ? plural(summary.item_count || 0, 'downloaded item') : 'downloaded items'} (${summary ? formatBytes(summary.total_size) : 'unknown size'}) are permanently removed.`,
      checkboxes: [
        { name: 'purge_metadata', label: 'Also delete cached metadata and images', checked: false },
        { name: 'delete_saved_jobs', label: 'Also delete scheduled jobs for this game', checked: false },
      ],
      requireText: name,
      confirmLabel: 'Delete archive', danger: true,
    });
    if (!res) return;
    const params = new URLSearchParams();
    if (res.checks.purge_metadata) params.set('purge_metadata', '1');
    if (res.checks.delete_saved_jobs) params.set('delete_saved_jobs', '1');
    await withBusy(button, 'Deleting…', async () => {
      await fetchJSON(`${API}${params.toString() ? `?${params}` : ''}`, { method: 'DELETE' });
      flash(`Deleted the ${name} archive.`);
      location.href = '/';
    });
  });

  // =========================================================================
  // Boot
  // =========================================================================
  readURL();
  syncControls();
  loadHero();
  loadSummary();
  loadPage(true);
  if (st.item) openItem(st.item);
  renderMeta(events.state);
})();
