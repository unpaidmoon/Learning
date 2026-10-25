/**
 * 独立候选的行为检查。通过表示观察与源码一致，不表示项目场景验收通过。
 * 使用本项目已固定的 TypeScript；不加载 DeepStudent 的应用或供应商执行层。
 */
import assert from 'node:assert/strict';
import { createHash, webcrypto } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const root = fileURLToPath(new URL('../', import.meta.url));
const requireProject = createRequire(new URL('../../../frontend/package.json', import.meta.url));
const ts = requireProject('typescript');
const chatPath = 'upstream/src/features/chat/components/';
const handoutPath = 'upstream/src/features/media-handout/';

function fixture({ secureCrypto = true } = {}) {
  const loaded = new Map();
  const window = new EventTarget();
  function load(relativePath) {
    const absolute = resolve(root, relativePath);
    assert.ok(absolute.startsWith(resolve(root, 'upstream') + '\\'));
    if (loaded.has(absolute)) return loaded.get(absolute).exports;
    const module = { exports: {} };
    loaded.set(absolute, module);
    const source = readFileSync(absolute, 'utf8');
    const output = ts.transpileModule(source, {
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
    }).outputText;
    vm.runInNewContext(output, {
      exports: module.exports,
      module,
      crypto: secureCrypto ? webcrypto : undefined,
      TextEncoder,
      window,
      require: (specifier) => {
        assert.ok(specifier.startsWith('.'), `unexpected runtime dependency: ${specifier}`);
        return load(resolve(dirname(absolute), specifier + '.ts'));
      },
    }, { filename: absolute });
    return module.exports;
  }
  return { load, window };
}

const plain = (value) => JSON.parse(JSON.stringify(value));

test('all archived originals retain manifest SHA-256', () => {
  const manifest = JSON.parse(readFileSync(resolve(root, 'manifest.json'), 'utf8'));
  assert.equal(manifest.source_commit, '635216503c0b34bdf812e17942e0f09f51872e6f');
  for (const item of manifest.files) {
    const data = readFileSync(resolve(root, item.target_path));
    assert.equal(createHash('sha256').update(data).digest('hex'), item.sha256, item.source_path);
  }
});

test('scanner handles escaped strings across every chunk boundary and stops at done', () => {
  const { createNdjsonParser } = fixture().load(chatPath + 'translationNdjsonParser.ts');
  const expected = { src: 'a {brace} "quoted" \\ newline\n', tgt: '译文' };
  const raw = '```json\n[' + JSON.stringify(expected) + ',{"done":true},{"src":"late","tgt":"迟到"}]';
  const parser = createNdjsonParser();
  const segments = [];
  for (const ch of raw) segments.push(...parser.push(ch).segments);
  assert.deepEqual(plain(segments), [expected]);
  assert.deepEqual(plain(parser.flush()), { segments: [], done: true });
});

test('scanner silently drops malformed objects: caller must track validity separately', () => {
  const { createNdjsonParser } = fixture().load(chatPath + 'translationNdjsonParser.ts');
  const parser = createNdjsonParser();
  assert.deepEqual(plain(parser.push('{"src":broken}{"src":"ok","tgt":"好"}')), {
    segments: [{ src: 'ok', tgt: '好' }], done: false,
  });
});

test('flush discards a truncated tail while returning no parse error', () => {
  const { createNdjsonParser } = fixture().load(chatPath + 'translationNdjsonParser.ts');
  const parser = createNdjsonParser();
  assert.equal(parser.push('{"src":"a","tgt":"甲"}{"src":"tail').segments.length, 1);
  assert.deepEqual(plain(parser.flush()), { segments: [], done: false });
});

test('fallback supports legacy segments wrapper without claiming completion', () => {
  const { parseAlignedFallback } = fixture().load(chatPath + 'translationNdjsonParser.ts');
  assert.deepEqual(plain(parseAlignedFallback('```json\n{"segments":[{"src":"a","tgt":"甲"},{}]}\n```')), [
    { src: 'a', tgt: '甲' },
  ]);
  assert.equal(parseAlignedFallback('incomplete {'), null);
});

test('cache key separates model and supplied context; SHA-256 uses 64 hex digits', async () => {
  const { buildCacheKey } = fixture().load(chatPath + 'translationCache.ts');
  const params = { mode: 'aligned', modelId: 'm1', srcLang: 'en', tgtLang: 'zh', source: 'x', contextBefore: '', contextAfter: '' };
  const base = await buildCacheKey(params);
  assert.match(base, /\|[0-9a-f]{64}$/);
  assert.notEqual(base, await buildCacheKey({ ...params, modelId: 'm2' }));
  assert.notEqual(base, await buildCacheKey({ ...params, contextBefore: 'neighbor' }));
  // 思考、提示、选区修订不属于此上游接口；正式包装须自行纳入命名空间。
});

test('cache weak fallback is observable without secure crypto', async () => {
  const { buildCacheKey } = fixture({ secureCrypto: false }).load(chatPath + 'translationCache.ts');
  const key = await buildCacheKey({ mode: 'streaming', modelId: 'm', srcLang: 'en', tgtLang: 'zh', source: 'x', contextBefore: '', contextAfter: '' });
  assert.match(key, /\|[0-9a-f]{1,8}$/);
});

