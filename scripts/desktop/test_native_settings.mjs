import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { installDesktopSettings } from '../../harness/plugins/desktop-settings.mjs';

test('native UI settings persist, keep execution settings guarded and do not accept failed writes', async () => {
  const directory = fs.mkdtempSync(path.resolve('.runtime/native-settings-'));
  const filename = path.join(directory, 'preferences.json');
  const entry = { id: 'ui-chat', options: { id: 'ui-chat', config: { performanceUsage: 'detailed' } } };
  let unavailable = false;
  let disposals;
  const editor = {
    configuration: () => [{ entry, inherited: { performanceUsage: 'detailed' }, override: {} }],
    edit: async () => { throw Error('managed execution configuration'); },
  };
  const scope = { configEditor: editor, effect: factory => { disposals = factory(); },
    loader: { async update(id, patch) {
      assert.equal(id, entry.id);
      if (unavailable) throw Error('loader refused');
      entry.options.config = patch.config;
    } } };
  installDesktopSettings({ inject: (_names, callback) => callback(scope) }, filename);
  await editor.edit(entry, raw => ({ ...raw, performanceUsage: 'compact' }));
  assert.equal(entry.options.config.performanceUsage, 'compact');
  assert.equal(JSON.parse(fs.readFileSync(filename)).namespaces['ui-chat'].performanceUsage, 'compact');
  assert.equal(editor.configuration()[0].override.performanceUsage, 'compact');
  await assert.rejects(editor.edit({ options: { id: 'learning-guard' } }, () => ({})), /managed/);
  unavailable = true;
  await assert.rejects(editor.edit(entry, () => ({ performanceUsage: 'detailed' })), /loader refused/);
  assert.equal(JSON.parse(fs.readFileSync(filename)).namespaces['ui-chat'].performanceUsage, 'compact');
  await disposals();
  fs.unlinkSync(filename);
  fs.rmdirSync(directory);
});
