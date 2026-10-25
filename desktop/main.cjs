// Window geometry and Windows caption behavior adapted from MIT-licensed
// deepseek-ai/deepseek-harness apps/desktop/src/main.ts at 687f282e8625c6b8d11ff58211a90524be2e4317.
// The original UI is served by the locked Harness runtime; this carrier owns
// only the local window. Application data and generation control stay in Python.
const { app, BrowserWindow, Menu, Tray, dialog, ipcMain, nativeTheme, session, shell } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const { allowOwnedFullscreen } = require('./permission-policy.cjs');
const { StartupSettings, ownFrame } = require('./startup-settings.cjs');

const root = path.resolve(__dirname, '..');
const target = new URL(process.env.LEARNING_DESKTOP_URL || 'http://127.0.0.1:8766/desktop/');
if (target.protocol !== 'http:' || target.hostname !== '127.0.0.1' || target.username || target.password || target.search || target.hash || target.pathname !== '/desktop/') {
  throw new Error('DESKTOP_INVALID_APPLICATION_URL');
}
const ownsBackend = process.env.LEARNING_DESKTOP_OWNS_BACKEND === '1';
let window;
let tray;
let quitting = false;
let quitPending = false;
app.setName('AI 辅助学习');
app.setPath('userData', process.env.LEARNING_DESKTOP_USER_DATA || path.join(root, '.runtime', 'desktop', 'electron-user-data'));
const resources = path.join(root, '.runtime', 'desktop', 'resources');
const ownerFile = process.env.LEARNING_DESKTOP_OWNER_FILE;
let ownerRecord;
function publishNativeOwner() {
  if (!ownerFile || process.platform !== 'win32' || !window || window.isDestroyed()) return;
  const bytes = window.getNativeWindowHandle();
  ownerRecord = { hwnd: bytes.readBigUInt64LE().toString(), pid: process.pid, registered_at_ms: Date.now() };
  const temporary = ownerFile + '.tmp';
  fs.writeFileSync(temporary, JSON.stringify(ownerRecord), { encoding: 'utf8', mode: 0o600 });
  fs.renameSync(temporary, ownerFile);
}
const appearance = () => ({
  height: 40,
  color: nativeTheme.shouldUseDarkColors ? '#1b1b1c' : '#f9fafb',
  symbolColor: nativeTheme.shouldUseDarkColors ? '#f9fafb' : '#0f1115',
});
const show = () => {
  if (!window || window.isDestroyed()) return;
  if (window.isMinimized()) window.restore();
  window.show();
  window.focus();
};

async function jsonRequest(route, init) {
  const response = await session.defaultSession.fetch(new URL(route, target.origin).href, init);
  if (!response.ok) throw new Error(`DESKTOP_RUNTIME_HTTP_${response.status}`);
  return response.json();
}

