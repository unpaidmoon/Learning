// Reuse project playback controls with the pinned Harness React and primitives.
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const outputRoot = path.join(root, '.runtime/desktop/client');
const compilerRoot = path.join(root, 'node_modules/.pnpm/rolldown@1.2.14/node_modules/rolldown');
const compiler = JSON.parse(await readFile(path.join(compilerRoot, 'package.json'), 'utf8'));
if (compiler.version !== '1.2.14') throw new Error('DESKTOP_VIDEO_COMPILER_VERSION_MISMATCH');
const { rolldown } = await import(pathToFileURL(path.join(compilerRoot, 'dist/index.mjs')).href);
const digest = value => createHash('sha256').update(value).digest('hex');
const normalize = value => value.replaceAll('\\', '/');
const css = new Map();
const sources = new Set();
const externals = new Set(['react', 'react/jsx-runtime', '@deepseek-ai/dsh-client-ui-primitives']);
const buttonPath = normalize(path.join(root, 'frontend/src/components/ui/button.tsx'));
const videoPath = normalize(path.join(root, 'frontend/src/media/VideoWorkspace.tsx'));
const build = await rolldown({
  input: path.join(root, 'desktop/src/video-entry.tsx'), cwd: root, platform: 'browser',
  external: id => externals.has(id), transform: { jsx: { runtime: 'automatic' } },
  plugins: [{
    name: 'learning-video-shared-controls',
    resolveId(source, importer) {
      if (!importer || !source.startsWith('.')) return null;
      const candidate = normalize(path.resolve(path.dirname(importer), source));
      if (candidate === buttonPath || candidate === buttonPath.slice(0, -4)) return '\0native-video-button';
      return null;
    },
    async load(id) {
      if (id === '\0native-video-button') return { code: `import {createElement,forwardRef} from 'react';import{Button as NativeButton}from '@deepseek-ai/dsh-client-ui-primitives';export const Button=forwardRef(function LearningVideoButton({variant='default',size='default',...props},ref){return createElement(NativeButton,{...props,ref,variant:({default:'primary',secondary:'outline',destructive:'outline',link:'ghost'})[variant]||variant,size:['sm','xs','icon-xs','icon-sm'].includes(size)?'sm':'md'})});`, moduleType: 'js' };
      if (id.endsWith('.css')) { css.set(id, await readFile(id, 'utf8')); sources.add(id); return { code: '', moduleType: 'js' }; }
      if (!id.startsWith('\0') && !id.includes('node_modules') && normalize(id).startsWith(normalize(root))) sources.add(id);
      if (normalize(id) === videoPath) {
        let code = await readFile(id, 'utf8');
        const before = code;
        code = code.replace(/\n  ref,\r?\n\}: \{/, '\n  workspaceHandleRef,\n}: {')
          .replace('ref?: Ref<VideoWorkspaceHandle>;', 'workspaceHandleRef?: Ref<VideoWorkspaceHandle>;')
          .replace('useImperativeHandle(ref,', 'useImperativeHandle(workspaceHandleRef,');
        if (code === before || code.includes('useImperativeHandle(ref,')) throw new Error('DESKTOP_VIDEO_REF_ADAPTER_MISMATCH');
        return { code, moduleType: 'tsx' };
      }
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
    for (; close < text.length && depth; close++) { if (text[close] === '{') depth++; else if (text[close] === '}') depth--; }
    const body = text.slice(open + 1, close - 1);
    const clean = header.replace(/\/\*[\s\S]*?\*\//g, '').trim();
    if (clean.startsWith('@media') || clean.startsWith('@supports')) output += header + '{' + scopeStyles(body) + '}';
    else if (clean.startsWith('@')) output += header + '{' + body + '}';
    else output += clean.split(',').map(selector => '.learning-video-components ' + selector.trim()).join(',\n') + '{' + body + '}';
    cursor = close;
  }
  return output;
}
try {
  const result = await build.generate({ format: 'cjs', exports: 'named', sourcemap: false, minify: false });
  const chunks = result.output.filter(item => item.type === 'chunk');
  if (chunks.length !== 1 || result.output.length !== 1) throw new Error('DESKTOP_VIDEO_UNEXPECTED_OUTPUT');
  const code = ['/* Shared existing project video behavior; Harness owns React and native controls. */',
    'window.LearningVideoComponents={create(nativeControls){',
    'const jsxRuntime={Fragment:nativeControls.React.Fragment,jsx:(type,props,key)=>nativeControls.React.createElement(type,key===undefined?props:{...props,key}),jsxs:(type,props,key)=>nativeControls.React.createElement(type,key===undefined?props:{...props,key})};',
    'const shared={react:nativeControls.React,"react/jsx-runtime":jsxRuntime,"@deepseek-ai/dsh-client-ui-primitives":{Button:nativeControls.Button,Menu:nativeControls.Menu}};',
    'const require=id=>{if(!shared[id])throw new Error("DESKTOP_VIDEO_EXTERNAL_UNAVAILABLE: "+id);return shared[id]};',
    'const module={exports:{}};const exports=module.exports;', chunks[0].code.trimEnd(), 'return module.exports;}};', ''].join('\n');
  const styles = [...css].sort(([a], [b]) => a.localeCompare(b)).map(([name, value]) => `/* ${path.basename(name)} */\n` + scopeStyles(value)).join('\n')
    + '\n.learning-video-components{height:100%;min-height:0;min-width:0;color:var(--dsw-alias-label-primary);font:inherit}.learning-video-components .video-error{font-size:13px;color:var(--dsw-alias-status-error)}\n';
  sources.add(fileURLToPath(import.meta.url));
  const inputs = Object.fromEntries(await Promise.all([...sources].sort().map(async name => [normalize(path.relative(root, name)), digest(await readFile(name))])));
  const manifest = { compiler: 'rolldown@1.2.14', shared_react: true, source_files: inputs, javascript_sha256: digest(code), css_sha256: digest(styles) };
  await mkdir(outputRoot, { recursive: true });
  if (process.argv.includes('--check')) {
    if (digest(await readFile(path.join(outputRoot, 'video-components.js'))) !== manifest.javascript_sha256
      || digest(await readFile(path.join(outputRoot, 'video-components.css'))) !== manifest.css_sha256) throw new Error('DESKTOP_VIDEO_BUILD_STALE');
  } else {
    await writeFile(path.join(outputRoot, 'video-components.js'), code);
    await writeFile(path.join(outputRoot, 'video-components.css'), styles);
    await writeFile(path.join(outputRoot, 'video-build.json'), JSON.stringify(manifest, null, 2) + '\n');
  }
  console.log(JSON.stringify({ javascript_bytes: Buffer.byteLength(code), css_bytes: Buffer.byteLength(styles), inputs: Object.keys(inputs).length, shared_react: true, javascript_sha256: manifest.javascript_sha256, css_sha256: manifest.css_sha256 }));
} finally { await build.close(); }
