/* Shared API helpers: fetchJSON, defaults, and the live Events client. */
(function () {
  'use strict';
  const SWA = (globalThis.SWA = globalThis.SWA || {});

  SWA.DEFAULTS = Object.freeze({
    pages: 'all',
    delayMin: 1.0,
    delayMax: 2.5,
    sortKey: 'trend_week',
    pageSize: 60,
    zipMax: 500,
  });

  SWA.TERMINAL = new Set(['done', 'partial', 'error', 'cancelled']);
  SWA.isActive = (run) => !!run && !SWA.TERMINAL.has(run.status);

  // Turn a FastAPI error body ({detail: string | [{loc, msg}]}) into readable text.
  function errorMessage(body, status, statusText) {
    const d = body && typeof body === 'object' ? body.detail : null;
    if (typeof d === 'string' && d) return d;
    if (Array.isArray(d) && d.length) {
      return d.map((e) => {
        if (!e || typeof e !== 'object') return String(e);
        const loc = Array.isArray(e.loc) ? e.loc.filter((p) => p !== 'body').join('.') : '';
        return (loc ? loc + ': ' : '') + (e.msg || JSON.stringify(e));
      }).join('; ');
    }
    if (d && typeof d === 'object') return JSON.stringify(d);
    if (status === 401) return 'Authentication required.';
    if (status === 403) return 'Request was refused (403).';
    return `Request failed (${status}${statusText ? ' ' + statusText : ''})`;
  }

  /**
   * fetch wrapper: JSON-encodes plain-object bodies, adds the CSRF header on
   * non-GET requests, throws Error(message) with .status on non-2xx.
   * Returns parsed JSON, text for non-JSON responses, or null for 204.
   */
  async function fetchJSON(url, opts = {}) {
    const method = (opts.method || 'GET').toUpperCase();
    const headers = new Headers(opts.headers || {});
    if (!headers.has('Accept')) headers.set('Accept', 'application/json');
    let body = opts.body;
    if (body !== undefined && body !== null && typeof body !== 'string' && !(body instanceof FormData)) {
      body = JSON.stringify(body);
      headers.set('Content-Type', 'application/json');
    }
    if (method !== 'GET' && method !== 'HEAD') headers.set('X-SWA', '1');

    let res;
    try {
      res = await fetch(url, { method, headers, body, signal: opts.signal, credentials: 'same-origin' });
    } catch (e) {
      if (e && e.name === 'AbortError') throw e;
      throw new Error('Network error: the server could not be reached.');
    }

    let data = null;
    if (res.status !== 204) {
      const ct = res.headers.get('content-type') || '';
      if (ct.includes('application/json')) {
        try { data = await res.json(); } catch (_) { data = null; }
      } else {
        data = await res.text();
      }
    }
    if (!res.ok) {
      const err = new Error(errorMessage(data, res.status, res.statusText));
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  SWA.fetchJSON = fetchJSON;
  SWA.errorMessage = errorMessage;

  const EVENT_TYPES = ['snapshot', 'run_queued', 'run_started', 'run_updated', 'log',
    'confirm', 'run_finished', 'saved_jobs_changed', 'archive_changed'];
  const LOG_TAIL_MAX = 2000;

  /**
   * Wraps EventSource('/api/events'). Keeps a small state mirror
   * ({current, queue, log, confirm}) and dispatches:
   *   - each event type with its payload
   *   - 'state' after anything that changes the mirror
   *   - 'connection' with 'connecting' | 'open' | 'reconnecting'
   */
  class EventsClient {
    constructor(url) {
      this.url = url;
      this.handlers = new Map();
      this.connection = 'connecting';
      this.es = null;
      this.retryTimer = null;
      this.retryDelay = 2000;
      this.state = { current: null, queue: [], log: [], confirm: null, ready: false };
    }

    on(type, fn) {
      if (!this.handlers.has(type)) this.handlers.set(type, new Set());
      this.handlers.get(type).add(fn);
      return () => this.handlers.get(type).delete(fn);
    }

    emit(type, payload) {
      const set = this.handlers.get(type);
      if (!set) return;
      for (const fn of [...set]) {
        try { fn(payload, this.state); } catch (e) { console.error('[events]', type, e); }
      }
    }

    start() {
      if (this.es || typeof EventSource === 'undefined') return;
      clearTimeout(this.retryTimer);
      const es = (this.es = new EventSource(this.url));
      es.onopen = () => { this.retryDelay = 2000; this.setConnection('open'); };
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) {
          // Browser gave up (e.g. non-SSE error response): retry ourselves with backoff.
          es.close();
          this.es = null;
          this.retryTimer = setTimeout(() => this.start(), this.retryDelay);
          this.retryDelay = Math.min(this.retryDelay * 2, 30000);
        }
        this.setConnection('reconnecting');
      };
      const handle = (e) => {
        let msg;
        try { msg = JSON.parse(e.data); } catch (_) { return; }
        if (!msg || typeof msg !== 'object') return;
        if (!msg.type && e.type !== 'message') msg.type = e.type;
        this.dispatch(msg);
      };
      es.onmessage = handle;
      // Also accept named SSE events, in case the server sets `event:` lines.
      for (const t of EVENT_TYPES) es.addEventListener(t, handle);
    }

    setConnection(state) {
      if (this.connection === state) return;
      this.connection = state;
      this.emit('connection', state);
    }

    dispatch(msg) {
      const changed = this.apply(msg);
      this.emit(msg.type, msg);
      if (changed) this.emit('state', this.state);
    }

    // Update the state mirror; returns true if it changed.
    apply(msg) {
      const s = this.state;
      const run = msg.run;
      switch (msg.type) {
        case 'snapshot':
          s.current = msg.current || null;
          s.queue = Array.isArray(msg.queue) ? msg.queue : [];
          s.log = Array.isArray(msg.log) ? msg.log.slice(-LOG_TAIL_MAX) : [];
          s.confirm = msg.confirm && s.current && s.current.status === 'waiting_confirm' ? msg.confirm : null;
          s.ready = true;
          return true;
        case 'run_queued':
          if (!s.queue.some((r) => r.id === run.id)) s.queue = [...s.queue, run];
          return true;
        case 'run_started':
          s.queue = s.queue.filter((r) => r.id !== run.id);
          s.current = run;
          s.log = [];
          s.confirm = null;
          return true;
        case 'run_updated':
          if (s.current && s.current.id === run.id) {
            s.current = run;
            if (run.status !== 'waiting_confirm') s.confirm = null;
          } else {
            s.queue = s.queue.map((r) => (r.id === run.id ? run : r));
          }
          return true;
        case 'run_finished':
          s.queue = s.queue.filter((r) => r.id !== run.id);
          if (s.current && s.current.id === run.id) s.current = run;
          if (s.confirm && s.confirm.run_id === run.id) s.confirm = null;
          return true;
        case 'log':
          if (s.current && s.current.id === msg.run_id) {
            s.log.push(msg.line);
            if (s.log.length > LOG_TAIL_MAX) s.log.splice(0, s.log.length - LOG_TAIL_MAX);
          }
          return false;
        case 'confirm':
          s.confirm = { run_id: msg.run_id, items: msg.items, bytes: msg.bytes, timeout_at: msg.timeout_at };
          return true;
        default:
          return false;
      }
    }
  }

  SWA.EventsClient = EventsClient;
  SWA.events = new EventsClient('/api/events');
})();
