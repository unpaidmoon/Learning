import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { webcrypto } from 'node:crypto';
import { test } from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../../desktop/client/draft-bridge.js', import.meta.url), 'utf8');
function store(value) {
  const listeners = new Set();
  return {
    getSnapshot: () => value,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    set(next) { value = next; for (const listener of listeners) listener(); },
  };
}
const initial = () => ({ text: '', attachment_ids: [], references: [], conditions: [], corrections: [], model: null, thinking: null });
function fixture(value = initial()) {
  const apiCalls = [];
  const errors = [];
  const inputStore = store({ draft: '', attachmentIds: [], phase: 'plain' });
  const session = store({ pendingSubmissions: [] });
  const uploads = store({});
  const descriptors = new Map();
  const metadata = new Map();
  const objects = new Map();
  const uploadsHeaders = [];
  let bridge;
  let next = 0;
  let clearError;
  let imageError;
  let flushError;
  let flushCount = 0;
  const recovered = [];
  let lookup = () => { throw new Error('submission lookup unavailable'); };
  const controllerStore = store({ ready: true, dirty: false, error: '', value });
  const controller = {
    subscribe: controllerStore.subscribe,
    snapshot: controllerStore.getSnapshot,
    update(patch) { const now = this.snapshot(); controllerStore.set({ ...now, value: { ...now.value, ...patch }, dirty: true }); },
    async flush() { flushCount++; if (flushError) throw flushError; controllerStore.set({ ...this.snapshot(), dirty: false }); },
    async reconcile() { controllerStore.set({ ...this.snapshot(), ready: true, error: '' }); return this.snapshot().value; },
    async load() { controllerStore.set({ ...this.snapshot(), ready: true, error: '' }); return this.snapshot().value; },
    async clear(requestId) {
      if (clearError) throw clearError;
      controller.clearedRequest = requestId;
      controllerStore.set({ ...this.snapshot(), value: initial(), dirty: false });
    },
  };
  const input = {
    state: inputStore,
    setDraft(draft) { inputStore.set({ ...inputStore.getSnapshot(), draft }); },
    addAttachments(ids) { inputStore.set({ ...inputStore.getSnapshot(), attachmentIds: [...inputStore.getSnapshot().attachmentIds, ...ids] }); return true; },
    removeAttachment(id) { inputStore.set({ ...inputStore.getSnapshot(), attachmentIds: inputStore.getSnapshot().attachmentIds.filter(value => value !== id) }); },
    notify() {},
  };
  const binding = { session };
  const ctx = {
    sessions: { scope: () => ({}), binding: () => binding },
    conversation: {
      input: { for: () => input }, fileUploads: uploads,
      resolveDraftAttachments: ids => ids.map(id => descriptors.get(id)).filter(Boolean),
      createDrafts(sid, fileList) {
        return fileList.map(file => {
          const image = file.type.startsWith('image/');
          const item = { id: `draft-${++next}`, kind: image ? 'image' : 'file', file };
          descriptors.set(item.id, item);
          if (!image) {
            const headers = bridge.uploadHeaders(file);
            uploadsHeaders.push({ file, headers });
            const receiptId = headers['X-Learning-Existing-Document'] || 'document_new';
            uploads.set({ ...uploads.getSnapshot(), [item.id]: { status: 'ready', receiptId } });
          }
          return item;
        });
      },
      releaseDraftAttachments(items) { for (const item of items) descriptors.delete(item.id); },
    },
  };
  async function api(path, body) {
    apiCalls.push({ path, body });
    if (path.startsWith('/submissions/')) return lookup(path.slice('/submissions/'.length));
    const id = path.match(/^\/attachments\/([^/]+)\/metadata$/)?.[1];
    if (id) {
      if (!metadata.has(id)) throw new Error('missing metadata');
      return metadata.get(id);
    }
    if (path === '/attachments') {
      if (imageError) throw imageError;
      return { attachment_id: `attachment_uploaded_${apiCalls.length}` };
    }
    throw new Error('Unexpected API: ' + path);
  }
  let fetchBlob = async path => objects.get(path);
  const browser = { window: {}, crypto: webcrypto, File, Blob, URL, btoa, atob, setTimeout, clearTimeout,
    location: { origin: 'http://127.0.0.1:8766' }, fetch: () => { throw new Error('unexpected network'); } };
  vm.runInNewContext(source, browser);
  bridge = browser.window.NativeDraftBridge.create({ ctx, api, getDraftController: () => controller,
    fetchBlob: path => fetchBlob(path), onError: (error, detail) => errors.push({ error, detail }),
    onRecovered: value => recovered.push(value), saveDelay: 5 });
  return { bridge, ctx, input, inputStore, session, uploads, descriptors, metadata, objects, apiCalls, errors, controller, controllerStore, uploadsHeaders,
    setFetchBlob(value) { fetchBlob = value; }, setClearError(value) { clearError = value; },
    setImageError(value) { imageError = value; }, setFlushError(value) { flushError = value; },
    setLookup(value) { lookup = value; }, recovered, flushCount: () => flushCount };
}
async function sha(blob) {
  return Buffer.from(await webcrypto.subtle.digest('SHA-256', await blob.arrayBuffer())).toString('hex');
}
async function savedFile(f, id, data, name, type, kind) {
  const blob = new Blob([data], { type });
  const url = '/api/v1/document-attachments/' + id + '/content';
  f.objects.set(url, blob);
  f.metadata.set(id, { attachment_id: id, confirmed: true, media_type: type, kind, filename: name,
    size_bytes: blob.size, sha256: await sha(blob), download_url: url });
  return blob;
}

