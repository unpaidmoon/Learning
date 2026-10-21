import { useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import type { FormEvent, Ref } from "react";
import { ApiError, api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { CaptureEditor } from "../media/CaptureEditor";
import { CaptureError, readImageFile } from "../media/capture";
import type { CaptureResult, CaptureSource } from "../media/capture";
import { loadCapture, saveCapture } from "./captureCache";
import { saveAllDrafts, useDraft } from "./draft";
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
}
interface Props {
  chatId: string;
  connected: boolean;
  canSubmit: boolean;
  configuredModel?: string;
  configuredThinking?: string;
  incomingCapture?: CaptureSource;
  onCaptureConsumed?: () => void;
  onOpenSource?: (source: VideoSource) => void;
  ref?: Ref<ChatWorkspaceHandle>;
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
      <img src={`/api/v1/attachments/${id}`} alt={`已确认提交的图片 ${index + 1}`} loading="lazy" />
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
  incomingCapture,
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
  const candidate = useMemo(
    () =>
      candidates[0] || (incomingCapture ? { source: incomingCapture, replaces: undefined } : null),
    [candidates, incomingCapture],
  );
  const model = value.model || configuredModel || "deepseek-flash";
  const thinking = (value.thinking ||
    configuredThinking ||
    "high") as Schema<"SubmissionInput">["thinking"];
  const editable = draft.ready && !busy && !pending;

  useImperativeHandle(
    ref,
    () => ({
      saveDraft: async () => {
        if (candidate) throw new Error("还有未确认的图片选区，请确认或取消后退出");
        await controller.flush();
        await saveAllDrafts();
      },
    }),
    [candidate, controller],
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
      if (controller.snapshot().dirty || candidate) {
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
  }, [controller, candidate]);
  const generations = chat?.generations || [];
  const locked =
    Boolean(acceptedGeneration) ||
    generations.some((entry) => generationActive(entry) || entry.state === "unknown");
  const consumeCandidate = () => {
    if (candidates.length) setCandidates((current) => current.slice(1));
    else onCaptureConsumed?.();
  };
  const send = async (submission: Schema<"SubmissionInput">) => {
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
      candidate ||
      composing.current ||
      (!value.text.trim() && !value.attachment_ids.length)
    )
      return;
    void send({
      chat_id: chatId,
      submission_id: crypto.randomUUID(),
      text: value.text,
      attachment_ids: value.attachment_ids,
      conditions: value.conditions,
      corrections: value.corrections,
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
  const addFiles = async (files: File[]) => {
    if (!editable) return;
    if (
      value.attachment_ids.length + candidates.length + (incomingCapture ? 1 : 0) + files.length >
      8
    ) {
      setError("每次最多加入 8 张确认图片");
      return;
    }
    setBusy(true);
    try {
      const sources = await Promise.all(files.map(readImageFile));
      setCandidates((current) => [...current, ...sources.map((source) => ({ source }))]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取所选图片");
    } finally {
      setBusy(false);
    }
  };
  const confirmAttachment = async (result: CaptureResult) => {
    if (!candidate || !editable) return;
    const currentIds = controller.snapshot().value.attachment_ids;
    if (
      (!candidate.replaces && currentIds.length >= 8) ||
      (candidate.replaces && !currentIds.includes(candidate.replaces))
    ) {
      throw new CaptureError(
        candidate.replaces ? "ATTACHMENT_REPLACEMENT_STALE" : "ATTACHMENT_LIMIT",
        candidate.replaces
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
      // The full original is never accepted by these API calls.
      await api.write<Schema<"AttachmentSourceResponse">>(
        `/attachments/${attachment.attachment_id}/source`,
        {
          chat_id: chatId,
          origin: result.source.source ? "video" : "image",
          ...result.source.source,
          source_width: result.source.width,
          source_height: result.source.height,
          crop: result.crop,
          edit_root_id: result.source.sourceId,
          parent_attachment_id: candidate.replaces,
        } satisfies Schema<"AttachmentSourceInput">,
      );
      try {
        await saveCapture(attachment.attachment_id, result);
      } catch {
        setError("选区已加入；本机原图编辑缓存不可用，重开后需重新选择原图。已确认选区不受影响。");
      }
      controller.update({
        attachment_ids: candidate.replaces
          ? value.attachment_ids.map((id) =>
              id === candidate.replaces ? attachment.attachment_id : id,
            )
          : [...value.attachment_ids, attachment.attachment_id],
      });
      consumeCandidate();
    } catch (reason) {
      setError(errorMessage(reason));
      throw reason;
    } finally {
      setBusy(false);
    }
  };
  const reedit = async (id: string) => {
    setBusy(true);
    try {
      const previous = await loadCapture(id);
      if (!previous) throw new Error("本机原截图不可用，请主动重新选择原图；不会补取视频画面。");
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
      <div className="chat-heading">
        <div>
          <h2>课程对话</h2>
          <p>只在发送后提交文字与确认选区</p>
        </div>
        <small className="muted">{chatId.slice(0, 8)}</small>
      </div>
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
        ) : !chat.messages?.length ? (
          <div className="conversation-empty">
            <h3>从当前不懂的地方问起</h3>
            <p>截图确认后直接进入草稿，也可以输入文字或粘贴多张图片。</p>
          </div>
        ) : (
          chat.messages.map((message) => {
            const generation =
              message.role === "assistant"
                ? generations.find((entry) => entry.generation_id === message.generation_id)
                : undefined;
            const body = generation?.body || message.text;
            const reasoning = generation?.reasoning || message.reasoning;
            return (
              <article className={`message ${message.role}`} key={message.message_id}>
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
                          ? (latex) =>
                              controller.update({
                                text: `${value.text}${value.text ? "\n\n" : ""}$$\n${latex}\n$$\n`,
                              })
                          : undefined
                      }
                    />
                  ) : (
                    <pre className="message-body">{body}</pre>
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
                      <small>事件水位 {generation.event_watermark}</small>
                      <small>
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
        {error && (
          <div className="inline-error" role="alert">
            {error}
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
        <form
          className="composer"
          onSubmit={submit}
          onPaste={(event) => {
            const files = Array.from(event.clipboardData.items)
              .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
              .map((item) => item.getAsFile())
              .filter((file): file is File => Boolean(file));
            if (files.length) {
              event.preventDefault();
              void addFiles(files);
            }
          }}
        >
          {value.attachment_ids.length > 0 && (
            <div className="draft-attachments" aria-label="本次确认图片">
              {value.attachment_ids.map((id, index) => (
                <figure key={id}>
                  <img src={`/api/v1/attachments/${id}`} alt={`待发送的确认图片 ${index + 1}`} />
                  <figcaption>
                    图片 {index + 1}
                    <button
                      type="button"
                      aria-label={`重新裁剪图片 ${index + 1}`}
                      disabled={!editable}
                      onClick={() => void reedit(id)}
                    >
                      重裁
                    </button>
                    <button
                      type="button"
                      aria-label={`移除图片 ${index + 1}`}
                      disabled={!editable}
                      onClick={() =>
                        controller.update({
                          attachment_ids: value.attachment_ids.filter((entry) => entry !== id),
                        })
                      }
                    >
                      移除
                    </button>
                  </figcaption>
                </figure>
              ))}
            </div>
          )}
          <label className="sr-only" htmlFor="question-input">
            问题或追问
          </label>
          <textarea
            id="question-input"
            placeholder="输入问题或继续追问…"
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
          <details className="input-context">
            <summary>题目条件与更正（按需填写）</summary>
            <div className="context-fields">
              <label>
                当前题目条件（每行一项）
                <textarea
                  rows={2}
                  value={value.conditions.join("\n")}
                  onChange={(event) =>
                    controller.update({ conditions: event.target.value.split("\n") })
                  }
                  disabled={!editable}
                />
              </label>
              <label>
                对前文的更正（每行一项）
                <textarea
                  rows={2}
                  value={value.corrections.join("\n")}
                  onChange={(event) =>
                    controller.update({ corrections: event.target.value.split("\n") })
                  }
                  disabled={!editable}
                />
              </label>
            </div>
          </details>
          <div className="composer-toolbar">
            <label className="file-picker">
              <span>＋ 选择图片</span>
              <input
                aria-label="选择待确认的图片"
                type="file"
                multiple
                accept="image/png,image/jpeg,image/webp"
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
                  new Set([configuredModel, "deepseek-flash", "deepseek-v4-pro"].filter(Boolean)),
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
                {["none", "low", "high", "max"].map((entry) => (
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
                Boolean(candidate)
              }
            >
              {busy ? "处理中…" : "发送"}
            </Button>
          </div>
          {model === "deepseek-v4-pro" && (
            <p className="composer-hint">仅支持文字；包含题图的对话请使用支持图片的模型。</p>
          )}
        </form>
        <div className="draft-save-status">
          <span role="status">
            {draft.saving
              ? "正在保存草稿…"
              : draft.dirty
                ? "草稿待保存"
                : draft.ready
                  ? "草稿已保存"
                  : "正在核对草稿…"}
          </span>
          <Button
            variant="ghost"
            size="sm"
            disabled={!draft.ready || draft.saving || busy}
            onClick={() => void controller.flush().catch(() => undefined)}
          >
            保存草稿
          </Button>
        </div>
        <p className="composer-hint">
          {locked
            ? "当前生成尚未结束或结果未知。可停止或核对后继续追问。"
            : "仅发送确认选区与输入 · Ctrl+Enter 发送 · 切换课程后台继续"}
        </p>
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
