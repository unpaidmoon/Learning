// Build existing learning modules against the original Harness React instance.
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const compilerRoot = path.join(root, 'node_modules/.pnpm/rolldown@1.2.14/node_modules/rolldown');
const compiler = JSON.parse(await readFile(path.join(compilerRoot, 'package.json'), 'utf8'));
if (compiler.version !== '1.2.14') throw new Error('DESKTOP_LIBRARY_COMPILER_VERSION_MISMATCH');
const { rolldown } = await import(pathToFileURL(path.join(compilerRoot, 'dist/index.mjs')).href);
const digest = value => createHash('sha256').update(value).digest('hex');
const directoryFlow = path.join(root, '.runtime/desktop/upstream/packages/client/ui-directory-picker-native/src/client/flow.ts');
const sourceLock = JSON.parse(await readFile(path.join(root, 'harness/desktop.lock.json'), 'utf8'));
if (sourceLock.reference_source.commit !== '687f282e8625c6b8d11ff58211a90524be2e4317'
    || digest(await readFile(directoryFlow)) !== 'd4bb4bf3383fe3240d32ad7e8676151607482327ec2758f411d7c2c505c78d36') {
  throw new Error('DESKTOP_DIRECTORY_FLOW_SOURCE_MISMATCH');
}
const css = new Map();
const usedSource = new Set();
const normalize = value => value.replaceAll('\\', '/');
const buttonPath = normalize(path.join(root, 'frontend/src/components/ui/button.tsx'));
const externals = new Set(['react', 'react/jsx-runtime', 'react/jsx-dev-runtime', 'react-dom', '@deepseek-ai/dsh-client-ui-primitives']);
const buttonAdapter = `import { createElement, forwardRef } from 'react';
import { Button as NativeButton } from '@deepseek-ai/dsh-client-ui-primitives';
export const Button = forwardRef(function LearningButton({variant='default',size='default',className='',...props},ref){
 const mapped={default:'primary',secondary:'outline',destructive:'outline',link:'ghost'}[variant]||variant;
 return createElement(NativeButton,{...props,ref,variant:mapped,size:['sm','xs','icon-xs','icon-sm'].includes(size)?'sm':'md',className:className+(variant==='destructive'?' learning-destructive-button':'')});
});`;
const build = await rolldown({
  input: path.join(root, 'desktop/src/library-entry.tsx'), cwd: root, platform: 'browser',
  external: id => externals.has(id),
  transform: { jsx: { runtime: 'automatic' } },
  plugins: [{
    name: 'learning-native-controls-and-styles',
    resolveId(source, importer) {
      if (source === '@learning/native-directory-flow') return directoryFlow;
      if (!importer || !source.startsWith('.')) return null;
      const candidate = normalize(path.resolve(path.dirname(importer), source));
      if (candidate === buttonPath.slice(0, -4) || candidate === buttonPath) return '\0learning-native-button';
      return null;
    },
    async load(id) {
      if (id === '\0learning-native-button') return { code: buttonAdapter, moduleType: 'js' };
      if (id.endsWith('.css')) { css.set(id, await readFile(id, 'utf8')); return { code: '', moduleType: 'js' }; }
      if (!id.includes('node_modules') && normalize(id).startsWith(normalize(root)) && !id.startsWith('\0')) usedSource.add(id);
      return null;
    },
  }],
});

