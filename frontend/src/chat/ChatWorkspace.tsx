import { useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import type { FormEvent, Ref } from "react";
import { ApiError, api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { Icon } from "../components/ui/Icon";
import { CaptureEditor } from "../media/CaptureEditor";
import { CaptureError, readImageFile } from "../media/capture";
import type { CaptureResult, CaptureSource } from "../media/capture";
import { loadCapture, saveCapture } from "./captureCache";
import { AttachmentCard } from "./AttachmentCard";
import { rememberAttachment } from "./attachmentMetadata";
import { readDocument } from "./documentFiles";
import { saveAllDrafts, useDraft } from "./draft";
import type { DraftReference } from "./draft";
import { MarkdownAnswer } from "./MarkdownAnswer";
import { generationActive, stateLabel } from "./state";
import "./chat.css";

const storageKey = (chatId: string) => `learning.pending.${chatId}`;
const acceptedKey = (chatId: string) => `learning.accepted.${chatId}`;
function readPending(chatId: string): Schema<"SubmissionInput"> | null {
  try {
    const value = JSON.parse(
      localStorage.getItem(storageKey(chatId)) ||
        sessionStorage.getItem(storageKey(chatId)) ||
        "null",
    );
    return value?.chat_id === chatId && typeof value?.submission_id === "string" ? value : null;
  } catch {
    return null;
  }
}
function retainPending(chatId: string, value: Schema<"SubmissionInput"> | null) {
  for (const storage of [localStorage, sessionStorage]) {
    if (value) storage.setItem(storageKey(chatId), JSON.stringify(value));
    else storage.removeItem(storageKey(chatId));
  }
}
export type VideoSource = NonNullable<CaptureSource["source"]>;
export interface ChatWorkspaceHandle {
  saveDraft: () => Promise<void>;
  focusInput: () => void;
}
interface Props {
  chatId: string;
  connected: boolean;
  canSubmit: boolean;
  configuredModel?: string;
  configuredThinking?: string;
  modelChoices?: string[];
  thinkingChoices?: Schema<"SubmissionInput">["thinking"][];
  modelThinkingChoices?: Record<string, Schema<"SubmissionInput">["thinking"][]>;
  modelImageSupport?: Record<string, boolean>;
  incomingCapture?: CaptureSource;
  incomingConfirmedCapture?: CaptureResult;
  onConfirmedCaptureConsumed?: (sourceId: string) => void;
  onCaptureConsumed?: () => void;
  onOpenSource?: (source: VideoSource) => void;
  ref?: Ref<ChatWorkspaceHandle>;
}
interface DocumentUpload {
  uploadId: string;
  file: File;
  state: "uploading" | "failed";
  error?: string;
}
function MessageAttachment({
  id,
  index,
  onOpenSource,
}: {
  id: string;
  index: number;
  onOpenSource?: (source: VideoSource) => void;
}) {
  const [source, setSource] = useState<Schema<"AttachmentSourceResponse"> | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    void api
      .read<Schema<"AttachmentSourceResponse">>(`/attachments/${id}/source`, abort.signal)
      .then(setSource)
      .catch(() => undefined);
    return () => abort.abort();
  }, [id]);
  return (
    <div className="message-attachment">
      <AttachmentCard id={id} index={index} />
      {source?.origin === "video" &&
        source.asset_id &&
        source.source_version &&
        source.time_ms != null && (
          <button
            type="button"
            className="attachment-source"
            disabled={!onOpenSource}
            onClick={() =>
              onOpenSource?.({
                asset_id: source.asset_id!,
                source_version: source.source_version!,
                time_ms: source.time_ms!,
              })
            }
          >
            来源 {Math.floor(source.time_ms / 60000)}:
            {String(Math.floor(source.time_ms / 1000) % 60).padStart(2, "0")}
          </button>
        )}
    </div>
  );
}

