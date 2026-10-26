/* Project learning plugin for the pinned, unmodified Harness browser client.
 * The renderer receives only the application session. Native RPC writes cross
 * the server-side business adapter; no Harness capability is exposed here.
 */
(() => {
  'use strict';
  const id = '@learning/desktop-video';
  const rawFetch = window.fetch.bind(window);
  const nativeHooks = {};
  const uploadIds = new Map();
  let csrf;
  let applicationInstance;
  let reconnecting;
  const ready = rawFetch('/api/v1/session', { credentials: 'same-origin', cache: 'no-store' })
    .then(async response => {
      if (!response.ok) throw new Error('DESKTOP_SESSION_REQUIRED');
      csrf = (await response.json()).csrf_token;
      const health = await rawFetch('/api/v1/health', { credentials: 'same-origin', cache: 'no-store' });
      if (health.ok) applicationInstance = (await health.json()).instance_id;
    });
  async function observeBackend() {
    if (reconnecting) return;
    reconnecting = (async () => {
      const healthResponse = await rawFetch('/api/v1/health', { credentials: 'same-origin', cache: 'no-store' });
      if (!healthResponse.ok) return;
      const health = await healthResponse.json();
      if (!health.ready || !applicationInstance || health.instance_id === applicationInstance) return;
      const sessionResponse = await rawFetch('/api/v1/session', { credentials: 'same-origin', cache: 'no-store' });
      if (!sessionResponse.ok) return;
      csrf = (await sessionResponse.json()).csrf_token;
      await nativeHooks.backendRecovered?.();
      applicationInstance = health.instance_id;
    })().catch(() => {}).finally(() => { reconnecting = null; });
    await reconnecting;
  }
  const backendObserver = setInterval(() => { void observeBackend(); }, 1000);
  window.addEventListener('pagehide', () => clearInterval(backendObserver), { once: true });
  async function api(path, value) {
    await ready;
    const response = await rawFetch('/api/v1' + path, {
      credentials: 'same-origin', cache: 'no-store',
      ...(value === undefined ? {} : {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Learning-CSRF': csrf },
        body: JSON.stringify(value),
      }),
    });
    const data = await response.json();
    if (!response.ok) throw Object.assign(new Error(`${data.message || '操作未完成'}（${data.code || response.status}）`), { code: data.code, status: response.status });
    return data;
  }
  // Seed a single business-owned blank before the native first-use navigation
  // starts. Both the restored native selection and this plugin reuse that id.
  const prepareCourse = ready.then(async () => {
    await api('/desktop/default/open', {});
  }).catch(() => {});
  const boot = window.__DSH_BOOT_READY__ ??= Promise.withResolvers();
  const resolveBoot = boot.resolve;
  boot.resolve = () => { void prepareCourse.then(() => resolveBoot()); };
  window.__DSH_TRANSPORT__ = {
    ...window.__DSH_TRANSPORT__,
    async fetch(input, init) {
      await ready;
      const target = new URL(input, document.baseURI);
      if (target.origin !== location.origin) throw new Error('DESKTOP_RPC_ORIGIN_DENIED');
      const headers = new Headers(init.headers);
      headers.set('X-Learning-CSRF', csrf);
      let envelope;
      let request;
      let operation;
      if (typeof init.body === 'string' && /application\/json/.test(headers.get('content-type') || '')) {
        envelope = JSON.parse(init.body);
        operation = String(envelope.method || '').replace('.', '/');
        const payload = envelope.payload?.args ?? envelope.payload;
        request = payload?.request ?? payload;
        if (operation === 'session/create' && request && nativeHooks.current) {
          const current = nativeHooks.current();
          if (current?.navigating && !request.sessionId) throw new Error('课程或对话正在切换（NAVIGATION_PENDING）');
          if (current?.sessionId && (!current.freshSeed || request.sessionId !== current.freshSeed) && (!current.readOnly || current.explicitNew) && (!request.workspaceId || request.workspaceId === current.courseId)) request.currentSessionId = current.sessionId;
        }
        if (operation === 'session/prompt' && request) {
          try { await nativeHooks.beforeSubmit?.(request.sessionId, request.requestId, request); }
          catch (error) { await nativeHooks.submissionFailed?.(request.sessionId, request.requestId); throw error; }
        }
        init = { ...init, body: JSON.stringify(envelope) };
      } else if (target.pathname.endsWith('/session/uploadFileBinary') && init.body instanceof Blob) {
        const bytes = await init.body.arrayBuffer();
        const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(value => value.toString(16).padStart(2, '0')).join('');
        const key = target.searchParams.get('sessionId') + ':' + target.searchParams.get('name') + ':' + hash;
        if (!uploadIds.has(key)) uploadIds.set(key, crypto.randomUUID());
        headers.set('X-Learning-Upload-Id', uploadIds.get(key));
        for (const [name, value] of Object.entries(nativeHooks.uploadHeaders?.(init.body) || {})) headers.set(name, value);
      }
      const reconciledResponse = result => result?.state === 'accepted' ? new Response(JSON.stringify({
        type: 'server-response', rpcId: envelope.rpcId, result: { ok: true, value: { accepted: true } },
      }), { headers: { 'Content-Type': 'application/json' } }) : null;
      let response;
      try { response = await rawFetch(target, { ...init, headers, credentials: 'same-origin' }); }
      catch (error) {
        if (operation === 'session/prompt') {
          const recovered = reconciledResponse(await nativeHooks.submissionUnknown?.(request.sessionId, request.requestId));
          if (recovered) return recovered;
        }
        throw error;
      }
      if (operation === 'session/prompt' && request) {
        let result;
        try { if (!response.ok) throw new Error('SUBMISSION_RESPONSE_UNAVAILABLE'); result = await response.clone().json(); }
        catch (error) {
          const recovered = reconciledResponse(await nativeHooks.submissionUnknown?.(request.sessionId, request.requestId));
          if (recovered) return recovered;
          throw error;
        }
        if (result.result?.ok) await nativeHooks.afterSubmitted?.(request.sessionId, request.requestId).catch(error => nativeHooks.error?.(error));
        else await nativeHooks.submissionFailed?.(request.sessionId, request.requestId);
      }
      return response;
    },
    createWebSocket(url) {
      // Connection establishes its unary generation before starting streams.
      if (!csrf) throw new Error('DESKTOP_SESSION_REQUIRED');
      const target = new URL(url, document.baseURI);
      if (target.host !== location.host) throw new Error('DESKTOP_RPC_ORIGIN_DENIED');
      return new WebSocket(target, ['learning.' + csrf]);
    },
  };
  window.__DSH_FILE_UPLOAD__ = { fetch: window.__DSH_TRANSPORT__.fetch };
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = '/desktop/learning.css';
  document.head.append(link);
  const libraryStyle = document.createElement('link');
  libraryStyle.rel = 'stylesheet';
  libraryStyle.href = '/desktop/course-library.css';
  for (const name of ['library-components.css', 'video-components.css']) {
    const style = document.createElement('link'); style.rel = 'stylesheet'; style.href = '/desktop/' + name;
    document.head.append(style);
  }
  document.head.append(libraryStyle);
  // Pre-register one ordinary client plugin. Shared React and primitives come
  // from the official shell's module table, preserving its component identity.
  window.__ModuleLoader__.load({ id, factory(require) {
    const React = require('react');
    const ReactDOM = require('react-dom');
    const ReactDOMClient = require('react-dom/client');
    const primitives = require('@deepseek-ai/dsh-client-ui-primitives');
    const { Button, Input, Modal, Menu, Switch, Checkbox } = primitives;
    const h = React.createElement;
    const { useState, useEffect, useRef } = React;
    let ctx;
    let selectedCourse;
    let activeSessionId;
    let library;
    let videoComponents;
    let drafts;
    let newChatPending;
    let newChatIntent = 0;
    let freshChatSeed;
    let navigationRevision = 0;
    let sourceJumpRevision = 0;
    let navigationPending = false;
    let attachmentsSessionId;
    let captureRequest;
    let reeditRequest;
    let globalError = '';
    let globalErrorCode;
    let globalErrorSession;
    let draftConflict;
    let unknownSubmissionSid;
    let immersive = false;
    let aiBeforeImmersive = false;
    const workspaceRef = { current: null };
    const videoHandles = new Map();
    let aiCollapsed = false;
    const materials = new Map();
    const listeners = new Set();
    const bindings = new Map();
    const notify = () => { for (const listener of listeners) listener(); };
    function reportError(error) {
      globalError = error.message || String(error);
      globalErrorCode = error.code;
      globalErrorSession = error.sessionId || activeSessionId;
      if (error.code === 'DRAFT_RESTORE_CONFLICT') draftConflict = { ...error, sessionId: error.sessionId || activeSessionId };
      if (error.code === 'SUBMISSION_RESULT_UNKNOWN') unknownSubmissionSid = error.sessionId || activeSessionId;
      notify();
    }
    function useRevision() {
      const [, update] = useState(0);
      useEffect(() => { const changed = () => update(value => value + 1); listeners.add(changed); return () => listeners.delete(changed); }, []);
    }
    async function bindingFor(sessionId) {
      if (!bindings.has(sessionId)) {
        bindings.set(sessionId, await api('/desktop/sessions/' + encodeURIComponent(sessionId)));
      }
      return bindings.get(sessionId);
    }
    function inputFor(sessionId) {
      const scope = ctx.sessions.scope(sessionId);
      if (!scope) throw new Error('DESKTOP_SESSION_NOT_READY');
      return ctx.conversation.input.for(scope);
    }
    const readOnlyHistory = () => !!bindings.get(activeSessionId)?.read_only;
    async function createLearningChat(courseId = selectedCourse, fresh = false) {
      newChatIntent++;
      const seed = fresh ? 'learning-create-' + crypto.randomUUID().replaceAll('-', '') : undefined;
      if (fresh) freshChatSeed = seed;
      try { return await ctx.sessions.create({ workspaceId: courseId, ...(seed ? { sessionId: seed } : {}) }); }
      finally { newChatIntent--; if (freshChatSeed === seed) freshChatSeed = undefined; }
    }
    async function newCourseChat(courseId) {
      if (newChatPending) return newChatPending;
      newChatPending = (async () => {
        if (captureRequest) throw new Error('请完成或取消当前截图（CAPTURE_CONTEXT_LOCKED）');
        if (navigationPending) throw new Error('请等待课程或对话切换完成（NAVIGATION_PENDING）');
        if (activeSessionId && !readOnlyHistory()) await drafts.beforeNavigate(activeSessionId);
        // An explicit course-row action owns a stable creation seed. Even an
        // unsent old draft keeps its own identity and readable workspaces.
        const sid = await createLearningChat(courseId, true);
        await bindingFor(sid);
        ctx.uiWorkspace.openSession(sid);
      })().finally(() => { newChatPending = null; notify(); });
      notify();
      return newChatPending;
    }
    async function chooseCourse(course, chatId, action = 'open') {
      if (captureRequest) throw new Error('请先完成或取消截图（CAPTURE_CONTEXT_LOCKED）');
      if (!chatId) {
        const changed = selectedCourse !== course.course_id;
        selectedCourse = course.course_id;
        if (changed) await library?.setCourse(selectedCourse);
        materialState().active = 'files';
        notify(); return;
      }
      if (activeSessionId && !readOnlyHistory()) await drafts?.beforeNavigate(activeSessionId);
      const opened = await api('/desktop/chats/' + chatId + '/' + action, {});
      bindings.set(opened.session_id, opened);
      activeSessionId = opened.session_id;
      ctx.uiWorkspace.openSession(opened.session_id);
      if (!opened.read_only) await drafts?.attach(opened.session_id, opened.chat_id);
      notify();
    }
    async function resolveWorkspaceInfo(course) {
      // Older running backends expose only id/title in /courses. Their native
      // workspace feed still contains the registration time; its path is an
      // execution carrier, so use the existing read-only properties endpoint
      // for the real course root, without enumerating or preparing materials.
      const native = ctx.workspaces.list.getSnapshot().items.find(item => item.workspaceId === course.course_id);
      const createdAt = course.created_at || native?.createdAt;
      if (!createdAt) throw new Error('工作区信息尚未就绪，请重新悬停（WORKSPACE_INFO_UNAVAILABLE）');
      const root = Object.hasOwn(course, 'root_path') ? course : await api('/courses/' + course.course_id + '/files/properties', { path: '' });
      return { root_path: root.root_path, created_at: createdAt };
    }
    function HistoryTitle({ displayTitle }) {
      const [open, setOpen] = useState(false);
      const [archived, setArchived] = useState(false);
      const [rename, setRename] = useState(null);
      const [name, setName] = useState('');
      const [confirmArchive, setConfirmArchive] = useState(null);
      const [busy, setBusy] = useState(false);
      const [error, setError] = useState('');
      const composing = useRef(false);
      const sessions = React.useSyncExternalStore(ctx.sessions.list.subscribe, ctx.sessions.list.getSnapshot);
      const workspaceStore = React.useMemo(() => ({
        subscribe: listener => ctx.workspaces.list.subscribe(listener),
        getSnapshot: () => ctx.workspaces.list.getSnapshot(),
      }), []);
      const workspaces = React.useSyncExternalStore(workspaceStore.subscribe, workspaceStore.getSnapshot);
      const archivedIds = new Set(workspaces.archivedSessionIds || []);
      const chats = Object.values(sessions.byId).filter(chat => archivedIds.has(chat.id) === archived).reverse();
      const safeTitle = chat => !chat.displayTitle || chat.displayTitle === 'workspace'
        || chat.displayTitle.startsWith('[[learning-generation:') || chat.displayTitle.startsWith('learning-')
        ? (chat.blank ? '新对话' : '当前对话') : chat.displayTitle;
      const show = async () => {
        setOpen(!open);
        if (open) return;
        setArchived(false); setError('');
        try {
          await ctx.sessions.refresh();
        } catch (reason) { if (activeSessionId) inputFor(activeSessionId).notify('error', reason.message); }
      };
      const act = async action => {
        if (busy) return;
        setBusy(true); setError('');
        try { await action(); await ctx.sessions.refresh(); }
        catch (reason) { setError(reason.message || '操作未完成，请核对当前对话'); }
        finally { setBusy(false); }
      };
      const archive = async (chat, stopActivity = false) => {
        try {
          const wasActive = chat.id === activeSessionId;
          if (chat.id === activeSessionId && !readOnlyHistory()) await drafts.beforeNavigate(chat.id);
          await ctx.uiWorkspace.archiveSession(chat.id, { stopActivity });
          setConfirmArchive(null);
          if (wasActive) {
            setOpen(false);
            const sid = await createLearningChat();
            ctx.uiWorkspace.openSession(sid);
          }
        } catch (reason) {
          if (reason.code === 'workspace/session-active' || reason.rpcError?.code === 'workspace/session-active') {
            setConfirmArchive(chat); setOpen(false); return;
          }
          throw reason;
        }
      };
      const saveRename = () => { if (!name.trim() || composing.current) return; void act(async () => {
        const result = await ctx.sessions.using(rename.id, { source: 'workspaceOperation' },
          reference => reference.binding.session.rename(name.trim()));
        if (!result.ok) throw new Error(result.error.message);
        setRename(null);
      }); };
      return h(React.Fragment, null, h(Menu, { open, portal: true, align: 'start', listClassName: 'learning-history-menu', onClose: () => setOpen(false),
        anchor: h(Button, { size: 'sm', variant: 'ghost', className: 'learning-conversation-title', title: displayTitle || '新对话',
          'aria-label': '选择历史对话', 'aria-expanded': open, onClick: show }, h('span', null, displayTitle || '新对话'), h(library.components.Icon, { name: 'chevron-down', size: 14 })),
      },
        h('div', { className: 'learning-history-heading' }, archived ? '已归档对话' : '历史对话'),
        chats.map(chat => h('div', { key: chat.id, className: 'learning-history-row', 'data-current': chat.id === activeSessionId },
          h(Button, { role: 'menuitem', variant: 'ghost', size: 'sm', className: 'learning-history-open', title: safeTitle(chat), disabled: busy,
            'aria-current': chat.id === activeSessionId ? 'true' : undefined,
            onClick: () => { if (archived) return; setOpen(false); void (async () => {
          if (captureRequest) throw new Error('请先完成或取消截图（CAPTURE_CONTEXT_LOCKED）');
          if (activeSessionId && !readOnlyHistory()) await drafts.beforeNavigate(activeSessionId);
          ctx.uiWorkspace.openSession(chat.id);
        })().catch(reportError); } }, safeTitle(chat)),
          h('div', { className: 'learning-history-actions' },
            h(Button, { role: 'menuitem', variant: 'ghost', size: 'sm', title: '重命名', 'aria-label': '重命名 ' + safeTitle(chat), disabled: busy,
              onClick: () => { setRename(chat); setName(safeTitle(chat)); setError(''); setOpen(false); } }, h(library.components.Icon, { name: 'rename', size: 15 })),
            h(Button, { role: 'menuitem', variant: 'ghost', size: 'sm', title: archived ? '取消归档' : '归档',
              'aria-label': (archived ? '取消归档 ' : '归档 ') + safeTitle(chat), disabled: busy,
              onClick: () => { void act(() => archived ? ctx.uiWorkspace.unarchiveSession(chat.id) : archive(chat)); } },
              h(library.components.Icon, { name: 'archive', size: 15 }))))),
        !chats.length && h('div', { className: 'learning-history-empty' }, archived ? '没有已归档对话' : '暂无历史对话'),
        error && h('div', { role: 'alert', className: 'learning-history-error' }, error),
        h(Button, { role: 'menuitem', variant: 'ghost', size: 'sm', className: 'learning-history-archive-toggle',
          onClick: () => { setArchived(!archived); setError(''); } }, archived ? '返回历史对话' : '已归档对话')),
        h(Modal, { open: !!rename, title: '重命名对话', closeLabel: '取消', onClose: () => { if(!busy)setRename(null); },
          footer: h(React.Fragment, null,
            h(Button, { variant: 'outline', disabled: busy, onClick: () => setRename(null) }, '取消'),
            h(Button, { disabled: busy || !name.trim(), onClick: saveRename }, '重命名')) },
          h(Input, { value: name, 'aria-label': '对话名称', 'data-modal-autofocus': true, disabled: busy,
            onChange: event => setName(event.target.value), onCompositionStart: () => { composing.current = true; },
            onCompositionEnd: () => { composing.current = false; }, onKeyDown: event => {
              if(event.key === 'Enter' && !event.nativeEvent.isComposing && !composing.current){event.preventDefault();saveRename();}
            } }), error && h('p', { role: 'alert' }, error)),
        h(Modal, { open: !!confirmArchive, title: '停止回答并归档？', closeLabel: '取消', onClose: () => { if(!busy)setConfirmArchive(null); },
          footer: h(React.Fragment, null,
            h(Button, { variant: 'outline', disabled: busy, onClick: () => setConfirmArchive(null) }, '取消'),
            h(Button, { disabled: busy, onClick: () => { void act(() => archive(confirmArchive, true)); } }, '停止并归档')) },
          h('p', null, '此对话仍有运行中的回答。停止并归档后，对话和附件继续保留。'), error && h('p', { role: 'alert' }, error)));
    }
    function WorkspaceMenu() {
      useRevision();
      const [open, setOpen] = useState(false);
      const [adding, setAdding] = useState(false);
      const [courses, setCourses] = useState([]);
      const ids = bindings.get(activeSessionId)?.workspace_ids || [];
      const choose = async identity => {
        if (identity === '__add') { setAdding(true); return; }
        const sid = activeSessionId;
        const next = ids.includes(identity) ? ids.filter(id => id !== identity) : [...ids, identity];
        const result = await api('/desktop/sessions/' + sid + '/workspaces', { course_ids: next });
        bindings.set(sid, result); setOpen(false); setAdding(false); notify();
      };
      const items = courses.filter(course => adding ? !ids.includes(course.course_id) : ids.includes(course.course_id))
        .map(course => ({ id: course.course_id, label: course.title }));
      if (!adding) items.push({ id: '__add', label: '＋ 添加工作区…' });
      const title = ids.map(id => courses.find(item => item.course_id === id)?.title || '课程').join('、') || '工作区';
      useEffect(() => { void api('/courses').then(setCourses).catch(reportError); }, [open]);
      return h(Menu, { open, portal: true, align: 'end', items, selectedIds: adding ? [] : ids, onClose: () => { setOpen(false); setAdding(false); },
        anchor: h(Button, { size: 'sm', variant: 'ghost', className: 'learning-icon-action', title, 'aria-label': '对话工作区', disabled: !activeSessionId || readOnlyHistory(), onClick: () => setOpen(true), icon: h(primitives.IconFolderCloseRegular, { size: 16 }) }),
        onSelect: identity => { void choose(identity).catch(reportError); } });
    }
    function VideoButton() {
      useRevision();
      return h(Button, { variant: 'ghost', size: 'sm', className: 'learning-new-chat learning-icon-action', title: '新对话', 'aria-label': '新对话',
        icon: h(library.components.Icon, { name: 'rename', size: 16 }), disabled: !selectedCourse || !!newChatPending || navigationPending, onClick: () => {
        if (newChatPending || !selectedCourse) return;
        newChatPending = (async () => {
          if (captureRequest) throw new Error('请完成或取消当前截图（CAPTURE_CONTEXT_LOCKED）');
          if (activeSessionId) {
            const current = await bindingFor(activeSessionId);
            const chat = await api('/chats/' + current.chat_id);
            if (current.course_id === selectedCourse && !chat.messages?.length && !chat.generations?.length) { inputFor(activeSessionId).focus(); return; }
            if (!readOnlyHistory()) await drafts.beforeNavigate(activeSessionId);
          }
          const sid = await createLearningChat();
          ctx.uiWorkspace.openSession(sid);
        })().catch(reportError).finally(() => { newChatPending = null; notify(); });
        notify();
      } });
    }
    function AiHeader() {
      useRevision();
      const sessions = React.useSyncExternalStore(ctx.sessions.list.subscribe, ctx.sessions.list.getSnapshot);
      const session = sessions.byId[activeSessionId];
      const rawTitle = session?.displayTitle;
      const title = !session || session.blank ? '新对话' : !rawTitle || rawTitle === selectedCourse
        || rawTitle === activeSessionId || rawTitle.startsWith('[[learning-generation:') ? '当前对话' : rawTitle;
      return h(React.Fragment, null,
        h('header', { className: 'learning-ai-header', 'data-testid': 'learning-ai-header' },
          h('div', { className: 'learning-ai-title' }, h(HistoryTitle, { displayTitle: title })), h('div', { className: 'learning-ai-actions', role: 'toolbar', 'aria-label': '对话操作' },
            h(WorkspaceMenu), h(VideoButton), h(CollapseAiButton))),
        readOnlyHistory() && h('div', { className: 'learning-import-history-notice', role: 'status' },
          bindings.get(activeSessionId)?.has_unresolved_history ? '导入历史只读；旧请求结果仍待核对，不会重新发送。' : '导入历史只读；继续提问将使用新的执行身份。',
          h(Button, { size: 'sm', onClick: () => { const mapped = bindings.get(activeSessionId);
            if (mapped.has_unresolved_history) void createLearningChat().then(sid => ctx.uiWorkspace.openSession(sid)).catch(reportError);
            else void chooseCourse({ course_id: selectedCourse }, mapped.chat_id, 'continue').catch(reportError);
          } }, bindings.get(activeSessionId)?.has_unresolved_history ? '新建对话继续' : '继续提问')),
        !readOnlyHistory() && bindings.get(activeSessionId)?.history_available && h(Button, { size: 'sm', onClick: () => {
          void chooseCourse({ course_id: selectedCourse }, bindings.get(activeSessionId).chat_id, 'history').catch(reportError);
        } }, '查看导入历史'));
    }
    function AttachmentsButton() {
      useRevision();
      return h(Button, { size: 'sm', variant: 'ghost', className: 'learning-icon-action', 'aria-label': '本对话附件', title: '本对话附件',
        icon: h(library.components.Icon, { name: 'file', size: 17 }), disabled: !activeSessionId || navigationPending,
        onClick: () => { ++sourceJumpRevision; attachmentsSessionId = activeSessionId; notify(); } });
    }
    function DataSettings() {
      return h('div', { className: 'learning-data-settings learning-legacy-module' },
        h(library.components.DataPanel, { CheckboxControl: Checkbox, beforeSave: async () => {
          if (activeSessionId && !readOnlyHistory()) await drafts.beforeNavigate(activeSessionId);
          for (const handle of videoHandles.values()) await handle.current?.savePosition?.();
        } }));
    }
    function ProjectWelcomeStep({ complete }) {
      const finished = useRef(false);
      useEffect(() => {
        if (!finished.current) { finished.current = true; complete(); }
      }, [complete]);
      return null;
    }
    function CollapseAiButton() {
      return h(Button, { variant: 'ghost', size: 'sm', className: 'learning-icon-action', 'aria-label': '收起 AI', title: '收起 AI · Ctrl+Shift+A',
        icon: h(library.components.Icon, { name: 'panel', size: 16 }), onClick: toggleAi });
    }
    function toggleAi() {
      aiCollapsed = !aiCollapsed; notify();
      if (!aiCollapsed && activeSessionId) inputFor(activeSessionId).focus();
      else if (immersive) void videoHandles.get(materialState().active)?.current?.getElement()?.play().catch(() => {});
    }
    function materialState() {
      if (!materials.has(selectedCourse)) materials.set(selectedCourse, { tabs: [], active: 'files' });
      return materials.get(selectedCourse);
    }
    const materialKey = value => value.asset_id + ':' + value.source_version;
    function openMaterial(asset, seekRequest) {
      if (navigationPending || captureRequest) return;
      ++sourceJumpRevision;
      const state = materialState();
      state.contextMedia = asset; state.contextFile = null;
      const key = materialKey(asset);
      if (!state.tabs.some(value => value.id === key)) state.tabs.push({ id: key, kind: 'video', video: asset, title: asset.display_name });
      else Object.assign(state.tabs.find(value => value.id === key), { video: asset, title: asset.display_name });
      if (seekRequest) state.tabs.find(value => value.id === key).seekRequest = seekRequest;
      state.active = key;
      notify();
    }
    function openFile(file, sourceJump) {
      if (navigationPending || captureRequest) return;
      ++sourceJumpRevision;
      const state = materialState();
      state.contextFile = file; state.contextMedia = null;
      const key = materialKey(file);
      if (!state.tabs.some(value => value.id === key)) state.tabs.push({ id: key, kind: file.generated_artifact?.kind === 'book' ? 'book' : 'file', file, title: file.entry.name });
      if (sourceJump) state.tabs.find(value => value.id === key).sourceJump = sourceJump;
      state.active = key;
      notify();
    }
    function setImmersive(value) {
      if (value === immersive) return;
      if (value) { aiBeforeImmersive = aiCollapsed; aiCollapsed = true; }
      else aiCollapsed = aiBeforeImmersive;
      immersive = value; notify();
    }
    async function beginWorkspaceCapture() {
      if (captureRequest || navigationPending || !activeSessionId || readOnlyHistory()) return;
      try {
        const sessionId = activeSessionId;
        const binding = await bindingFor(sessionId);
        if (sessionId !== activeSessionId) return;
        const state = materialState();
        videoHandles.get(state.active)?.current?.pauseForCapture();
        void videoComponents.interactionPriority(true).catch(() => {});
        captureRequest = { sessionId, binding, workspace: true, sourceCourseId: state.contextMedia?.source_course_id || selectedCourse };
        notify();
      } catch (error) { reportError(error); }
    }
    async function openAttachmentSource(source, courseId, sessionId) {
      if (navigationPending || captureRequest || selectedCourse !== courseId || activeSessionId !== sessionId)
        throw new Error('对话已改变，请重新打开附件（ATTACHMENT_CONTEXT_CHANGED）');
      const navigation = navigationRevision;
      const jump = ++sourceJumpRevision;
      const assets = await api('/courses/' + courseId + '/videos');
      if (jump !== sourceJumpRevision || navigation !== navigationRevision) return;
      if (!assets.some(value => value.asset_id === source.asset_id && value.source_version === source.source_version))
        throw new Error('原视频不在当前课程或版本已改变，已保留提交的图片（SOURCE_UNAVAILABLE）');
      const asset = await api('/videos/' + source.asset_id + '/open', { source_version: source.source_version });
      if (jump !== sourceJumpRevision || navigation !== navigationRevision || selectedCourse !== courseId || activeSessionId !== sessionId) return;
      if (asset.source_version !== source.source_version)
        throw new Error('原视频来源不可用，已保留提交的图片（SOURCE_UNAVAILABLE）');
      attachmentsSessionId = null;
      openMaterial(asset, { time_ms: source.time_ms });
    }
    function ChatAttachments({ sessionId }) {
      const [rows, setRows] = useState(null);
      const [error, setError] = useState('');
      useEffect(() => {
        let live = true;
        void (async () => {
          const binding = await bindingFor(sessionId);
          const chat = await api('/chats/' + binding.chat_id);
          const ids = [...new Set(chat.messages.flatMap(message => message.attachment_ids || []))];
          const entries = await Promise.all(ids.map(async id => {
            try {
              const metadata = await api('/attachments/' + id + '/metadata');
              let source;
              if (metadata.kind === 'image') {
                try { source = await api('/attachments/' + id + '/source'); }
                catch (reason) { if (reason.status !== 404) throw reason; }
              }
              return { id, metadata, source, courseId: binding.course_id };
            } catch (reason) { return { id, error: reason.message }; }
          }));
          if (live) setRows(entries);
        })().catch(reason => { if (live) setError(reason.message); });
        return () => { live = false; };
      }, [sessionId]);
      return h('div', { className: 'learning-history-attachments', 'data-testid': 'learning-history-attachments' },
        error ? h('p', { role: 'alert' }, error) : rows === null ? h('p', { role: 'status' }, '正在读取附件…')
          : !rows.length ? h('p', null, '此对话尚未提交附件')
            : rows.map((row, index) => {
              if (row.error) return h('p', { key: row.id, role: 'alert' }, row.error);
              const { metadata, source } = row;
              const filename = metadata.filename || `图片 ${index + 1}.png`;
              return h('section', { key: row.id, 'data-attachment-id': row.id },
                metadata.kind === 'image' && h('img', { src: metadata.download_url, alt: `已确认提交的图片 ${index + 1}`, loading: 'lazy' }),
                h('span', null, filename),
                h('a', { href: metadata.download_url, download: filename, 'aria-label': '下载附件原件 ' + filename }, '下载原件'),
                source?.origin === 'video' && source.asset_id && source.source_version && source.time_ms != null
                  && h(Button, { size: 'sm', 'aria-label': '回到视频来源', onClick: () => {
                    void openAttachmentSource(source, row.courseId, sessionId).catch(reportError);
                  } }, `来源 ${Math.floor(source.time_ms / 60000)}:${String(Math.floor(source.time_ms / 1000) % 60).padStart(2, '0')}`));
            }));
    }
    function AppOverlays() {
      useRevision();
      const capturing = !!captureRequest;
      useEffect(() => {
        if (!capturing) return;
        const renew = () => { void videoComponents.interactionPriority(true, 'desktop-capture').catch(() => {}); };
        renew();
        const timer = setInterval(renew, 2000);
        return () => { clearInterval(timer); void videoComponents.interactionPriority(false, 'desktop-capture').catch(() => {}); };
      }, [capturing]);
      return h(React.Fragment, null,
        navigationPending && h('div', { className: 'learning-navigation-wait', role: 'status' }, '正在切换…'),
        (globalError || unknownSubmissionSid) && h('div', { className: 'learning-global-error', role: 'alert' }, globalError || '提交结果尚未确认，请先核对。',
          unknownSubmissionSid && h(Button, { size: 'sm', onClick: () => { void drafts.reconcileSubmission(unknownSubmissionSid).then(result => {
            if (result.state !== 'unknown') { unknownSubmissionSid = null; globalError = ''; notify(); void ctx.sessions.refresh(); }
          }).catch(reportError); } }, '核对提交'),
          !unknownSubmissionSid && !draftConflict && ['DRAFT_NOT_READY', 'DRAFT_SESSION_NOT_READY', 'DRAFT_SAVE_FAILED'].includes(globalErrorCode)
            && h(Button, { size: 'sm', onClick: () => { void drafts.recover().catch(reportError); } }, '接回草稿'),
          h(Button, { size: 'sm', onClick: () => { globalError = ''; notify(); } }, '关闭')),
        h(Modal, { open: !!attachmentsSessionId, title: '本对话附件', closeLabel: '关闭附件', onClose: () => { ++sourceJumpRevision; attachmentsSessionId = null; notify(); } },
          attachmentsSessionId && h(ChatAttachments, { key: attachmentsSessionId, sessionId: attachmentsSessionId })),
        h(Modal, { open: !!draftConflict, title: '选择要继续的草稿', closeLabel: '暂不切换',
          onClose: () => { draftConflict = null; notify(); }, footer: h(React.Fragment, null,
            h(Button, { onClick: () => { const sid = draftConflict.sessionId; draftConflict = null; void drafts.resolveConflict(sid, 'current').catch(reportError); notify(); } }, '保留当前输入'),
            h(Button, { onClick: () => { const sid = draftConflict.sessionId; draftConflict = null; void drafts.resolveConflict(sid, 'saved').catch(reportError); notify(); } }, '使用已保存草稿')),
        }, h('p', null, '恢复期间出现了新的输入，两份内容均已保留。请选择继续哪一份。'),
          draftConflict && h('div', { className: 'learning-draft-choices' },
            h('section', null, h('strong', null, '当前输入'), h('pre', null, inputFor(draftConflict.sessionId).state.getSnapshot().draft || '')),
            h('section', null, h('strong', null, '已保存草稿'), h('pre', null, draftConflict.savedDraft?.text || '')))),
        captureRequest?.workspace && h(library.components.WorkspaceCapture, {
          root: workspaceRef.current,
          onConfirm: async result => {
            const captured = captureRequest;
            await publishCapture(result, captured.binding, captured.sessionId);
            captureRequest = null; notify(); void videoComponents.interactionPriority(false).catch(() => {});
          },
          onCancel: () => { captureRequest = null; notify(); void videoComponents.interactionPriority(false).catch(() => {}); },
        }),
        reeditRequest && h(library.components.CaptureEditor, { source: reeditRequest.source,
          onConfirm: async result => { const target = reeditRequest; await publishCapture(result, target.binding, target.sessionId, target.replaces); reeditRequest = null; captureRequest = null; notify(); },
          onCancel: () => { reeditRequest = null; captureRequest = null; notify(); } }),
      );
    }
    function DraftImageAction({ id, index, sessionId }) {
      const [metadata, setMetadata] = useState(null);
      useEffect(() => { let live = true; void api('/attachments/' + id + '/metadata').then(value => { if (live) setMetadata(value); }).catch(reportError); return () => { live = false; }; }, [id]);
      if (metadata?.kind !== 'image') return null;
      return h(Button, { size: 'sm', onClick: () => { void (async () => {
        if (captureRequest || navigationPending) return;
        await drafts.beforeNavigate(sessionId);
        const binding = await bindingFor(sessionId);
        const controller = library.components.getDraftController(binding.chat_id);
        const position = controller.snapshot().value.attachment_ids.indexOf(id);
        const nativeId = inputFor(sessionId).state.getSnapshot().attachmentIds[position];
        if (!nativeId) throw new Error('原附件已改变，请重新选择（DRAFT_ATTACHMENT_UNAVAILABLE）');
        const stored = await library.components.loadCapture(id);
        let source = stored ? { ...stored.source, initialCrop: stored.crop } : null;
        if (!source) {
          const response = await rawFetch(metadata.download_url, { credentials: 'same-origin' });
          if (!response.ok) throw new Error('原附件无法读取（ATTACHMENT_MISSING）');
          source = await library.components.readImageFile(new File([await response.blob()], '已确认图片.png', { type: metadata.media_type }));
          source.origin = 'image';
        }
        reeditRequest = { sessionId, binding, source, replaces: { id, nativeId } };
        captureRequest = reeditRequest; notify();
      })().catch(reportError); } }, '重新裁剪图片 ' + (index + 1));
    }
    function ReferenceCards({ controller, sessionId }) {
      const view = React.useSyncExternalStore(controller.subscribe, controller.snapshot);
      const selections = view.value.source_snapshot_ids || [];
      const readCourses = view.value.read_course_ids || [];
      if (!view.value.references.length && !view.value.attachment_ids.length && !selections.length && !readCourses.length) return null;
      return h('div', { className: 'learning-native-references learning-legacy-module', 'aria-label': '本次引用' },
        readCourses.length > 0 && h(ReadCourseSummary, { ids: readCourses, controller }),
        view.value.attachment_ids.map((id, index) => h(DraftImageAction, { key: id, id, index, sessionId })),
        selections.map(id => h(SourceSelectionCard, { key: id, id, sessionId, controller })),
        view.value.references.map((reference, index) => h('section', { key: reference.reference_id },
          h('small', null, '引用回答'), h(library.components.MarkdownAnswer, { text: reference.markdown }),
          h(Button, { size: 'sm', 'aria-label': '移除引用 ' + (index + 1), onClick: () => controller.update({ references: view.value.references.filter(value => value.reference_id !== reference.reference_id) }) }, '移除'))));
    }
    function ReadCourseSummary({ ids, controller }) {
      const [courses, setCourses] = useState([]);
      useEffect(() => { let live = true; void api('/courses').then(value => { if (live) setCourses(value); }).catch(() => {}); return () => { live = false; }; }, []);
      return h('section', null, h('small', null, '资料读取范围：' + ids.map(id => courses.find(course => course.course_id === id)?.title || '已指定课程').join('、')),
        h(Button, { size: 'sm', onClick: () => controller.update({ read_course_ids: [] }) }, '恢复仅当前课程'));
    }
    function SourceSelectionCard({ id, sessionId, controller }) {
      const [source, setSource] = useState(null);
      const [error, setError] = useState('');
      useEffect(() => {
        let live = true;
        void bindingFor(sessionId).then(binding => api('/courses/' + binding.course_id + '/learning/selections/' + id))
          .then(value => { if (live) setSource(value); }).catch(reason => { if (live) setError(reason.message); });
        return () => { live = false; };
      }, [id, sessionId]);
      const pages = source?.reference.parts?.map(part => part.page_label || part.page_index + 1).join('、');
      const caption = source?.reference.kind === 'subtitle';
      const captionTime = caption ? Math.floor(source.reference.start_ms / 60000) + ':' + String(Math.floor(source.reference.start_ms / 1000) % 60).padStart(2, '0') : '';
      return h('section', { 'data-source-snapshot': id },
        h('small', null, caption ? (source.content_role === 'user_note' ? '手改字幕引用 · ' : '字幕引用 · ') + captionTime : pages ? '资料引用 · 第 ' + pages + ' 页' : '资料引用'),
        error ? h('p', { role: 'alert' }, error) : h(library.components.MarkdownAnswer, { text: source?.text || '正在读取选区…' }),
        (source?.reference.kind === 'document_selection' || caption) && h(Button, { size: 'sm', onClick: () => { void (async () => {
          if (navigationPending || captureRequest || activeSessionId !== sessionId) throw new Error('对话正在切换，请稍后回看（NAVIGATION_PENDING）');
          const revision = ++sourceJumpRevision;
          const binding = await bindingFor(sessionId);
          const file = await api('/courses/' + binding.course_id + '/learning/selections/' + id + '/open', {});
          if (revision !== sourceJumpRevision || activeSessionId !== sessionId) return;
          if (caption && file.video) openMaterial({ ...file.video, source_course_id: source.course_id }, { time_ms: source.reference.start_ms });
          else openFile({ ...file, learning_source_owner: binding.course_id, learning_source_snapshot: id }, { request_id: crypto.randomUUID(), reference: source.reference });
        })().catch(reason => setError(reason.message)); } }, '回看原文'),
        h(Button, { size: 'sm', 'aria-label': '移除资料引用', onClick: () => controller.update({ source_snapshot_ids: (controller.snapshot().value.source_snapshot_ids || []).filter(value => value !== id) }) }, '移除'));
    }
    async function sourceAction(action) {
      if (action.intent !== 'quote') throw new Error('此专门流程已取消（LEARNING_FEATURE_REMOVED）');
      if (navigationPending || !activeSessionId) throw new Error('请等待课程切换完成（NAVIGATION_PENDING）');
      const sessionId = activeSessionId;
      const binding = await bindingFor(sessionId);
      if (binding.course_id !== action.file.course_id) {
        const visible = materialState().tabs.some(tab => tab.file?.course_id === action.file.course_id
          && tab.file?.asset_id === action.file.asset_id && tab.file?.source_version === action.file.source_version);
        if (!visible) throw new Error('选区不属于原对话或已核定跨课程来源（SOURCE_COURSE_DENIED）');
      }
      const source = await api('/courses/' + action.file.course_id + '/learning/selections/pdf', action.selection);
      const controller = library.components.getDraftController(binding.chat_id);
      if (!controller.snapshot().ready) throw new Error('原对话草稿尚未就绪，请保留选区（DRAFT_NOT_READY）');
      const identities = controller.snapshot().value.source_snapshot_ids || [];
      if (identities.length >= 100 && !identities.includes(source.snapshot_id)) throw new Error('当前引用已达上限（SOURCE_SELECTION_LIMIT）');
      controller.update({ source_snapshot_ids: [...new Set([...identities, source.snapshot_id])] });
      await controller.flush();
      if (activeSessionId === sessionId) { aiCollapsed = false; notify(); }
      return source;
    }
    async function subtitleAction(action) {
      if (navigationPending || !activeSessionId) throw new Error('请等待课程切换完成（NAVIGATION_PENDING）');
      const sessionId = activeSessionId;
      const binding = await bindingFor(sessionId);
      if (binding.course_id !== action.course_id) throw new Error('字幕不属于当前课程（SOURCE_COURSE_DENIED）');
      const source = await api('/courses/' + binding.course_id + '/learning/transcripts/' + action.track_id + '/selections', action.capture);
      const controller = library.components.getDraftController(binding.chat_id);
      if (!controller.snapshot().ready) throw new Error('原对话草稿尚未就绪，字幕仍已保存（DRAFT_NOT_READY）');
      const identities = controller.snapshot().value.source_snapshot_ids || [];
      if (identities.length >= 100 && !identities.includes(source.snapshot_id)) throw new Error('当前引用已达上限（SOURCE_SELECTION_LIMIT）');
      controller.update({ source_snapshot_ids: [...new Set([...identities, source.snapshot_id])] });
      await controller.flush();
      if (activeSessionId === sessionId) { aiCollapsed = false; notify(); inputFor(sessionId).focus(); }
      return source;
    }
    function ReferenceDock({ sessionId }) {
      const [controller, setController] = useState(null);
      useEffect(() => { let live = true; bindingFor(sessionId).then(value => { if (live) setController(library.components.getDraftController(value.chat_id)); }).catch(reportError); return () => { live = false; }; }, [sessionId]);
      return controller ? h(ReferenceCards, { controller, sessionId }) : null;
    }
    async function addMaterialQuote(quote) {
      try {
        if (navigationPending) throw new Error('请等待课程切换完成（NAVIGATION_PENDING）');
        const sid = activeSessionId;
        const input = inputFor(sid);
        const before = input.state.getSnapshot().draft || '';
        const binding = await bindingFor(sid);
        if (!library.components.getDraftController(binding.chat_id).snapshot().ready) throw new Error('原对话草稿尚未就绪，请保留选区（DRAFT_NOT_READY）');
        input.setDraft([before, `引用《${quote.title}》：\n${quote.markdown || quote.text}`].filter(Boolean).join('\n\n'));
        await library.components.getDraftController(binding.chat_id).flush();
        aiCollapsed = false; notify();
      } catch (error) { reportError(error); throw error; }
    }
    async function publishCapture(result, binding, sessionId, replaces) {
      const input = inputFor(sessionId);
      if (replaces && !input.state.getSnapshot().attachmentIds.includes(replaces.nativeId)) throw new Error('原附件已改变，请重新选择（DRAFT_ATTACHMENT_UNAVAILABLE）');
      // Decode confirmed image bytes locally: fetching a data URL is blocked by
      // the native client's same-origin CSP and must not require a policy change.
      const bytes = Uint8Array.from(atob(result.dataUrl.split(',')[1]), value => value.charCodeAt(0));
      const blob = new Blob([bytes], { type: 'image/png' });
      const attachment = await api('/attachments', { confirmed: true, media_type: 'image/png', data_base64: result.dataUrl.split(',')[1] });
      if (result.source.source) result.source.source = { ...result.source.source,
        source_course_id: result.source.source.source_course_id || captureRequest?.sourceCourseId || binding.course_id };
      await api('/attachments/' + attachment.attachment_id + '/source', {
        chat_id: binding.chat_id, origin: result.source.origin || (result.source.source ? 'video' : 'workspace'),
        ...result.source.source, source_width: result.source.width, source_height: result.source.height,
        crop: result.crop, edit_root_id: result.source.sourceId,
        parent_attachment_id: replaces?.id,
      });
      await library.components.saveCapture(attachment.attachment_id, result);
      const prepared = ctx.conversation.createDrafts(sessionId, [new File([blob], 'learning-capture_' + attachment.attachment_id + '.png', { type: 'image/png' })]);
      if (replaces && !input.removeAttachment(replaces.nativeId)) throw new Error('输入框正在提交，请稍后重试（DESKTOP_COMPOSER_BUSY）');
      if (!input.addAttachments(prepared.map(value => value.id))) {
        if (replaces) input.addAttachments([replaces.nativeId]);
        ctx.conversation.releaseDraftAttachments(prepared);
        throw new Error('输入框正在提交，请稍后重试（DESKTOP_COMPOSER_BUSY）');
      }
      aiCollapsed = false; notify(); input.focus();
    }
    async function openWorkbenchSource(source, sourceOwner) {
      if (navigationPending || captureRequest || !activeSessionId) throw Error('NAVIGATION_PENDING');
      const sid = activeSessionId, revision = ++sourceJumpRevision;
      const binding = await bindingFor(sid);
      const owner = sourceOwner || binding.course_id;
      const allowed = await api('/courses/' + owner + '/learning/selections/' + source.snapshot_id);
      if (allowed.course_id !== source.course_id || allowed.content_hash !== source.content_hash) throw Error('SOURCE_COURSE_DENIED');
      let located = source, offset = 0;
      for (let depth = 0; located.reference.kind === 'snapshot_slice'; depth++) {
        if (depth >= 16) throw Error('SOURCE_REFERENCE_DEPTH');
        offset += located.reference.start;
        located = await api('/courses/' + located.course_id + '/learning/selections/' + located.reference.parent_snapshot_id);
      }
      const file = await api('/courses/' + owner + '/learning/selections/' + source.snapshot_id + '/open', {});
      if (revision !== sourceJumpRevision || sid !== activeSessionId) return;
      if (located.reference.kind === 'subtitle' && file.video) openMaterial({ ...file.video, source_course_id: located.course_id }, { time_ms: located.reference.start_ms });
      else {
        let reference = located.reference;
        if (source.reference.kind === 'snapshot_slice' && reference.kind === 'document') reference = { ...reference,
          start: reference.start + offset, end: reference.start + offset + [...source.text].length, regions: [] };
        if (reference.kind === 'document_image' || reference.kind === 'document_region_image') reference = { kind: 'document', asset_id: reference.asset_id,
          source_version: reference.source_version, source_digest: reference.source_digest,
          page_index: reference.page_index, page_label: null, parser_version: reference.parser_version, start: 0, end: 0, regions: reference.region ? [reference.region] : [] };
        openFile({ ...file, learning_source_owner: owner, learning_source_snapshot: source.snapshot_id }, { request_id: crypto.randomUUID(), reference });
      }
    }
    function GeneratedDocument({ taskId, sid, title }) {
      const [value, setValue] = useState(null);
      const [error, setError] = useState('');
      useEffect(() => {
        let live = true, timer;
        const read = async () => {
          try {
            const result = await api('/desktop/sessions/' + sid + '/documents/' + taskId);
            if (!live) return;
            setValue(result);
            if (result.file && result.open_requested && ['completed', 'partial'].includes(result.task.state)
                && sid === activeSessionId && !captureRequest && !navigationPending) {
              const key = 'learning.opened-result.' + taskId;
              if (!localStorage.getItem(key)) { localStorage.setItem(key, '1'); openFile(result.file); }
            }
            if (!['completed', 'partial', 'failed', 'cancelled'].includes(result.task.state)) timer = setTimeout(read, 1500);
          } catch (reason) { if (live) setError(reason.message); }
        };
        void read();
        return () => { live = false; clearTimeout(timer); };
      }, [taskId, sid]);
      const controls = async action => {
        try {
          const binding = await bindingFor(sid);
          await api('/courses/' + binding.course_id + '/learning/tasks/' + taskId + '/control/' + action,
            { operation_id: crypto.randomUUID(), expected_revision: value.task.revision });
        } catch (reason) { setError(reason.message); }
      };
      return h('span', { className: 'learning-document-result' },
        value?.file ? h(Button, { size: 'sm', variant: 'ghost', onClick: () => openFile(value.file) }, title || value.file.entry.name)
          : h('span', { role: 'status' }, (title || '文档') + ' · ' + ({ queued: '等待生成', preparing: '正在处理', running: '正在生成', paused: '已暂停', failed: '未完成', completed: '已完成', partial: '部分完成', unknown: '结果待核对' }[value?.task.state] || '正在读取')),
        value && ['queued', 'preparing', 'running'].includes(value.task.state) && h(Button, { size: 'sm', variant: 'ghost', onClick: () => { void controls('pause'); } }, '暂停'),
        value?.task.state === 'paused' && h(Button, { size: 'sm', variant: 'ghost', onClick: () => { void controls('resume'); } }, '继续'),
        value?.outline?.proposal && h('span', null,
          h('ol', null, ...value.outline.proposal.chapters.map(chapter => h('li', { key: chapter.chapter_id }, chapter.title))),
          h(Button, { size: 'sm', onClick: () => { void (async () => {
            const binding = await bindingFor(sid);
            await api('/courses/' + binding.course_id + '/learning/tasks/' + taskId + '/book-outline', {
              operation_id: crypto.randomUUID(), expected_revision: value.outline.task_revision,
              proposal_hash: value.outline.proposal_hash, outline: value.outline.proposal });
          })().catch(reason => setError(reason.message)); } }, '确认目录并生成')),
        error && h('span', { role: 'alert' }, error));
    }
    function SubtitleProgress({ sid, turn }) {
      const [items, setItems] = useState([]);
      useEffect(() => {
        let live = true, timer;
        const read = async () => {
          try {
            const values = await api('/desktop/sessions/' + sid + '/answer-subtitles?native_turn=' + turn);
            if (!live) return;
            setItems(values);
            if (values.some(item => ['queued', 'running'].includes(item.job.state))) timer = setTimeout(read, 1500);
          } catch (error) { if (live) reportError(error); }
        };
        void read(); return () => { live = false; clearTimeout(timer); };
      }, [sid, turn]);
      return items.map(({ job, video }) => h('span', { key: job.job_id, role: 'status' },
        '字幕 · ' + ({ queued: '等待处理', running: '正在转写', completed: '已完成', failed: '未完成', paused: '已暂停' }[job.state] || job.state),
        h(Button, { size: 'sm', variant: 'ghost', onClick: () => { materialState().subtitleAsset = video.asset_id; openMaterial({ ...video, source_course_id: job.course_id }); } }, '查看字幕')));
    }
    function RequestedTaskActions({ turn: location }) {
      useRevision();
      const sid = activeSessionId;
      const [links, setLinks] = useState([]), [sources, setSources] = useState([]), [turn, setTurn] = useState(null);
      useEffect(() => {
        let live = true;
        setLinks([]); setSources([]); setTurn(null);
        if (!sid) return;
        const snapshot = ctx.uiConversation.binding(sid).target('chat').getSnapshot();
        const tail = [...(snapshot?.nodes?.values() || [])].find(node => node.kind === 'turn-tail'
          && node.data?.turn === location.turn);
        if (!tail || !Number.isSafeInteger(tail.data.turn) || tail.data.turn < 1) return;
        setTurn(tail.data.turn);
        void Promise.all([
          api('/desktop/sessions/' + sid + '/learning-task-links?native_turn=' + tail.data.turn),
          api('/desktop/sessions/' + sid + '/answer-sources?native_turn=' + tail.data.turn),
        ]).then(([tasks, evidence]) => { if (live) { setLinks(tasks); setSources(evidence); } }).catch(reportError);
        return () => { live = false; };
      }, [sid, location.turn]);
      return h('div', { className: 'learning-message-results' },
        turn && h(SubtitleProgress, { sid, turn }),
        ...links.map(task => h(GeneratedDocument, { key: task.task_id, taskId: task.task_id, sid, title: task.title })),
        ...sources.map((item, index) => h(Button, { key: item.source.snapshot_id, size: 'sm', variant: 'ghost',
          onClick: () => { void openWorkbenchSource(item.source).catch(reportError); } }, '[' + (index + 1) + '] ' + item.label)));
    }
    function MaterialsRoot() {
      const courseTitle = library.useCourseTitle();
      useRevision();
      const state = materialState();
      const resize = useRef(null);
      const finishResize = () => {
        resize.current = null;
        const frame = document.querySelector('[data-learning-frame]');
        if (frame) delete frame.dataset.learningResizing;
      };
      const setWidth = x => {
        const frame = document.querySelector('[data-learning-frame]');
        if (!frame) return;
        const bounds = frame.getBoundingClientRect();
        const sidebar = frame.querySelector('[data-learning-region="courses"]')?.getBoundingClientRect().width || 0;
        const width = Math.max(300, Math.min((bounds.width - sidebar) * .65, bounds.right - x));
        frame.style.setProperty('--learning-ai-width', width + 'px');
      };
      return h('section', { className: 'learning-materials', 'data-testid': 'learning-materials', 'data-session-id': activeSessionId },
        h('div', { className: 'learning-material-tabs', role: 'tablist', 'aria-label': '资料标签' },
          h(Button, { role: 'tab', 'aria-selected': state.active === 'files', size: 'sm', variant: state.active === 'files' ? 'outline' : 'ghost',
            title: courseTitle, onClick: () => { ++sourceJumpRevision; state.active = 'files'; notify(); } }, courseTitle),
          state.tabs.map(item => h('span', { key: item.id, className: 'learning-material-tab' },
            h(Button, { role: 'tab', 'aria-selected': state.active === item.id, size: 'sm', variant: state.active === item.id ? 'outline' : 'ghost',
              onClick: () => { ++sourceJumpRevision; state.active = item.id;
                if (item.kind === 'file') { state.contextFile = item.file; state.contextMedia = null; }
                else { state.contextMedia = item.video; state.contextFile = null; } notify(); } }, item.title),
            h(Button, { size: 'sm', 'aria-label': '关闭 ' + item.title, onClick: () => {
              ++sourceJumpRevision;
              state.tabs = state.tabs.filter(value => value.id !== item.id);
              if (state.contextFile && materialKey(state.contextFile) === item.id) state.contextFile = null;
              if (state.contextMedia && materialKey(state.contextMedia) === item.id) state.contextMedia = null;
              if (state.active === item.id) state.active = 'files';
              notify();
            } }, '×'))),
        ),
        aiCollapsed && !immersive && h('aside', { className: 'learning-ai-rail', 'aria-label': 'AI 侧栏' },
          h(Button, { size: 'sm', variant: 'ghost', className: 'learning-icon-action', 'aria-label': '展开 AI', title: '展开 AI · Ctrl+Shift+A',
            icon: h(library.components.Icon, { name: 'panel', size: 16 }), onClick: toggleAi })),
        immersive && aiCollapsed && h(Button, { className: 'learning-immersive-ai-edge', 'aria-label': '展开 AI 问答', title: 'Ctrl+Shift+A',
          onMouseEnter: toggleAi, onFocus: toggleAi, onClick: () => { if (aiCollapsed) toggleAi(); } }, '问答'),
        h('div', { className: 'learning-material-body', role: 'tabpanel' },
          h('div', { hidden: state.active !== 'files', className: 'learning-material-page' }, h(library.LibraryFiles)),
          activeSessionId && state.tabs.map(item => h('div', { key: item.id, hidden: state.active !== item.id, className: 'learning-material-page' },
            item.kind === 'video' ? h(VideoPanel, { sessionId: activeSessionId, selectedAsset: item.video, active: state.active === item.id, tabId: item.id, seekRequest: item.seekRequest })
              : item.kind === 'book' ? h(library.components.BookReader, { courseId: item.file.generated_artifact.course_id, artifactId: item.file.generated_artifact.artifact_id, fixedRevision: item.file.generated_artifact.revision_id, onOpenSource: source => { void openWorkbenchSource(source).catch(reportError); } })
              : h(library.ResourceViewer, { file: item.file, onQuote: addMaterialQuote, onSourceAction: sourceAction, sourceJump: item.sourceJump, visible: state.active === item.id })))),
        !aiCollapsed && h('div', { className: 'learning-ai-resizer', role: 'separator', tabIndex: 0, 'aria-label': '调整 AI 宽度', 'aria-orientation': 'vertical',
          onPointerDown: event => {
            resize.current = event.pointerId;
            const frame = document.querySelector('[data-learning-frame]');
            if (frame) frame.dataset.learningResizing = '';
            event.currentTarget.setPointerCapture(event.pointerId);
          },
          onPointerMove: event => { if (resize.current === event.pointerId) setWidth(event.clientX); },
          onPointerUp: finishResize, onPointerCancel: finishResize, onLostPointerCapture: finishResize,
          onKeyDown: event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
            event.preventDefault(); const chat = document.querySelector('[data-learning-region="ai"]');
            if (chat) setWidth(chat.getBoundingClientRect().left + (event.key === 'ArrowLeft' ? -16 : 16));
          } },
        }),
      );
    }
    function VideoPanel({ sessionId, selectedAsset, active, tabId, seekRequest }) {
      const [asset, setAsset] = useState(selectedAsset);
      const handle = useRef(null);
      const appliedSeek = useRef(null);
      useEffect(() => { videoHandles.set(tabId, handle); return () => videoHandles.delete(tabId); }, [tabId]);
      useEffect(() => {
        if (selectedAsset.platform === 'bilibili' && selectedAsset.playback_session_id !== asset?.playback_session_id && handle.current)
          void handle.current.openAsset(selectedAsset).catch(reportError);
      }, [selectedAsset]);
      useEffect(() => {
        if (!seekRequest || appliedSeek.current === seekRequest || !handle.current?.getElement()
          || asset?.asset_id !== selectedAsset.asset_id || asset?.source_version !== selectedAsset.source_version) return;
        appliedSeek.current = seekRequest;
        void handle.current.openAsset(selectedAsset, seekRequest.time_ms).catch(reportError);
      }, [asset, seekRequest, selectedAsset]);
      return h('section', { className: 'learning-video-panel learning-video-components learning-legacy-module',
        'data-testid': 'learning-video', 'data-session-id': sessionId },
        h(videoComponents.VideoWorkspace, {
          courseId: selectedAsset.source_course_id || selectedCourse, assetId: selectedAsset.asset_id, openedAsset: selectedAsset,
          showSubtitles: materialState().subtitleAsset === selectedAsset.asset_id,
          onSubtitleAction: subtitleAction,
          active, connected: true, minimal: true, workspaceHandleRef: handle, workspaceRef,
          onLibraryChange: () => {}, onAssetChange: setAsset,
          onOpenChat: () => { aiCollapsed = false; notify(); inputFor(sessionId).focus(); },
          onImmersiveChange: setImmersive,
        }),
      );
    }
    return {
      inject: ['slots', 'sessions', 'workspaces', 'uiWorkspace', 'uiConversation', 'conversation', 'sidebarRight', 'sidebarRightTabs', 'theme'],
      async apply(context) {
        try {
        ctx = context;
        const disposers = [];
        library = window.LearningCourseLibrary.create({ React, ReactDOM, Button, Input, Modal, Menu,
          HoverCard: primitives.HoverCard, HoverCardGroup: primitives.HoverCardGroup, Tooltip: primitives.Tooltip,
          icons: primitives, api, chooseCourse, newCourseChat, resolveWorkspaceInfo });
        videoComponents = window.LearningVideoComponents.create({ React, Button, Menu });
        await Promise.all([library.components.initialize(), videoComponents.initialize()]);
        drafts = window.NativeDraftBridge.create({ ctx, api, getDraftController: library.components.getDraftController,
          onError: (error, info) => reportError(Object.assign(error, { sessionId: info?.sessionId || activeSessionId, savedDraft: info?.savedDraft })),
          onRecovered: info => {
            if (globalErrorSession === info.sessionId && !unknownSubmissionSid && !draftConflict
                && ['DRAFT_NOT_READY', 'DRAFT_SESSION_NOT_READY', 'DRAFT_SAVE_FAILED', 'DRAFT_RESTORE_CONFLICT'].includes(globalErrorCode)) {
              globalError = ''; globalErrorCode = null; globalErrorSession = null; notify();
            }
          },
          fetchBlob: async url => { const response = await rawFetch(url, { credentials: 'same-origin' }); if (!response.ok) throw new Error('附件无法恢复（ATTACHMENT_MISSING）'); return response.blob(); } });
        Object.assign(nativeHooks, { current: () => ({ sessionId: activeSessionId, courseId: selectedCourse, navigating: navigationPending, readOnly: readOnlyHistory(), explicitNew: newChatIntent > 0, freshSeed: freshChatSeed }),
          backendRecovered: async () => {
            // Refresh application capabilities and reads only. Draft/message
            // DOM stays mounted; failed submissions are never sent again.
            await Promise.all([library.components.initialize(), videoComponents.initialize()]);
            await library.refresh();
            bindings.clear();
            await ctx.sessions.refresh();
            await drafts.recover();
            notify();
          },
          beforeSubmit: async (sid, requestId, request) => {
            if (navigationPending) throw new Error('请等待课程或对话切换完成（NAVIGATION_PENDING）');
            const state = materialState();
            const tab = state.tabs.find(item => item.id === state.active && item.kind === 'video');
            const player = tab && videoHandles.get(tab.id)?.current?.getElement();
            // Freeze metadata at Send, before draft IO. This reads the playhead
            // only; the separate confirmed-image pathway remains unchanged.
            const time = player?.currentTime;
            const questionVideo = tab && Number.isFinite(time) && time >= 0 ? {
              asset_id: tab.video.asset_id, source_version: tab.video.source_version, time_ms: Math.round(time * 1000), source_course_id: tab.video.source_course_id || selectedCourse,
            } : null;
            const currentFile = state.tabs.find(item => item.id === state.active && ['file', 'book'].includes(item.kind))?.file;
            let pageIndex = 0;
            if (currentFile?.viewer === 'pdf') {
              const frame = document.querySelector('.learning-material-page:not([hidden]) iframe.pdf-reader-frame');
              pageIndex = Math.max(0, (frame?.contentWindow?.PDFViewerApplication?.pdfViewer?.currentPageNumber || 1) - 1);
            }
            const prepared = await drafts.beforeSubmit(sid, requestId, request);
            if (currentFile) prepared.current_document = { course_id: currentFile.course_id,
              asset_id: currentFile.asset_id, source_version: currentFile.source_version, page_index: pageIndex };
            if (questionVideo) prepared.question_video = questionVideo;
            return prepared;
          },
          afterSubmitted: drafts.afterSubmitted, submissionFailed: drafts.submissionFailed,
          submissionUnknown: (...args) => drafts.submissionUnknown(...args),
          uploadHeaders: drafts.uploadHeaders, error: reportError });
        disposers.push(() => drafts.dispose());
        disposers.push(ctx.inject(['commandUi'], scope => scope.effect(() => {
          const commands = library.components.installBilibiliCommand({
            commandUi: scope.get('commandUi'),
            getScope: () => ({ sessionId: activeSessionId, courseId: selectedCourse || 'course_unclassified', navigating: navigationPending }),
            openImport: courseId => {
              ++sourceJumpRevision; immersive = false; materialState().active = 'files'; notify();
              library.components.requestBilibiliImport(courseId);
            }, onError: reportError,
          });
          return () => commands.dispose();
        }, 'learning: Bilibili import entry')));
        const quotes = window.LearningAnswerQuotes.create({ React, ReactDOMClient, flushSync: ReactDOM.flushSync, Button,
          ctx, api, components: library.components, getActiveSession: () => activeSessionId, bindingFor, onError: reportError,
          onQuote: async reference => {
            if (navigationPending) throw new Error('请等待课程或对话切换完成（NAVIGATION_PENDING）');
            const sid = activeSessionId;
            const binding = await bindingFor(sid);
            const controller = library.components.getDraftController(binding.chat_id);
            const values = controller.snapshot().value.references;
            if (values.length >= 100) throw new Error('本次引用数量已达上限（REFERENCE_LIMIT）');
            controller.update({ references: [...values, reference] });
            await controller.flush();
            aiCollapsed = false; notify(); inputFor(sid).focus();
          } });
        disposers.push(() => quotes.dispose());
        disposers.push(library.subscribeOpenVideo(openMaterial));
        disposers.push(library.subscribeOpenFile(openFile));
        const followSource = event => {
          const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null;
          if (!anchor) return;
          const url = new URL(anchor.href, location.href);
          const match = url.origin === location.origin && url.pathname.match(/^\/api\/v1\/courses\/([A-Za-z0-9_-]+)\/learning\/selections\/([A-Za-z0-9_-]+)$/);
          if (!match) return;
          event.preventDefault(); event.stopPropagation();
          void api('/courses/' + match[1] + '/learning/selections/' + match[2])
            .then(source => openWorkbenchSource(source, match[1])).catch(reportError);
        };
        document.addEventListener('click', followSource, true);
        disposers.push(() => document.removeEventListener('click', followSource, true));
        const register = (name, options, component) => disposers.push(ctx.slots.inject(name, () => {
          try { return ctx.slots.register({ name, ...options }, component); }
          catch (error) { console.error('LEARNING_DESKTOP_SLOT_FAILED:', name, String(error.message || error)); throw error; }
        }));
        register('sidebar.workspaces', { priority: -100 }, library.CourseDirectory);
        // This independent learning app uses its own About/account surface.
        // Skip the upstream product marketing notice without writing an ack or
        // granting any Host settings, credential, or model permissions.
        register('settings.onboarding', { id: 'welcome-notice', priority: -100, order: -100 }, ProjectWelcomeStep);
        register('rightbar', { priority: -100 }, MaterialsRoot);
        register('conversation.header', { priority: -100 }, AiHeader);
        register('shell.overlay', { id: 'learning-app-overlays', order: 0 }, AppOverlays);
        register('settings.section', { id: 'learning-data', order: 70, label: '数据与存储' }, DataSettings);
        register('settings.section', { id: 'platform-connections', order: 65, label: '平台连接' }, () => h('div', { className: 'learning-legacy-module' }, h(library.components.PlatformSettings)));
        register('settings.general.item', { id: 'learning-startup', order: 60 }, () =>
          h('div', { className: 'learning-legacy-module learning-startup-settings' },
            h(library.components.StartupSettingsControl, { Toggle: Switch })));
        register('conversation.input.dock', { id: 'learning-references', order: 0 }, ReferenceDock);
        register('conversation.chat.turnTail', { id: 'learning-results', order: 50 }, RequestedTaskActions);
        const revealTask = event => {
          const detail = event.detail;
          if (!detail || detail.courseId !== selectedCourse || !activeSessionId || navigationPending
            || materialState().active === 'workbench' || !/^[A-Za-z0-9_-]{1,160}$/.test(detail.taskId)) return;
          const sid = activeSessionId;
          void api('/courses/' + detail.courseId + '/learning/tasks/' + detail.taskId).then(() => {
            if (sid === activeSessionId && selectedCourse === detail.courseId && !navigationPending) {
              notify();
            }
          }).catch(reportError);
        };
        window.addEventListener('learning-open-task', revealTask);
        disposers.push(() => window.removeEventListener('learning-open-task', revealTask));
        document.documentElement.dataset.learningDesktop = 'native-harness';
        const syncLayout = () => {
          const frame = document.querySelector('[data-shell-bottom]')?.parentElement;
          if (!frame) return;
          frame.dataset.learningFrame = '';
          workspaceRef.current = frame;
          const columns = [...frame.children].filter(element => !element.hasAttribute('data-shell-bottom') && !element.hasAttribute('data-shell-overlay') && !element.hasAttribute('data-side'));
          for (const [index, region] of ['courses', 'ai', 'materials'].entries()) {
            if (columns[index]) columns[index].dataset.learningRegion = region;
          }
          const nativeWidth = Number(frame.style.gridTemplateColumns.match(/^([\d.]+)px/)?.[1] || 280);
          const width = immersive ? '0px' : nativeWidth < 100 ? nativeWidth + 'px' : '240px';
          if (frame.style.getPropertyValue('--learning-course-width') !== width) frame.style.setProperty('--learning-course-width', width);
          const collapsed = aiCollapsed ? 'true' : 'false';
          if (frame.dataset.learningAiCollapsed !== collapsed) frame.dataset.learningAiCollapsed = collapsed;
          frame.dataset.learningImmersive = String(immersive);
          frame.dataset.learningHistory = String(readOnlyHistory());
          frame.dataset.learningSidebarNarrow = String(nativeWidth < 100);
          for (const editor of frame.querySelectorAll('[data-composer-input][contenteditable="true"]')) {
            const label = editor.getAttribute('aria-label') || '';
            if (/^(描述你想要构建|发消息或创建任务|Describe what you want to build|Message or run a task)/.test(label)) editor.setAttribute('aria-label', '输入问题');
          }
          // The pinned native hero stack is [brand, workspace, draft dock, bar].
          // Keep its native editor mounted while adapting only its chrome/layout.
          for (const composer of frame.querySelectorAll('[data-phase="hero"] [data-slot="conversation.composer"]')) {
            const fallback = composer.querySelector('[data-chain-overlay-fallback="conversation.composer"]') || composer;
            const stack = fallback.firstElementChild;
            if (!stack?.querySelector('[data-slot="conversation.composer.bar"]')) continue;
            stack.dataset.learningComposerStack = '';
            for (const chrome of [...stack.children].slice(0, 2)) {
              if (!chrome.querySelector('[data-composer-input]')) chrome.dataset.learningHeroChrome = '';
            }
          }
        };
        const observer = new MutationObserver(syncLayout);
        observer.observe(document.getElementById('root'), { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'contenteditable', 'data-placeholder'] });
        listeners.add(syncLayout);
        disposers.push(() => { observer.disconnect(); listeners.delete(syncLayout); });
        const syncSession = () => {
          const current = Object.values(ctx.sessions.list.getSnapshot().byId).find(value => (value.retainedBy.mainView || 0) > 0);
          if (!current || current.id === activeSessionId) return;
          activeSessionId = current.id;
          void bindingFor(current.id).then(value => {
            if (activeSessionId !== current.id) return;
            if (!selectedCourse) { selectedCourse = value.course_id; void library.setCourse(value.course_id); }
            if (!value.read_only) void drafts.attach(current.id, value.chat_id).catch(reportError);
            notify();
          }).catch(() => {});
        };
        disposers.push(ctx.sessions.list.subscribe(syncSession));
        const shortcut = event => {
          if (!event.ctrlKey || !event.shiftKey || event.altKey || event.repeat) return;
          if (event.code === 'KeyS') { event.preventDefault(); void beginWorkspaceCapture(); }
          if (event.code === 'KeyA') {
            event.preventDefault(); toggleAi();
          }
        };
        document.addEventListener('keydown', shortcut);
        disposers.push(() => document.removeEventListener('keydown', shortcut));
        const pagehide = () => { if (activeSessionId && !readOnlyHistory()) void drafts.beforeNavigate(activeSessionId).catch(() => {}); };
        window.addEventListener('pagehide', pagehide);
        disposers.push(() => window.removeEventListener('pagehide', pagehide));
        void (async () => {
          try {
            const courses = await api('/courses');
            const requested = new URLSearchParams(location.search).get('course_id');
            const course = requested ? courses.find(value => value.course_id === requested) : courses[0];
            if (course) await chooseCourse(course);
            else { library.openCourseDialog(); }
          } catch (reason) { reportError(reason); console.error('LEARNING_COURSE_OPEN_FAILED:', String(reason.message)); }
        })();
        ctx.effect(() => () => { for (const dispose of disposers.reverse()) dispose(); link.remove(); libraryStyle.remove(); });
        } catch (error) {
          console.error('LEARNING_DESKTOP_PLUGIN_FAILED:', String(error.message || error));
          throw error;
        }
      },
    };
  } });
  // The closed SEA omits this one ordinary browser provider, although its
  // native Workspace UI and Remote declarations are present. The build script
  // supplies the unmodified fixed-source provider, using the shared modules.
  const graph = window.__DSH_BOOT__;
  const unavailable = new Set(['@deepseek-ai/dsh-client-ui-sidebar-terminal', '@deepseek-ai/dsh-client-hmr']);
  graph.entries = graph.entries.filter(value => !unavailable.has(value.id));
  graph.batches = graph.batches.map(value => ({ ...value, entries: value.entries.filter(name => !unavailable.has(name)) }))
    .filter(value => value.entries.length);
  const workspaceId = '@deepseek-ai/dsh-api-workspace-controller';
  if (!graph.entries.some(value => value.id === workspaceId)) {
    graph.entries.push({ id: workspaceId, url: 'native-workspace.js', rev: 'learning-pinned-workspace-v1',
      inject: ['@deepseek-ai/dsh-api-gateway', '@deepseek-ai/dsh-client-connection'],
      external: ['@deepseek-ai/dsh-api-gateway/client'],
    });
    graph.batches.push({ phase: 'application', url: 'native-workspace.js', rev: 'learning-pinned-workspace-v1', entries: [workspaceId] });
  }
  const entry = { id, url: 'learning-bridge.js', rev: 'learning-desktop-v1',
    inject: ['@deepseek-ai/dsh-api-session-controller', workspaceId, '@deepseek-ai/dsh-client-ui-conversation',
      '@deepseek-ai/dsh-client-ui-sidebar-right', '@deepseek-ai/dsh-client-ui-workspace'],
  };
  graph.entries.push(entry);
  graph.batches.push({ phase: 'application', url: entry.url, rev: entry.rev, entries: [id] });
})();
