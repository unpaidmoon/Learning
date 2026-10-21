import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api, errorMessage } from "./api/client";
import type { Schema } from "./api/client";
import { Button } from "./components/ui/button";
import { ChatWorkspace } from "./chat/ChatWorkspace";

const capabilityNames: Record<keyof Schema<"Capabilities">, string> = {
  harness: "Harness 执行层",
  question_answering: "图文问答",
  persistence: "业务保存",
  video: "视频学习",
  transcription: "本地字幕",
  knowledge_base: "知识库",
};

const capabilityLabels = {
  ready: "已连接",
  unavailable: "不可用",
  not_configured: "未配置",
  not_implemented: "未接入",
};

async function loadConnection(signal?: AbortSignal) {
  const health = await api.read<Schema<"HealthResponse">>("/health", signal);
  if (!health.ready) throw new Error("后台尚未就绪");
  await api.connect(signal);
  const courses = await api.read<Schema<"CourseResponse">[]>("/courses", signal);
  return { health, courses };
}

export function App() {
  const [health, setHealth] = useState<Schema<"HealthResponse"> | null>(null);
  const [connecting, setConnecting] = useState(true);
  const [error, setError] = useState("");
  const [courses, setCourses] = useState<Schema<"CourseResponse">[]>([]);
  const [courseId, setCourseId] = useState("");
  const [chats, setChats] = useState<Schema<"ChatResponse">[]>([]);
  const [chatId, setChatId] = useState("");
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [dark, setDark] = useState(false);
  const courseEpoch = useRef(0);

  const connect = async () => {
    try {
      const value = await loadConnection();
      setHealth(value.health);
      setCourses(value.courses);
    } catch (reason) {
      setHealth(null);
      setError(errorMessage(reason));
    } finally {
      setConnecting(false);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    void loadConnection(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setHealth(value.health);
          setCourses(value.courses);
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) {
          setHealth(null);
          setError(errorMessage(reason));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setConnecting(false);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      void api
        .read<Schema<"HealthResponse">>("/health", controller.signal)
        .then((value) => setHealth(value.ready ? value : null))
        .catch(() => {
          if (!controller.signal.aborted) {
            setHealth(null);
            setError("后台连接中断，已保存内容仍保留；请重新连接");
          }
        });
    }, 5_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);

  const chooseCourse = async (id: string) => {
    const epoch = ++courseEpoch.current;
    setCourseId(id);
    setChatId("");
    setChats([]);
    setError("");
    if (!id) return;
    try {
      const next = await api.read<Schema<"ChatResponse">[]>(`/courses/${id}/chats`);
      if (epoch === courseEpoch.current) setChats(next);
    } catch (reason) {
      if (epoch === courseEpoch.current) setError(errorMessage(reason));
    }
  };

  const createCourse = async (event: FormEvent) => {
    event.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    try {
      const course = await api.write<Schema<"CourseResponse">>("/courses", {
        title: title.trim(),
      } satisfies Schema<"CourseInput">);
      setCourses((values) => [...values, course]);
      setTitle("");
      await chooseCourse(course.course_id);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const createChat = async () => {
    setBusy(true);
    try {
      const chat = await api.write<Schema<"ChatResponse">>(`/courses/${courseId}/chats`);
      setChats((values) => [...values, chat]);
      setChatId(chat.chat_id);
      setError("");
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={dark ? "app-shell dark" : "app-shell"}>
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            学
          </span>
          <div>
            <h1>AI 辅助学习</h1>
            <p>P0 工程与持久化验证界面</p>
          </div>
        </div>
        <div className="header-actions">
          <span className={`connection-badge ${health?.ready ? "connected" : ""}`} role="status">
            <span aria-hidden="true" />
            {connecting ? "正在连接后台" : health?.ready ? "后台已连接" : "后台未连接"}
          </span>
          <Button variant="ghost" onClick={() => setDark((value) => !value)}>
            {dark ? "浅色" : "深色"}
          </Button>
          <Button
            variant="outline"
            disabled={connecting}
            onClick={() => {
              setConnecting(true);
              setError("");
              void connect();
            }}
          >
            重新连接
          </Button>
        </div>
      </header>
      <div className="scope-notice">
        当前仅验收工程、执行与保存。视频截图学习闭环将在后续阶段实现。
      </div>
      {error && (
        <div className="global-error" role="alert">
          {error}
        </div>
      )}
      <div className="workspace">
        <aside className="sidebar" aria-label="课程与对话">
          <section>
            <h2>课程</h2>
            <label className="sr-only" htmlFor="course-select">
              选择课程
            </label>
            <select
              id="course-select"
              value={courseId}
              disabled={!health?.ready}
              onChange={(event) => void chooseCourse(event.target.value)}
            >
              <option value="">请选择课程</option>
              {courses.map((course) => (
                <option key={course.course_id} value={course.course_id}>
                  {course.title}
                </option>
              ))}
            </select>
            <form className="course-form" onSubmit={(event) => void createCourse(event)}>
              <label className="sr-only" htmlFor="course-title">
                新课程名称
              </label>
              <input
                id="course-title"
                placeholder="新课程名称"
                maxLength={200}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                disabled={!health?.ready || busy}
              />
              <Button
                type="submit"
                variant="outline"
                disabled={!health?.ready || busy || !title.trim()}
              >
                新建
              </Button>
            </form>
          </section>
          <section className="chat-nav">
            <div className="section-row">
              <h2>对话记录</h2>
              <Button
                variant="ghost"
                disabled={!courseId || busy}
                onClick={() => void createChat()}
              >
                ＋ 新对话
              </Button>
            </div>
            {!courseId ? (
              <p className="muted">选择课程后新建或接续历史。</p>
            ) : chats.length ? (
              <ul>
                {chats.map((chat, index) => (
                  <li key={chat.chat_id}>
                    <button
                      className={chatId === chat.chat_id ? "chat-link selected" : "chat-link"}
                      onClick={() => setChatId(chat.chat_id)}
                    >
                      <span>对话 {index + 1}</span>
                      <small>{chat.chat_id.slice(0, 8)}</small>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">暂无对话，可以新建一段验证。</p>
            )}
          </section>
          <section className="capabilities" aria-label="实际能力状态">
            <h2>能力状态</h2>
            {Object.entries(capabilityNames).map(([name, label]) => {
              const state = health?.capabilities[name as keyof Schema<"Capabilities">];
              return (
                <div className="capability-row" key={name}>
                  <span>{label}</span>
                  <span className={state === "ready" ? "available" : "muted"}>
                    {state ? capabilityLabels[state] : "状态未知"}
                  </span>
                </div>
              );
            })}
          </section>
        </aside>
        <main className="main-panel">
          {chatId ? (
            <ChatWorkspace
              key={chatId}
              chatId={chatId}
              canSubmit={health?.capabilities.question_answering === "ready"}
              connected={Boolean(health?.ready)}
              configuredModel={health?.configured_model}
              configuredThinking={health?.configured_thinking}
            />
          ) : (
            <div className="empty-state">
              <span className="empty-mark" aria-hidden="true">
                学
              </span>
              <h2>从一段对话开始验证</h2>
              <p>选择或新建课程，再新建对话。仅在你确认并发送后，AI 才会收到文字与图片。</p>
              <div className="empty-detail">
                <span>实际增量与停止</span>
                <span>保存与重开</span>
                <span>执行未知时核对</span>
              </div>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
