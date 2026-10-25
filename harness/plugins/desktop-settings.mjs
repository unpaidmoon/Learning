// Native presentation settings for the pinned SEA, whose ConfigEditor cannot
// reconcile its root Include. Keep native schemas, validation and forms; apply
// only reviewed presentation entries through the native Loader.
import fs from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

const allowed = id => id.startsWith('ui-') || ['locale', 'agent-preset-registry', 'agent-default-model'].includes(id);
export function installDesktopSettings(ctx, filename) {
  if (!filename) return;
  ctx.inject(['configEditor'], scope => {
    const editor = scope.configEditor;
    const originalEdit = editor.edit;
    const originalConfiguration = editor.configuration;
    const read = () => {
      if (!fs.existsSync(filename)) return { version: 1, namespaces: {} };
      const record = JSON.parse(fs.readFileSync(filename, 'utf8'));
      if (record.version !== 1 || !record.namespaces || Array.isArray(record.namespaces)
          || typeof record.namespaces !== 'object') throw Error('DESKTOP_SETTINGS_STORAGE_INVALID');
      return record;
    };
    const write = record => {
      fs.mkdirSync(path.dirname(filename), { recursive: true });
      const temporary = filename + '.' + randomUUID() + '.tmp';
      try {
        const fd = fs.openSync(temporary, 'wx', 0o600);
        try { fs.writeFileSync(fd, JSON.stringify(record) + '\n'); fs.fsyncSync(fd); }
        finally { fs.closeSync(fd); }
        fs.renameSync(temporary, filename);
      } finally { fs.rmSync(temporary, { force: true }); }
    };
    let pending = Promise.resolve();
    const configuration = function () {
      const saved = read().namespaces;
      return originalConfiguration.call(this).map(row => allowed(row.entry.options.id)
        && Object.hasOwn(saved, row.entry.options.id) ? { ...row, override: saved[row.entry.options.id] } : row);
    };
    const edit = function (entry, change) {
      if (!allowed(entry.options.id)) return originalEdit.call(this, entry, change);
      const work = pending.catch(() => {}).then(async () => {
        const row = originalConfiguration.call(this).find(row => row.entry === entry);
        if (!row) throw Error('DESKTOP_SETTINGS_ENTRY_UNAVAILABLE');
        // Settings' callback owns schema validation, volatile-field admission
        // and the expected revision check. Never synthesize a successful view.
        const previous = structuredClone(entry.options.config ?? {});
        const next = change(structuredClone(previous), structuredClone(row.inherited));
        const record = read();
        await scope.loader.update(entry.id, { config: next });
        try { write({ version: 1, namespaces: { ...record.namespaces, [entry.options.id]: next } }); }
        catch (error) { await scope.loader.update(entry.id, { config: previous }); throw error; }
      });
      pending = work;
      return work;
    };
    editor.configuration = configuration;
    editor.edit = edit;
    scope.effect(() => async () => {
      await pending.catch(() => {});
      if (editor.edit === edit) editor.edit = originalEdit;
      if (editor.configuration === configuration) editor.configuration = originalConfiguration;
    });
  });
}