test('failed initial draft read reconnects without saving until a new native edit', async () => {
  const f = fixture();
  f.controllerStore.set({ ...f.controller.snapshot(), ready: false, error: 'read failed' });
  await assert.rejects(f.bridge.attach('s1', 'chat1'), /DRAFT_NOT_READY/);
  f.input.setDraft('尚未保存的当前输入');
  const before = f.flushCount();
  await f.bridge.recover();
  assert.equal(f.inputStore.getSnapshot().draft, '尚未保存的当前输入');
  assert.equal(f.flushCount(), before);
  assert.equal(f.apiCalls.length, 0);
  assert.equal(f.recovered.length, 1);
  f.input.setDraft('恢复后用户继续编辑');
  await f.bridge.beforeNavigate('s1');
  assert.equal(f.controller.snapshot().value.text, '恢复后用户继续编辑');
  f.bridge.dispose();
});

test('failed autosave work is reconciled without replay and new edits can save', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  f.setFlushError(new Error('save result unknown'));
  f.input.setDraft('保存时断线');
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.ok(f.errors.length > 0);
  f.setFlushError(null);
  const before = f.flushCount();
  await f.bridge.recover();
  assert.equal(f.flushCount(), before);
  f.input.setDraft('接回后的新编辑');
  await f.bridge.beforeNavigate('s1');
  assert.equal(f.controller.snapshot().value.text, '接回后的新编辑');
  f.bridge.dispose();
});

test('typing while the recovery GET is pending stays local and saves only on the next edit', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  let entered, release;
  const reading = new Promise(resolve => { entered = resolve; });
  const waiting = new Promise(resolve => { release = resolve; });
  f.controller.reconcile = async () => { entered(); await waiting; return f.controller.snapshot().value; };
  const before = f.flushCount();
  const recovering = f.bridge.recover();
  await reading;
  f.input.setDraft('接回期间仍可编辑');
  release();
  await recovering;
  assert.equal(f.controller.snapshot().value.text, '接回期间仍可编辑');
  assert.equal(f.flushCount(), before);
  f.input.setDraft('接回之后的新编辑');
  await f.bridge.beforeNavigate('s1');
  assert.equal(f.controller.snapshot().value.text, '接回之后的新编辑');
  f.bridge.dispose();
});

