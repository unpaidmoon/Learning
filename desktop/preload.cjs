// Adapted from Harness apps/desktop/src/preload-windows.ts (MIT, pinned source
// in harness/desktop.lock.json). Exposes no filesystem or credential access.
const { ipcRenderer, contextBridge } = require('electron');
if (process.isMainFrame) contextBridge.exposeInMainWorld('learningDesktopRuntime', {
  startupStatus: () => ipcRenderer.invoke('learning-desktop:startup', { method: 'get' }),
  setStartup: value => {
    if (!navigator.userActivation?.isActive) return Promise.reject(Error('STARTUP_USER_ACTION_REQUIRED'));
    return ipcRenderer.invoke('learning-desktop:startup', { method: 'set', value });
  },
  reconcileStartup: operation_id => ipcRenderer.invoke('learning-desktop:startup', { method: 'reconcile', operation_id }),
});
if (process.platform === 'win32') {
  const install = () => {
    const root = document.documentElement;
    root.dataset.windowsTitlebar = '';
    root.style.setProperty('--dsh-windows-titlebar-height', '40px');
    const probe = document.createElement('span');
    probe.style.cssText = 'position:fixed;visibility:hidden;pointer-events:none;background-color:var(--dsw-specific-sidebar-fill);color:var(--dsw-alias-label-primary)';
    document.body.append(probe);
    let previous = '';
    const send = () => {
      const style = getComputedStyle(probe);
      const value = { color: style.backgroundColor, symbolColor: style.color };
      const current = JSON.stringify(value);
      if (current === previous) return;
      previous = current;
      ipcRenderer.send('learning-desktop:appearance', value);
    };
    const observer = new MutationObserver(send);
    observer.observe(document.body, { attributes: true, attributeFilter: ['data-ds-dark-theme', 'style'] });
    observer.observe(document.head, { childList: true, subtree: true, characterData: true });
    document.head.addEventListener('load', send, true);
    window.addEventListener('pagehide', () => { observer.disconnect(); probe.remove(); document.head.removeEventListener('load', send, true); }, { once: true });
    send();
  };
  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', install, { once: true });
  else install();
}