test('cache evicts least recently used item at 100 entries', () => {
  const { writeCache, readCache, clearCache } = fixture().load(chatPath + 'translationCache.ts');
  clearCache();
  for (let i = 0; i < 100; i++) writeCache(String(i), { mode: 'streaming', text: String(i) });
  assert.equal(readCache('0').text, '0');
  writeCache('100', { mode: 'streaming', text: '100' });
  assert.equal(readCache('1'), null);
  assert.equal(readCache('0').text, '0');
});

test('upstream model assignment event invalidates memory cache', () => {
  const { load, window } = fixture();
  const { writeCache, readCache } = load(chatPath + 'translationCache.ts');
  writeCache('key', { mode: 'streaming', text: 'old' });
  window.dispatchEvent(new Event('model_assignments_changed'));
  assert.equal(readCache('key'), null);
});

test('IR preserves table/list structure and rejects ragged table', () => {
  const { parseSectionBlocks } = fixture().load(handoutPath + 'ir.ts');
  const blocks = parseSectionBlocks(JSON.stringify({ blocks: [
    { type: 'list', ordered: true, items: ['1. A', '2. B'] },
    { type: 'table', header: ['X', 'Y'], rows: [['1', '2']] },
  ] }), { startSec: 0, endSec: 20 });
  assert.deepEqual(plain(blocks[0]), { type: 'list', ordered: true, items: ['A', 'B'] });
  assert.throws(() => parseSectionBlocks(JSON.stringify({ blocks: [{ type: 'table', header: ['X'], rows: [['1', '2']] }] }), { startSec: 0, endSec: 20 }), /row has/);
});

test('IR silently discards unavailable figure time and clocks drop fractions', () => {
  const { parseSectionBlocks, parseClock } = fixture().load(handoutPath + 'ir.ts');
  assert.equal(parseClock('00:01.999'), 1);
  const blocks = parseSectionBlocks('{"blocks":[{"type":"para","text":"text"},{"type":"figure","time":"00:50"}]}', { startSec: 0, endSec: 20 });
  assert.deepEqual(plain(blocks), [{ type: 'para', text: 'text' }]);
});

test('Markdown retains formula/table escaping but omits a missing image', () => {
  const { handoutToMarkdown } = fixture().load(handoutPath + 'markdown.ts');
  const markdown = handoutToMarkdown({ resourceId: 'r1', title: 'T', summary: '', lang: 'en', sections: [
    { heading: 'H', startSec: 1.9, blocks: [
      { type: 'para', text: '$a+b$' },
      { type: 'table', header: ['A|B'], rows: [['C']] },
      { type: 'figure', ts: 2, caption: 'missing-image' },
    ] },
  ] });
  assert.ok(markdown.includes('$a+b$'));
  assert.ok(markdown.includes('A\\|B'));
  assert.ok(markdown.includes('[媒体@r1:00:01]'));
  assert.ok(!markdown.includes('missing-image'));
});

test('transcript formatting filters done segments and rounds displayed seconds down', () => {
  const { usableSegments, segmentsToTranscript } = fixture().load(handoutPath + 'windowing.ts');
  const segments = usableSegments([
    { idx: 2, startMs: 2999, endMs: 4000, text: ' second ', status: 1 },
    { idx: 1, startMs: 1111, endMs: 2000, text: 'first', status: 1 },
    { idx: 3, startMs: 4000, endMs: 5000, text: 'missing', status: 2 },
  ]);
  assert.equal(segmentsToTranscript(segments), '[00:01] first\n[00:02] second');
  assert.equal(segments[0].startMs, 1111);
});

test('long single line can be rejoined but later hard chunks lose timestamp provenance', () => {
  const { chunkTranscript } = fixture().load(handoutPath + 'windowing.ts');
  const line = '[00:01] ' + '长段字幕'.repeat(100);
  const chunks = chunkTranscript(line, 32);
  assert.ok(chunks.length > 2);
  assert.ok(chunks.every((part) => part.length <= 32));
  assert.equal(chunks.join(''), line);
  assert.ok(!chunks[1].startsWith('[00:01]'));
});

test('default transcript window includes neighbors and start-only filtering misses overlap', () => {
  const { sectionTranscriptWindow } = fixture().load(handoutPath + 'windowing.ts');
  const segments = [
    { idx: 1, startMs: 8000, endMs: 9000, text: 'neighbor', status: 1 },
    { idx: 2, startMs: 15000, endMs: 16000, text: 'selected', status: 1 },
    { idx: 3, startMs: 1000, endMs: 12000, text: 'overlapping long segment', status: 1 },
  ];
  const range = { startSec: 10, endSec: 20 };
  assert.deepEqual(plain(sectionTranscriptWindow(segments, range).map((s) => s.idx)), [1, 2]);
  assert.deepEqual(plain(sectionTranscriptWindow(segments, range, 0).map((s) => s.idx)), [2]);
});

test('outline limits headings to 12 while extending last time; this is not a coverage proof', () => {
  const { parseOutline } = fixture().load(handoutPath + 'windowing.ts');
  const sections = Array.from({ length: 15 }, (_, i) => ({ heading: String(i), start: `00:${i * 10}`, points: [String(i)] }));
  const outline = parseOutline(JSON.stringify({ title: 'T', sections }), 160, 'fallback');
  assert.equal(outline.sections.length, 12);
  assert.equal(outline.sections.at(-1).endSec, 160);
  assert.equal(outline.sections.at(-1).points[0], '11');
});