test('an unchanged server head is not a conflict with locally dirty text after a failed save', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  f.setFlushError(new Error('backend went away'));
  f.input.setDraft('本地尚未保存的新输入');
  await new Promise(resolve => setTimeout(resolve, 25));
  f.setFlushError(null);
  f.controller.reconcile = async () => {
    f.controllerStore.set({ ...f.controller.snapshot(), ready: true, error: '', dirty: true });
    return initial(); // Authoritative GET proves the old revision never changed.
  };
  const before = f.flushCount();
  assert.equal((await f.bridge.recover())[0].state, 'ready');
  assert.equal(f.flushCount(), before);
  assert.equal(f.inputStore.getSnapshot().draft, '本地尚未保存的新输入');
  f.input.setDraft('用户继续输入');
  await f.bridge.beforeNavigate('s1');
  assert.equal(f.controller.snapshot().value.text, '用户继续输入');
  f.bridge.dispose();
});

test('recovery keeps a confirmed image object and never uploads it a second time', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  const draft = f.ctx.conversation.createDrafts('s1', [new File(['owned image'], 'picked.png', { type: 'image/png' })])[0];
  f.input.addAttachments([draft.id]);
  await f.bridge.beforeNavigate('s1');
  const ids = [...f.controller.snapshot().value.attachment_ids];
  const calls = f.apiCalls.length, flushes = f.flushCount();
  await f.bridge.recover();
  assert.equal(f.apiCalls.length, calls);
  assert.equal(f.flushCount(), flushes);
  assert.deepEqual([...f.inputStore.getSnapshot().attachmentIds], [draft.id]);
  f.input.setDraft('恢复后保留同一图片继续问');
  await f.bridge.beforeNavigate('s1');
  assert.deepEqual([...f.controller.snapshot().value.attachment_ids], ids);
  assert.equal(f.apiCalls.filter(call => call.path === '/attachments').length, 1);
  f.bridge.dispose();
});

test('a changed server draft remains a choice, never an automatic overwrite on recovery', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  f.input.setDraft('当前窗口的输入');
  await f.bridge.beforeNavigate('s1');
  const remote = { ...initial(), text: '另一窗口的新版本' };
  f.controller.reconcile = async () => {
    f.controllerStore.set({ ...f.controller.snapshot(), ready: false, error: 'newer version' });
    return remote;
  };
  f.controller.load = async () => {
    f.controllerStore.set({ ...f.controller.snapshot(), value: remote, ready: true, error: '' });
    return remote;
  };
  const before = f.flushCount();
  assert.equal((await f.bridge.recover())[0].state, 'conflict');
  assert.equal(f.flushCount(), before);
  assert.equal(f.inputStore.getSnapshot().draft, '当前窗口的输入');
  await assert.rejects(f.bridge.beforeSubmit('s1', 'new', { content: [] }), /DRAFT_RESTORE_CONFLICT/);
  await f.bridge.resolveConflict('s1', 'current');
  assert.equal(f.controller.snapshot().value.text, '当前窗口的输入');
  f.bridge.dispose();
});

test('text and the same native image remain attached when saving before navigation', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  f.input.setDraft('尚未发送的问题');
  const file = new File(['selected PNG bytes'], 'picked.png', { type: 'image/png' });
  const draft = f.ctx.conversation.createDrafts('s1', [file])[0];
  f.input.addAttachments([draft.id]);
  await f.bridge.beforeNavigate('s1');
  assert.equal(f.controller.snapshot().value.text, '尚未发送的问题');
  assert.equal(f.controller.snapshot().value.attachment_ids.length, 1);
  assert.equal(f.inputStore.getSnapshot().attachmentIds[0], draft.id);
  assert.equal(f.apiCalls.filter(call => call.path === '/attachments').length, 1);
  f.bridge.dispose();
});