async function requestQuit() {
  if (quitPending || quitting) return;
  quitPending = true;
  try {
    if (ownsBackend) {
      const runtime = await jsonRequest('/api/v1/runtime');
      let confirmActive = false;
      if (runtime.active_generations.length || runtime.active_tasks?.length || runtime.active_preparations?.length || runtime.active_data_operations?.length) {
        const answer = await dialog.showMessageBox(window, {
          type: 'question', title: '退出 AI 辅助学习',
          message: '仍有回答、学习任务或数据操作在进行或等待核对，保存并请求停止后退出？',
          buttons: ['取消', '退出'], defaultId: 0, cancelId: 0,
        });
        if (answer.response !== 1) return;
        confirmActive = true;
      }
      const auth = await jsonRequest('/api/v1/session');
      const result = await jsonRequest('/api/v1/runtime/exit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Learning-CSRF': auth.csrf_token, Origin: target.origin },
        body: JSON.stringify({ instance_id: runtime.instance_id, confirm_active: confirmActive }),
      });
      if (result.instance_id !== runtime.instance_id || !result.exit_requested) throw new Error('DESKTOP_EXIT_NOT_CONFIRMED');
      process.stdout.write(`LEARNING_DESKTOP_EXIT_CONFIRMED ${runtime.instance_id}\n`);
    }
    quitting = true;
    // A request can originate inside before-quit. Restart the accepted quit
    // after that cancelled event unwinds; a nested synchronous quit is ignored.
    setImmediate(() => app.quit());
  } catch (error) {
    await dialog.showMessageBox(window, { type: 'error', title: '暂时无法退出', message: '后台尚未确认退出。窗口继续保留，请稍后重试。', detail: String(error.message || error), buttons: ['确定'] });
  } finally {
    quitPending = false;
  }
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', show);
  app.on('before-quit', (event) => { if (!quitting) { event.preventDefault(); void requestQuit(); } });
  app.on('window-all-closed', () => {});
  app.on('will-quit', () => {
    if (!ownerFile || !ownerRecord) return;
    try {
      const current = JSON.parse(fs.readFileSync(ownerFile, 'utf8'));
      if (current.pid === process.pid && current.hwnd === ownerRecord.hwnd) fs.unlinkSync(ownerFile);
    } catch {}
  });
  app.whenReady().then(async () => {
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      { label: 'AI 辅助学习', submenu: [
        { label: '显示窗口', click: show },
        { label: '关于', click: () => dialog.showMessageBox(window, { title: 'AI 辅助学习', message: 'AI 辅助学习桌面试用', detail: '界面基于 DeepSeek Harness 开源客户端。\n本项目独立维护，与 DeepSeek 官方桌面发行版不同。', buttons: ['确定'] }) },
        { type: 'separator' }, { label: '退出', click: () => void requestQuit() },
      ] },
      { label: '编辑', submenu: [{ role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' }, { type: 'separator' }, { role: 'cut', label: '剪切' }, { role: 'copy', label: '复制' }, { role: 'paste', label: '粘贴' }, { role: 'selectAll', label: '全选' }] },
      { label: '视图', submenu: [{ role: 'reload', label: '重新载入' }, { role: 'togglefullscreen', label: '全屏' }, { role: 'toggleDevTools', label: '开发工具' }] },
    ]));
    session.defaultSession.setPermissionRequestHandler((contents, permission, callback, details) => {
      callback(allowOwnedFullscreen(permission, contents, window?.webContents, details, target.origin));
    });
    window = new BrowserWindow({
      width: 1280, height: 820, minWidth: 720, minHeight: 600,
      title: 'AI 辅助学习', show: false, icon: path.join(resources, 'icon-windows.png'),
      ...(process.platform === 'win32' ? { titleBarStyle: 'hidden', titleBarOverlay: appearance() } : {}),
      webPreferences: { preload: path.join(__dirname, 'preload.cjs'), nodeIntegration: false, contextIsolation: true, sandbox: true, webSecurity: true, devTools: true },
    });
    publishNativeOwner();
    // Admit native-picker requests only after restoring/focusing this owned
    // window. The renderer never receives a HWND or a window-management IPC.
    session.defaultSession.webRequest.onBeforeRequest({ urls: [`${target.origin}/api/v1/courses/*`] }, (details, callback) => {
      const pathname = new URL(details.url).pathname;
      if (details.webContentsId === window.webContents.id && details.method === 'POST' && window.isEnabled()
        && (/^\/api\/v1\/courses\/folder(?:\/pick)?$/.test(pathname)
          || /^\/api\/v1\/courses\/[^/]+\/(?:root|videos\/pick|files\/import)$/.test(pathname))) show();
      callback({});
    });
    window.webContents.setWindowOpenHandler(({ url }) => {
      const destination = new URL(url);
      if (['https:', 'http:'].includes(destination.protocol) && !destination.username && !destination.password) void shell.openExternal(destination.href);
      return { action: 'deny' };
    });
    window.webContents.on('will-navigate', (event, url) => {
      if (new URL(url).origin !== target.origin) event.preventDefault();
    });
    window.on('page-title-updated', event => event.preventDefault());
    window.on('close', (event) => { if (!quitting) { event.preventDefault(); window.hide(); } });
    window.once('ready-to-show', () => { if (process.env.LEARNING_DESKTOP_TEST_HIDDEN !== '1') show(); });
    window.webContents.on('did-finish-load', () => {
      if (process.env.LEARNING_DESKTOP_TEST_HIDDEN !== '1') show();
      process.stdout.write('LEARNING_DESKTOP_WINDOW_READY\n');
    });
    ipcMain.on('learning-desktop:appearance', (event, value) => {
      if (event.sender !== window.webContents || process.platform !== 'win32' || !value || typeof value.color !== 'string' || typeof value.symbolColor !== 'string') return;
      if (!/^rgba?\([\d., ]+\)$|^#[a-f\d]{6}$/i.test(value.color) || !/^rgba?\([\d., ]+\)$|^#[a-f\d]{6}$/i.test(value.symbolColor)) return;
      window.setTitleBarOverlay({ height: 40, color: value.color, symbolColor: value.symbolColor });
    });
    const startup = new StartupSettings({ app, root, userData: app.getPath('userData'),
      profile: ownsBackend && process.env.LEARNING_APP_DATA_DIR ? path.dirname(process.env.LEARNING_APP_DATA_DIR) : null,
      port: target.port });
    ipcMain.handle('learning-desktop:startup', (event, value) => {
      if (!ownFrame(event, window, target)) throw Error('DESKTOP_RUNTIME_BRIDGE_DENIED');
      return startup.request(value);
    });
    tray = new Tray(path.join(resources, 'tray-windows.ico'));
    tray.setToolTip('AI 辅助学习');
    tray.setContextMenu(Menu.buildFromTemplate([{ label: '打开 AI 辅助学习', click: show }, { label: '退出 AI 辅助学习', click: () => void requestQuit() }]));
    tray.on('click', show);
    try { await window.loadURL(target.href); }
    catch { await dialog.showMessageBox(window, { type: 'error', title: '无法打开学习界面', message: 'DESKTOP_PAGE_UNAVAILABLE：请检查本项目后台是否已就绪。', buttons: ['确定'] }); quitting = true; app.quit(); }
  });
}
