/* Workspace navigation using Harness controls and its unmodified native picker
 * flow. Business identities, file operations and chat scope stay project-owned. */
(() => {
  'use strict';
  window.LearningCourseLibrary = {
    create({ React, Button, Input, Modal, Menu, HoverCard, HoverCardGroup, Tooltip, icons, ReactDOM, api, chooseCourse, newCourseChat, resolveWorkspaceInfo }) {
      const h = React.createElement;
      const { useEffect, useState, useSyncExternalStore } = React;
      const components = window.LearningLibraryComponents.create({ React, Button, Input, Modal, ReactDOM });
      const subscribers = new Set(), videoListeners = new Set(), fileListeners = new Set();
      const operationKey = 'learning.workspace.pending-operation';
      let pending = null, initialError = '', storageBlocked = false;
      try {
        pending = JSON.parse(localStorage.getItem(operationKey) || 'null');
        if (pending && (!['rename-course', 'detach'].includes(pending.kind)
          || !/^[A-Za-z0-9_-]{1,160}$/.test(pending.courseId)
          || !/^[A-Za-z0-9_-]{1,160}$/.test(pending.operationId))) throw new Error('invalid operation');
      } catch {
        pending = null; storageBlocked = true;
        initialError = '工作区操作记录无法读取，暂时不能修改工作区（WORKSPACE_OPERATION_STORAGE_INVALID）';
      }
      let epoch = 0, revision = 0;
      let state = { courseId: null, courses: [], folder: '', pinned: [], error: initialError,
        pointerInside: true,
        busy: false, adding: false, pickerRequest: 0, action: null, pending, review: false,
        componentsReady: false, componentRevision: 0 };
      const publish = patch => { state = { ...state, ...patch }; for (const listener of subscribers) listener(); };
      const subscribe = listener => { subscribers.add(listener); return () => subscribers.delete(listener); };
      const snapshot = () => state;
      const useLibrary = () => useSyncExternalStore(subscribe, snapshot, snapshot);
      const icon = (name, size = 16) => h(icons[name], { size });
      const orderedCourses = data => [
        ...data.courses.filter(course => data.pinned.includes(course.course_id)),
        ...data.courses.filter(course => !data.pinned.includes(course.course_id)),
      ];
      const checkpoint = value => {
        if (value) localStorage.setItem(operationKey, JSON.stringify(value));
        else localStorage.removeItem(operationKey);
        publish({ pending: value, review: false });
      };
      void components.initialize().then(() => publish({ componentsReady: true }))
        .catch(reason => publish({ error: reason.message || 'DESKTOP_LIBRARY_CONNECT_FAILED' }));
      async function refresh() {
        const at = epoch, readRevision = ++revision;
        try {
          const courses = await api('/courses');
          if (at !== epoch || readRevision !== revision) return;
          publish({ courses, componentRevision: state.componentRevision + 1 });
          const pins = await api('/courses/navigation');
          if (at === epoch && readRevision === revision) publish({ pinned: pins.pinned_course_ids });
        } catch (reason) {
          if (at === epoch && readRevision === revision) publish({ error: reason.message });
        }
      }
      function setCourse(courseId) {
        if (state.courseId === courseId) return refresh();
        epoch++; revision++;
        publish({ courseId, folder: '' });
        return refresh();
      }
      async function run(callback) {
        if (state.busy) return;
        publish({ busy: true, error: '' });
        try { await callback(); } catch (reason) { publish({ error: reason.message }); }
        finally { publish({ busy: false }); }
      }
      async function selectCourse(course, folder = '') {
        await chooseCourse(course);
        if (state.courseId === course.course_id) publish({ folder });
      }
      function openCourseDialog() {
        if (!state.busy && !state.adding && !storageBlocked && !state.pending)
          publish({ adding: true, pickerRequest: state.pickerRequest + 1, error: '' });
      }
      async function pickDirectory() {
        const selected = await api('/courses/folder/pick', {});
        return selected?.path ?? null;
      }
      function NativePicker() {
        const data = useLibrary();
        if (!data.componentsReady) return null;
        // Each explicit Add owns a fresh native flow; an earlier cancel cannot
        // carry its armed effect into the next invocation.
        return h(components.NativeDirectoryFlow, { key: data.pickerRequest, open: data.adding, busy: data.busy, pick: pickDirectory,
          onCancel: () => publish({ adding: false }),
          onError: message => publish({ adding: false, error: message }),
          onPicked: path => { void run(async () => {
            try {
              const course = await api('/courses/folder', { path });
              if (course) { await refresh(); await selectCourse(course); }
            } finally { publish({ adding: false }); }
          }); },
        });
      }
      async function finishOperation(operation) {
        await refresh();
        if (operation.kind === 'detach' && state.courseId === operation.courseId) {
          const next = state.courses.find(course => course.course_id === 'course_unclassified') || state.courses[0];
          if (next) await selectCourse(next);
          else await setCourse(null);
        }
        checkpoint(null);
        publish({ action: null });
      }
      async function changeWorkspace(action, name) {
        if (state.pending || storageBlocked) return;
        await run(async () => {
          const operation = { courseId: action.courseId, kind: action.kind, operationId: crypto.randomUUID() };
          checkpoint(operation);
          try {
            const result = await api('/courses/' + operation.courseId + '/' + operation.kind, {
              operation_id: operation.operationId, ...(operation.kind === 'detach' ? { confirm: true } : { name: name.trim() }),
            });
            if (result.state === 'completed') await finishOperation(operation);
          } catch (reason) {
            if (reason.status >= 400 && reason.status < 500 && !/UNKNOWN|REVIEW/.test(reason.code || '')) checkpoint(null);
            throw reason;
          }
        });
      }
      async function reconcile(acknowledge = false) {
        const operation = state.pending;
        if (!operation) return;
        await run(async () => {
          const base = '/courses/' + operation.courseId + '/files/operations/' + operation.operationId;
          const result = await api(base + (acknowledge ? '/reconcile' : ''), acknowledge ? { confirm: true } : undefined);
          if (result.state === 'completed') await finishOperation(operation);
          else if (['failed', 'reviewed'].includes(result.state)) { checkpoint(null); publish({ action: null }); await refresh(); }
          else publish({ review: true });
        });
      }
      function WorkspaceDialog() {
        const data = useLibrary(), action = data.action;
        const [name, setName] = useState('');
        useEffect(() => { setName(action?.title || ''); }, [action]);
        const close = () => { if (!state.busy) publish({ action: null }); };
        const renaming = action?.kind === 'rename-course';
        return h(Modal, { open: !!action, onClose: close, title: renaming ? '重命名工作区' : '移除工作区', closeLabel: '关闭工作区操作',
          footer: h(React.Fragment, null,
            h(Button, { disabled: data.busy, onClick: close }, '取消'),
            h(Button, { variant: 'primary', disabled: data.busy || !!data.pending || (renaming && !name.trim()),
              onClick: () => { void changeWorkspace(action, name); } }, renaming ? '保存' : '移除工作区')),
        }, renaming ? h('div', { className: 'learning-workspace-form' },
          h('label', null, '工作区名称', h(Input, { value: name, 'aria-label': '工作区名称', maxLength: 200, disabled: data.busy,
            onChange: event => setName(event.target.value),
            onKeyDown: event => { if (event.key === 'Enter' && name.trim() && !data.busy && !data.pending) { event.preventDefault(); void changeWorkspace(action, name); } } })),
          h('p', null, '只修改软件中的显示名，磁盘文件夹名称保持不变。'))
          : h('p', null, '将“' + (action?.title || '') + '”从工作区列表移除，磁盘文件、聊天和历史引用会保留。'),
        data.error && h('p', { role: 'alert', className: 'learning-library-error' }, data.error));
      }
      function FolderToggle({ expanded, onToggle, name }) {
        return h(Button, { size: 'sm', variant: 'ghost', className: 'learning-workspace-toggle',
          'aria-label': (expanded ? '收起文件夹 ' : '展开文件夹 ') + name, 'aria-expanded': expanded, onClick: onToggle },
          h('span', { className: 'learning-workspace-folder', 'aria-hidden': true }, icon(expanded ? 'IconFolderOpenRegular' : 'IconFolderCloseRegular')),
          h('span', { className: 'learning-workspace-chevron', 'aria-hidden': true, 'data-expanded': expanded }, icon('IconTriangleRightFillRegular')));
      }
      function FolderChildren({ course, path, revision }) {
        const [read, setRead] = useState({ entries: [], error: '', loading: true });
        useEffect(() => {
          let live = true;
          // Keep existing rows mounted during a refresh so browsing a folder
          // does not collapse its expanded descendants or lose keyboard focus.
          setRead(value => ({ ...value, error: '', loading: true }));
          void api('/courses/' + course.course_id + '/files?path=' + encodeURIComponent(path)).then(value => {
            if (live) setRead({ entries: value.entries.filter(entry => entry.kind === 'directory'), error: '', loading: false });
          }).catch(reason => { if (live) setRead({ entries: [], error: reason.message, loading: false }); });
          return () => { live = false; };
        }, [course.course_id, path, revision]);
        if (read.error) return h('p', { role: 'alert', className: 'learning-library-error' }, read.error);
        return h('ul', { className: 'learning-workspace-children', 'aria-label': (path || course.title) + '的子文件夹', 'aria-busy': read.loading },
          ...read.entries.map(entry => h(FolderRow, { key: entry.path, course, entry, revision })));
      }
      function FolderRow({ course, entry, revision }) {
        const data = useLibrary(), [expanded, setExpanded] = useState(false);
        const selected = data.courseId === course.course_id && data.folder === entry.path;
        return h('li', null,
          h('div', { className: 'learning-workspace-row learning-subfolder-row', 'data-selected': selected, 'data-folder-path': entry.path },
            h(FolderToggle, { expanded, name: entry.name, onToggle: () => setExpanded(value => !value) }),
            h(Button, { size: 'sm', variant: 'ghost', className: 'learning-workspace-name', title: entry.name,
              'aria-current': selected ? 'true' : undefined, disabled: data.busy,
              onClick: () => { void run(() => selectCourse(course, entry.path)); } }, h('span', null, entry.name))),
          expanded && h(FolderChildren, { course, path: entry.path, revision }));
      }
      function WorkspaceRow({ course }) {
        const data = useLibrary(), [open, setOpen] = useState(false), [expanded, setExpanded] = useState(false);
        const [infoRequest, setInfoRequest] = useState(0), [info, setInfo] = useState({});
        useEffect(() => {
          if (!infoRequest || (Object.hasOwn(course, 'root_path') && course.created_at)) return;
          let live = true;
          setInfo(value => ({ ...value, loading: true, error: '' }));
          void resolveWorkspaceInfo(course).then(value => { if (live) setInfo(value); })
            .catch(reason => { if (live) setInfo({ error: reason.message }); });
          return () => { live = false; };
        }, [infoRequest, course.course_id, course.root_path, course.created_at, data.componentRevision]);
        const details = { ...course, ...info };
        const pinned = data.pinned.includes(course.course_id);
        const system = course.course_id === 'course_unclassified';
        const disabled = data.busy || data.adding || !!data.pending || storageBlocked;
        const items = [{ id: 'pin', label: pinned ? '取消置顶' : '置顶', icon: icon('IconPinOutlineRegular') }];
        if (!system) items.push(
          { id: 'rename-course', label: '重命名', icon: icon('IconEditOutlineRegular') },
          { id: 'detach', label: '移除', icon: icon('IconTrashOutlineRegular'), danger: true });
        const row = h('div', { className: 'learning-workspace-row',
          onPointerEnter: () => setInfoRequest(value => value + 1),
          'data-selected': data.courseId === course.course_id && !data.folder, 'data-menu-open': open },
          h(FolderToggle, { expanded, name: course.title, onToggle: () => setExpanded(value => !value) }),
          h(Button, { size: 'sm', variant: 'ghost', className: 'learning-workspace-name',
            'aria-label': course.title, 'aria-current': data.courseId === course.course_id && !data.folder ? 'true' : undefined,
            disabled: data.busy, onClick: () => { void run(() => selectCourse(course)); } }, h('span', null, course.title)),
          pinned && h('span', { className: 'learning-workspace-pin', role: 'img', 'aria-label': '已置顶', title: '已置顶' }, icon('IconPinFillRegular', 14)),
          h(Menu, { open, portal: true, listClassName: 'learning-menu-surface', items: items.map(item => ({ ...item, disabled })), onClose: () => setOpen(false),
            anchor: h(Button, { size: 'sm', variant: 'ghost', className: 'learning-workspace-actions', 'aria-label': '工作区“' + course.title + '”的操作',
              title: '工作区操作', disabled, icon: icon('IconEllipsisOutlineRegular'), onClick: () => setOpen(value => !value) }),
            onSelect: kind => {
              setOpen(false);
              if (kind === 'pin') void run(async () => {
                try {
                  const result = await api('/courses/' + course.course_id + '/pin', { pinned: !pinned });
                  publish({ pinned: result.pinned_course_ids });
                } catch (reason) { await refresh(); throw reason; }
              });
              else publish({ action: { kind, courseId: course.course_id, title: course.title }, error: '' });
            },
          }),
          h(Tooltip, { label: '新对话', side: 'bottom', align: 'end', delayMs: 500, portal: true },
            h(Button, { size: 'sm', variant: 'ghost', className: 'learning-workspace-actions learning-workspace-new-chat',
              'aria-label': '在“' + course.title + '”中新建对话', disabled,
              icon: icon('IconNewChatOutlineRegular'), onClick: () => { void run(() => newCourseChat(course.course_id)); } })));
        const created = new Date(details.created_at || NaN);
        const creationTime = Number.isFinite(created.getTime())
          ? '创建于 ' + created.getFullYear() + '年' + (created.getMonth() + 1) + '月' + created.getDate() + '日 '
            + String(created.getHours()).padStart(2, '0') + ':' + String(created.getMinutes()).padStart(2, '0') : '创建时间未知';
        return h('li', { 'data-course-id': course.course_id },
          h(HoverCard, { anchor: row, openDelayMs: 800, grouped: true, disabled: open || !data.pointerInside,
            copyText: details.root_path || undefined, copyLabel: '复制', copiedLabel: '已复制',
            content: h('div', { className: 'learning-workspace-hover' },
              h('div', { className: 'learning-workspace-hover-title' }, course.title),
              info.error ? h('div', { className: 'learning-workspace-hover-path', role: 'status' }, info.error)
                : !Object.hasOwn(details, 'root_path') || (!details.created_at && info.loading)
                  ? h('div', { className: 'learning-workspace-hover-path', role: 'status' }, '正在读取工作区信息…')
                  : h(React.Fragment, null,
                    h('div', { className: 'learning-workspace-hover-path' }, details.root_path || '未绑定文件夹'),
                    h('div', { className: 'learning-workspace-hover-time' }, creationTime))) }),
          expanded && h(FolderChildren, { course, path: '', revision: data.componentRevision }));
      }
      function CourseDirectory({ wide = true, expandSidebar } = {}) {
        const data = useLibrary();
        useEffect(() => { void refresh(); }, []);
        useEffect(() => {
          const leave = () => { if (state.pointerInside) publish({ pointerInside: false }); };
          const enter = () => { if (!state.pointerInside) publish({ pointerInside: true }); };
          const exitDocument = event => { if (event.relatedTarget === null) leave(); };
          window.addEventListener('blur', leave);
          window.addEventListener('focus', enter);
          document.addEventListener('pointerout', exitDocument);
          document.addEventListener('pointermove', enter);
          document.addEventListener('keydown', enter);
          return () => {
            window.removeEventListener('blur', leave);
            window.removeEventListener('focus', enter);
            document.removeEventListener('pointerout', exitDocument);
            document.removeEventListener('pointermove', enter);
            document.removeEventListener('keydown', enter);
          };
        }, []);
        return h(React.Fragment, null,
          wide ? h('section', { className: 'learning-course-directory', 'data-pointer-inside': data.pointerInside, 'data-testid': 'learning-course-directory', 'aria-label': '工作区' },
            h('div', { className: 'learning-library-course-heading' }, h('span', null, '工作区'),
              h(Button, { size: 'sm', variant: 'ghost', 'aria-label': '添加工作区', title: '添加工作区',
                disabled: data.busy || data.adding || !!data.pending || !data.componentsReady || storageBlocked,
                icon: h(components.Icon, { name: 'plus', size: 16 }), onClick: openCourseDialog })),
            h(HoverCardGroup, { className: 'learning-library-directory-scroll' },
              h('ul', { 'aria-label': '工作区列表', className: 'learning-workspace-list' },
                ...orderedCourses(data).map(course => h(WorkspaceRow, { key: course.course_id, course })))),
            data.error && !data.action && h('p', { role: 'alert', className: 'learning-library-error' }, data.error),
            data.pending && h('div', { className: 'learning-workspace-reconcile' },
              h('p', null, '工作区操作结果尚未确认，未自动重试。'),
              h(Button, { size: 'sm', variant: 'ghost', disabled: data.busy, onClick: () => { void reconcile(); } }, '核对工作区操作'),
              data.review && h(Button, { size: 'sm', variant: 'ghost', disabled: data.busy,
                onClick: () => { void reconcile(true); } }, '已核对实际状态，解除锁定')))
            : h('div', { className: 'learning-course-rail', 'data-testid': 'learning-course-directory' },
              h(Button, { size: 'sm', variant: 'ghost', 'aria-label': '展开工作区', icon: icon('IconFolderCloseRegular', 18), onClick: () => expandSidebar?.() })),
          h(NativePicker), h(WorkspaceDialog));
      }
      function LibraryFiles() {
        const data = useLibrary();
        return h('section', { className: 'learning-library-files learning-legacy-module', 'data-testid': 'learning-library-files', 'aria-label': '工作区文件' },
          data.courseId && data.componentsReady ? h(components.FileManager, {
            key: data.courseId, courseId: data.courseId, connected: true, path: data.folder,
            MenuComponent: Menu,
            revision: data.componentRevision, onPathChange: folder => publish({ folder }),
            onOpen: file => {
              const listeners = file.viewer === 'video' && file.video ? videoListeners : fileListeners;
              for (const listener of listeners) listener(file.viewer === 'video' && file.video ? file.video : file);
            }, onChanged: () => { void refresh(); },
          }) : h(Button, { variant: 'ghost', onClick: openCourseDialog }, '添加工作区'));
      }
      return {
        CourseDirectory, LibraryFiles, ResourceViewer: components.MaterialViewer, components, setCourse, refresh, openCourseDialog,
        getCourseId() { return state.courseId; },
        getCourseTitle() { return state.courses.find(course => course.course_id === state.courseId)?.title || '未分类'; },
        useCourseTitle() { const current = useLibrary(); return current.courses.find(course => course.course_id === current.courseId)?.title || '未分类'; },
        subscribeOpenVideo(listener) { videoListeners.add(listener); return () => videoListeners.delete(listener); },
        subscribeOpenFile(listener) { fileListeners.add(listener); return () => fileListeners.delete(listener); },
      };
    },
  };
})();