export function ChatWorkspace({
  chatId,
  connected,
  canSubmit,
  configuredModel,
  configuredThinking,
  modelChoices,
  thinkingChoices,
  modelThinkingChoices,
  modelImageSupport,
  incomingCapture,
  incomingConfirmedCapture,
  onConfirmedCaptureConsumed,
  onCaptureConsumed,
  onOpenSource,
  ref,
}: Props) {
  const draft = useDraft(chatId);
  const { controller, value } = draft;
  const [chat, setChat] = useState<Schema<"ChatResponse"> | null>(null);
  const [pending, setPending] = useState(() => readPending(chatId));
  const [knownReceipt, setKnownReceipt] = useState(() =>
    Boolean(localStorage.getItem(acceptedKey(chatId))),
  );
  const [retryAllowed, setRetryAllowed] = useState(false);
  const [candidates, setCandidates] = useState<{ source: CaptureSource; replaces?: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [acceptedGeneration, setAcceptedGeneration] = useState("");
  const messagesRef = useRef<HTMLDivElement>(null);
  const followBottom = useRef(true);
  const composing = useRef(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [focusRequest, setFocusRequest] = useState(0);
  const focusHandled = useRef(0);
  const receivedCaptureIds = useRef(new Set<string>());
  const [capturedSources, setCapturedSources] = useState<Set<string>>(() => new Set());
  const captureTask = useRef<Promise<void> | null>(null);
  const captureInProgress = useRef(false);
  const captureFailure = useRef(false);
  const [failedIncomingCapture, setFailedIncomingCapture] = useState<CaptureResult | null>(null);
  const [documentUploads, setDocumentUploads] = useState<DocumentUpload[]>([]);
  const documentUploadRecords = useRef(new Map<string, DocumentUpload>());
  const documentUploadTasks = useRef(new Map<string, Promise<void>>());
  const selectedFilesReading = useRef(false);
  const imageCandidatesPending = useRef(0);
  const candidate = useMemo(
    () =>
      candidates[0] || (incomingCapture ? { source: incomingCapture, replaces: undefined } : null),
    [candidates, incomingCapture],
  );
  const model = value.model || configuredModel || "deepseek-flash";
  const thinking = (value.thinking ||
    configuredThinking ||
    "high") as Schema<"SubmissionInput">["thinking"];
  const allowedThinking = modelThinkingChoices?.[model] ??
    thinkingChoices ?? ["none", "low", "high", "max"];
  const modelSupported = modelChoices === undefined || modelChoices.includes(model);
  const thinkingSupported = allowedThinking.includes(thinking);
  const editable = draft.ready && !busy && !pending;
  const captureQueued = Boolean(
    incomingConfirmedCapture && !capturedSources.has(incomingConfirmedCapture.source.sourceId),
  );
  const textOnlyModel = modelImageSupport
    ? modelImageSupport[model] === false
    : model === "deepseek-v4-pro";

  useEffect(() => {
    // Uploads temporarily disable the textarea. Focus only after React has
    // committed its enabled DOM, and keep the request scoped to this chat view.
    if (
      !editable ||
      captureQueued ||
      failedIncomingCapture ||
      candidate ||
      focusHandled.current === focusRequest
    )
      return;
    const input = inputRef.current;
    if (!input || input.disabled) return;
    focusHandled.current = focusRequest;
    input.focus();
  }, [editable, captureQueued, failedIncomingCapture, candidate, focusRequest]);

  useImperativeHandle(
    ref,
    () => ({
      focusInput: () => setFocusRequest((current) => current + 1),
      saveDraft: async () => {
        if (selectedFilesReading.current)
          throw new Error("附件仍在读取或加入草稿，请等待完成后继续");
        await Promise.all(documentUploadTasks.current.values());
        if (documentUploadRecords.current.size)
          throw new Error("还有附件未加入草稿，请重试或取消该附件后继续");
        await captureTask.current;
        if (
          captureFailure.current ||
          (incomingConfirmedCapture &&
            !receivedCaptureIds.current.has(incomingConfirmedCapture.source.sourceId))
        )
          throw new Error("截图尚未加入草稿，请重新加入或取消后退出");
        if (candidate || imageCandidatesPending.current)
          throw new Error("还有未确认的图片选区，请确认或取消后退出");
        // A newly mounted, untouched chat has nothing to write before leaving.
        // Its initial read may still be pending; never invent an empty revision.
        const currentDraft = controller.snapshot();
        if (currentDraft.ready || currentDraft.dirty) await controller.flush();
        await saveAllDrafts();
      },
    }),
    [candidate, controller, incomingConfirmedCapture],
  );
  useEffect(() => {
    const abort = new AbortController();
    let timer = 0;
    const refresh = async () => {
      try {
        const result = await api.read<Schema<"ChatResponse">>(`/chats/${chatId}`, abort.signal);
        if (result.chat_id !== chatId) throw new Error("对话身份不匹配");
        if (!abort.signal.aborted) {
          setChat(result);
          setAcceptedGeneration((current) =>
            result.generations?.some((entry) => entry.generation_id === current) ? "" : current,
          );
        }
      } catch (reason) {
        if (!abort.signal.aborted) setError(errorMessage(reason));
      } finally {
        if (!abort.signal.aborted) timer = window.setTimeout(() => void refresh(), 350);
      }
    };
    void refresh();
    return () => {
      abort.abort();
      window.clearTimeout(timer);
    };
  }, [chatId]);
  useEffect(() => {
    const element = messagesRef.current;
    if (element && followBottom.current) element.scrollTop = element.scrollHeight;
  }, [chat]);
  useEffect(() => {
    const flush = () => void controller.flush().catch(() => undefined);
    const hide = () => {
      if (document.visibilityState === "hidden") flush();
    };
    const protect = (event: BeforeUnloadEvent) => {
      if (
        controller.snapshot().dirty ||
        candidate ||
        captureInProgress.current ||
        captureFailure.current ||
        incomingConfirmedCapture ||
        documentUploadRecords.current.size ||
        selectedFilesReading.current
      ) {
        event.preventDefault();
        flush();
      }
    };
    window.addEventListener("pagehide", flush);
    window.addEventListener("beforeunload", protect);
    document.addEventListener("visibilitychange", hide);
    return () => {
      window.removeEventListener("pagehide", flush);
      window.removeEventListener("beforeunload", protect);
      document.removeEventListener("visibilitychange", hide);
      flush();
    };
  }, [controller, candidate, incomingConfirmedCapture]);
  const generations = chat?.generations || [];
  const locked =
    Boolean(acceptedGeneration) ||
    generations.some((entry) => generationActive(entry) || entry.state === "unknown");
  const consumeCandidate = () => {
    if (candidates.length) {
      imageCandidatesPending.current = Math.max(0, imageCandidatesPending.current - 1);
      setCandidates((current) => current.slice(1));
    } else onCaptureConsumed?.();
  };
  const send = async (submission: Schema<"SubmissionInput">) => {
    if (!canSubmit) {
      setError("执行层暂不可用，原问题保留，请先核对连接。");
      return;
    }
    setBusy(true);
    setError("");
    setRetryAllowed(false);
    let receiptKnown = false;
    try {
      await controller.flush();
      retainPending(chatId, submission);
      setPending(submission);
      const receipt = await api.write<Schema<"SubmissionResponse">>("/submissions", submission);
      receiptKnown = true;
      localStorage.setItem(acceptedKey(chatId), receipt.generation_id);
      setKnownReceipt(true);
      setAcceptedGeneration(receipt.generation_id);
      await controller.clear(submission.submission_id);
      retainPending(chatId, null);
      localStorage.removeItem(acceptedKey(chatId));
      setKnownReceipt(false);
      setPending(null);
      followBottom.current = true;
      setChat(await api.read<Schema<"ChatResponse">>(`/chats/${chatId}`));
    } catch (reason) {
      if (receiptKnown)
        setError(
          "提交已接受；最新记录或草稿清理暂未完成。请重新连接后查看，切勿重复发送已接受内容。",
        );
      else if (
        reason instanceof ApiError &&
        reason.status &&
        reason.status >= 400 &&
        reason.status < 500
      ) {
        retainPending(chatId, null);
        setPending(null);
        setError(errorMessage(reason));
      } else setError(`${errorMessage(reason)}。输入仍保留；提交回执未知时先核对。`);
    } finally {
      setBusy(false);
    }
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (
      !canSubmit ||
      !connected ||
      pending ||
      locked ||
      busy ||
      !draft.ready ||
      !modelSupported ||
      !thinkingSupported ||
      !model.trim() ||
      failedIncomingCapture ||
      captureQueued ||
      candidate ||
      documentUploadRecords.current.size > 0 ||
      selectedFilesReading.current ||
      composing.current ||
      (!value.text.trim() && !value.attachment_ids.length)
    )
      return;
    void send({
      chat_id: chatId,
      submission_id: crypto.randomUUID(),
      text: value.text,
      attachment_ids: value.attachment_ids,
      references: value.references,
      source_snapshot_ids: value.source_snapshot_ids || [],
      model,
      thinking,
    });
  };
  const lookup = async () => {
    if (!pending) return;
    setBusy(true);
    setError("");
    try {
      const receipt = await api.read<Schema<"SubmissionResponse">>(
        `/submissions/${pending.submission_id}`,
      );
      setAcceptedGeneration(receipt.generation_id);
      localStorage.setItem(acceptedKey(chatId), receipt.generation_id);
      setKnownReceipt(true);
      await controller.clear(pending.submission_id);
      retainPending(chatId, null);
      localStorage.removeItem(acceptedKey(chatId));
      setKnownReceipt(false);
      setPending(null);
      setChat(await api.read<Schema<"ChatResponse">>(`/chats/${chatId}`));
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) {
        setRetryAllowed(true);
        setError("后台未找到这次提交。可主动重新发送同一次提交，身份保持不变。");
      } else setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  const command = async (generationId: string, action: "cancel" | "reconcile") => {
    setBusy(true);
    setError("");
    try {
      const result = await api.write<Schema<"GenerationResponse">>(
        `/generations/${generationId}/${action}`,
      );
      setChat((current) =>
        current
          ? {
              ...current,
              generations: (current.generations || []).map((entry) =>
                entry.generation_id === result.generation_id ? result : entry,
              ),
            }
          : current,
      );
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  const uploadDocument = (entry: DocumentUpload) => {
    const existing = documentUploadTasks.current.get(entry.uploadId);
    if (existing) return existing;
    const update = (value: DocumentUpload | null) => {
      if (value) documentUploadRecords.current.set(entry.uploadId, value);
      else documentUploadRecords.current.delete(entry.uploadId);
      setDocumentUploads([...documentUploadRecords.current.values()]);
    };
    update({ ...entry, state: "uploading", error: undefined });
    const task = (async () => {
      setBusy(true);
      try {
        const { data, mediaType } = await readDocument(entry.file);
        const attachment = await api.write<Schema<"AttachmentMetadataResponse">>(
          "/document-attachments",
          {
            chat_id: chatId,
            upload_id: entry.uploadId,
            filename: entry.file.name,
            media_type: mediaType,
            data_base64: data,
            confirmed: true,
          },
          { timeoutMs: 120_000 },
        );
        rememberAttachment(attachment);
        const ids = controller.snapshot().value.attachment_ids;
        controller.update({
          attachment_ids: ids.includes(attachment.attachment_id)
            ? ids
            : [...ids, attachment.attachment_id],
        });
        update(null);
        setFocusRequest((current) => current + 1);
      } catch (reason) {
        update({
          ...entry,
          state: "failed",
          error:
            reason instanceof ApiError
              ? errorMessage(reason)
              : reason instanceof Error
                ? reason.message
                : errorMessage(reason),
        });
      } finally {
        setBusy(false);
      }
    })().finally(() => documentUploadTasks.current.delete(entry.uploadId));
    documentUploadTasks.current.set(entry.uploadId, task);
    return task;
  };
  const addFiles = async (files: File[]) => {
    if (!editable) return;
    if (
      value.attachment_ids.length +
        candidates.length +
        documentUploadRecords.current.size +
        (incomingCapture ? 1 : 0) +
        files.length >
      8
    ) {
      setError("每次最多加入 8 个附件");
      return;
    }
    selectedFilesReading.current = true;
    setBusy(true);
    try {
      const images = files.filter((file) => file.type.startsWith("image/"));
      const documents = files.filter((file) => !file.type.startsWith("image/"));
      const sources = await Promise.all(images.map(readImageFile));
      imageCandidatesPending.current += sources.length;
      setCandidates((current) => [...current, ...sources.map((source) => ({ source }))]);
      for (const file of documents)
        await uploadDocument({ uploadId: crypto.randomUUID(), file, state: "uploading" });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取所选图片");
    } finally {
      selectedFilesReading.current = false;
      setBusy(false);
    }
  };
  const persistAttachment = useCallback(
    async (result: CaptureResult, replaces?: string) => {
      const currentIds = controller.snapshot().value.attachment_ids;
      if ((!replaces && currentIds.length >= 8) || (replaces && !currentIds.includes(replaces))) {
        throw new CaptureError(
          replaces ? "ATTACHMENT_REPLACEMENT_STALE" : "ATTACHMENT_LIMIT",
          replaces
            ? "原附件已移除，请取消后重新选择。"
            : "每次最多加入 8 张确认图片，请取消后移除其他图片。",
        );
      }
      setBusy(true);
      setError("");
      try {
        const attachment = await api.write<Schema<"AttachmentResponse">>("/attachments", {
          data_base64: result.dataUrl.split(",")[1],
          media_type: "image/png",
          confirmed: true,
        } satisfies Schema<"AttachmentInput">);
        // API requests contain the confirmed rectangle only; originals remain in this browser.
        await api.write<Schema<"AttachmentSourceResponse">>(
          `/attachments/${attachment.attachment_id}/source`,
          {
            chat_id: chatId,
            origin: result.source.origin || (result.source.source ? "video" : "image"),
            ...result.source.source,
            source_width: result.source.width,
            source_height: result.source.height,
            crop: result.crop,
            edit_root_id: result.source.sourceId,
            parent_attachment_id: replaces,
          } satisfies Schema<"AttachmentSourceInput">,
        );
        try {
          await saveCapture(attachment.attachment_id, result);
        } catch {
          setError(
            "选区已加入；本机原图编辑缓存不可用，重开后需重新选择原图。已确认选区不受影响。",
          );
        }
        const latestIds = controller.snapshot().value.attachment_ids;
        controller.update({
          attachment_ids: replaces
            ? latestIds.map((id) => (id === replaces ? attachment.attachment_id : id))
            : [...latestIds, attachment.attachment_id],
        });
        setFocusRequest((current) => current + 1);
      } catch (reason) {
        setError(errorMessage(reason));
        throw reason;
      } finally {
        setBusy(false);
      }
    },
    [chatId, controller],
  );
  const confirmAttachment = async (result: CaptureResult) => {
    if (!candidate || !editable) return;
    await persistAttachment(result, candidate.replaces);
    consumeCandidate();
  };
  const receiveConfirmedCapture = useCallback(
    (result: CaptureResult) => {
      setCapturedSources((current) =>
        current.has(result.source.sourceId)
          ? current
          : new Set([...current, result.source.sourceId]),
      );
      captureInProgress.current = true;
      captureFailure.current = false;
      const task = persistAttachment(result)
        .then(() => {
          setFailedIncomingCapture(null);
          onConfirmedCaptureConsumed?.(result.source.sourceId);
        })
        .catch((reason) => {
          setError(
            reason instanceof CaptureError
              ? `${reason.message}（${reason.code}）`
              : errorMessage(reason),
          );
          captureFailure.current = true;
          setFailedIncomingCapture(result);
        })
        .finally(() => {
          captureInProgress.current = false;
        });
      captureTask.current = task;
    },
    [persistAttachment, onConfirmedCaptureConsumed],
  );
  useEffect(() => {
    if (!incomingConfirmedCapture || !editable || busy) return;
    const identity = incomingConfirmedCapture.source.sourceId;
    if (receivedCaptureIds.current.has(identity)) return;
    receivedCaptureIds.current.add(identity);
    receiveConfirmedCapture(incomingConfirmedCapture);
  }, [incomingConfirmedCapture, editable, busy, receiveConfirmedCapture]);
  const reedit = async (id: string) => {
    setBusy(true);
    try {
      const previous = await loadCapture(id);
      if (!previous) throw new Error("本机原截图不可用，请主动重新选择原图；不会补取视频画面。");
      imageCandidatesPending.current += 1;
      setCandidates((current) => [
        { source: { ...previous.source, initialCrop: previous.crop }, replaces: id },
        ...current,
      ]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "本机原截图不可用");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="chat-workspace">
      <div
        className="messages"
        aria-label="对话内容"
        ref={messagesRef}
        onScroll={(event) => {
          const element = event.currentTarget;
          followBottom.current =
            element.scrollHeight - element.scrollTop - element.clientHeight < 72;
        }}
      >
        {!chat ? (
          <p className="muted" role="status">
            正在读取已保存记录…
          </p>
        ) : (
          (chat.messages || []).map((message) => {
            const generation =
              message.role === "assistant"
                ? generations.find((entry) => entry.generation_id === message.generation_id)
                : undefined;
            const body = generation?.body || message.text;
            const reasoning = generation?.reasoning || message.reasoning;
            return (
              <article
                id={`message-${message.message_id}`}
                className={`message ${message.role}`}
                key={message.message_id}
              >
                <div className="message-label">{message.role === "user" ? "我" : "学习 AI"}</div>
                <div className="message-content">
                  {reasoning && message.role === "assistant" && (
                    <details className="reasoning">
                      <summary>思考内容（可展开）</summary>
                      <pre>{reasoning}</pre>
                    </details>
                  )}
                  {message.role === "assistant" ? (
                    <MarkdownAnswer
                      text={body || "正在等待内容…"}
                      onQuote={
                        editable
                          ? (selection) => {
                              const current = controller.snapshot().value.references;
                              if (current.length >= 20) {
                                setError("每次最多加入 20 条引用，请先移除其他引用。");
                                return;
                              }
                              controller.update({
                                references: [
                                  ...current,
                                  {
                                    reference_id: crypto.randomUUID(),
                                    message_id: message.message_id,
                                    ...selection,
                                  },
                                ],
                              });
                              inputRef.current?.focus();
                            }
                          : undefined
                      }
                    />
                  ) : (
                    <>
                      {(
                        (message as typeof message & { references?: DraftReference[] })
                          .references || []
                      ).map((reference) => (
                        <div className="message-reference" key={reference.reference_id}>
                          <small>↗ 引用回答</small>
                          <MarkdownAnswer text={reference.markdown} />
                        </div>
                      ))}
                      <pre className="message-body">{body}</pre>
                    </>
                  )}
                  {Boolean(message.attachment_ids?.length) && (
                    <div className="message-attachments">
                      {message.attachment_ids?.map((id, index) => (
                        <MessageAttachment
                          key={id}
                          id={id}
                          index={index}
                          onOpenSource={onOpenSource}
                        />
                      ))}
                    </div>
                  )}
                  {generation && (
                    <div className="generation-footer">
                      <span role="status">{stateLabel(generation.state)}</span>
                      <small title={`事件水位 ${generation.event_watermark}`}>
                        {generation.usage?.status === "known"
                          ? `用量：输入 ${generation.usage.input_tokens ?? "未知"} / 输出 ${generation.usage.output_tokens ?? "未知"}`
                          : "用量未知"}
                      </small>
                      {generation.error_code && <small>{generation.error_code}</small>}
                      {generationActive(generation) && (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={!connected || busy || generation.cancel_requested}
                          onClick={() => void command(generation.generation_id, "cancel")}
                        >
                          {generation.cancel_requested ? "正在停止" : "停止回答"}
                        </Button>
                      )}
                      {generation.state === "unknown" && (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={!connected || busy}
                          onClick={() => void command(generation.generation_id, "reconcile")}
                        >
                          核对执行结果
                        </Button>
                      )}
                    </div>
                  )}
                </div>
              </article>
            );
          })
        )}
      </div>
      <div className="composer-area">
        {documentUploads.map((upload) => (
          <div
            className="document-upload-state draft-warning"
            role={upload.state === "failed" ? "alert" : "status"}
            key={upload.uploadId}
          >
            <span>{upload.file.name}</span>
            <span>{upload.state === "uploading" ? "正在加入附件…" : upload.error}</span>
            {upload.state === "failed" && (
              <>
                <Button
                  type="button"
                  variant="ghost"
                  disabled={!editable}
                  onClick={() => void uploadDocument(upload)}
                >
                  重试附件
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => {
                    documentUploadRecords.current.delete(upload.uploadId);
                    setDocumentUploads([...documentUploadRecords.current.values()]);
                  }}
                >
                  取消附件
                </Button>
              </>
            )}
          </div>
        ))}
        {error && (
          <div className="inline-error" role="alert">
            {error}
          </div>
        )}
        {failedIncomingCapture && (
          <div className="draft-warning" role="alert">
            截图尚未加入，选区仍保留。
            <Button
              variant="ghost"
              disabled={!editable}
              onClick={() => receiveConfirmedCapture(failedIncomingCapture)}
            >
              重新加入截图
            </Button>
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => {
                captureFailure.current = false;
                setFailedIncomingCapture(null);
                setError("");
                onConfirmedCaptureConsumed?.(failedIncomingCapture.source.sourceId);
              }}
            >
              取消这张截图
            </Button>
          </div>
        )}
        {draft.error && (
          <div className="draft-warning" role="alert">
            {draft.error}
            <Button variant="ghost" onClick={() => void controller.load()}>
              重新连接草稿
            </Button>
            {!draft.ready && (
              <Button
                variant="ghost"
                onClick={() => {
                  if (window.confirm("将使用服务器保存的草稿，替换当前本地草稿。继续吗？"))
                    void controller.load(true);
                }}
              >
                查看已保存草稿
              </Button>
            )}
          </div>
        )}
        {pending && (
          <div className="pending-notice" role="status">
            <span>
              {knownReceipt
                ? "这次提交已接受，草稿清理尚待核对。不会重新发送。"
                : "这次提交的回执尚未确认。先核对，不会自动重发。"}
            </span>
            <Button variant="outline" disabled={!connected || busy} onClick={() => void lookup()}>
              核对提交
            </Button>
            {retryAllowed && (
              <Button disabled={!connected || busy} onClick={() => void send(pending)}>
                重新发送同次提交
              </Button>
            )}
          </div>
        )}
        {!canSubmit && <p className="muted">问答尚未接通，已保存对话仍可查看。</p>}
        {!modelSupported && (
          <p className="composer-blocked" role="status">
            {modelChoices?.length
              ? "当前选择不在可用模型目录中，请手动选择模型。"
              : "当前接入方式尚未提供可用模型，请检查连接状态。"}
          </p>
        )}
        {modelSupported && !thinkingSupported && (
          <p className="composer-blocked" role="status">
            当前模型不支持已选思考强度，请手动重新选择。
          </p>
        )}
        {textOnlyModel && (
          <p className="composer-blocked" role="status">
            仅支持文字；包含题图的对话请使用支持图片的模型。
          </p>
        )}
        <form
          className="composer"
          onSubmit={submit}
          onPaste={(event) => {
            const files = Array.from(event.clipboardData.items)
              .filter((item) => item.kind === "file")
              .map((item) => item.getAsFile())
              .filter((file): file is File => Boolean(file));
            if (files.length) {
              event.preventDefault();
              void addFiles(files);
            }
          }}
        >
          {value.references.length > 0 && (
            <div className="draft-references" aria-label="本次引用">
              {value.references.map((reference, index) => (
                <section
                  className="reference-card"
                  aria-label={`引用 ${index + 1}`}
                  key={reference.reference_id}
                >
                  <header>
                    <button
                      type="button"
                      className="reference-source"
                      aria-label={`查看引用来源 ${index + 1}`}
                      onClick={() =>
                        document
                          .getElementById(`message-${reference.message_id}`)
                          ?.scrollIntoView({ block: "center", behavior: "smooth" })
                      }
                    >
                      ↗ 对话引用 {index + 1}
                    </button>
                    <button
                      type="button"
                      className="reference-remove"
                      aria-label={`移除引用 ${index + 1}`}
                      disabled={!editable}
                      onClick={() =>
                        controller.update({
                          references: value.references.filter(
                            (entry) => entry.reference_id !== reference.reference_id,
                          ),
                        })
                      }
                    >
                      ×
                    </button>
                  </header>
                  <MarkdownAnswer text={reference.markdown} />
                </section>
              ))}
            </div>
          )}
          {value.attachment_ids.length > 0 && (
            <div className="draft-attachments" aria-label="本次附件">
              {value.attachment_ids.map((id, index) => (
                <AttachmentCard
                  key={id}
                  id={id}
                  index={index}
                  draft
                  disabled={!editable}
                  onReedit={() => void reedit(id)}
                  onRemove={() =>
                    controller.update({
                      attachment_ids: value.attachment_ids.filter((entry) => entry !== id),
                    })
                  }
                />
              ))}
            </div>
          )}
          <label className="sr-only" htmlFor="question-input">
            问题或追问
          </label>
          <textarea
            ref={inputRef}
            id="question-input"
            placeholder="问点什么，或继续追问…"
            rows={3}
            value={value.text}
            onChange={(event) => controller.update({ text: event.target.value })}
            disabled={!editable}
            onCompositionStart={() => {
              composing.current = true;
            }}
            onCompositionEnd={() => {
              composing.current = false;
            }}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                (event.ctrlKey || event.metaKey) &&
                !event.nativeEvent.isComposing &&
                !composing.current
              ) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <div className="composer-toolbar">
            <label className="file-picker" title="添加附件">
              <Icon name="plus" />
              <input
                aria-label="添加附件"
                type="file"
                multiple
                accept="image/png,image/jpeg,image/webp,application/pdf,text/plain,text/markdown,.pdf,.txt,.md,.markdown"
                disabled={!editable || value.attachment_ids.length >= 8}
                onChange={(event) => {
                  const files = Array.from(event.target.files || []);
                  event.target.value = "";
                  void addFiles(files);
                }}
              />
            </label>
            <label className="model-field">
              模型
              <input
                aria-label="模型名称"
                list="learning-models"
                value={model}
                onChange={(event) => controller.update({ model: event.target.value || null })}
                disabled={!editable}
              />
              <datalist id="learning-models">
                {Array.from(
                  new Set(
                    [
                      configuredModel,
                      ...(modelChoices ?? ["deepseek-flash", "deepseek-v4-pro"]),
                    ].filter(Boolean),
                  ),
                ).map((entry) => (
                  <option key={entry} value={entry} />
                ))}
              </datalist>
            </label>
            <label className="thinking-field">
              思考
              <select
                aria-label="思考强度"
                value={thinking}
                onChange={(event) =>
                  controller.update({
                    thinking: event.target.value as Schema<"SubmissionInput">["thinking"],
                  })
                }
                disabled={!editable}
              >
                {!thinkingSupported && (
                  <option value={thinking} disabled>
                    {thinking}（不可用）
                  </option>
                )}
                {allowedThinking.map((entry) => (
                  <option key={entry} value={entry}>
                    {entry === "none" ? "关闭" : entry}
                  </option>
                ))}
              </select>
            </label>
            <Button
              type="submit"
              disabled={
                !editable ||
                !canSubmit ||
                !connected ||
                locked ||
                (!value.text.trim() && !value.attachment_ids.length) ||
                !model.trim() ||
                !modelSupported ||
                !thinkingSupported ||
                Boolean(failedIncomingCapture) ||
                captureQueued ||
                documentUploads.length > 0 ||
                Boolean(candidate)
              }
            >
              <span aria-hidden="true">↑</span>
              <span className="sr-only">{busy ? "处理中…" : "发送"}</span>
            </Button>
          </div>
        </form>
      </div>
      {candidate && (
        <CaptureEditor
          key={candidate.source.sourceId}
          source={candidate.source}
          onConfirm={confirmAttachment}
          onCancel={consumeCandidate}
        />
      )}
    </div>
  );
}
