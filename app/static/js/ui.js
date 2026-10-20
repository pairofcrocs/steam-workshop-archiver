/* UI helpers: DOM builder, toasts, dialogs, formatters, debounce, busy buttons. */
(function () {
  'use strict';
  const SWA = (globalThis.SWA = globalThis.SWA || {});

  // ---- DOM builder -------------------------------------------------------
  // el('button', {class: 'btn', text: 'Go', on: {click: fn}, dataset: {id: 1}}, child, ...)
  const PROPS = new Set(['hidden', 'disabled', 'checked', 'value', 'selected', 'indeterminate']);
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === undefined || v === null || v === false) continue;
        if (k === 'class') node.className = v;
        else if (k === 'text') node.textContent = v;
        else if (k === 'on') for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
        else if (k === 'dataset') Object.assign(node.dataset, v);
        else if (PROPS.has(k)) node[k] = v;
        else node.setAttribute(k, v === true ? '' : String(v));
      }
    }
    append(node, children);
    return node;
  }
  function append(node, children) {
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      node.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
    }
    return node;
  }
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

  // ---- Formatters --------------------------------------------------------
  function formatBytes(n) {
    if (n === null || n === undefined || isNaN(n)) return '–';
    n = Number(n);
    if (n < 1024) return `${n} B`;
    const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return `${n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2)} ${units[i]}`;
  }
  function formatNumber(n) { return Number(n || 0).toLocaleString(); }

  const RTF = typeof Intl !== 'undefined' && Intl.RelativeTimeFormat
    ? new Intl.RelativeTimeFormat(undefined, { numeric: 'auto', style: 'short' }) : null;
  const REL_STEPS = [[60, 'second'], [3600, 'minute', 60], [86400, 'hour', 3600],
    [86400 * 30, 'day', 86400], [86400 * 365, 'month', 86400 * 30], [Infinity, 'year', 86400 * 365]];
  // Epoch seconds -> "5 min. ago" / "in 3 hr."
  function formatRelative(ts) {
    if (!ts) return 'never';
    const diff = ts - Date.now() / 1000;
    const abs = Math.abs(diff);
    if (abs < 45) return diff < 0 ? 'just now' : 'in a moment';
    for (const [limit, unit, div] of REL_STEPS) {
      if (abs < limit) {
        const v = Math.round(diff / (div || 1));
        return RTF ? RTF.format(v, unit) : `${v} ${unit}s`;
      }
    }
    return '';
  }
  function formatDateTime(ts) {
    if (!ts) return '–';
    return new Date(ts * 1000).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }
  function formatDate(ts) {
    if (!ts) return '–';
    return new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }
  function formatDuration(sec) {
    if (sec === null || sec === undefined || isNaN(sec) || sec < 0) return '–';
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
    if (m) return `${m}m ${String(s).padStart(2, '0')}s`;
    return `${s}s`;
  }
  // <time> element: relative text, absolute on hover.
  function timeEl(ts, { relative = true } = {}) {
    if (!ts) return el('span', { class: 'muted', text: 'never' });
    return el('time', {
      datetime: new Date(ts * 1000).toISOString(),
      title: formatDateTime(ts),
      text: relative ? formatRelative(ts) : formatDateTime(ts),
    });
  }

  // ---- Misc --------------------------------------------------------------
  function debounce(fn, ms) {
    let t = null;
    const d = (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
    d.cancel = () => clearTimeout(t);
    return d;
  }

  // setBusy(btn, true, 'Saving…') disables + relabels; setBusy(btn, false) restores.
  function setBusy(btn, busy, label) {
    if (!btn) return;
    if (busy) {
      if (btn.dataset.busy === '1') return;
      btn.dataset.busy = '1';
      btn.dataset.wasDisabled = btn.disabled ? '1' : '';
      btn._label = Array.from(btn.childNodes);
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      clear(btn).append(el('span', { class: 'spinner spinner-sm', 'aria-hidden': 'true' }), label || btn.textContent || '…');
    } else if (btn.dataset.busy === '1') {
      clear(btn).append(...(btn._label || []));
      btn.disabled = btn.dataset.wasDisabled === '1';
      btn.removeAttribute('aria-busy');
      delete btn.dataset.busy;
    }
  }
  // Run an async action with the button busy; errors become toasts.
  async function withBusy(btn, label, fn) {
    setBusy(btn, true, label);
    try { return await fn(); } catch (e) { if (e && e.name !== 'AbortError') toast(e.message, 'error'); return undefined; } finally { setBusy(btn, false); }
  }

  const STATUS_LABELS = {
    queued: 'Queued', running: 'Running', waiting_confirm: 'Awaiting confirmation', done: 'Done',
    partial: 'Partial', error: 'Error', cancelled: 'Cancelled',
  };
  function statusBadge(status) {
    if (!status) return el('span', { class: 'badge badge-muted', text: 'Never run' });
    const active = status === 'running' || status === 'waiting_confirm';
    return el('span', { class: `badge badge-${status}` },
      active ? el('span', { class: 'dot pulse', 'aria-hidden': 'true' }) : null,
      STATUS_LABELS[status] || status);
  }

  function sourceLabel(o) {
    const t = o.source_type || 'workshop';
    if (t === 'collection') return 'Collection';
    if (t === 'items') {
      const n = String(o.source_value || '').split(/[\s,]+/).filter(Boolean).length;
      return n ? `${n} item${n === 1 ? '' : 's'}` : 'Specific items';
    }
    return 'Whole Workshop';
  }

  // ---- Toasts ------------------------------------------------------------
  function toast(message, type = 'info', { timeout } = {}) {
    const host = document.getElementById('toasts');
    if (!host) { console.log(`[${type}]`, message); return; }
    const t = el('div', { class: `toast toast-${type}`, role: type === 'error' ? 'alert' : 'status' },
      el('span', { class: 'toast-msg', text: String(message) }),
      el('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-sm', 'aria-label': 'Dismiss notification', text: '✕', on: { click: () => close() } }));
    const close = () => { t.classList.add('leaving'); setTimeout(() => t.remove(), 200); };
    host.append(t);
    const ms = timeout ?? (type === 'error' ? 8000 : 4000);
    if (ms > 0) setTimeout(close, ms);
    return close;
  }

  // ---- Dialogs -----------------------------------------------------------
  // Open a native <dialog> modally; focus returns to the opener on close.
  function openDialog(dialog, { returnFocus, focus } = {}) {
    const opener = returnFocus || document.activeElement;
    if (!dialog.dataset.wired) {
      dialog.dataset.wired = '1';
      // Click on the backdrop (outside the dialog box) closes it.
      dialog.addEventListener('mousedown', (e) => { dialog._downOnBackdrop = e.target === dialog; });
      dialog.addEventListener('click', (e) => {
        if (e.target === dialog && dialog._downOnBackdrop && !dialog.hasAttribute('data-no-light-dismiss')) dialog.close('dismiss');
      });
      $$('[data-close]', dialog).forEach((b) => b.addEventListener('click', () => dialog.close('cancel')));
    }
    dialog.addEventListener('close', () => {
      const target = dialog._returnFocus;
      if (target && target.isConnected && typeof target.focus === 'function') target.focus();
    }, { once: true });
    dialog._returnFocus = opener;
    if (!dialog.open) dialog.showModal();
    const f = focus ? (typeof focus === 'string' ? $(focus, dialog) : focus) : null;
    if (f) f.focus();
    document.documentElement.classList.add('modal-open');
    dialog.addEventListener('close', () => {
      if (!document.querySelector('dialog[open]')) document.documentElement.classList.remove('modal-open');
    }, { once: true });
  }

  let dialogSeq = 0;
  /**
   * confirmDialog({title, message, confirmLabel, danger, checkboxes:[{name,label,checked}], requireText})
   * Resolves {checks: {name: bool}} on confirm, or null on cancel.
   */
  function confirmDialog(opts) {
    const id = `cdlg-${++dialogSeq}`;
    const checks = (opts.checkboxes || []).map((c) => {
      const input = el('input', { type: 'checkbox', name: c.name, checked: !!c.checked });
      return { name: c.name, input, row: el('label', { class: 'check' }, input, el('span', { text: c.label })) };
    });
    let typed = null;
    if (opts.requireText) {
      typed = el('input', { type: 'text', id: `${id}-type`, autocomplete: 'off', spellcheck: 'false' });
    }
    const okBtn = el('button', { type: 'submit', class: `btn ${opts.danger ? 'btn-danger' : 'btn-primary'}`, text: opts.confirmLabel || 'Confirm' });
    const cancelBtn = el('button', { type: 'button', class: 'btn btn-secondary', text: opts.cancelLabel || 'Cancel', 'data-close': '' });
    const body = el('div', { class: 'dlg-body' },
      opts.message ? el('p', { id: `${id}-desc`, class: 'dlg-message', text: opts.message }) : null,
      opts.details || null,
      checks.length ? el('div', { class: 'stack-sm' }, checks.map((c) => c.row)) : null,
      typed ? el('div', { class: 'field' },
        el('label', { class: 'label', for: typed.id }, 'Type ', el('strong', { class: 'mono', text: opts.requireText }), ' to confirm'),
        typed) : null);
    const form = el('form', { method: 'dialog', class: 'dlg-form' },
      el('header', { class: 'dlg-head' }, el('h2', { id: `${id}-title`, class: 'dlg-title', text: opts.title || 'Are you sure?' })),
      body,
      el('footer', { class: 'dlg-foot' }, el('span', { class: 'spacer' }), cancelBtn, okBtn));
    const dialog = el('dialog', { class: 'dlg dlg-sm', 'aria-labelledby': `${id}-title`, 'aria-describedby': opts.message ? `${id}-desc` : null }, form);
    if (typed) {
      const sync = () => { okBtn.disabled = typed.value.trim().toLowerCase() !== opts.requireText.trim().toLowerCase(); };
      typed.addEventListener('input', sync);
      sync();
    }
    document.body.append(dialog);
    return new Promise((resolve) => {
      let result = null;
      form.addEventListener('submit', (e) => {
        if (okBtn.disabled) { e.preventDefault(); return; }
        result = { checks: Object.fromEntries(checks.map((c) => [c.name, c.input.checked])) };
      });
      dialog.addEventListener('close', () => { resolve(result); setTimeout(() => dialog.remove(), 0); }, { once: true });
      openDialog(dialog, { focus: typed || (opts.danger ? cancelBtn : okBtn) });
    });
  }

  // Inline field errors: setFieldError(input, 'msg') / setFieldError(input, '')
  function setFieldError(input, message) {
    if (!input) return;
    const field = input.closest('.field') || input.parentElement;
    let err = field.querySelector(':scope > .field-error');
    if (!err) {
      err = el('p', { class: 'field-error', id: (input.id || `f${++dialogSeq}`) + '-err', hidden: true });
      field.append(err);
    }
    err.textContent = message || '';
    err.hidden = !message;
    if (message) {
      input.setAttribute('aria-invalid', 'true');
      const ids = new Set((input.getAttribute('aria-describedby') || '').split(' ').filter(Boolean));
      ids.add(err.id);
      input.setAttribute('aria-describedby', [...ids].join(' '));
    } else {
      input.removeAttribute('aria-invalid');
    }
  }

  // One-shot flash message carried across a navigation.
  function flash(message, type = 'success') {
    try { sessionStorage.setItem('swa-flash', JSON.stringify({ message, type })); } catch (_) { /* ignore */ }
  }
  function showFlash() {
    try {
      const raw = sessionStorage.getItem('swa-flash');
      if (!raw) return;
      sessionStorage.removeItem('swa-flash');
      const { message, type } = JSON.parse(raw);
      if (message) toast(message, type);
    } catch (_) { /* ignore */ }
  }

  SWA.ui = {
    el, append, $, $$, clear, formatBytes, formatNumber, formatRelative, formatDateTime, formatDate,
    formatDuration, timeEl, debounce, setBusy, withBusy, statusBadge, sourceLabel, STATUS_LABELS,
    toast, openDialog, confirmDialog, setFieldError, flash, showFlash,
  };
})();
