const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID, createHash } = require('node:crypto');
const { StartupSettings, ownFrame } = require('../startup-settings.cjs');

function fixture() {
  const base = path.resolve(__dirname, '../../.runtime/tests/startup-node-' + randomUUID());
  const root = path.join(base, '运行包 空格');
  fs.mkdirSync(root, { recursive: true });
  const executable = Buffer.from('self-created launcher fixture; not executed');
  fs.writeFileSync(path.join(root, '启动学习.exe'), executable);
  fs.writeFileSync(path.join(root, 'desktop-release.json'), JSON.stringify({ files: { '启动学习.exe': { bytes: executable.length, sha256: createHash('sha256').update(executable).digest('hex') } } }));
  const app = {
    calls: [], saved: null, approved: true, loseAck: false,
    getLoginItemSettings(options) {
      if (this.saved) assert.deepEqual(options.args, this.saved.args);
      return { openAtLogin: Boolean(this.saved?.openAtLogin), launchItems: this.saved?.openAtLogin ? [{ ...this.saved, scope: 'user', enabled: this.approved }] : [] };
    },
    setLoginItemSettings(value) {
      this.calls.push(value); this.saved = value;
      if (this.loseAck) throw Error('owned lost acknowledgment');
    },
  };
  const control = new StartupSettings({ app, root, userData: path.join(base, 'browser'), profile: path.join(base, '资料 位置'), port: 19455, platform: 'win32' });
  return { app, control, root };
}
const operation = () => 'startup_' + randomUUID().replaceAll('-', '');

test('status reads never create a startup entry and explicit change uses only fixed owned argv', () => {
  const { control, app } = fixture();
  const initial = control.status();
  assert.equal(initial.enabled, false);
  assert.equal(app.calls.length, 0);
  const id = operation();
  const value = { operation_id: id, enabled: true, expected_revision: initial.revision };
  assert.equal(control.set(value).state, 'completed');
  assert.equal(control.set(value).state, 'completed');
  assert.equal(app.calls.length, 1);
  assert.equal(app.calls[0].path, control.entry);
  assert.deepEqual(app.calls[0].args, control.args);
  assert.ok(control.args[1].startsWith('"') && control.args[1].endsWith('"'));
  const enabled = control.status();
  control.set({ operation_id: operation(), enabled: false, expected_revision: enabled.revision });
  assert.equal(control.set(value).current.enabled, false);
  assert.equal(app.calls.length, 2);
});

test('system approval is an independent fact, missing approval stays unknown', () => {
  const { control, app } = fixture();
  control.set({ operation_id: operation(), enabled: true, expected_revision: control.status().revision });
  app.approved = false;
  assert.equal(control.status().registered, true);
  assert.equal(control.status().enabled, false);
  app.approved = undefined;
  assert.equal(control.status().enabled, null);
});

test('lost native acknowledgment only reconciles actual state without another write', () => {
  const { control, app } = fixture();
  app.loseAck = true;
  const id = operation();
  assert.equal(control.set({ operation_id: id, enabled: true, expected_revision: control.status().revision }).state, 'unknown');
  assert.equal(control.reconcile(id).state, 'completed');
  assert.equal(app.calls.length, 1);
});

test('stale snapshots, extra paths and invalid operations are denied before system mutation', () => {
  const { control, app } = fixture();
  assert.throws(() => control.set({ operation_id: operation(), enabled: true, expected_revision: 'stale' }), /STARTUP_STATUS_CONFLICT/);
  assert.throws(() => control.set({ operation_id: operation(), enabled: true, expected_revision: control.status().revision, path: 'foreign' }), /STARTUP_INPUT_INVALID/);
  assert.throws(() => control.request({ method: 'shell' }), /STARTUP_METHOD_DENIED/);
  assert.throws(() => control.reconcile('../foreign'), /STARTUP_OPERATION_ID_INVALID/);
  assert.equal(app.calls.length, 0);
});

test('only the owned main frame at the local app origin may invoke the bridge', () => {
  const frame = { url: 'http://127.0.0.1:19455/desktop/' };
  const webContents = { mainFrame: frame };
  const owner = { webContents, isDestroyed: () => false };
  const target = new URL(frame.url);
  assert.equal(ownFrame({ sender: webContents, senderFrame: frame }, owner, target), true);
  assert.equal(ownFrame({ sender: webContents, senderFrame: { url: frame.url } }, owner, target), false);
  assert.equal(ownFrame({ sender: {}, senderFrame: frame }, owner, target), false);
  frame.url = 'https://foreign.invalid/desktop/';
  assert.equal(ownFrame({ sender: webContents, senderFrame: frame }, owner, target), false);
});