test('restored document retains original name and reuses its receipt without document preparation', async () => {
  const f = fixture({ ...initial(), text: '已保存文档问题', attachment_ids: ['document_abc'] });
  await savedFile(f, 'document_abc', '%PDF synthetic only', '讲义.pdf', 'application/pdf', 'pdf');
  await f.bridge.attach('s1', 'chat1');
  assert.equal(f.inputStore.getSnapshot().draft, '已保存文档问题');
  assert.equal(f.uploadsHeaders.length, 1);
  assert.equal(f.uploadsHeaders[0].file.name, '讲义.pdf');
  assert.equal(f.uploadsHeaders[0].headers['X-Learning-Existing-Document'], 'document_abc');
  await f.bridge.beforeNavigate('s1');
  assert.deepEqual([...f.controller.snapshot().value.attachment_ids], ['document_abc']);
  assert.equal(f.apiCalls.some(call => call.path.includes('document-attachments')), false);
  f.bridge.dispose();
});

test('late saved draft restoration never overwrites text entered while attachment bytes load', async () => {
  const f = fixture({ ...initial(), text: '旧草稿', attachment_ids: ['document_slow'] });
  const blob = await savedFile(f, 'document_slow', 'saved whole text', 'a.txt', 'text/plain', 'text');
  let finish;
  const gate = new Promise(resolve => { finish = resolve; });
  f.setFetchBlob(async () => gate);
  const pending = f.bridge.attach('s1', 'chat1');
  await new Promise(resolve => setTimeout(resolve, 5));
  f.input.setDraft('用户新输入');
  finish(blob);
  await assert.rejects(pending, error => error.code === 'DRAFT_RESTORE_CONFLICT');
  assert.equal(f.inputStore.getSnapshot().draft, '用户新输入');
  assert.equal(f.controller.snapshot().value.text, '旧草稿');
  await f.bridge.resolveConflict('s1', 'current');
  assert.equal(f.controller.snapshot().value.text, '用户新输入');
  f.bridge.dispose();
});

test('sending persists exact outgoing draft and successful clear preserves newly typed follow-up', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  f.input.setDraft('第一问');
  // Harness clears optimistically before its RPC carrier is invoked.
  f.session.set({ pendingSubmissions: [{ requestId: 'request_1' }] });
  f.input.setDraft('');
  const request = { content: [{ type: 'text', text: '第一问' }] };
  await f.bridge.beforeSubmit('s1', 'request_1', request);
  assert.equal(f.controller.snapshot().value.text, '第一问');
  f.input.setDraft('已经开始输入第二问');
  const result = await f.bridge.afterSubmitted('s1', 'request_1');
  assert.equal(result.cleared, true);
  assert.equal(f.controller.clearedRequest, 'request_1');
  f.session.set({ pendingSubmissions: [] });
  await f.bridge.beforeNavigate('s1');
  assert.equal(f.controller.snapshot().value.text, '已经开始输入第二问');
  f.bridge.dispose();
});

test('image autosave and native prompt share the same confirmed bytes and business attachment', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  const file = new File(['selected image'], 'a.png', { type: 'image/png' });
  f.input.addAttachments(f.ctx.conversation.createDrafts('s1', [file]).map(item => item.id));
  await f.bridge.beforeNavigate('s1');
  const id = f.controller.snapshot().value.attachment_ids[0];
  const request = { content: [{ type: 'image', name: 'a.png', mediaType: 'image/png', data: Buffer.from('selected image').toString('base64') }] };
  await f.bridge.beforeSubmit('s1', 'request_image', request);
  assert.equal(request.content[0].name, `learning-capture_${id}.png`);
  assert.equal(f.controller.snapshot().value.attachment_ids[0], id);
  assert.equal(f.apiCalls.filter(call => call.path === '/attachments').length, 1);
  f.bridge.dispose();
});

