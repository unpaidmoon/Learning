// Build the fixed, standalone reader. Exporting later only verifies/copies it.
import { readFile, writeFile, mkdir, readdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const compilerRoot = path.join(root, 'node_modules/.pnpm/rolldown@1.2.14/node_modules/rolldown');
const compiler = JSON.parse(await readFile(path.join(compilerRoot, 'package.json'), 'utf8'));
if (compiler.version !== '1.2.14') throw new Error('OFFLINE_COMPILER_VERSION_MISMATCH');
const { rolldown } = await import(pathToFileURL(path.join(compilerRoot, 'dist/index.mjs')).href);
const digest = value => createHash('sha256').update(value).digest('hex');
const normalize = value => value.replaceAll('\\', '/');
const styles = new Map(), sources = new Set();
const buttonPath = normalize(path.join(root, 'frontend/src/components/ui/button'));
const frontendRequire = createRequire(path.join(root, 'frontend/package.json'));
const build = await rolldown({
  cwd: root, input: path.join(root, 'frontend/src/offline/entry.tsx'), platform: 'browser',
  transform: { jsx: { runtime: 'automatic' }, define: { 'process.env.NODE_ENV': '"production"' } },
  plugins: [{
    name: 'offline-reader-closed-resources',
    resolveId(source, importer) {
      if (importer === '\0offline-button' && source === 'react') return frontendRequire.resolve('react');
      if (!importer || !source.startsWith('.')) return null;
      const resolved = normalize(path.resolve(path.dirname(importer), source));
      if (resolved === buttonPath || resolved === buttonPath + '.tsx') return '\0offline-button';
      return null;
    },
    async load(id) {
      if (id === '\0offline-button') return { code: `import {createElement,forwardRef} from 'react';
export const Button=forwardRef(function OfflineButton({variant,size,...props},ref){return createElement('button',{...props,ref})});`, moduleType: 'js' };
      if (id.endsWith('.css')) {
        styles.set(id, await readFile(id, 'utf8'));
        return { code: '', moduleType: 'js' };
      }
      if (!id.startsWith('\0') && normalize(id).startsWith(normalize(root))) sources.add(id);
      return null;
    },
  }],
});
try {
  const result = await build.generate({ format: 'iife', sourcemap: false, minify: false });
  const chunks = result.output.filter(item => item.type === 'chunk');
  if (chunks.length !== 1 || chunks[0].imports.length || chunks[0].dynamicImports.length) throw new Error('OFFLINE_RUNTIME_IMPORT_DENIED');
  const javascript = chunks[0].code.replace(/<\/script/gi, '<\\/script');
  let css = '';
  for (const [id, text] of [...styles].sort(([a], [b]) => a.localeCompare(b))) {
    let content = text;
    if (id.endsWith('katex.min.css')) {
      const matches = [...text.matchAll(/url\(([^)]+)\)/g)];
      for (const match of matches) {
        const relative = match[1].replace(/^['"]|['"]$/g, '');
        if (!/^fonts\/[A-Za-z0-9_-]+\.(?:woff2?|ttf)$/.test(relative)) throw new Error('OFFLINE_FONT_PATH_DENIED');
        const fontPath = path.join(path.dirname(id), relative);
        const bytes = await readFile(fontPath);
        sources.add(fontPath);
        const mime = relative.endsWith('.woff2') ? 'font/woff2' : relative.endsWith('.woff') ? 'font/woff' : 'font/ttf';
        content = content.replaceAll(match[0], `url(data:${mime};base64,${bytes.toString('base64')})`);
      }
    }
    if (/@import\b|https?:\/\//i.test(content) || /<\/style/i.test(content)) throw new Error('OFFLINE_STYLE_EXTERNAL_DENIED');
    css += '\n' + content;
    sources.add(id);
  }
  const packages = new Map();
  for (const id of sources) {
    if (!normalize(id).includes('/node_modules/')) continue;
    let current = path.dirname(id);
    while (current !== root && current !== path.dirname(current)) {
      try {
        const pkg = JSON.parse(await readFile(path.join(current, 'package.json'), 'utf8'));
        if (pkg.name && pkg.version) { packages.set(`${pkg.name}@${pkg.version}`, { directory: current, pkg }); break; }
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
      current = path.dirname(current);
    }
  }
  let notices = 'Learning offline reader\nFixed dependency licenses, included with this exported reader.\n';
  const dependencies = [], missingLicenses = [];
  for (const [identity, { directory, pkg }] of [...packages].sort(([a], [b]) => a.localeCompare(b))) {
    const names = (await readdir(directory)).filter(name => /^(?:licen[cs]e|copying|notice)(?:[.-]|$)/i.test(name));
    if (!names.length) {
      const fallback = path.join(root, 'resources/workbench/notices/offline', identity.replaceAll('/', '_') + '.LICENSE');
      try {
        notices += `\n\n===== ${identity} (${pkg.license || 'see below'}) =====\n` + await readFile(fallback, 'utf8') + '\n';
        sources.add(fallback);
        dependencies.push({ name: pkg.name, version: pkg.version, license: pkg.license || null });
      } catch (error) { if (error.code !== 'ENOENT') throw error; missingLicenses.push(identity); }
      continue;
    }
    notices += `\n\n===== ${identity} (${pkg.license || 'see below'}) =====\n`;
    for (const name of names.sort()) {
      const file = path.join(directory, name);
      sources.add(file);
      notices += '\n' + await readFile(file, 'utf8') + '\n';
    }
    dependencies.push({ name: pkg.name, version: pkg.version, license: pkg.license || null });
  }
  if (missingLicenses.length) throw new Error('OFFLINE_DEPENDENCY_LICENSE_MISSING: ' + missingLicenses.join(', '));
  sources.add(fileURLToPath(import.meta.url));
  const inputs = {};
  for (const id of [...sources].sort()) inputs[normalize(path.relative(root, id))] = digest(await readFile(id));
  const files = { 'reader.js': javascript, 'reader.css': css, 'notices.txt': notices };
  const lock = { format: 1, release_id: 'offline-reader-v3', compiler: { name: 'rolldown', version: '1.2.14' },
    lock_sha256: digest(await readFile(path.join(root, 'pnpm-lock.yaml'))), inputs, dependencies,
    files: Object.entries(files).map(([name, text]) => ({ path: name, bytes: Buffer.byteLength(text), sha256: digest(text) })) };
  const directory = path.join(root, '.runtime/workbench/offline-reader', lock.release_id);
  const lockPath = path.join(root, 'resources/workbench/offline-runtime.lock.json');
  if (process.argv.includes('--check')) {
    if (JSON.stringify(JSON.parse(await readFile(lockPath, 'utf8'))) !== JSON.stringify(lock)) throw new Error('OFFLINE_BUILD_LOCK_MISMATCH');
    for (const spec of lock.files) if (digest(await readFile(path.join(directory, spec.path))) !== spec.sha256) throw new Error('OFFLINE_ARTIFACT_MISMATCH');
  } else {
    await mkdir(directory, { recursive: true });
    for (const [name, content] of Object.entries(files)) await writeFile(path.join(directory, name), content);
    await writeFile(lockPath, JSON.stringify(lock, null, 2) + '\n');
  }
  console.log(JSON.stringify({ files: lock.files, dependencies: dependencies.length, source_files: sources.size }));
} finally { await build.close(); }
