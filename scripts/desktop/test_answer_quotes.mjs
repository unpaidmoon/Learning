// Test the actual selection-event adapter with the compiled project renderer.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { test } from 'node:test';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const frontendRequire = createRequire(path.join(root, 'frontend/package.json'));
const { JSDOM } = frontendRequire('jsdom');
const React = frontendRequire('react');
const ReactDOM = frontendRequire('react-dom');
const ReactDOMClient = frontendRequire('react-dom/client');
const katex = frontendRequire('katex');
const librarySource = await readFile(path.join(root, '.runtime/desktop/client/library-components.js'), 'utf8');
const adapterSource = await readFile(path.join(root, 'desktop/client/answer-quotes.js'), 'utf8');

function fixture(source, html, { duplicate = false } = {}) {
  const dom = new JSDOM('<!doctype html><html><body><div data-learning-region="ai"><div data-chat-node-key="native-one" data-chat-flow-kind="assistant-step"><div class="_markdown_fixture"></div></div></div></body></html>', { url: 'http://127.0.0.1:19030/desktop/' });
  for (const name of ['window', 'document', 'Node', 'Element', 'HTMLElement', 'Range', 'NodeFilter', 'MutationObserver', 'getComputedStyle', 'DOMParser']) {
    globalThis[name] = typeof dom.window[name] === 'function' && name === 'getComputedStyle' ? dom.window[name].bind(dom.window) : dom.window[name];
  }
  globalThis.innerWidth = 1440;
  vm.runInThisContext(librarySource, { filename: 'library-components.js' });
  vm.runInThisContext(adapterSource, { filename: 'answer-quotes.js' });
  function Button({ children, variant: _variant, size: _size, ...props }) { return React.createElement('button', props, children); }
  const components = window.LearningLibraryComponents.create({ React, ReactDOM, Button, Input: 'input', Modal: () => null });
  const native = document.querySelector('._markdown_fixture'); native.innerHTML = html;
  const references = [], errors = [], calls = [];
  let currentSource = source;
  const bridge = window.LearningAnswerQuotes.create({ React, ReactDOMClient, flushSync: ReactDOM.flushSync, Button,
    ctx: { uiConversation: { binding: () => ({ target: () => ({ getSnapshot: () => ({ nodes: { get: key => key === 'native-one'
      ? { kind: 'assistant-step', data: { blocks: [{ kind: 'text', text: currentSource }] } } : undefined } }) }) }) } },
    api: async route => { calls.push(route); return { messages: [{ message_id: 'assistant-one', role: 'assistant', text: source },
      ...(duplicate ? [{ message_id: 'assistant-two', role: 'assistant', text: source }] : [])] }; },
    components, getActiveSession: () => 'session-one', bindingFor: async () => ({ chat_id: 'chat-one' }),
    onQuote: value => references.push(value), onError: value => errors.push(value),
  });
  return { dom, native, bridge, references, errors, calls, changeSource(value) { currentSource = value; }, close() { bridge.dispose(); dom.window.close(); } };
}
function select(node, start = 0, end = node.textContent.length) {
  const range = document.createRange();
  if (node.nodeType === Node.TEXT_NODE) { range.setStart(node, start); range.setEnd(node, end); }
  else range.selectNodeContents(node);
  const selected = window.getSelection(); selected.removeAllRanges(); selected.addRange(range);
}
async function add(f) {
  await f.bridge.inspect();
  const button = document.querySelector('[data-learning-answer-quote] button');
  assert.ok(button, document.querySelector('[data-learning-answer-quote]')?.textContent);
  button.click(); await Promise.resolve();
  assert.equal(f.references.length, 1);
  assert.equal(f.errors.length, 0);
  assert.deepEqual(f.calls, ['/chats/chat-one']);
  return f.references[0];
}

test('native prose selection keeps exact UTF-16 offsets and business message identity', async () => {
  const source = '先看😀条件，然后推导。';
  const f = fixture(source, `<p>${source}</p>`);
  try {
    select(f.native.querySelector('p').firstChild, 5, 7);
    const quote = await add(f);
    assert.equal(quote.message_id, 'assistant-one');
    assert.equal(quote.markdown, source.slice(5, 7));
    assert.equal(quote.source_start, 5); assert.equal(quote.source_end, 7);
  } finally { f.close(); }
});