function scopeStyles(text) {
  let output = '', cursor = 0;
  while (cursor < text.length) {
    const open = text.indexOf('{', cursor);
    if (open < 0) return output + text.slice(cursor);
    const header = text.slice(cursor, open);
    let depth = 1, close = open + 1;
    for (; close < text.length && depth; close++) {
      if (text[close] === '{') depth++;
      else if (text[close] === '}') depth--;
    }
    const body = text.slice(open + 1, close - 1);
    const clean = header.replace(/\/\*[\s\S]*?\*\//g, '').trim();
    if (clean.startsWith('@media') || clean.startsWith('@supports')) output += header + '{' + scopeStyles(body) + '}';
    else if (clean.startsWith('@')) output += header + '{' + body + '}';
    else output += clean.split(',').map(selector => '.learning-legacy-module ' + selector.trim()).join(',\n') + '{' + body + '}';
    cursor = close;
  }
  return output;
}
try {
  const built = await build.generate({ format: 'cjs', exports: 'named', sourcemap: false, minify: false });
  const chunks = built.output.filter(value => value.type === 'chunk');
  if (chunks.length !== 1) throw new Error('DESKTOP_LIBRARY_UNEXPECTED_OUTPUT');
  const output = [
    '/* Existing learning modules, built with fixed project dependencies. Shared React is supplied by Harness. */',
    'window.LearningLibraryComponents = { create(nativeControls) {',
    'const __learningRuntime={Fragment:nativeControls.React.Fragment,jsx:(type,props,key)=>nativeControls.React.createElement(type,key===undefined?props:{...props,key}),jsxs:(type,props,key)=>nativeControls.React.createElement(type,key===undefined?props:{...props,key})};',
    'const __learningShared={react:nativeControls.React,"react/jsx-runtime":__learningRuntime,"react/jsx-dev-runtime":__learningRuntime,"react-dom":nativeControls.ReactDOM,"@deepseek-ai/dsh-client-ui-primitives":{Button:nativeControls.Button,Input:nativeControls.Input,Modal:nativeControls.Modal}};',
    'const require=id=>{if(!__learningShared[id])throw new Error("DESKTOP_LIBRARY_EXTERNAL_UNAVAILABLE: "+id);return __learningShared[id]};',
    'const module={exports:{}};const exports=module.exports;', chunks[0].code,
    'return module.exports; } };', '',
  ].join('\n');
  let styles = '';
  for (const [name, value] of [...css].sort(([left], [right]) => left.localeCompare(right))) {
    // Harness already owns KaTeX styling/fonts. Capture overlays have unique
    // root classes and can be portalled to body by their existing component.
    if (name.includes('katex.min.css')) continue;
    const capture = /(?:WorkspaceCapture|CaptureEditor|cropper)\.css$/i.test(name);
    styles += `\n/* ${path.basename(name)} */\n` + (capture ? value : scopeStyles(value));
  }
  const base = await readFile(path.join(root, 'frontend/src/styles.css'), 'utf8');
  const resourceStart = base.indexOf('.resource-viewer {');
  const resourceEnd = base.indexOf('.panel-resizer {', resourceStart);
  styles += '\n' + scopeStyles(base.slice(resourceStart, resourceEnd));
  const chat = await readFile(path.join(root, 'frontend/src/chat/chat.css'), 'utf8');
  styles += '\n' + scopeStyles(chat.slice(chat.indexOf('.markdown-answer {'), chat.indexOf('@media (max-width: 600px)')));
  styles += '\n.learning-legacy-module,.capture-editor {--foreground:var(--dsw-alias-label-primary);--muted-foreground:var(--dsw-alias-label-secondary);--background:var(--dsw-alias-bg-base);--surface:var(--dsw-alias-bg-base);--surface-elevated:var(--dsw-alias-bg-layer-2);--secondary:var(--dsw-alias-interactive-bg-hover);--muted:var(--dsw-alias-interactive-bg-hover);--border:var(--dsw-alias-border-l3);--primary:var(--dsw-alias-brand-primary);--accent:var(--dsw-alias-link);--destructive:var(--dsw-alias-state-error-primary);--ring:var(--dsw-alias-brand-primary);color:var(--foreground);font:inherit;min-width:0;min-height:0;}\n';
  styles += '.learning-material-viewer{height:100%;overflow:auto;position:relative}.learning-material-viewer>.markdown-answer{max-width:900px;padding:28px;margin:auto}.learning-material-selection{position:sticky;bottom:10px;display:flex;justify-content:flex-end;padding:8px}.learning-destructive-button{color:var(--destructive)!important}.learning-legacy-module .file-dialog-backdrop{z-index:1200}.learning-legacy-module input,.learning-legacy-module select,.learning-legacy-module textarea{color:var(--foreground);background:var(--background);border:1px solid var(--border);border-radius:8px;padding:6px 9px;min-width:0;}\n';
  const sources = [...usedSource, ...css.keys(), path.join(root, 'frontend/src/styles.css'), path.join(root, 'frontend/src/chat/chat.css'), path.join(root, 'scripts/desktop/build-learning.mjs')];
  const inputFiles = {};
  for (const name of [...new Set(sources)].sort()) inputFiles[normalize(path.relative(root, name))] = digest(await readFile(name));
  const manifest = { compiler: { name: 'rolldown', version: '1.2.14' }, input_files: inputFiles,
    lock_sha256: digest(await readFile(path.join(root, 'pnpm-lock.yaml'))), javascript_sha256: digest(output), css_sha256: digest(styles) };
  const target = path.join(root, '.runtime/desktop/client/library-components.js');
  const styleTarget = path.join(root, '.runtime/desktop/client/library-components.css');
  const manifestTarget = path.join(root, '.runtime/desktop/client/library-build.json');
  if (process.argv.includes('--check')) {
    if (digest(await readFile(target)) !== manifest.javascript_sha256 || digest(await readFile(styleTarget)) !== manifest.css_sha256) throw new Error('DESKTOP_LIBRARY_ARTIFACT_MISMATCH');
    if (JSON.stringify(JSON.parse(await readFile(manifestTarget, 'utf8'))) !== JSON.stringify(manifest)) throw new Error('DESKTOP_LIBRARY_SOURCE_MISMATCH');
  } else {
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, output); await writeFile(styleTarget, styles);
    await writeFile(manifestTarget, JSON.stringify(manifest, null, 2) + '\n');
  }
  console.log(JSON.stringify({ javascript_bytes: Buffer.byteLength(output), css_bytes: Buffer.byteLength(styles), inputs: Object.keys(inputFiles).length, shared_react: true,
    javascript_sha256: manifest.javascript_sha256, css_sha256: manifest.css_sha256,
    outputs: [target, styleTarget, manifestTarget].map(value => normalize(path.relative(root, value))) }));
} finally { await build.close(); }
