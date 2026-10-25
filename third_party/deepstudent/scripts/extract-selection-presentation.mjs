// Only original source types and display formatters; no upstream storage/chat.
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const require = createRequire(path.join(root, 'frontend/package.json'));
const ts = require('typescript');
if (ts.version !== '5.9.3') throw new Error('SELECTION_COMPILER_VERSION_MISMATCH');
const digest = value => createHash('sha256').update(value).digest('hex');
const sourcePath = 'third_party/deepstudent/upstream/src/features/chat/context/selectionRef.ts';
const original = await readFile(path.join(root, sourcePath));
if (digest(original) !== '56ddea18cf3ac459b348f5ed8edc8fc9c8e55f018114c7fd2779c58c88e35347') throw new Error('SELECTION_UPSTREAM_HASH_MISMATCH');
const source = ts.createSourceFile(sourcePath, original.toString('utf8'), ts.ScriptTarget.Latest, true);
const names = new Set(['SelectionSourceKind', 'SelectionRefData', 'describeSelectionLocator', 'buildSelectionDisplayName']);
const selected = source.statements.filter(node => node.name && names.has(node.name.text));
if (selected.length !== names.size) throw new Error('SELECTION_DECLARATION_MISSING');
const output = '/* DeepStudent 635216503c0b34bdf812e17942e0f09f51872e6f, AGPL-3.0-or-later.\n'
  + ' * Original selected declarations, unchanged. Source and license accompany the project.\n'
  + ' * Storage, snapshot truncation, navigation and upstream app imports are excluded. */\n'
  + "import { t } from './selectionLocale';\n\n" + selected.map(node => node.getText(source)).join('\n\n') + '\n';
const target = path.join(root, 'third_party/deepstudent/web/selectionPresentation.ts');
const locale = path.join(root, 'third_party/deepstudent/web/selectionLocale.ts');
const provenance = { source_repository: 'https://github.com/helixnow/deep-student',
  source_commit: '635216503c0b34bdf812e17942e0f09f51872e6f', source_path: 'src/features/chat/context/selectionRef.ts',
  source_sha256: digest(original), license: 'AGPL-3.0-or-later',
  license_path: 'third_party/deepstudent/notices/LICENSE.AGPL-3.0',
  output_path: 'third_party/deepstudent/web/selectionPresentation.ts', output_sha256: digest(output),
  declarations: selected.map(node => ({ name: node.name.text, unchanged_text_sha256: digest(node.getText(source)) })),
  locale_adapter_sha256: digest(await readFile(locale)), build_typescript: ts.version,
  runtime_role: 'source position labels only; project immutable snapshots remain authoritative' };
if (process.argv.includes('--check')) {
  if (await readFile(target, 'utf8') !== output) throw new Error('SELECTION_EXTRACT_CHANGED');
  if (JSON.stringify(JSON.parse(await readFile(path.join(path.dirname(target), 'selection-provenance.json'), 'utf8'))) !== JSON.stringify(provenance)) throw new Error('SELECTION_PROVENANCE_CHANGED');
} else {
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(target, output);
  await writeFile(path.join(path.dirname(target), 'selection-provenance.json'), JSON.stringify(provenance, null, 2) + '\n');
}
console.log(JSON.stringify({ declarations: selected.length, output_sha256: provenance.output_sha256 }));