test('accepted prompt is not reported as rejected when a newer saved draft prevents clearing', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  await f.bridge.beforeSubmit('s1', 'request_1', { content: [{ type: 'text', text: '已发送' }] });
  f.controller.update({ text: '另一个界面的新草稿' });
  f.setClearError(Object.assign(new Error('新草稿已保存'), { code: 'DRAFT_REVISION_CONFLICT' }));
  const result = await f.bridge.afterSubmitted('s1', 'request_1');
  assert.equal(result.cleared, false);
  assert.equal(f.controller.snapshot().value.text, '另一个界面的新草稿');
  await assert.rejects(f.bridge.beforeNavigate('s1'), error => error.code === 'DRAFT_RESTORE_CONFLICT');
  f.bridge.dispose();
});

test('an uncertain image publish is not silently repeated by a later autosave', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  f.setImageError(new Error('transport result unknown'));
  const file = new File(['selected image'], 'a.png', { type: 'image/png' });
  f.input.addAttachments(f.ctx.conversation.createDrafts('s1', [file]).map(item => item.id));
  await assert.rejects(f.bridge.beforeNavigate('s1'), /transport result unknown/);
  await assert.rejects(f.bridge.beforeNavigate('s1'), /transport result unknown/);
  assert.equal(f.apiCalls.filter(call => call.path === '/attachments').length, 1);
  assert.equal(f.inputStore.getSnapshot().attachmentIds.length, 1);
  f.bridge.dispose();
});

test('text reaches the local business draft immediately, while optimistic send clear does not erase it', async () => {
  const f = fixture({ ...initial(), text: '已有文字', attachment_ids: ['document_local'] });
  await savedFile(f, 'document_local', 'saved notes', 'notes.txt', 'text/plain', 'text');
  await f.bridge.attach('s1', 'chat1');
  f.input.setDraft('刚刚输入，立刻刷新也应保留');
  // Deliberately do not wait for the debounce timer or fromNative's async work.
  assert.equal(f.controller.snapshot().value.text, '刚刚输入，立刻刷新也应保留');
  assert.deepEqual([...f.controller.snapshot().value.attachment_ids], ['document_local']);
  assert.equal(f.controller.snapshot().dirty, true);
  f.session.set({ pendingSubmissions: [{ requestId: 'request_pending' }] });
  f.input.setDraft('');
  assert.equal(f.controller.snapshot().value.text, '刚刚输入，立刻刷新也应保留');
  assert.deepEqual([...f.controller.snapshot().value.attachment_ids], ['document_local']);
  f.bridge.dispose();
});

test('native reload can restore the same text first and business attachment objects afterwards', async () => {
  const f = fixture({ ...initial(), text: '同一份未发送草稿', attachment_ids: ['document_reload'] });
  await savedFile(f, 'document_reload', 'saved document', 'notes.txt', 'text/plain', 'text');
  f.input.setDraft('同一份未发送草稿');
  await f.bridge.attach('s1', 'chat1');
  assert.equal(f.inputStore.getSnapshot().draft, '同一份未发送草稿');
  assert.equal(f.inputStore.getSnapshot().attachmentIds.length, 1);
  assert.equal(f.errors.length, 0);
  assert.equal(f.uploadsHeaders[0].headers['X-Learning-Existing-Document'], 'document_reload');
  f.bridge.dispose();
});

test('native asynchronous text-only cache restoration still hydrates the same business attachments', async () => {
  const f = fixture({ ...initial(), text: '原生缓存稍后恢复的文字', attachment_ids: ['document_async'] });
  await savedFile(f, 'document_async', 'saved notes', 'notes.md', 'text/markdown', 'markdown');
  const attached = f.bridge.attach('s1', 'chat1');
  // Native editor import can run after the Session scope exists. The text is
  // exactly the saved draft, while browser File objects cannot survive reload.
  f.input.setDraft('原生缓存稍后恢复的文字');
  await attached;
  assert.equal(f.inputStore.getSnapshot().draft, '原生缓存稍后恢复的文字');
  assert.equal(f.inputStore.getSnapshot().attachmentIds.length, 1);
  f.bridge.dispose();
});

