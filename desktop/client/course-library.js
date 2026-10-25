/* Course navigation for the learning layout. React and controls are supplied
 * by the original Harness client; only project business routes are used. */
(() => {
  'use strict';
  window.LearningCourseLibrary = {
    create({ React, Button, Input, Modal, ReactDOM, api, chooseCourse }) {
      const h = React.createElement;
      const { useEffect, useState, useSyncExternalStore } = React;
      const components = window.LearningLibraryComponents.create({ React, Button, Input, Modal, ReactDOM });
      const subscribers = new Set();
      const videoListeners = new Set();
      const fileListeners = new Set();
      let epoch = 0;
      let revision = 0;
      let state = { courseId: null, courses: [], listing: null, children: {}, expanded: new Set(['']),
        folder: '', folderEntries: [], legacy: [], error: '', busy: false, dialog: false, componentsReady: false, componentRevision: 0 };
      const publish = patch => { state = { ...state, ...patch }; for (const listener of subscribers) listener(); };
      const subscribe = listener => { subscribers.add(listener); return () => subscribers.delete(listener); };
      const snapshot = () => state;
      void components.initialize().then(() => publish({ componentsReady: true }))
        .catch(reason => publish({ error: reason.message || 'DESKTOP_LIBRARY_CONNECT_FAILED' }));
      function useLibrary() {
        return useSyncExternalStore(subscribe, snapshot, snapshot);
      }
      const pathQuery = path => encodeURIComponent(path);
      async function listing(courseId, path) {
        return api('/courses/' + courseId + '/files?path=' + pathQuery(path));
      }
      async function refresh() {
        const at = epoch, readRevision = ++revision, courseId = state.courseId;
        try {
          const courses = await api('/courses');
          if (at !== epoch || readRevision !== revision) return;
          publish({ courses });
          if (!courseId) return;
          const root = await listing(courseId, '');
          const legacy = root.root_status === 'ready' ? [] : await api('/courses/' + courseId + '/videos');
          const children = { '': root.entries };
          const expanded = new Set(state.expanded);
          for (const path of expanded) {
            if (!path) continue;
            try { children[path] = (await listing(courseId, path)).entries; }
            catch { expanded.delete(path); }
          }
          let folder = state.folder, folderEntries = root.entries;
          if (folder) {
            try { folderEntries = (await listing(courseId, folder)).entries; }
            catch { folder = ''; }
          }
          if (at !== epoch || readRevision !== revision) return;
          publish({ listing: root, children, expanded, folder, folderEntries, legacy, error: '', componentRevision: state.componentRevision + 1 });
        } catch (reason) { if (at === epoch && readRevision === revision) publish({ error: reason.message }); }
      }
      function setCourse(courseId) {
        if (state.courseId === courseId) return refresh();
        epoch++; revision++;
        publish({ courseId, listing: null, children: {}, expanded: new Set(['']), folder: '', folderEntries: [], legacy: [], error: '' });
        return refresh();
      }
      async function run(callback) {
        if (state.busy) return;
        publish({ busy: true, error: '' });
        try { await callback(); } catch (reason) { publish({ error: reason.message }); }
        finally { publish({ busy: false }); }
      }
      async function selectCourse(course) {
        await chooseCourse(course);
        await setCourse(course.course_id);
        publish({ dialog: false });
      }
      function CoursePicker() {
        const data = useLibrary();
        const [mode, setMode] = useState('choose');
        const [folder, setFolder] = useState(null);
        const [name, setName] = useState('');
        const [error, setError] = useState('');
        const [busy, setBusy] = useState(false);
        useEffect(() => {
          if (data.dialog) { void refresh(); setError(''); }
        }, [data.dialog]);
        const perform = async callback => {
          if (busy) return;
          setBusy(true); setError('');
          try { await callback(); } catch (reason) { setError(reason.message); }
          finally { setBusy(false); }
        };
        const close = () => { if (!busy) publish({ dialog: false }); };
        return h(Modal, {
          open: data.dialog, onClose: close, title: mode === 'create' ? '创建课程' : '切换课程', closeLabel: '关闭课程选择',
          footer: mode === 'create' ? h(React.Fragment, null,
            h(Button, { disabled: busy, onClick: () => { setMode('choose'); setError(''); } }, '返回'),
            h(Button, { variant: 'primary', disabled: busy || !folder || !name.trim(), onClick: () => perform(async () => {
              const course = await api('/courses/folder', { path: folder.path, title: name.trim() });
              if (course) { await selectCourse(course); setFolder(null); setName(''); setMode('choose'); }
            }) }, '创建课程'),
          ) : h(Button, { variant: 'primary', disabled: busy, onClick: () => { setMode('create'); setError(''); } }, '创建课程'),
        }, mode === 'create' ? h('div', { className: 'learning-library-create' },
          h(Button, { variant: 'outline', disabled: busy, onClick: () => perform(async () => {
            const selected = await api('/courses/folder/pick', {});
            if (selected) { setFolder(selected); setName(value => value.trim() ? value : selected.name); }
          }) }, '选择课程文件夹'),
          folder && h('p', { className: 'learning-library-folder', title: folder.path }, folder.path),
          h('label', null, '课程名称', h(Input, { value: name, 'aria-label': '课程名称', disabled: busy,
            onChange: event => setName(event.target.value) })),
        ) : h('div', { className: 'learning-library-course-list' },
          data.courses.length ? data.courses.map(course => h(Button, {
            key: course.course_id, variant: 'ghost', disabled: busy,
            onClick: () => perform(() => selectCourse(course)),
          }, course.title)) : h('p', null, '创建课程后打开视频学习'),
        ), (error || data.error) && h('p', { role: 'alert', className: 'learning-library-error' }, error || data.error));
      }
      function CourseDirectory({ wide = true, expandSidebar } = {}) {
        const data = useLibrary();
        const [headerContainer, setHeaderContainer] = useState(null);
        const title = data.courses.find(course => course.course_id === data.courseId)?.title || data.listing?.title || '选择课程';
        useEffect(() => { void refresh(); }, []);
        if (!wide) return h(React.Fragment, null,
          h('div', { className: 'learning-course-rail', 'data-testid': 'learning-course-directory' },
            h(Button, { size: 'sm', variant: 'ghost', className: 'learning-icon-action', 'aria-label': '展开课程目录', title: title + ' · 展开课程目录',
              icon: h(components.Icon, { name: 'folder', size: 18 }), onClick: () => { expandSidebar?.(); if (!data.courseId) publish({ dialog: true }); } })),
          h(CoursePicker));
        return h('section', { className: 'learning-course-directory', 'data-testid': 'learning-course-directory', 'aria-label': '课程与目录' },
          h('div', { className: 'learning-library-course-heading' },
            h(Button, { variant: 'ghost', className: 'learning-library-course-name', onClick: () => publish({ dialog: true }), title }, h('span', { className: 'learning-course-title' }, title), h(components.Icon, { name: 'chevron-down', size: 14 })),
            h('div', { className: 'learning-course-actions learning-legacy-module', ref: setHeaderContainer }),
          ),
          h('div', { className: 'learning-library-directory-scroll learning-legacy-module' },
            data.courseId && data.componentsReady ? h(components.CourseExplorer, {
              key: data.courseId, courseId: data.courseId, connected: true, revision: data.componentRevision,
              compact: true, headerContainer,
              onOpen: file => {
                if (file.viewer === 'video' && file.video) { for (const listener of videoListeners) listener(file.video); }
                else { for (const listener of fileListeners) listener(file); }
              },
              onLegacyVideo: asset => { for (const listener of videoListeners) listener(asset); },
              onCourseChanged: () => { void refresh(); },
              onDetached: () => { void (async () => {
                const courses = await api('/courses');
                if (courses.length) await selectCourse(courses[0]);
                else { await setCourse(null); publish({ dialog: true }); }
              })().catch(reason => publish({ error: reason.message })); },
              onDirectorySelect: folder => publish({ folder }),
            }) : h('p', { className: 'learning-library-hint' }, data.courseId ? '正在读取课程文件…' : '选择课程后浏览资料'),
          ),
          data.error && h('p', { role: 'alert', className: 'learning-library-error' }, data.error),
          h(CoursePicker),
        );
      }
      function LibraryFiles() {
        const data = useLibrary();
        return h('section', { className: 'learning-library-files learning-legacy-module', 'data-testid': 'learning-library-files', 'aria-label': '课程文件' },
          data.courseId && data.componentsReady ? h(components.FileManager, {
            key: data.courseId, courseId: data.courseId, connected: true, path: data.folder,
            revision: data.componentRevision, onPathChange: folder => publish({ folder }),
            onOpen: file => {
              if (file.viewer === 'video' && file.video) { for (const listener of videoListeners) listener(file.video); }
              else { for (const listener of fileListeners) listener(file); }
            },
            onChanged: () => { void refresh(); },
          }) : h(Button, { variant: 'ghost', onClick: () => publish({ dialog: true }) }, '选择课程'),
          data.error && h('p', { role: 'alert', className: 'learning-library-error' }, data.error),
        );
      }
      return {
        CourseDirectory, LibraryFiles, ResourceViewer: components.MaterialViewer, components, setCourse, refresh,
        openCourseDialog() { publish({ dialog: true }); },
        getCourseId() { return state.courseId; },
        subscribeOpenVideo(listener) { videoListeners.add(listener); return () => videoListeners.delete(listener); },
        subscribeOpenFile(listener) { fileListeners.add(listener); return () => fileListeners.delete(listener); },
      };
    },
  };
})();
