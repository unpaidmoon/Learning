import { useState } from "react";
import { MarkdownAnswer } from "../chat/MarkdownAnswer";
import type { OfflineChatIndex, OfflineChatPage } from "./model";
import { localAttachment, localChat, localSibling } from "./model";
import "./offlineChat.css";

const states: Record<string, string> = {
  completed: "已完成",
  cancelled: "已停止，保留已有内容",
  truncated: "输出截断，保留已有内容",
  unknown: "原结果待核对",
  pending: "原请求尚待处理",
  running: "导出时尚在生成",
  accepted: "原请求已接受",
  failed: "原请求未完成",
  published: "已保存",
  saved: "已保存",
  submitted: "已提交",
};

function savedTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

export function OfflineChatReader({ document }: { document: OfflineChatPage | OfflineChatIndex }) {
  const [start, setStart] = useState(0);
  if (document.format === "learning-offline-chat-index-v1") {
    return (
      <main className="offline-chat">
        <h1>聊天记录</h1>
        <p>离线阅读，不需要软件后台。来源原件未随包时，回看原件需重新定位。</p>
        <p>导出时间：{savedTime(document.exported_at)}</p>
        <nav aria-label="课程与聊天">
          {document.courses.map((course) => (
            <section key={course.id}>
              <h2>{course.title}</h2>
              {course.chats.length === 0 && <p>此课程没有聊天记录。</p>}
              <ul>
                {course.chats.map((chat, index) => (
                  <li key={index}>
                    {localChat(chat.path) ? (
                      <a href={localChat(chat.path)}>{chat.title}</a>
                    ) : (
                      <span>{chat.title}（链接不可用）</span>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </nav>
      </main>
    );
  }
  return (
    <main className="offline-chat">
      <header>
        <a href="../index.html">返回课程与聊天</a>
        <p>{document.course}</p>
        <h1>{document.title}</h1>
        <p>离线记录，仅供阅读。原结果状态保留，不会重新调用模型。</p>
      </header>
      <section aria-label="已保存消息">
        {document.messages.slice(start, start + 100).map((message) => (
          <article key={message.id} className="offline-message">
            <h2>{message.role === "user" ? "你" : message.role === "assistant" ? "AI" : "记录"}</h2>
            <p className="offline-message-status">
              {savedTime(message.created_at)} · {states[message.status] ?? "已保存"}
            </p>
            {message.text ? <MarkdownAnswer text={message.text} /> : <p>此时尚无已保存正文。</p>}
            {message.reasoning && (
              <details>
                <summary>已保存的思考内容</summary>
                <MarkdownAnswer text={message.reasoning} />
              </details>
            )}
            {!!message.attachments.length && (
              <ul aria-label="消息附件">
                {message.attachments.map((attachment, index) => {
                  const path = localAttachment(attachment.path);
                  return (
                    <li key={index}>
                      {path &&
                      ["image/png", "image/jpeg", "image/webp"].includes(attachment.media_type) ? (
                        <figure>
                          <img src={path} alt={attachment.filename} loading="lazy" />
                          <figcaption>{attachment.filename}</figcaption>
                        </figure>
                      ) : path ? (
                        <a href={path} download={attachment.filename}>
                          {attachment.filename}
                        </a>
                      ) : (
                        <span>{attachment.filename}（附件路径不可用）</span>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
            {!!message.sources.length && (
              <details>
                <summary>来源</summary>
                <ul>
                  {message.sources.map((label, index) => (
                    <li key={index}>{label}</li>
                  ))}
                </ul>
              </details>
            )}
          </article>
        ))}
      </section>
      {document.navigation && (
        <nav aria-label="聊天分页">
          {localSibling(document.navigation.previous) && (
            <a href={localSibling(document.navigation.previous)}>上一页消息</a>
          )}
          <span>
            {document.navigation.first}—{document.navigation.last} / {document.navigation.total}
          </span>
          {localSibling(document.navigation.next) && (
            <a href={localSibling(document.navigation.next)}>下一页消息</a>
          )}
        </nav>
      )}
      {document.messages.length > 100 && (
        <nav aria-label="消息分页">
          <button disabled={start === 0} onClick={() => setStart(0)}>
            最早消息
          </button>
          <button disabled={start === 0} onClick={() => setStart((old) => Math.max(0, old - 100))}>
            上一页
          </button>
          <span>
            {start + 1}—{Math.min(start + 100, document.messages.length)} /{" "}
            {document.messages.length}
          </span>
          <button
            disabled={start + 100 >= document.messages.length}
            onClick={() => setStart((old) => old + 100)}
          >
            下一页
          </button>
          <button
            disabled={start + 100 >= document.messages.length}
            onClick={() => setStart(Math.floor((document.messages.length - 1) / 100) * 100)}
          >
            最新消息
          </button>
        </nav>
      )}
    </main>
  );
}