test('typing and clearing while loading is preserved as a new edit, not overwritten by an older draft', async () => {
  const f = fixture({ ...initial(), text: '较早的已保存文字' });
  const attached = f.bridge.attach('s1', 'chat1');
  f.input.setDraft('新的编辑');
  f.input.setDraft('');
  await assert.rejects(attached, error => error.code === 'DRAFT_RESTORE_CONFLICT');
  assert.equal(f.inputStore.getSnapshot().draft, '');
  assert.equal(f.controller.snapshot().value.text, '较早的已保存文字');
  f.bridge.dispose();
});

test('a preflight draft flush failure can be released without trapping later editing', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  f.setFlushError(new Error('local draft save failed'));
  f.input.setDraft('第一版');
  await new Promise(resolve => setTimeout(resolve, 15));
  await assert.rejects(f.bridge.beforeSubmit('s1', 'preflight_1', { content: [{ type: 'text', text: '第一版' }] }), /local draft save failed/);
  f.bridge.submissionFailed('s1', 'preflight_1');
  f.setFlushError(null);
  f.input.setDraft('修正后的新输入');
  await f.bridge.beforeNavigate('s1');
  assert.equal(f.controller.snapshot().value.text, '修正后的新输入');
  await f.bridge.beforeSubmit('s1', 'preflight_2', { content: [{ type: 'text', text: '修正后的新输入' }] });
  assert.equal(f.controller.snapshot().value.text, '修正后的新输入');
  f.bridge.dispose();
});

test('unknown admission stays blocked until the original request is reconciled, without re-posting', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  await f.bridge.beforeSubmit('s1', 'unknown_1', { content: [{ type: 'text', text: '结果未知的原问题' }] });
  const unknown = await f.bridge.submissionUnknown('s1', 'unknown_1');
  assert.equal(unknown.state, 'unknown');
  const callsBeforeRecover = f.apiCalls.length, savesBeforeRecover = f.flushCount();
  assert.equal((await f.bridge.recover())[0].state, 'blocked');
  assert.equal(f.apiCalls.length, callsBeforeRecover);
  assert.equal(f.flushCount(), savesBeforeRecover);
  f.bridge.submissionFailed('s1', 'unknown_1');
  await assert.rejects(f.bridge.beforeSubmit('s1', 'different_2', { content: [{ type: 'text', text: '不能绕过未知重发' }] }), error => error.code === 'SUBMISSION_RESULT_UNKNOWN');
  f.setLookup(id => ({ submission_id: id, generation_id: 'generation_accepted', state: 'running' }));
  const reconciled = await f.bridge.reconcileSubmission('s1');
  assert.equal(reconciled.state, 'accepted');
  assert.equal(f.controller.clearedRequest, 'unknown_1');
  const lookups = f.apiCalls.filter(call => call.path.startsWith('/submissions/'));
  assert.equal(lookups.length, 2);
  assert.ok(lookups.every(call => call.path === '/submissions/unknown_1' && call.body === undefined));
  f.bridge.dispose();
});

test('only an explicit business not-found result releases an unaccepted request for editing', async () => {
  const f = fixture();
  await f.bridge.attach('s1', 'chat1');
  await f.bridge.beforeSubmit('s1', 'missing_1', { content: [{ type: 'text', text: '未接受的草稿' }] });
  f.setLookup(() => { throw Object.assign(new Error('generic missing route'), { status: 404 }); });
  assert.equal((await f.bridge.submissionUnknown('s1', 'missing_1')).state, 'unknown');
  f.setLookup(() => { throw Object.assign(new Error('not accepted'), { status: 404, code: 'SUBMISSION_NOT_FOUND' }); });
  assert.equal((await f.bridge.reconcileSubmission('s1')).state, 'not_accepted');
  assert.equal(f.controller.clearedRequest, undefined);
  f.input.setDraft('用户显式修改后重新发送');
  await f.bridge.beforeSubmit('s1', 'explicit_2', { content: [{ type: 'text', text: '用户显式修改后重新发送' }] });
  assert.equal(f.controller.snapshot().value.text, '用户显式修改后重新发送');
  f.bridge.dispose();
});
