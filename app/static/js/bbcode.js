/*
 * Safe BBCode renderer for Steam Workshop descriptions.
 * bbcodeToTree(text, {imageMap}) -> plain tree {tag, attrs, children} / {text}
 * renderBBCode(text, {imageMap}) -> DocumentFragment built with createElement
 * Never touches innerHTML; unknown or unbalanced tags stay as literal text.
 */
(function (root) {
  'use strict';

  const TAG_RE = /\[(\/?)(\*|[a-z][a-z0-9]{0,9})(?:=([^\]\n]{0,2000}))?\]/gi;
  const PAIRED = new Set(['b', 'i', 'u', 's', 'strike', 'h1', 'h2', 'h3', 'url', 'list', 'olist',
    'quote', 'code', 'spoiler', 'table', 'tr', 'th', 'td']);
  // Tags whose content is taken verbatim (no nested parsing). Steam parses
  // tags inside [code] (authors use it as a styled box), so it isn't here.
  const RAW = new Set(['noparse', 'img']);
  const BLOCK = new Set(['h1', 'h2', 'h3', 'list', 'olist', 'quote', 'code', 'hr', 'table', 'tr']);
  const NO_TEXT_CHILDREN = new Set(['list', 'olist', 'table', 'tr']);

  // Output whitelist (also enforced again when building DOM nodes).
  const ALLOWED_TAGS = new Set(['strong', 'em', 'u', 's', 'h3', 'h4', 'h5', 'a', 'img', 'ul', 'ol', 'li',
    'blockquote', 'div', 'pre', 'code', 'span', 'hr', 'br', 'table', 'tbody', 'tr', 'th', 'td']);
  const ALLOWED_ATTRS = new Set(['href', 'rel', 'target', 'src', 'alt', 'loading', 'decoding',
    'referrerpolicy', 'class', 'tabindex']);
  const LINK_ATTRS = { rel: 'noopener noreferrer nofollow', target: '_blank' };
  const IMG_HOSTS = [/\.steamstatic\.com$/, /\.steamusercontent\.com$/, /\.akamaihd\.net$/, /^steamcommunity\.com$/];

  function safeUrl(raw) {
    if (typeof raw !== 'string') return null;
    let s = raw.trim().replace(/^["']|["']$/g, '');
    if (!/^https?:\/\//i.test(s)) return null;
    try {
      const u = new URL(s);
      if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
      return u;
    } catch (_) {
      return null;
    }
  }

  function allowedImageHost(u) {
    const host = u.hostname.toLowerCase();
    return IMG_HOSTS.some((re) => re.test(host));
  }

  // Only same-origin paths from the server's desc_image_map are trusted.
  function localPath(v) {
    return typeof v === 'string' && /^\/(?!\/)[^\s"'<>\\]*$/.test(v) ? v : null;
  }

  // ---- Parsing: source -> AST ------------------------------------------
  // AST node: {text} | {name, arg, open, children} | {name, raw}
  function parse(src) {
    const rootNode = { name: '#root', children: [] };
    const stack = [rootNode];
    const top = () => stack[stack.length - 1];
    const pushText = (t) => { if (t) top().children.push({ text: t }); };
    let pos = 0;
    TAG_RE.lastIndex = 0;
    let m;
    while ((m = TAG_RE.exec(src))) {
      pushText(src.slice(pos, m.index));
      pos = TAG_RE.lastIndex;
      const closing = m[1] === '/';
      const name = m[2].toLowerCase();
      const arg = m[3];

      if (!closing && RAW.has(name)) {
        const end = indexOfCI(src, `[/${name}]`, pos);
        if (end === -1) { pushText(m[0]); continue; }
        top().children.push({ name, arg, raw: src.slice(pos, end) });
        pos = end + name.length + 3;
        TAG_RE.lastIndex = pos;
        continue;
      }
      if (name === 'hr') {
        if (closing) continue; // tolerate Steam's "[hr][/hr]"
        top().children.push({ name: 'hr', children: [] });
        continue;
      }
      if (name === '*') {
        if (closing) continue; // "[/*]" is sometimes used; items close implicitly
        const listIdx = findLast(stack, (n) => n.name === 'list' || n.name === 'olist');
        if (listIdx === -1) { pushText(m[0]); continue; }
        closeAbove(stack, listIdx);
        stack.push({ name: '*', open: m[0], children: [] });
        continue;
      }
      if (!PAIRED.has(name)) { pushText(m[0]); continue; }
      if (!closing) {
        stack.push({ name, arg, open: m[0], children: [] });
        continue;
      }
      const idx = findLast(stack, (n) => n.name === name);
      if (idx <= 0) { pushText(m[0]); continue; }
      closeAbove(stack, idx);
      const node = stack.pop();
      node.closed = true;
      top().children.push(node);
    }
    pushText(src.slice(pos));
    closeAbove(stack, 0);
    return rootNode;
  }

  function indexOfCI(haystack, needle, from) {
    return haystack.toLowerCase().indexOf(needle.toLowerCase(), from);
  }
  function findLast(arr, pred) {
    for (let i = arr.length - 1; i >= 0; i--) if (pred(arr[i])) return i;
    return -1;
  }
  // Pop everything above stack[idx]: list items close normally, other
  // unclosed tags are unwrapped back into literal text.
  function closeAbove(stack, idx) {
    while (stack.length - 1 > idx) {
      const node = stack.pop();
      const parent = stack[stack.length - 1];
      if (node.name === '*') parent.children.push(node);
      else parent.children.push(...unwrap(node));
    }
  }
  function unwrap(node) {
    const out = [{ text: node.open }];
    for (const c of node.children) {
      if (c.name === '*') out.push({ text: c.open }, ...c.children);
      else out.push(c);
    }
    return out;
  }

  // ---- AST -> output tree ------------------------------------------------
  const el = (tag, attrs, children) => ({ tag, attrs: attrs || {}, children: children || [] });
  const txt = (text) => ({ text });

  function convertChildren(nodes, parentName, ctx) {
    const out = [];
    let prevBlock = parentName !== '#root' && BLOCK.has(parentName);
    nodes.forEach((n, i) => {
      if (n.text !== undefined) {
        let t = n.text;
        if (NO_TEXT_CHILDREN.has(parentName) && !t.trim()) return;
        if (parentName === '*' || parentName === 'td' || parentName === 'th') {
          if (i === 0) t = t.replace(/^\s+/, '');
          if (i === nodes.length - 1) t = t.replace(/\s+$/, '');
        }
        if (prevBlock) t = t.replace(/^[ \t]*\n/, '');
        const next = nodes[i + 1];
        if (next && next.name && BLOCK.has(next.name)) t = t.replace(/\n[ \t]*$/, '');
        out.push(...textWithBreaks(t));
        prevBlock = false;
        return;
      }
      out.push(...convertNode(n, ctx));
      prevBlock = BLOCK.has(n.name);
    });
    return out;
  }

  function textWithBreaks(t) {
    if (!t) return [];
    const parts = t.split('\n');
    const out = [];
    parts.forEach((p, i) => {
      if (i > 0) out.push(el('br'));
      if (p) out.push(txt(p));
    });
    return out;
  }

  const SIMPLE = { b: 'strong', i: 'em', u: 'u', s: 's', strike: 's', list: 'ul', olist: 'ol', '*': 'li',
    tr: 'tr', th: 'th', td: 'td' };
  const HEADINGS = { h1: 'h3', h2: 'h4', h3: 'h5' };

  function convertNode(n, ctx) {
    const kids = () => convertChildren(n.children || [], n.name, ctx);
    switch (n.name) {
      case 'noparse':
        return textWithBreaks(n.raw);
      case 'code':
        return [el('div', { class: 'bb-code' }, kids())];
      case 'img':
        return convertImage(n, ctx);
      case 'hr':
        return [el('hr')];
      case 'url': {
        const label = kids();
        const u = safeUrl(n.arg !== undefined ? n.arg : plainText(n.children));
        if (!u) return n.arg !== undefined ? label : textWithBreaks(plainText(n.children));
        return [el('a', Object.assign({ href: u.href }, LINK_ATTRS), label.length ? label : [txt(u.href)])];
      }
      case 'quote': {
        const children = kids();
        const author = n.arg ? n.arg.trim().replace(/^["']|["']$/g, '').split(';')[0] : '';
        if (author) children.unshift(el('div', { class: 'bb-quote-author' }, [txt(`${author} wrote:`)]));
        return [el('blockquote', { class: 'bb-quote' }, children)];
      }
      case 'spoiler':
        return [el('span', { class: 'bb-spoiler', tabindex: '0' }, kids())];
      case 'table':
        return [el('table', { class: 'bb-table' }, [el('tbody', {}, kids())])];
      default:
        if (HEADINGS[n.name]) return [el(HEADINGS[n.name], { class: `bb-${n.name}` }, kids())];
        if (SIMPLE[n.name]) return [el(SIMPLE[n.name], {}, kids())];
        return kids();
    }
  }

  function convertImage(n, ctx) {
    const raw = n.raw.trim();
    const mapped = ctx.imageMap && Object.prototype.hasOwnProperty.call(ctx.imageMap, raw)
      ? localPath(ctx.imageMap[raw]) : null;
    const imgAttrs = (src) => ({ src, alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer', class: 'bb-img' });
    if (mapped) return [el('img', imgAttrs(mapped))];
    const u = safeUrl(raw);
    if (!u) return textWithBreaks(`[img]${n.raw}[/img]`);
    if (allowedImageHost(u)) {
      u.protocol = 'https:'; // CSP only allows https image hosts
      return [el('img', imgAttrs(u.href))];
    }
    return [el('a', Object.assign({ href: u.href, class: 'bb-img-link' }, LINK_ATTRS), [txt(u.href)])];
  }

  function plainText(nodes) {
    return nodes.map((c) => (c.text !== undefined ? c.text : c.raw !== undefined ? c.raw : plainText(c.children || []))).join('');
  }

  function bbcodeToTree(text, opts) {
    const src = String(text == null ? '' : text).replace(/\r\n?/g, '\n');
    const ctx = { imageMap: (opts && opts.imageMap) || {} };
    const ast = parse(src);
    return el('#fragment', {}, convertChildren(ast.children, '#root', ctx));
  }

  // Escaped HTML serialisation of a tree (used by tests / debugging only).
  function escapeHTML(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  const VOID = new Set(['br', 'hr', 'img']);
  function treeToHTML(node) {
    if (node.text !== undefined) return escapeHTML(node.text);
    const inner = node.children.map(treeToHTML).join('');
    if (node.tag === '#fragment') return inner;
    const attrs = Object.entries(node.attrs).map(([k, v]) => ` ${k}="${escapeHTML(v)}"`).join('');
    return VOID.has(node.tag) ? `<${node.tag}${attrs}>` : `<${node.tag}${attrs}>${inner}</${node.tag}>`;
  }

  // Plain-text excerpt (e.g. for card snippets).
  function bbcodeToText(text) {
    const walk = (n) => (n.text !== undefined ? n.text : n.tag === 'br' ? '\n' : n.tag === 'img' ? '' : n.children.map(walk).join(''));
    return walk(bbcodeToTree(text)).replace(/\n{3,}/g, '\n\n').trim();
  }

  function renderBBCode(text, opts) {
    const frag = document.createDocumentFragment();
    for (const child of bbcodeToTree(text, opts).children) {
      const n = buildNode(child);
      if (n) frag.appendChild(n);
    }
    return frag;
  }

  function buildNode(node) {
    if (node.text !== undefined) return document.createTextNode(node.text);
    if (!ALLOWED_TAGS.has(node.tag)) return null;
    const elem = document.createElement(node.tag);
    for (const [k, v] of Object.entries(node.attrs)) {
      if (ALLOWED_ATTRS.has(k)) elem.setAttribute(k, v);
    }
    if (node.tag === 'a' && !/^https?:/i.test(elem.getAttribute('href') || '')) elem.removeAttribute('href');
    for (const c of node.children) {
      const n = buildNode(c);
      if (n) elem.appendChild(n);
    }
    return elem;
  }

  const api = { bbcodeToTree, renderBBCode, treeToHTML, bbcodeToText, safeUrl, ALLOWED_TAGS, ALLOWED_ATTRS };
  root.renderBBCode = renderBBCode;
  root.bbcodeToTree = bbcodeToTree;
  root.bbcodeToText = bbcodeToText;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