test('native whole formula maps through actual project KaTeX source checks', async () => {
  const latex = 'x^2-5x+6=(x-2)(x-3)', source = `方程 $${latex}$。`;
  const f = fixture(source, `<p>方程 ${katex.renderToString(latex)}。</p>`);
  try {
    select(f.native.querySelector('.katex-html'));
    const quote = await add(f);
    assert.equal(quote.markdown, `$${latex}$`);
    assert.equal(quote.source_start, source.indexOf(latex));
    assert.equal(quote.source_end, source.indexOf(latex) + latex.length);
  } finally { f.close(); }
});

test('native partial formula does not expand to the surrounding equation', async () => {
  const latex = 'x^2-5x+6=(x-2)(x-3)', source = `方程 $${latex}$。`;
  const f = fixture(source, `<p>方程 ${katex.renderToString(latex)}。</p>`);
  try {
    const walker = document.createTreeWalker(f.native.querySelector('.katex-html'), NodeFilter.SHOW_TEXT);
    let digit;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) if (node.data === '5') { digit = node; break; }
    assert.ok(digit); select(digit);
    const quote = await add(f);
    assert.equal(quote.markdown, '$5$');
    assert.equal(quote.source_start, source.indexOf('5'));
    assert.equal(quote.source_end, source.indexOf('5') + 1);
  } finally { f.close(); }
});

test('ambiguous repeated response content refuses a guessed message identity', async () => {
  const f = fixture('相同回答', '<p>相同回答</p>', { duplicate: true });
  try {
    select(f.native.querySelector('p').firstChild); await f.bridge.inspect();
    assert.equal(document.querySelector('[data-learning-answer-quote] button'), null);
    assert.match(document.querySelector('[data-learning-answer-quote]').textContent, /TEXT_SELECTION_UNMAPPED/);
    assert.deepEqual(f.references, []);
  } finally { f.close(); }
});

test('different native rendered text refuses mirrored expansion', async () => {
  const f = fixture('正确答案', '<p>显示不同答案</p>');
  try {
    select(f.native.querySelector('p').firstChild); await f.bridge.inspect();
    assert.equal(document.querySelector('[data-learning-answer-quote] button'), null);
    assert.match(document.querySelector('[data-learning-answer-quote]').textContent, /TEXT_SELECTION_UNMAPPED/);
    assert.deepEqual(f.references, []);
  } finally { f.close(); }
});

test('unselected native painted math structure cannot become a full formula quote', async () => {
  const latex = 'x+y', source = `$${latex}$`;
  const f = fixture(source, `<p>${katex.renderToString(latex)}</p>`);
  try {
    const visual = f.native.querySelector('.katex-html');
    visual.append(document.createElementNS('http://www.w3.org/2000/svg', 'svg'));
    const walker = document.createTreeWalker(visual, NodeFilter.SHOW_TEXT);
    const nodes = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) if (node.data.trim()) nodes.push(node);
    const range = document.createRange(); range.setStart(nodes[0], 0); range.setEnd(nodes.at(-1), nodes.at(-1).length);
    window.getSelection().removeAllRanges(); window.getSelection().addRange(range);
    await f.bridge.inspect();
    assert.equal(document.querySelector('[data-learning-answer-quote] button'), null);
    assert.match(document.querySelector('[data-learning-answer-quote]').textContent, /FORMULA_SELECTION_UNMAPPED/);
    assert.deepEqual(f.references, []);
  } finally { f.close(); }
});

test('late source changes invalidate the displayed quote action', async () => {
  const f = fixture('原始回答', '<p>原始回答</p>');
  try {
    select(f.native.querySelector('p').firstChild); await f.bridge.inspect();
    const button = document.querySelector('[data-learning-answer-quote] button'); assert.ok(button);
    f.changeSource('原始回答现在变长了'); button.click(); await Promise.resolve();
    assert.deepEqual(f.references, []);
    assert.equal(document.querySelector('[data-learning-answer-quote]').style.display, 'none');
  } finally { f.close(); }
});
