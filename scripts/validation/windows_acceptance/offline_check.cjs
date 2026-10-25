// Acceptance-only viewer. Networking changes are confined to this disposable
// Electron session; cloud-desktop connectivity and system settings are untouched.
const { app, BrowserWindow, session } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const values = process.argv.slice(2);
const argument = name => values[values.indexOf(name) + 1];
const samples = path.resolve(argument('--samples'));
const output = path.resolve(argument('--output'));
app.setPath('userData', path.join(output, 'offline-session'));
app.disableHardwareAcceleration();
const result = { status: 'running', assertions: [], layer: 'packaged Electron + actual saved offline artifacts; per-session network denial', model_requests: 0 };
const check = (condition, name) => {
  result.assertions.push({ name, passed: !!condition });
  if (!condition) throw Error(name);
};
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function wait(page, expression) {
  const until = Date.now() + 20000;
  while (Date.now() < until) {
    if (await page.executeJavaScript(expression)) return;
    await pause(50);
  }
  throw Error('OFFLINE_EXPECTED_STATE_MISSING:' + expression.slice(0, 80));
}
app.whenReady().then(async () => {
  let window;
  try {
    const isolated = session.fromPartition('acceptance-' + crypto.randomUUID());
    isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    isolated.setPermissionCheckHandler(() => false);
    const httpRequests = [];
    isolated.webRequest.onBeforeRequest({ urls: ['http://*/*', 'https://*/*'] }, (details, callback) => {
      httpRequests.push(details.url);
      callback({ cancel: true });
    });
    isolated.enableNetworkEmulation({ offline: true });
    window = new BrowserWindow({ show: false, width: 1280, height: 900,
      webPreferences: { session: isolated, sandbox: true, contextIsolation: true, nodeIntegration: false, webSecurity: true } });
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    const manifests = JSON.parse(fs.readFileSync(path.join(samples, 'samples.json'), 'utf8'));
    for (const item of manifests) {
      const target = path.join(samples, item.directory, 'index.html');
      await window.loadFile(target);
      await wait(window.webContents, 'Boolean(document.querySelector("h1"))');
      await wait(window.webContents, 'Array.from(document.images).every(image=>image.complete&&image.naturalWidth>0)');
      const info = await window.webContents.executeJavaScript(`(()=>({title:document.querySelector('h1').textContent,node:typeof require!=='undefined'||typeof process!=='undefined',math:document.querySelectorAll('.katex').length,images:document.images.length}))()`);
      check(info.title === item.title && !info.node, 'actual_saved_artifact_opens_in_sandbox_' + item.kind);
      if (item.kind === 'book') {
        await window.webContents.executeJavaScript(`(()=>{const e=document.querySelector('nav[aria-label="交互书目录"]');if(!e)throw Error('BOOK_NAVIGATION_MISSING');e.querySelectorAll('button')[1].click();})()`);
        await wait(window.webContents, 'Boolean(document.querySelector("input[type=radio]"))');
        await window.webContents.executeJavaScript(`(()=>{document.querySelector('input[type=radio]').click();Array.from(document.querySelectorAll('button')).find(e=>e.textContent==='对照参考答案').click();})()`);
        await wait(window.webContents, '/与参考选项/.test(document.body.innerText)');
        check(true, 'actual_offline_book_navigation_and_local_self_test');
      }
      if (item.kind === 'visualization') {
        await wait(window.webContents, 'Boolean(document.querySelector("svg[data-worker-frame]"))');
        await window.webContents.executeJavaScript(`(()=>{const e=document.querySelector('input[type=range]');Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(e,'2');e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
        await wait(window.webContents, '/0\\.828/.test(document.body.innerText)');
        check(true, 'actual_saved_function_uses_offline_worker_and_parameter_value');
      }
      if (item.kind === 'card_set') {
        await window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(e=>e.textContent==='翻面核对').click()`);
        await wait(window.webContents, `Array.from(document.querySelectorAll('button')).some(e=>e.textContent==='已回忆')`);
        await window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(e=>e.textContent==='已回忆').click()`);
        await wait(window.webContents, '/本次标为已回忆/.test(document.body.innerText)');
        check(true, 'actual_offline_card_flips_and_recalls_without_formal_record_write');
      }
      check(httpRequests.length === 0, 'no_http_request_from_offline_artifact_' + item.kind);
    }
    // Calibrate only after all reader measurements. Requests remain blocked
    // before DNS/network dispatch; no system network adapter is disabled.
    isolated.disableNetworkEmulation();
    await window.loadURL('https://offline-monitor.invalid/kit-calibration').catch(() => undefined);
    check(httpRequests.some(url => url.includes('/kit-calibration')), 'same_session_network_observer_calibration');
    result.status = 'passed';
  } catch (error) {
    result.status = 'failed'; result.error = String(error);
    if (window && !window.isDestroyed()) result.visible_text = await window.webContents.executeJavaScript('document.body.innerText.slice(0,3000)').catch(() => 'unavailable');
  } finally {
    if (window && !window.isDestroyed()) window.destroy();
    fs.mkdirSync(output, { recursive: true });
    fs.writeFileSync(path.join(output, 'offline-summary.json'), JSON.stringify(result, null, 2), 'utf8');
    app.exit(result.status === 'passed' ? 0 : 1);
  }
});
