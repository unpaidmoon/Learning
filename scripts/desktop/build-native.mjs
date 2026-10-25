// Bundle only the original React-free Workspace client omitted by the SEA
// Host when its native directory picker is unavailable. No upstream edits.
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const manifestPath = path.join(root, 'desktop/native-workspace.sources.json');
const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
const sourceRoot = path.join(root, '.runtime/desktop/upstream');
const lock = JSON.parse(await readFile(path.join(root, 'harness/desktop.lock.json'), 'utf8'));
if (manifest.upstream_commit !== lock.reference_source.commit) throw new Error('DESKTOP_NATIVE_SOURCE_VERSION_MISMATCH');
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
for (const [name, expected] of Object.entries(manifest.input_files)) {
  if (digest(await readFile(path.join(sourceRoot, name))) !== expected) throw new Error(`DESKTOP_NATIVE_SOURCE_HASH_MISMATCH: ${name}`);
}
const compilerRoot = path.join(root, `node_modules/.pnpm/rolldown@${manifest.compiler.version}/node_modules/rolldown`);
const compilerPackage = JSON.parse(await readFile(path.join(compilerRoot, 'package.json'), 'utf8'));
if (compilerPackage.version !== manifest.compiler.version) throw new Error('DESKTOP_NATIVE_COMPILER_VERSION_MISMATCH');
const { rolldown } = await import(pathToFileURL(path.join(compilerRoot, 'dist/index.mjs')).href);
const entry = path.join(sourceRoot, 'packages/api/workspace-controller/src/client/index.ts');
const usedExternals = new Set();
const build = await rolldown({
  input: entry,
  cwd: root,
  external: id => {
    if (!id.startsWith('@deepseek-ai/')) return false;
    if (!manifest.external_modules.includes(id)) throw new Error(`DESKTOP_NATIVE_UNDECLARED_EXTERNAL: ${id}`);
    usedExternals.add(id);
    return true;
  },
  platform: 'browser',
  treeshake: true,
});
try {
  const result = await build.generate({ format: 'cjs', exports: 'named', sourcemap: false, minify: false });
  const chunks = result.output.filter(item => item.type === 'chunk');
  if (chunks.length !== 1 || result.output.length !== 1) throw new Error('DESKTOP_NATIVE_UNEXPECTED_OUTPUT');
  const code = [
    '// MIT Copyright (c) 2026 DeepSeek. See ../LICENSE-HARNESS and ../native-workspace.sources.json.',
    `// Unmodified upstream Workspace client; source commit ${manifest.upstream_commit}.`,
    `window.__ModuleLoader__.load({ id: ${JSON.stringify(manifest.module_id)}, factory: (require) => {`,
    'const module = { exports: {} }; const exports = module.exports;',
    chunks[0].code.trimEnd(),
    'return module.exports;',
    '} });',
    '',
  ].join('\n');
  const hash = digest(Buffer.from(code));
  if (process.argv.includes('--record')) {
    manifest.output_sha256 = hash;
    await writeFile(manifestPath, JSON.stringify(manifest, null, 2) + '\n');
  } else if (manifest.output_sha256 !== hash) {
    throw new Error(`DESKTOP_NATIVE_BUILD_HASH_MISMATCH: ${hash}`);
  }
  const output = path.join(root, manifest.output);
  if (process.argv.includes('--check')) {
    if (digest(await readFile(output)) !== hash) throw new Error('DESKTOP_NATIVE_ARTIFACT_HASH_MISMATCH');
  } else {
    await mkdir(path.dirname(output), { recursive: true });
    await writeFile(output, code);
  }
  console.log(JSON.stringify({ module_id: manifest.module_id, bytes: Buffer.byteLength(code), sha256: hash, external_modules: [...usedExternals].sort() }));
} finally {
  await build.close();
}
