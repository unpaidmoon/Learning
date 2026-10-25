// Current-profile Windows startup uses Electron's approval-aware native API.
// No command/path comes from the renderer; only explicit boolean mutations.
const fs = require('node:fs');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');

const digest = value => createHash('sha256').update(typeof value === 'string' ? value : JSON.stringify(value)).digest('hex');
const fail = code => { throw new Error(code); };
function write(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${randomUUID()}.tmp`;
  const handle = fs.openSync(temporary, 'wx', 0o600);
  try { fs.writeFileSync(handle, JSON.stringify(value)); fs.fsyncSync(handle); }
  finally { fs.closeSync(handle); }
  fs.renameSync(temporary, file);
}

function ownFrame(event, owner, target) {
  if (!owner || owner.isDestroyed() || event.sender !== owner.webContents
    || event.senderFrame !== owner.webContents.mainFrame) return false;
  try {
    const url = new URL(event.senderFrame.url);
    return !url.username && !url.password && url.origin === target.origin && url.pathname === target.pathname;
  } catch { return false; }
}

class StartupSettings {
  constructor({ app, root, userData, profile, port, platform = process.platform }) {
    this.app = app;
    this.entry = path.join(root, '启动学习.exe');
    this.manifest = path.join(root, 'desktop-release.json');
    this.directory = path.join(userData, 'runtime-operations', 'startup');
    this.profile = profile;
    this.platform = platform;
    // Windows forbids quote characters in paths; double trailing separators
    // before adding quotes to preserve the normal Windows argv contract.
    if (profile && /["\0\r\n]/.test(profile)) fail('STARTUP_PROFILE_INVALID');
    const quotedProfile = profile ? '"' + profile.replace(/(\\+)$/, '$1$1') + '"' : '';
    this.args = ['--profile-dir', quotedProfile, '--backend-port', String(port)];
    this.name = 'LearningWorkbench-' + digest(String(profile || '')).slice(0, 16);
  }

  available() {
    if (this.platform !== 'win32' || !this.profile) return false;
    try {
      const spec = JSON.parse(fs.readFileSync(this.manifest, 'utf8')).files['启动学习.exe'];
      return !fs.lstatSync(this.entry).isSymbolicLink() && fs.statSync(this.entry).size === spec.bytes
        && createHash('sha256').update(fs.readFileSync(this.entry)).digest('hex') === spec.sha256;
    } catch { return false; }
  }

  status() {
    if (!this.available()) return { available: false, enabled: false, registered: false, revision: null, error_code: 'STARTUP_PORTABLE_ENTRY_REQUIRED' };
    try {
      const state = this.app.getLoginItemSettings({ path: this.entry, args: this.args });
      if (typeof state.openAtLogin !== 'boolean' || !Array.isArray(state.launchItems)) fail('STARTUP_STATUS_UNKNOWN');
      const own = state.launchItems.filter(item => item.name === this.name && item.scope === 'user');
      if (state.openAtLogin && !own.length) return { available: true, enabled: null, registered: true, revision: null, error_code: 'STARTUP_OTHER_ENTRY_EXISTS' };
      if (own.some(item => typeof item.enabled !== 'boolean')) fail('STARTUP_STATUS_UNKNOWN');
      const registered = state.openAtLogin && own.length > 0;
      const enabled = registered && own.some(item => item.enabled === true);
      return { available: true, enabled, registered, revision: digest({ registered, enabled, name: this.name, entry: this.entry, args: this.args }), error_code: null };
    } catch { return { available: true, enabled: null, registered: null, revision: null, error_code: 'STARTUP_STATUS_UNAVAILABLE' }; }
  }

  file(identity) {
    if (typeof identity !== 'string' || !/^startup_[a-f0-9]{32}$/.test(identity)) fail('STARTUP_OPERATION_ID_INVALID');
    return path.join(this.directory, identity + '.json');
  }

  reconcile(identity) {
    const file = this.file(identity);
    if (!fs.existsSync(file)) return { state: 'not_found', current: this.status(), error_code: 'STARTUP_OPERATION_NOT_FOUND' };
    const operation = JSON.parse(fs.readFileSync(file, 'utf8'));
    const current = this.status();
    if (operation.state !== 'completed' && current.available && current.enabled === operation.enabled) {
      operation.state = 'completed'; write(file, operation);
    }
    return { state: operation.state, current, error_code: operation.state === 'completed' ? null : 'STARTUP_RESULT_UNKNOWN' };
  }

  set(request) {
    if (!request || Object.keys(request).sort().join() !== 'enabled,expected_revision,operation_id'
      || typeof request.enabled !== 'boolean' || typeof request.expected_revision !== 'string') fail('STARTUP_INPUT_INVALID');
    const file = this.file(request.operation_id), requestHash = digest(request);
    if (fs.existsSync(file)) {
      if (JSON.parse(fs.readFileSync(file, 'utf8')).request_hash !== requestHash) fail('STARTUP_OPERATION_CONFLICT');
      return this.reconcile(request.operation_id);
    }
    const current = this.status();
    if (!current.available) fail('STARTUP_PORTABLE_ENTRY_REQUIRED');
    if (current.revision !== request.expected_revision) fail('STARTUP_STATUS_CONFLICT');
    const operation = { state: 'prepared', enabled: request.enabled, request_hash: requestHash };
    write(file, operation);
    try {
      this.app.setLoginItemSettings({ openAtLogin: request.enabled, enabled: request.enabled, name: this.name, path: this.entry, args: this.args });
    } catch {
      operation.state = 'unknown'; write(file, operation);
      return { state: 'unknown', current: this.status(), error_code: 'STARTUP_RESULT_UNKNOWN' };
    }
    return this.reconcile(request.operation_id);
  }

  request(value) {
    if (!value || typeof value !== 'object') fail('STARTUP_INPUT_INVALID');
    if (value.method === 'get' && Object.keys(value).length === 1) return this.status();
    if (value.method === 'set' && Object.keys(value).length === 2) return this.set(value.value);
    if (value.method === 'reconcile' && Object.keys(value).length === 2) return this.reconcile(value.operation_id);
    fail('STARTUP_METHOD_DENIED');
  }
}

module.exports = { StartupSettings, ownFrame };
