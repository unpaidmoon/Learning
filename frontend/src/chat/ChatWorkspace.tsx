import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { ApiError, api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { generationActive, stateLabel } from "./state";

const storageKey = (chatId: string) => `learning.pending.${chatId}`;

function readPending(chatId: string): Schema<"SubmissionInput"> | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey(chatId)) || "null");
    return value?.chat_id === chatId && typeof value?.submission_id === "string" ? value : null;
  } catch {
    return null;
  }
}

export function ChatWorkspace({
  chatId,
  connected,
  canSubmit,
  configuredModel,
  configuredThinking,
}: {
  chatId: string;
  connected: boolean;
  canSubmit: boolean;
  configuredModel?: string;
  configuredThinking?: string;
}) {
  const [chat, setChat] = useState<Schema<"ChatResponse"> | null>(null);
  const [pending, setPending] = useState(() => readPending(chatId));
  const [retryAllowed, setRetryAllowed] = useState(false);
  const [text, setText] = useState(pending?.text || "");
  const [conditions, setConditions] = useState(pending?.conditions?.join("\n") || "");
  const [corrections, setCorrections] = useState(pending?.corrections?.join("\n") || "");
  const [model, setModel] = useState(pending?.model || "deepseek-flash");
  const [thinking, setThinking] = useState<Schema<"SubmissionInput">["thinking"]>(
    pending?.thinking || "high",
  );
  const [attachments, setAttachments] = useState<Schema<"AttachmentResponse">[]>([]);
  const [candidate, setCandidate] = useState<{ file: File; url: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [acceptedGeneration, setAcceptedGeneration] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let timer = 0;
    const refresh = async () => {
      try {
        const value = await api.read<Schema<"ChatResponse">>(`/chats/${chatId}`, controller.signal);
        if (value.chat_id !== chatId) throw new Error("对话身份不匹配");
        if (!controller.signal.aborted) {
          setChat(value);
          setAcceptedGeneration((current) =>
            value.generations?.some((generation) => generation.generation_id === current)
              ? ""
              : current,
          );
        }
      } catch (reason) {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(() => void refresh(), 500);
      }
    };
    void refresh();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [chatId]);

  useEffect(() => {
    return () => {
      if (candidate) URL.revokeObjectURL(candidate.url);
    };
  }, [candidate]);

  const generations = chat?.generations || [];
  const locked =
    Boolean(acceptedGeneration) ||
    generations.some(
      (generation) => generationActive(generation) || generation.state === "unknown",
    );

  const send = async (value: Schema<"SubmissionInput">) => {
    setBusy(true);
    setError("");
    setRetryAllowed(false);
    let receiptKnown = false;
    try {
      // Save the business identity before the first dispatch. Browser refresh never creates a retry.
      sessionStorage.setItem(storageKey(chatId), JSON.stringify(value));
      setPending(value);
      const receipt = await api.write<Schema<"SubmissionResponse">>("/submissions", value);
      receiptKnown = true;
      setAcceptedGeneration(receipt.generation_id);
      sessionStorage.removeItem(storageKey(chatId));
      setPending(null);
      setText("");
      setConditions("");
      setCorrections("");
      setAttachments([]);
      setChat(await api.read<Schema<"ChatResponse">>(`/chats/${chatId}`));
    } catch (reason) {
      if (receiptKnown) {
        setError("提交已接受，最新内容暂时无法读取。请重新连接后查看，不会重复发送。");
      } else if (
        reason instanceof ApiError &&
        reason.status &&
        reason.status >= 400 &&
        reason.status < 500
      ) {
        // Explicit application rejection is not a lost receipt. Keep the draft editable.
        sessionStorage.removeItem(storageKey(chatId));
        setPending(null);
        setError(errorMessage(reason));
      } else {
        setError(`${errorMessage(reason)}。提交状态需核对，请保留当前输入。`);
      }
    } finally {
      setBusy(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit || pending || locked || !text.trim() || busy) return;
    void send({
      chat_id: chatId,
      submission_id: crypto.randomUUID(),
      text,
      attachment_ids: attachments.map((attachment) => attachment.attachment_id),
      conditions: conditions.split("\n").filter((value) => value.trim()),
      corrections: corrections.split("\n").filter((value) => value.trim()),
      model,
      thinking,
    });
  };

  const lookup = async () => {
    if (!pending) return;
    setBusy(true);
    setError("");
    try {
      await api.read<Schema<"SubmissionResponse">>(`/submissions/${pending.submission_id}`);
      sessionStorage.removeItem(storageKey(chatId));
      setPending(null);
      setText("");
      setConditions("");
      setCorrections("");
      setAttachments([]);
      setChat(await api.read<Schema<"ChatResponse">>(`/chats/${chatId}`));
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) {
        setRetryAllowed(true);
        setError("后台未找到这次提交。可主动重新发送同一次提交，身份保持不变。");
      } else {
        setError(errorMessage(reason));
      }
    } finally {
      setBusy(false);
    }
  };

  const command = async (generationId: string, action: "cancel" | "reconcile") => {
    setBusy(true);
    setError("");
    try {
      const value = await api.write<Schema<"GenerationResponse">>(
        `/generations/${generationId}/${action}`,
      );
      setChat((current) =>
        current
          ? {
              ...current,
              generations: (current.generations || []).map((generation) =>
                generation.generation_id === value.generation_id ? value : generation,
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

  const confirmAttachment = async () => {
    if (!candidate || attachments.length >= 8) return;
    setBusy(true);
    setError("");
    try {
      const dataUrl = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as string);
        reader.onerror = () => reject(new Error("无法读取图片"));
        reader.readAsDataURL(candidate.file);
      });
      const value = await api.write<Schema<"AttachmentResponse">>("/attachments", {
        data_base64: dataUrl.split(",")[1],
        media_type: candidate.file.type as Schema<"AttachmentInput">["media_type"],
        confirmed: true,
      } satisfies Schema<"AttachmentInput">);
      setAttachments((current) => [...current, value]);
      setCandidate(null);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="chat-workspace">
      <div className="chat-heading">
        <div>
          <h2>课程对话</h2>
          <p>文字与确认图片 · 真实执行 · 业务保存</p>
          {configuredModel && (
            <p>
              当前执行配置：{configuredModel} / {configuredThinking}。更改需修改后台配置并重启。
            </p>
          )}
        </div>
        <small className="muted">{chatId.slice(0, 8)}</small>
      </div>
      <div className="messages" aria-label="对话内容">
        {!chat ? (
          <p className="muted" role="status">
            正在读取已保存记录…
          </p>
        ) : !chat.messages?.length ? (
          <div className="conversation-empty">
            <h3>写下要验证的问题</h3>
            <p>可以主动选择图片，确认加入后再发送。未发送的图片不会交给 AI。</p>
          </div>
        ) : (
          chat.messages.map((message) => {
            const generation =
              message.role === "assistant"
                ? generations.find((value) => value.generation_id === message.generation_id)
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
                  <pre className="message-body">{body || "正在等待内容…"}</pre>
                  {Boolean(message.attachment_ids?.length) && (
                    <div className="message-attachments">
                      {message.attachment_ids?.map((id, index) => (
                        <img
                          key={id}
                          src={`/api/v1/attachments/${id}`}
                          alt={`已确认提交的图片 ${index + 1}`}
                          loading="lazy"
                        />
                      ))}
                    </div>
                  )}
                  {generation && message.role === "assistant" && (
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
        {pending && (
          <div className="pending-notice" role="status">
            <span>这次提交的回执尚未确认。先核对，不会自动重发。</span>
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
        <form className="composer" onSubmit={submit}>
          {attachments.length > 0 && (
            <div className="draft-attachments" aria-label="本次确认图片">
              {attachments.map((attachment, index) => (
                <figure key={attachment.attachment_id}>
                  <img
                    src={`/api/v1/attachments/${attachment.attachment_id}`}
                    alt={`待发送的确认图片 ${index + 1}`}
                  />
                  <figcaption>
                    图片 {index + 1}
                    <button
                      type="button"
                      disabled={busy || Boolean(pending)}
                      onClick={() =>
                        setAttachments((current) =>
                          current.filter(
                            (value) => value.attachment_id !== attachment.attachment_id,
                          ),
                        )
                      }
                    >
                      移除
                    </button>
                  </figcaption>
                </figure>
              ))}
            </div>
          )}
          {candidate && (
            <div className="attachment-confirmation">
              <img src={candidate.url} alt="待确认的所选图片，确认后完整加入本次输入" />
              <div>
                <strong>确认这张图片的范围</strong>
                <p>仅加入当前看到的所选图片。发送问题时才会提交给 AI。</p>
                <div className="button-row">
                  <Button type="button" disabled={busy} onClick={() => void confirmAttachment()}>
                    确认加入
                  </Button>
                  <Button type="button" variant="ghost" onClick={() => setCandidate(null)}>
                    取消
                  </Button>
                </div>
              </div>
            </div>
          )}
          <label className="sr-only" htmlFor="question-input">
            问题或追问
          </label>
          <textarea
            id="question-input"
            placeholder="输入问题或继续追问…"
            rows={3}
            value={text}
            onChange={(event) => setText(event.target.value)}
            disabled={busy || Boolean(pending)}
          />
          <details className="input-context">
            <summary>题目条件与更正（按需填写）</summary>
            <div className="context-fields">
              <label>
                当前题目条件（每行一项）
                <textarea
                  rows={2}
                  value={conditions}
                  onChange={(event) => setConditions(event.target.value)}
                  disabled={busy || Boolean(pending)}
                />
              </label>
              <label>
                对前文的更正（每行一项）
                <textarea
                  rows={2}
                  value={corrections}
                  onChange={(event) => setCorrections(event.target.value)}
                  disabled={busy || Boolean(pending)}
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
                accept="image/png,image/jpeg,image/webp"
                disabled={busy || Boolean(pending) || attachments.length >= 8}
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  event.target.value = "";
                  if (!file) return;
                  if (
                    !["image/png", "image/jpeg", "image/webp"].includes(file.type) ||
                    file.size > 10_000_000
                  ) {
                    setError("请选择不超过 10MB 的 PNG、JPEG 或 WebP 图片");
                    return;
                  }
                  setCandidate({ file, url: URL.createObjectURL(file) });
                }}
              />
            </label>
            <label className="model-field">
              模型
              <input
                aria-label="模型名称"
                value={model}
                onChange={(event) => setModel(event.target.value)}
                disabled={busy || Boolean(pending)}
              />
            </label>
            <label className="thinking-field">
              思考
              <select
                aria-label="思考强度"
                value={thinking}
                onChange={(event) =>
                  setThinking(event.target.value as Schema<"SubmissionInput">["thinking"])
                }
                disabled={busy || Boolean(pending)}
              >
                {["none", "low", "medium", "high", "xhigh", "max"].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <Button
              type="submit"
              disabled={
                !canSubmit ||
                !connected ||
                busy ||
                Boolean(pending) ||
                locked ||
                !text.trim() ||
                !model.trim() ||
                Boolean(candidate)
              }
            >
              {busy ? "处理中…" : "发送"}
            </Button>
          </div>
        </form>
        <p className="composer-hint">
          {locked
            ? "当前生成尚未结束或结果未知。可停止或核对后继续追问。"
            : "只发送已确认的图片与输入；切换课程不会停止后台生成。"}
        </p>
      </div>
    </div>
  );
}
