// Build a sealed text-only closure from fixed original DeepStudent declarations.
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { adaptAlignedParser } from '../../third_party/deepstudent/scripts/adapt-aligned-parser.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(path.join(root, 'frontend/package.json'));
const ts = require('typescript');
if (ts.version !== '5.9.3') throw new Error('CONTENT_COMPILER_VERSION_MISMATCH');
const digest = value => createHash('sha256').update(value).digest('hex');
const sources = {}, originalDeclarations = {};
async function extract(name, expected, wanted) {
  const relative = `third_party/deepstudent/upstream/src/features/media-handout/${name}.ts`;
  const bytes = await readFile(path.join(root, relative));
  if (digest(bytes) !== expected) throw new Error('CONTENT_UPSTREAM_HASH_MISMATCH');
  sources[relative] = expected;
  const source = ts.createSourceFile(relative, bytes.toString('utf8'), ts.ScriptTarget.Latest, true);
  const declarations = source.statements.filter(node => node.name && wanted.includes(node.name.text)
    || ts.isVariableStatement(node) && node.declarationList.declarations.some(item => wanted.includes(item.name.getText(source))));
  if (declarations.length !== wanted.length) throw new Error('CONTENT_DECLARATION_MISSING');
  for (const node of declarations) originalDeclarations[node.name?.text || node.declarationList.declarations[0].name.getText(source)] = digest(node.getText(source));
  return declarations.map(node => node.getText(source)).join('\n\n') + '\n';
}
const chunkSource = await extract('windowing', 'b84e36c4b3d9bba1c6ef1ac8d53641a87fb8ae9770c664fce130309ec97691ec', ['CHUNK_CHARS', 'chunkTranscript']);
const timeSource = await extract('ir', '9f6e31a9c1d5d2b099eeb690d317d869bc8291f9d1fb910fb9928bfad6d7e381', ['formatClock']);
const chunk = ts.transpileModule(chunkSource, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
const parserPath = 'third_party/deepstudent/upstream/src/features/chat/components/translationNdjsonParser.ts';
const typesPath = 'third_party/deepstudent/upstream/src/features/chat/components/translationTypes.ts';
const parserRaw = await readFile(path.join(root, parserPath));
const typesRaw = await readFile(path.join(root, typesPath));
if (digest(parserRaw) !== '86118e364aa9c87b8e285d0a8923e320e6e9f931437c85fa8e624ca774931b64'
  || digest(typesRaw) !== '4c1c77e6b389c7562df35d187f57be04114e8cb7c98fc7d1c974a7b43da009d8') throw Error('ALIGNED_UPSTREAM_HASH_MISMATCH');
sources[parserPath] = digest(parserRaw); sources[typesPath] = digest(typesRaw);
const adapted = adaptAlignedParser(ts, parserRaw.toString('utf8'), typesRaw.toString('utf8'));
const aligned = ts.transpileModule(adapted.source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
if (/require\(/.test(aligned)) throw Error('CONTENT_RUNTIME_IMPORT_DENIED');
if (/require\(/.test(chunk)) throw new Error('CONTENT_RUNTIME_IMPORT_DENIED');
const time = '/* Original formatClock, DeepStudent 635216503c0b34bdf812e17942e0f09f51872e6f.\n'
  + ' * ir.ts: MIT License, Copyright (c) 2026 BA7MLV. Full original notice is in the accompanying upstream source. */\n' + timeSource;
const helper = await readFile(path.join(root, 'third_party/deepstudent/runtime/content-helper.cjs'));
const files = { 'helper.cjs': helper, 'dist/chunkTranscript.cjs': Buffer.from(chunk),
  'dist/alignedParser.cjs': Buffer.from(aligned),
  'notices/translationNdjsonParser.ts': parserRaw, 'notices/translationTypes.ts': typesRaw,
  'notices/alignedParser.adapted.ts': Buffer.from(adapted.source),
  'notices/aligned-adaptation.json': Buffer.from(JSON.stringify({ source_commit: '635216503c0b34bdf812e17942e0f09f51872e6f',
    declarations: adapted.declarations, edits: adapted.edits }, null, 2) + '\n'),
  'notices/LICENSE.AGPL-3.0': await readFile(path.join(root, 'third_party/deepstudent/notices/LICENSE.AGPL-3.0')),
  'notices/windowing.ts': await readFile(path.join(root, 'third_party/deepstudent/upstream/src/features/media-handout/windowing.ts')),
  'notices/ir.ts': await readFile(path.join(root, 'third_party/deepstudent/upstream/src/features/media-handout/ir.ts')) };
const lock = { manifest_version: 1, release_id: 'source-content-node24-20261024-r2',
  source_commit: '635216503c0b34bdf812e17942e0f09f51872e6f', source_files: sources, declarations: originalDeclarations,
  aligned_parser: { declarations: adapted.declarations, adapted_sha256: digest(adapted.source), edits: adapted.edits },
  node_sha256: '63c259c81e5d472b5f11c8d506070130cb04a1ecf84b80377a34ed6ec9048088',
  protocol: { version: 1, max_input_bytes: 2097152, max_output_bytes: 131072, timeout_seconds: 5 },
  files: Object.entries(files).map(([name, bytes]) => ({ path: name, bytes: bytes.length, sha256: digest(bytes) })),
  time_presentation: { path: 'third_party/deepstudent/web/timePresentation.ts', sha256: digest(time) }, build_typescript: ts.version };
const directory = path.join(root, '.runtime/workbench/content', lock.release_id);
const lockPath = path.join(root, 'resources/workbench/content-runtime.lock.json');
const timePath = path.join(root, lock.time_presentation.path);
if (process.argv.includes('--check')) {
  if (JSON.stringify(JSON.parse(await readFile(lockPath, 'utf8'))) !== JSON.stringify(lock)) throw new Error('CONTENT_BUILD_LOCK_MISMATCH');
  for (const spec of lock.files) if (digest(await readFile(path.join(directory, spec.path))) !== spec.sha256) throw new Error('CONTENT_ARTIFACT_MISMATCH');
  if (digest(await readFile(timePath)) !== lock.time_presentation.sha256) throw new Error('CONTENT_TIME_EXTRACT_MISMATCH');
} else {
  for (const [name, bytes] of Object.entries(files)) { const target = path.join(directory, name); await mkdir(path.dirname(target), { recursive: true }); await writeFile(target, bytes); }
  await writeFile(lockPath, JSON.stringify(lock, null, 2) + '\n');
  await writeFile(timePath, time);
}
console.log(JSON.stringify({ release_id: lock.release_id, declarations: Object.keys(originalDeclarations), files: lock.files, time_sha256: lock.time_presentation.sha256 }));
