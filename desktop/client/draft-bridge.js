/* Business-owned persistence for the original Harness composer. No model calls. */
(() => {
  'use strict';
  const captureName = /^learning-capture_(attachment_[A-Za-z0-9_-]+)\.png$/;
  const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  const fault = (code, message) => Object.assign(new Error(`${message}（${code}）`), { code });
  const fingerprint = value => JSON.stringify([value.draft || '', value.attachmentIds || []]);
  const businessFingerprint = value => JSON.stringify([value.text || '', value.attachment_ids || [],
    value.references || [], value.source_snapshot_ids || [], value.read_course_ids || [], value.model || null, value.thinking || null]);
  const filled = value => !!(value.text || value.attachment_ids?.length || value.references?.length || value.source_snapshot_ids?.length);
  function base64(bytes) {
    let text = '';
    for (let index = 0; index < bytes.length; index += 16384) text += String.fromCharCode(...bytes.subarray(index, index + 16384));
    return btoa(text);
  }
  async function hash(bytes) {
    const value = await crypto.subtle.digest('SHA-256', bytes);
    return [...new Uint8Array(value)].map(byte => byte.toString(16).padStart(2, '0')).join('');
  }
  function bytesFromBase64(text) {
    const decoded = atob(text);
    return Uint8Array.from(decoded, character => character.charCodeAt(0));
  }
  window.NativeDraftBridge = {
    create({ ctx, api, getDraftController, fetchBlob, onError, onRecovered, saveDelay = 250 }) {
      if (typeof getDraftController !== 'function') throw fault('DRAFT_ADAPTER_UNAVAILABLE', '草稿保存模块尚未就绪');
      const entries = new Map();
      const images = new Map();
      const files = new WeakMap();
      const restoredDocuments = new WeakMap();
      let disposed = false;
      let recovery;
      function report(entry, error) {
        if (disposed || entries.get(entry.sid) !== entry) return;
        entry.error = error;
        if (entry.reported === error.message) return;
        entry.reported = error.message;
        if (onError) onError(error, { sessionId: entry.sid, chatId: entry.chatId,
          savedDraft: entry.controller.snapshot().value });
        else entry.input?.notify('error', error.message);
      }
      async function blobAt(url) {
        if (fetchBlob) return fetchBlob(url);
        const target = new URL(url, location.origin);
        if (target.origin !== location.origin) throw fault('DRAFT_ATTACHMENT_UNAVAILABLE', '草稿附件来源无效');
        const response = await fetch(target, { credentials: 'same-origin', cache: 'no-store' });
        if (!response.ok) throw fault('DRAFT_ATTACHMENT_UNAVAILABLE', '已保存的草稿附件无法读取');
        return response.blob();
      }
      async function storeImage(data, mediaType, name) {
        const bytes = typeof data === 'string' ? bytesFromBase64(data) : new Uint8Array(await data.arrayBuffer());
        if (!['image/png', 'image/jpeg', 'image/webp'].includes(mediaType) || !bytes.length || bytes.length > 10000000) {
          throw fault('IMAGE_SIZE_INVALID', '请选择不超过 10MB 的 PNG、JPEG 或 WebP 图片');
        }
        const digest = await hash(bytes);
        const matched = captureName.exec(name || '');
        // Identical pixels can belong to different course-bound sources.
        // Never replace a named confirmed capture with another source's id.
        const key = mediaType + ':' + digest + ':' + (matched?.[1] || '');
        if (!images.has(key)) {
          images.set(key, (async () => {
            if (matched) {
              const metadata = await api('/attachments/' + matched[1] + '/metadata');
              if (!metadata.confirmed || metadata.media_type !== mediaType || metadata.sha256 !== digest) {
                throw fault('CONFIRMED_ATTACHMENT_CHANGED', '确认的截图内容已变化');
              }
              return matched[1];
            }
            const result = await api('/attachments', { confirmed: true, media_type: mediaType, data_base64: base64(bytes) });
            return result.attachment_id;
          })());
        }
        // A transport failure may have published the object. Keep that result
        // unknown instead of making a later autosave silently upload it again.
        return await images.get(key);
      }
      function sessionPending(entry) {
        return !!ctx.sessions.binding(entry.sid)?.session.getSnapshot().pendingSubmissions?.length;
      }
      function selectedInput(entry) {
        const value = entry.input.state.getSnapshot();
        return { draft: value.draft || '', attachmentIds: [...(value.attachmentIds || [])], phase: value.phase };
      }
      async function waitReady(entry) {
        const deadline = Date.now() + 20000;
        while (!entry.controller.snapshot().ready) {
          const state = entry.controller.snapshot();
          if (state.error) throw fault('DRAFT_NOT_READY', state.error);
          if (Date.now() >= deadline) throw fault('DRAFT_NOT_READY', '草稿版本尚未核对，请稍后重试');
          await pause(25);
        }
      }
      async function waitUpload(entry, draft) {
        const cached = files.get(draft.file);
        if (cached) return cached;
        const deadline = Date.now() + 300000;
        while (true) {
          const upload = ctx.conversation.fileUploads.getSnapshot()[draft.id];
          if (upload?.status === 'ready') {
            files.set(draft.file, upload.receiptId);
            return upload.receiptId;
          }
          if (upload?.status === 'error') throw fault('DRAFT_UPLOAD_FAILED', upload.message || '草稿附件上传未完成');
          if (disposed || Date.now() >= deadline) throw fault('DRAFT_UPLOAD_PENDING', '草稿附件仍在上传，请完成后再切换');
          await pause(50);
        }
      }
      async function fromNative(entry, snapshot) {
        const drafts = ctx.conversation.resolveDraftAttachments(snapshot.attachmentIds);
        if (drafts.length !== snapshot.attachmentIds.length) throw fault('DRAFT_ATTACHMENT_UNAVAILABLE', '草稿附件尚未接回，请保留当前页面');
        const attachmentIds = [];
        for (const draft of drafts) {
          let id = files.get(draft.file);
          if (!id) {
            id = draft.kind === 'image'
              ? await storeImage(draft.file, draft.file.type, draft.file.name)
              : await waitUpload(entry, draft);
            files.set(draft.file, id);
          }
          attachmentIds.push(id);
        }
        return { text: snapshot.draft, attachment_ids: attachmentIds };
      }
      async function restore(entry, value, force = false) {
        const initial = selectedInput(entry);
        if (!force && entry.editedDuringAttach && !initial.draft && value.text) {
          entry.conflict = value;
          throw fault('DRAFT_RESTORE_CONFLICT', '恢复期间当前输入已被编辑清空；保留当前状态，请先选择要继续的草稿');
        }
        const missingSavedObjects = !force && !entry.sawAttachmentsDuringAttach
          && initial.draft === (value.text || '') && !initial.attachmentIds.length
          && !!value.attachment_ids?.length
          && !entry.attachAttachmentCount;
        if (!force && (initial.draft || initial.attachmentIds.length) && !missingSavedObjects) {
          const local = await fromNative(entry, initial);
          if (filled(value) && (value.text !== local.text || JSON.stringify(value.attachment_ids || []) !== JSON.stringify(local.attachment_ids))) {
            entry.conflict = value;
            throw fault('DRAFT_RESTORE_CONFLICT', '已保存草稿与当前输入不同；两份内容均保留，请先选择要继续的草稿');
          }
          return;
        }
        if (!filled(value)) return;
        const prepared = [];
        for (const id of value.attachment_ids || []) {
          const metadata = await api('/attachments/' + id + '/metadata');
          if (!metadata.confirmed) throw fault('DRAFT_ATTACHMENT_UNAVAILABLE', '草稿包含尚未确认的附件');
          const blob = await blobAt(metadata.download_url);
          if (blob.size !== metadata.size_bytes || await hash(await blob.arrayBuffer()) !== metadata.sha256) {
            throw fault('CONFIRMED_ATTACHMENT_CHANGED', '已保存的草稿附件内容已变化');
          }
          const image = metadata.kind === 'image';
          const name = image ? `learning-capture_${id}.png` : metadata.filename;
          const file = new File([blob], name, { type: metadata.media_type });
          files.set(file, id);
          if (!image) restoredDocuments.set(file, id);
          prepared.push(file);
        }
        // Do not apply a late server response over characters or attachments
        // entered while original files were being restored.
        if (disposed) throw fault('DRAFT_ADAPTER_DISPOSED', '草稿界面已关闭');
        if (fingerprint(selectedInput(entry)) !== fingerprint(initial)) {
          entry.conflict = value;
          throw fault('DRAFT_RESTORE_CONFLICT', '草稿恢复期间已有新输入；保留当前输入，请先选择要继续的草稿');
        }
        entry.muting = true;
        let drafts = [];
        try {
          if (force) {
            for (const id of selectedInput(entry).attachmentIds) entry.input.removeAttachment(id);
          }
          drafts = ctx.conversation.createDrafts(entry.sid, prepared);
          if (!entry.input.addAttachments(drafts.map(draft => draft.id))) throw fault('DRAFT_COMPOSER_BUSY', '输入框正在提交，请稍后恢复草稿');
          entry.input.setDraft(value.text || '');
          entry.lastFingerprint = fingerprint(selectedInput(entry));
        } catch (error) {
          ctx.conversation.releaseDraftAttachments(drafts);
          throw error;
        } finally { entry.muting = false; }
      }
      async function persist(entry) {
        await entry.loading;
        if (entry.recovering) return;
        if (entry.conflict) throw fault('DRAFT_RESTORE_CONFLICT', '草稿存在两份版本，请先选择要继续的草稿');
        if (entry.recoveryBlocked) throw entry.error;
        if (entry.submitting || sessionPending(entry)) return;
        const snapshot = selectedInput(entry);
        if (snapshot.phase && !['plain', 'claimed'].includes(snapshot.phase)) return;
        const captured = fingerprint(snapshot);
        const payload = await fromNative(entry, snapshot);
        if (entry.submitting || sessionPending(entry)) return;
        const previous = entry.controller.snapshot().value;
        if (previous.text !== payload.text || JSON.stringify(previous.attachment_ids || []) !== JSON.stringify(payload.attachment_ids)) {
          entry.controller.update(payload);
        }
        await entry.controller.flush();
        if (fingerprint(selectedInput(entry)) !== captured) schedule(entry);
      }
      function schedule(entry) {
        if (entry.muting || entry.submitting || !entry.input) return;
        clearTimeout(entry.timer);
        entry.timer = setTimeout(() => {
          entry.work = (entry.work || Promise.resolve()).then(() => persist(entry));
          entry.work.catch(error => report(entry, error));
        }, saveDelay);
      }
      function nativeChanged(entry) {
        if (!entry.input || entry.muting || entry.submitting || entry.recovering) return;
        // A disposed native generation may reset its stores while another
        // Session is being selected. Those teardown writes are not user edits.
        if (ctx.sessions.binding(entry.sid) !== entry.binding) return;
        const next = fingerprint(selectedInput(entry));
        if (next === entry.lastFingerprint && !entry.waitingPending) return;
        entry.lastFingerprint = next;
        entry.waitingPending = sessionPending(entry);
        if (!entry.waitingPending) {
          const native = selectedInput(entry);
          const business = entry.controller.snapshot();
          if (!entry.conflict && !entry.recoveryBlocked
              && (!native.phase || ['plain', 'claimed'].includes(native.phase))) entry.lastEditableSnapshot = native;
          if (business.ready && !entry.conflict && !entry.recoveryBlocked
              && (!native.phase || ['plain', 'claimed'].includes(native.phase))
              && business.value.text !== native.draft) {
            // DraftController.update synchronously protects text in its local
            // recovery cache. Attachment identities stay untouched until their
            // existing async uploads/confirmation have been reconciled below.
            // A native optimistic-send clear is excluded by pendingSubmissions.
            entry.controller.update({ text: native.draft });
          }
          schedule(entry);
        }
      }
      async function attach(sid, chatId, { readOnly = false } = {}) {
        if (recovery && !readOnly) await recovery;
        let entry = entries.get(sid);
        if (entry) {
          if (entry.chatId !== chatId) throw fault('DRAFT_SESSION_MISMATCH', '对话草稿身份不一致');
          await entry.loading;
          if (ctx.sessions.binding(sid) === entry.binding) return entry;
          clearTimeout(entry.timer);
          for (const unsubscribe of entry.unsubscribers) unsubscribe();
          entries.delete(sid);
        }
        entry = { sid, chatId, controller: getDraftController(chatId), timer: null,
          submitting: null, muting: false, conflict: null, input: null, work: Promise.resolve(), unsubscribers: [] };
        entries.set(sid, entry);
        entry.loading = (async () => {
          let binding;
          for (let count = 0; count < 800; count++) {
            binding = ctx.sessions.binding(sid);
            const state = binding?.session.getSnapshot();
            if (state?.openState === 'error') throw fault('DRAFT_SESSION_NOT_READY', '当前对话读取失败，请先重新连接');
            if (binding && ctx.sessions.scope(sid) && (!state.openState || state.openState === 'open')) break;
            await pause(25);
          }
          if (!binding || (binding.session.getSnapshot().openState && binding.session.getSnapshot().openState !== 'open')) {
            throw fault('DRAFT_SESSION_NOT_READY', '当前对话尚未就绪');
          }
          entry.binding = binding;
          entry.input = ctx.conversation.input.for(ctx.sessions.scope(sid));
          entry.attachFingerprint = fingerprint(selectedInput(entry));
          entry.attachAttachmentCount = selectedInput(entry).attachmentIds.length;
          entry.lastFingerprint = entry.attachFingerprint;
          entry.attachWatch = entry.input.state.subscribe(() => {
            if (!entry.muting && fingerprint(selectedInput(entry)) !== entry.attachFingerprint) entry.editedDuringAttach = true;
            if (!entry.muting && selectedInput(entry).attachmentIds.length) entry.sawAttachmentsDuringAttach = true;
          });
          entry.unsubscribers.push(entry.controller.subscribe(() => {
            const state = entry.controller.snapshot();
            if (state.error) report(entry, fault('DRAFT_SAVE_FAILED', state.error));
          }));
          await waitReady(entry);
          if (!readOnly) await restore(entry, entry.controller.snapshot().value);
          entry.attachWatch(); entry.attachWatch = null;
          entry.lastEditableSnapshot = selectedInput(entry);
          entry.unsubscribers.push(entry.input.state.subscribe(() => nativeChanged(entry)));
          entry.unsubscribers.push(binding.session.subscribe(() => nativeChanged(entry)));
          if (!readOnly) {
            if (selectedInput(entry).draft || selectedInput(entry).attachmentIds.length) entry.lastFingerprint = null;
            nativeChanged(entry);
          }
        })().catch(error => {
          entry.attachWatch?.(); entry.attachWatch = null;
          if (error.code === 'DRAFT_RESTORE_CONFLICT') entry.conflictedDuringAttach = true;
          report(entry, error); throw error;
        });
        await entry.loading;
        return entry;
      }
      async function recoverEntries() {
        const results = [];
        for (const entry of [...entries.values()]) {
          clearTimeout(entry.timer);
          entry.recovering = true;
          try {
            // Settle observations of old writes; never replay an old promise,
            // attachment upload, optimistic submission or accepted clear.
            await Promise.allSettled([entry.loading, entry.work]);
            if (disposed) break;
            if (entry.unknown || entry.conflict || entry.recoveryBlocked || entry.submitting || sessionPending(entry)) {
              results.push({ sessionId: entry.sid, state: 'blocked' }); continue;
            }
            const local = entry.controller.snapshot().value;
            const remote = await entry.controller.reconcile();
            if (!remote) throw fault('DRAFT_NOT_READY', entry.controller.snapshot().error || '草稿读取尚未完成');
            const native = entry.input ? selectedInput(entry) : null;
            const nativeDrafts = native ? ctx.conversation.resolveDraftAttachments(native.attachmentIds) : [];
            const knownIds = nativeDrafts.map(draft => files.get(draft.file));
            const sameObjects = !!native && knownIds.length === native.attachmentIds.length
              && knownIds.every(Boolean) && JSON.stringify(knownIds) === JSON.stringify(remote.attachment_ids || []);
            const matches = !!native && native.draft === (remote.text || '') && sameObjects;
            const missingObjects = !!native && !native.draft && !native.attachmentIds.length
              && !!remote.attachment_ids?.length && !entry.editedDuringAttach;
            if (!entry.controller.snapshot().ready || missingObjects
                || (!entry.controller.snapshot().dirty && native && (native.draft || native.attachmentIds.length || entry.editedDuringAttach)
                  && businessFingerprint(local) !== businessFingerprint(remote) && !matches)) {
              entry.conflict = remote;
              entry.recoveryLocal = local;
              entry.recoveryConflict = true;
              entry.loading = Promise.resolve(); entry.work = Promise.resolve();
              entry.conflictedDuringAttach = true;
              throw fault('DRAFT_RESTORE_CONFLICT', '已保存草稿与当前输入不同；两份内容均保留，请选择要继续的草稿');
            }
            if (native && !native.draft && !native.attachmentIds.length && !entry.editedDuringAttach
                && remote.text && !remote.attachment_ids?.length) {
              entry.muting = true;
              try { entry.input.setDraft(remote.text); } finally { entry.muting = false; }
            }
            entry.attachWatch?.(); entry.attachWatch = null;
            for (const unsubscribe of entry.unsubscribers) unsubscribe();
            entries.delete(entry.sid);
            const connected = await attach(entry.sid, entry.chatId, { readOnly: true });
            const current = selectedInput(connected);
            // Protect text locally; do not start an autosave just because the
            // backend returned. The next user edit/navigation/send can save.
            if (connected.controller.snapshot().value.text !== current.draft)
              connected.controller.update({ text: current.draft }, false);
            const value = { sessionId: entry.sid, chatId: entry.chatId, state: 'ready' };
            results.push(value); onRecovered?.(value);
          } catch (error) {
            report(entry, error);
            results.push({ sessionId: entry.sid, state: entry.conflict ? 'conflict' : 'failed' });
          } finally { entry.recovering = false; }
        }
        return results;
      }
      function recover() {
        if (!recovery) recovery = recoverEntries().finally(() => { recovery = null; });
        return recovery;
      }
      async function beforeNavigate(sid) {
        if (recovery) await recovery;
        const selected = sid ? [entries.get(sid)].filter(Boolean) : [...entries.values()];
        for (const entry of selected) {
          clearTimeout(entry.timer);
          await entry.loading;
          await entry.work;
          if (ctx.sessions.binding(entry.sid) !== entry.binding) {
            await entry.controller.flush();
            continue;
          }
          for (let attempt = 0; attempt < 8; attempt++) {
            const before = fingerprint(selectedInput(entry));
            await persist(entry);
            if (before === fingerprint(selectedInput(entry))) break;
            if (attempt === 7) throw fault('DRAFT_EDITING', '输入仍在变化，请稍后切换');
          }
          if (entry.submitting || sessionPending(entry)) throw fault('DRAFT_SUBMISSION_PENDING', '当前提交尚未确认，请稍后切换');
          if (entry.controller.snapshot().error) throw fault('DRAFT_SAVE_FAILED', entry.controller.snapshot().error);
        }
      }
      async function beforeSubmit(sid, requestId, request) {
        if (recovery) await recovery;
        const entry = entries.get(sid);
        if (!entry) throw fault('DRAFT_SESSION_NOT_READY', '当前对话草稿尚未接回');
        await entry.loading;
        if (entry.conflict) throw fault('DRAFT_RESTORE_CONFLICT', '请先处理当前草稿冲突');
        if (entry.unknown) throw fault('SUBMISSION_RESULT_UNKNOWN', '上次提交结果尚未确认，请先核对提交');
        if (entry.recoveryBlocked) throw entry.error;
        if (entry.submitting && entry.submitting !== requestId) throw fault('DRAFT_SUBMISSION_PENDING', '当前提交尚未确认，请稍后发送');
        clearTimeout(entry.timer);
        entry.submitting = requestId;
        await entry.work;
        const ids = [];
        const texts = [];
        for (const part of request.content || []) {
          if (part.type === 'text') texts.push(part.text);
          else if (part.type === 'image') {
            const id = await storeImage(part.data, part.mediaType, part.name);
            ids.push(id);
            part.name = `learning-capture_${id}.png`;
          } else if (part.type === 'file') ids.push(part.receiptId);
          else throw fault('DRAFT_ATTACHMENT_UNSUPPORTED', '此附件暂不能加入问题');
        }
        const references = entry.controller.snapshot().value.references || [];
        const source_snapshot_ids = entry.controller.snapshot().value.source_snapshot_ids || [];
        const read_course_ids = entry.controller.snapshot().value.read_course_ids || [];
        const payload = { text: texts.join('\n'), attachment_ids: ids, references, source_snapshot_ids, ...(read_course_ids.length ? { read_course_ids } : {}) };
        request.references = references;
        request.source_snapshot_ids = source_snapshot_ids;
        request.read_course_ids = read_course_ids;
        entry.controller.update(payload);
        await entry.controller.flush();
        entry.sent = { requestId, payload, nativeAttachmentIds: [...(entry.lastEditableSnapshot?.attachmentIds || [])] };
        return request;
      }
      async function afterSubmitted(sid, requestId, { reconciled = false } = {}) {
        const entry = entries.get(sid);
        if (!entry || entry.submitting !== requestId || entry.sent?.requestId !== requestId) return;
        let cleared = false;
        const sent = entry.sent;
        try { await entry.controller.clear(requestId); cleared = true; }
        catch (error) {
          // Admission already succeeded. A failed draft clear must never make
          // native prompt look rejected or restore/re-submit an accepted turn.
          entry.recoveryBlocked = true;
          if (typeof entry.controller.load === 'function') await entry.controller.load(true);
          entry.conflict = entry.controller.snapshot().value;
          report(entry, error);
        }
        finally {
          entry.submitting = null;
          entry.sent = null;
          if (cleared) {
            const current = selectedInput(entry);
            if (reconciled && current.draft === sent.payload.text
                && JSON.stringify(current.attachmentIds) === JSON.stringify(sent.nativeAttachmentIds)) {
              // The native transport error may have restored its optimistic
              // draft. Remove only that unchanged, now-proven accepted copy.
              entry.muting = true;
              try {
                for (const id of current.attachmentIds) entry.input.removeAttachment(id);
                entry.input.setDraft('');
              } finally { entry.muting = false; }
            }
            entry.lastFingerprint = null; nativeChanged(entry);
          }
        }
        return { cleared };
      }
      function submissionFailed(sid, requestId) {
        const entry = entries.get(sid);
        if (!entry || entry.submitting !== requestId) return;
        if (entry.unknown) return;
        entry.submitting = null;
        entry.sent = null;
        entry.work = Promise.resolve();
        // Keep the exact persisted submitted draft until Harness restores its
        // rejected optimistic send. A transient empty editor is not new input.
        entry.lastFingerprint = fingerprint(selectedInput(entry));
      }
      async function reconcileSubmission(sid) {
        const entry = entries.get(sid);
        if (!entry?.unknown) return { state: 'none' };
        if (entry.reconciling) return entry.reconciling;
        const requestId = entry.unknown.requestId;
        entry.reconciling = (async () => {
          let acceptedSubmission;
          try {
            const submission = await api('/submissions/' + encodeURIComponent(requestId));
            if (submission?.submission_id !== requestId) throw fault('SUBMISSION_LOOKUP_INVALID', '提交核对返回了不同的请求身份');
            acceptedSubmission = submission;
            entry.unknown = null;
            entry.recoveryBlocked = false;
            entry.error = null; entry.reported = null;
            const cleanup = await afterSubmitted(sid, requestId, { reconciled: true });
            return { state: 'accepted', requestId, submission, cleared: !!cleanup?.cleared };
          } catch (error) {
            if (acceptedSubmission) {
              // Rendering/cleanup cannot turn a proven accepted request back
              // into an unknown admission or authorize another dispatch.
              entry.recoveryBlocked = true;
              report(entry, fault('DRAFT_CLEAR_RECONCILE_FAILED', '提交已确认，草稿清理仍需核对；已保留当前输入'));
              return { state: 'accepted', requestId, submission: acceptedSubmission, cleared: false };
            }
            if (error.status === 404 && error.code === 'SUBMISSION_NOT_FOUND') {
              entry.unknown = null;
              entry.recoveryBlocked = false;
              entry.error = null; entry.reported = null;
              submissionFailed(sid, requestId);
              return { state: 'not_accepted', requestId };
            }
            entry.recoveryBlocked = true;
            report(entry, fault('SUBMISSION_RESULT_UNKNOWN', '尚未确认上次提交结果，请保持草稿并再次核对提交'));
            return { state: 'unknown', requestId };
          }
        })();
        try { return await entry.reconciling; }
        finally { entry.reconciling = null; }
      }
      async function submissionUnknown(sid, requestId) {
        const entry = entries.get(sid);
        if (!entry || entry.sent?.requestId !== requestId) throw fault('DRAFT_SESSION_NOT_READY', '原提交草稿尚未准备好核对');
        if (entry.unknown && entry.unknown.requestId !== requestId) throw fault('SUBMISSION_RESULT_UNKNOWN', '另一次提交仍等待核对');
        clearTimeout(entry.timer);
        entry.unknown = { requestId };
        entry.recoveryBlocked = true;
        entry.error = fault('SUBMISSION_RESULT_UNKNOWN', '上次提交结果尚未确认，请先核对提交');
        return reconcileSubmission(sid);
      }
      async function resolveConflict(sid, choice) {
        if (!['current', 'saved'].includes(choice)) throw fault('DRAFT_CHOICE_INVALID', '请选择要继续的草稿');
        const entry = entries.get(sid);
        if (entry?.unknown) throw fault('SUBMISSION_RESULT_UNKNOWN', '请先核对上次提交结果，不能切换为另一份草稿重发');
        if (!entry?.conflict) return;
        const saved = entry.conflict;
        if (entry.recoveryConflict) {
          const current = await entry.controller.load(true, false);
          if (!current || !entry.controller.snapshot().ready || businessFingerprint(current) !== businessFingerprint(saved)) {
            if (current) entry.conflict = current;
            throw fault('DRAFT_RESTORE_CONFLICT', '已保存草稿再次变化；两份内容仍保留，请重新选择');
          }
          if (choice === 'current') entry.controller.update(entry.recoveryLocal, false);
          entry.recoveryConflict = false;
        }
        entry.conflict = null;
        entry.error = null;
        entry.reported = null;
        entry.recoveryBlocked = false;
        // A rejected loading promise is not a transport retry. Only the user
        // explicitly chooses which existing draft to continue.
        entry.loading = Promise.resolve();
        if (choice === 'saved') await restore(entry, saved, true);
        else if (choice !== 'current') throw fault('DRAFT_CHOICE_INVALID', '请选择要继续的草稿');
        if (!entry.unsubscribers.length || entry.conflictedDuringAttach) {
          entry.unsubscribers.push(entry.input.state.subscribe(() => nativeChanged(entry)));
          entry.unsubscribers.push(ctx.sessions.binding(sid).session.subscribe(() => nativeChanged(entry)));
          entry.conflictedDuringAttach = false;
        }
        await persist(entry);
        onRecovered?.({ sessionId: sid, chatId: entry.chatId, state: 'ready' });
      }
      function dispose() {
        disposed = true;
        for (const entry of entries.values()) {
          clearTimeout(entry.timer);
          for (const unsubscribe of entry.unsubscribers) unsubscribe();
        }
        entries.clear();
      }
      return { attach, recover, beforeNavigate, beforeSubmit, afterSubmitted, submissionFailed, submissionUnknown, reconcileSubmission, resolveConflict, dispose,
        uploadHeaders(body) { const id = restoredDocuments.get(body); return id ? { 'X-Learning-Existing-Document': id } : {}; },
        snapshot(sid) { return entries.get(sid)?.controller.snapshot(); },
      };
    },
  };
})();
