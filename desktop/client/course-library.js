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
        publish({ dialog: false });
      }
      function CoursePicker() {
        const data = useLibrary();
        const [folder, setFolder] = useState(null);
        const [name, setName] = useState('');
        const [error, setError] = useState('');
        const [busy, setBusy] = useState(false);
        const perform = async callback => {
          if (busy) return;
          setBusy(true); setError('');
          try { await callback(); } catch (reason) { setError(reason.message); }
          finally { setBusy(false); }
        };
        const close = () => { if (!busy) publish({ dialog: false }); };
        return h(Modal, {
          open: data.dialog, onClose: close, title: '添加课程', closeLabel: '关闭课程选择',
          footer: h(React.Fragment, null,
            h(Button, { disabled: busy, onClick: close }, '取消'),
            h(Button, { variant: 'primary', disabled: busy || !folder || !name.trim(), onClick: () => perform(async () => {
              const course = await api('/courses/folder', { path: folder.path, title: name.trim() });
              if (course) { await selectCourse(course); setFolder(null); setName(''); }
            }) }, '创建课程')),
        }, h('div', { className: 'learning-library-create' },
          h(Button, { variant: 'outline', disabled: busy, onClick: () => perform(async () => {
            const selected = await api('/courses/folder/pick', {});
            if (selected) { setFolder(selected); setName(value => value.trim() ? value : selected.name); }
          }) }, '选择课程文件夹'),
          folder && h('p', { className: 'learning-library-folder', title: folder.path }, folder.path),
          h('label', null, '课程名称', h(Input, { value: name, 'aria-label': '课程名称', disabled: busy,
            onChange: event => setName(event.target.value) }))),
          error && h('p', { role: 'alert', className: 'learning-library-error' }, error));
      }
      function CourseFolder({ course }) {
        const data = useLibrary();
        const active = data.courseId === course.course_id;
        const [expanded, setExpanded] = useState(active);
        const [headerContainer, setHeaderContainer] = useState(null);
        const select = async (folder = '') => {
          try {
            await selectCourse(course);
            if (state.courseId === course.course_id) publish({ folder });
          } catch (reason) { publish({ error: reason.message }); }
        };
        return h('li', { role: 'treeitem', 'aria-expanded': expanded, 'aria-selected': active,
          'data-course-id': course.course_id },
          h('div', { className: 'learning-course-folder-row', 'data-selected': active },
            h(Button, { size: 'sm', variant: 'ghost', className: 'learning-folder-toggle',
              'aria-label': (expanded ? '收起课程 ' : '展开课程 ') + course.title,
              onClick: () => setExpanded(value => !value),
              icon: h(components.Icon, { name: expanded ? 'chevron-down' : 'chevron', size: 14 }) }),
            h(Button, { size: 'sm', variant: 'ghost', className: 'learning-course-folder-name',
              title: course.title, 'aria-label': course.title, onClick: () => { void select(); },
              icon: h(components.Icon, { name: 'folder', size: 17 }) }, h('span', null, course.title)),
            h('div', { className: 'learning-course-actions learning-legacy-module', ref: setHeaderContainer })),
          expanded && data.componentsReady && h('div', { className: 'learning-course-folder-children learning-legacy-module' },
            h(components.CourseExplorer, {
              courseId: course.course_id, connected: true, revision: data.componentRevision,
              compact: true, foldersOnly: true, headerContainer,
              onOpen: () => {}, onDirectorySelect: folder => { void select(folder); },
              onCourseChanged: () => { void refresh(); },
              onDetached: () => { void (async () => {
                if (state.courseId === course.course_id) {
                  const courses = await api('/courses');
                  if (courses.length) await selectCourse(courses[0]);
                  else await setCourse(null);
                } else await refresh();
              })().catch(reason => publish({ error: reason.message })); },
            })));
      }
      function CourseDirectory({ wide = true, expandSidebar } = {}) {
        const data = useLibrary();
        useEffect(() => { void refresh(); }, []);
        if (!wide) return h('div', { className: 'learning-course-rail', 'data-testid': 'learning-course-directory' },
          h(Button, { size: 'sm', variant: 'ghost', 'aria-label': '展开课程目录',
            icon: h(components.Icon, { name: 'folder', size: 18 }), onClick: () => expandSidebar?.() }));
        return h('section', { className: 'learning-course-directory', 'data-testid': 'learning-course-directory', 'aria-label': '课程与目录' },
          h('div', { className: 'learning-library-course-heading' }, h('span', null, '课程'),
            h(Button, { size: 'sm', variant: 'ghost', 'aria-label': '添加课程', title: '添加课程',
              icon: h(components.Icon, { name: 'plus', size: 16 }), onClick: () => publish({ dialog: true }) })),
          h('div', { className: 'learning-library-directory-scroll' },
            h('ul', { role: 'tree', 'aria-label': '课程文件夹', className: 'learning-library-tree' },
              ...data.courses.map(course => h(CourseFolder, { key: course.course_id, course })))),
          data.error && h('p', { role: 'alert', className: 'learning-library-error' }, data.error),
          h(CoursePicker));
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
