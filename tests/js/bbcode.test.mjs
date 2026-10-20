import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { bbcodeToTree, treeToHTML, bbcodeToText, ALLOWED_TAGS, ALLOWED_ATTRS } = require('../../app/static/js/bbcode.js');

const html = (src, opts) => treeToHTML(bbcodeToTree(src, opts));

function walk(node, fn) {
  fn(node);
  for (const c of node.children || []) walk(c, fn);
}
function elements(tree) {
  const out = [];
  walk(tree, (n) => { if (n.tag && n.tag !== '#fragment') out.push(n); });
  return out;
}
function assertSafe(tree) {
  for (const n of elements(tree)) {
    assert.ok(ALLOWED_TAGS.has(n.tag), `unexpected tag ${n.tag}`);
    for (const [k, v] of Object.entries(n.attrs)) {
      assert.ok(ALLOWED_ATTRS.has(k), `unexpected attribute ${k}`);
      assert.ok(!/^on/i.test(k), `event handler attribute ${k}`);
      if (k === 'href' || k === 'src') assert.match(v, /^(https?:\/\/|\/(?!\/))/i, `unsafe ${k}: ${v}`);
    }
  }
}

test('XSS payload in [img] produces no onerror attribute and no raw HTML', () => {
  const src = '[img]http://x/[[/img]]" onerror="alert(1)"';
  const tree = bbcodeToTree(src);
  assertSafe(tree);
  const out = treeToHTML(tree);
  assert.doesNotMatch(out, /<img[^>]*onerror/i);
  assert.doesNotMatch(out, /onerror="/i); // only ever appears escaped, as text
  // Untrusted host -> rendered as a link, not an image.
  assert.equal(elements(tree).filter((n) => n.tag === 'img').length, 0);
  // The trailing junk is plain text.
  assert.ok(out.includes('onerror=&quot;alert(1)&quot;'));
});

test('raw HTML in text is never emitted as markup', () => {
  const tree = bbcodeToTree('<script>alert(1)</script><img src=x onerror=alert(1)>');
  assert.deepEqual(elements(tree), []);
  assert.doesNotMatch(treeToHTML(tree), /<script|<img/i);
});

test('javascript: and other non-http URLs are rejected', () => {
  for (const src of [
    '[url=javascript:alert(1)]click[/url]',
    '[url]javascript:alert(1)[/url]',
    '[url=" javascript:alert(1)"]x[/url]',
    '[url=data:text/html,hi]x[/url]',
    '[img]javascript:alert(1)[/img]',
    '[url=//evil.example]x[/url]',
  ]) {
    const tree = bbcodeToTree(src);
    assertSafe(tree);
    assert.equal(elements(tree).filter((n) => n.tag === 'a' || n.tag === 'img').length, 0, src);
  }
  assert.equal(html('[url=javascript:alert(1)]click[/url]'), 'click');
});

test('links get safe rel/target attributes', () => {
  const tree = bbcodeToTree('[url=https://example.com/a?b=1&c="2"]Site[/url]');
  const [a] = elements(tree);
  assert.equal(a.tag, 'a');
  assert.equal(a.attrs.rel, 'noopener noreferrer nofollow');
  assert.equal(a.attrs.target, '_blank');
  assert.ok(!a.attrs.href.includes('"'));
  assert.equal(html('[url]https://example.com[/url]'),
    '<a href="https://example.com/" rel="noopener noreferrer nofollow" target="_blank">https://example.com</a>');
});

test('images: imageMap, allowed hosts, others become links', () => {
  const orig = 'https://example.com/pic.png';
  const mapped = bbcodeToTree(`[img]${orig}[/img]`, { imageMap: { [orig]: '/api/workshop/1/desc-images/2/a.png' } });
  const [img] = elements(mapped);
  assert.equal(img.tag, 'img');
  assert.equal(img.attrs.src, '/api/workshop/1/desc-images/2/a.png');
  assert.equal(img.attrs.loading, 'lazy');

  const steam = elements(bbcodeToTree('[img]http://images.steamusercontent.com/ugc/1/x.jpg[/img]'))[0];
  assert.equal(steam.tag, 'img');
  assert.equal(steam.attrs.src, 'https://images.steamusercontent.com/ugc/1/x.jpg');

  const other = elements(bbcodeToTree(`[img]${orig}[/img]`))[0];
  assert.equal(other.tag, 'a');
  assert.equal(other.attrs.href, orig);

  // Map values that aren't local paths are ignored.
  const bad = elements(bbcodeToTree(`[img]${orig}[/img]`, { imageMap: { [orig]: 'javascript:alert(1)' } }))[0];
  assert.equal(bad.tag, 'a');
});

test('nested tags', () => {
  assert.equal(html('[b]bold [i]both[/i][/b]'), '<strong>bold <em>both</em></strong>');
  assert.equal(html('[quote=Gabe][u]hi[/u][/quote]'),
    '<blockquote class="bb-quote"><div class="bb-quote-author">Gabe wrote:</div><u>hi</u></blockquote>');
  assert.equal(html('[url=https://a.com][b]x[/b][/url]'),
    '<a href="https://a.com/" rel="noopener noreferrer nofollow" target="_blank"><strong>x</strong></a>');
});

test('unknown bracketed text like [ZM] is preserved literally', () => {
  assert.equal(html('[ZM] All-around Enhancement'), '[ZM] All-around Enhancement');
  assert.equal(html('Version [1.2] [b]new[/b]'), 'Version [1.2] <strong>new</strong>');
  assert.equal(bbcodeToText('[ZM] Lite [b]v2[/b]'), '[ZM] Lite v2');
});

test('unbalanced tags render as literal text', () => {
  assert.equal(html('[b]never closed'), '[b]never closed');
  assert.equal(html('stray close[/i] here'), 'stray close[/i] here');
  assert.equal(html('[b][i]x[/b]'), '<strong>[i]x</strong>');
  assert.equal(html('[img]no end'), '[img]no end');
});

test('noparse keeps its content verbatim; code is parsed but escaped', () => {
  assert.equal(html('[noparse][b]not bold[/b][/noparse]'), '[b]not bold[/b]');
  assert.equal(html('[code][i]x[/i] <a>[/code]'), '<div class="bb-code"><em>x</em> &lt;a&gt;</div>');
});

test('lists', () => {
  assert.equal(html('[list]\n[*]one\n[*]two [b]2[/b]\n[/list]'),
    '<ul><li>one</li><li>two <strong>2</strong></li></ul>');
  assert.equal(html('[olist][*]a[*]b[/olist]'), '<ol><li>a</li><li>b</li></ol>');
  assert.equal(html('[*] stray item'), '[*] stray item');
  assert.equal(html('[list][*]unclosed'), '[list][*]unclosed');
});

test('newlines, headings, hr, spoiler, table', () => {
  assert.equal(html('a\nb'), 'a<br>b');
  assert.equal(html('[h1]Title[/h1]\nbody'), '<h3 class="bb-h1">Title</h3>body');
  assert.equal(html('x[hr][/hr]y'), 'x<hr>y');
  assert.equal(html('[spoiler]s[/spoiler]'), '<span class="bb-spoiler" tabindex="0">s</span>');
  assert.equal(html('[table]\n[tr][th]H[/th][/tr]\n[tr][td]D[/td][/tr]\n[/table]'),
    '<table class="bb-table"><tbody><tr><th>H</th></tr><tr><td>D</td></tr></tbody></table>');
  assert.equal(html('[strike]old[/strike] [s]gone[/s]'), '<s>old</s> <s>gone</s>');
});

test('empty and non-string input', () => {
  assert.equal(html(''), '');
  assert.equal(html(null), '');
  assert.equal(html(undefined), '');
});

test('[code] content is parsed like Steam does, [noparse] stays literal', () => {
  const out = html('[code]Hi [u]Valve[/u]\n[list][*]one[/list][/code] [noparse][b]x[/b][/noparse]');
  assert.match(out, /<div class="bb-code">Hi <u>Valve<\/u>/);
  assert.match(out, /<li>one<\/li>/);
  assert.match(out, /\[b\]x\[\/b\]/);
});
