import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties, FormEvent, PointerEvent } from "react";
import { api, errorMessage } from "./api/client";
import type { Schema } from "./api/client";
import { Button } from "./components/ui/button";
import { ChatWorkspace } from "./chat/ChatWorkspace";
import type { ChatWorkspaceHandle } from "./chat/ChatWorkspace";
import { VideoWorkspace } from "./media/VideoWorkspace";
import type { VideoWorkspaceHandle } from "./media/VideoWorkspace";
import type { CaptureSource } from "./media/capture";

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
  const [dark, setDark] = useState(() => localStorage.getItem("learning.theme") === "dark");
  const [sidebarVisible, setSidebarVisible] = useState(true);
  const [chatVisible, setChatVisible] = useState(true);
  const [chatWidth, setChatWidth] = useState(43);
  const [immersive, setImmersive] = useState(false);
  const [capture, setCapture] = useState<CaptureSource>();
  const [exitStatus, setExitStatus] = useState("");
  const [exitConfirmation, setExitConfirmation] = useState<Schema<"RuntimeResponse"> | null>(null);
  const [exiting, setExiting] = useState(false);
  const courseEpoch = useRef(0);
  const courseChats = useRef(new Map<string, string>());
  const creatingChats = useRef(new Map<string, Promise<Schema<"ChatResponse">>>());
  const selectedCourse = useRef("");
  const videoRef = useRef<VideoWorkspaceHandle>(null);
  const chatRef = useRef<ChatWorkspaceHandle>(null);
  const studyColumns = useRef<HTMLDivElement>(null);
  const resizeStart = useRef<{ x: number; width: number; percent: number } | null>(null);

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
            if (!exiting) setError("后台连接中断，已保存内容仍保留；请重新连接");
          }
        });
    }, 5_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [exiting]);

  const chooseCourse = async (id: string) => {
    const epoch = ++courseEpoch.current;
    try {
      await videoRef.current?.savePosition();
      await chatRef.current?.saveDraft();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : errorMessage(reason));
      return;
    }
    if (epoch !== courseEpoch.current) return;
    selectedCourse.current = id;
    setCourseId(id);
    setChatId(courseChats.current.get(id) || "");
    setChats([]);
    setCapture(undefined);
    setImmersive(false);
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

  const newChat = async (id: string, force = false) => {
    if (!force && courseChats.current.has(id)) return courseChats.current.get(id)!;
    let pending = creatingChats.current.get(id);
    if (!pending) {
      pending = api.write<Schema<"ChatResponse">>(`/courses/${id}/chats`);
      creatingChats.current.set(id, pending);
    }
    try {
      const chat = await pending;
      courseChats.current.set(id, chat.chat_id);
      if (selectedCourse.current === id) {
        setChats((values) =>
          values.some((value) => value.chat_id === chat.chat_id) ? values : [...values, chat],
        );
        setChatId(chat.chat_id);
      }
      return chat.chat_id;
    } finally {
      creatingChats.current.delete(id);
    }
  };

  const createChat = async () => {
    setBusy(true);
    try {
      await chatRef.current?.saveDraft();
      await newChat(courseId, true);
      setCapture(undefined);
      setChatVisible(true);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const openChat = async (source?: CaptureSource) => {
    if (!courseId) return;
    try {
      await newChat(courseId);
      if (selectedCourse.current !== courseId) return;
      setChatVisible(true);
      if (source) setCapture(source);
    } catch (reason) {
      setError(errorMessage(reason));
    }
  };

  const selectChat = async (id: string) => {
    try {
      await chatRef.current?.saveDraft();
      courseChats.current.set(courseId, id);
      setChatId(id);
      setCapture(undefined);
      setChatVisible(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : errorMessage(reason));
    }
  };

  const closeChat = async () => {
    setChatVisible(false);
    if (immersive) await videoRef.current?.resumeFullscreen();
  };
  const resize = (event: PointerEvent<HTMLDivElement>) => {
    const start = resizeStart.current;
    if (!start) return;
    setChatWidth(
      Math.min(65, Math.max(28, start.percent - ((event.clientX - start.x) / start.width) * 100)),
    );
  };

  const requestExit = async (confirmed?: Schema<"RuntimeResponse">) => {
    setBusy(true);
    try {
      await videoRef.current?.savePosition();
      await chatRef.current?.saveDraft();
      const runtime = confirmed || (await api.read<Schema<"RuntimeResponse">>("/runtime"));
      if (!runtime.can_exit) {
        setExitStatus("当前后台不由统一入口管理，请在所属启动终端中退出。");
        return;
      }
      if (runtime.active_generations.length && !confirmed) {
        setExitConfirmation(runtime);
        return;
      }
      await api.write("/runtime/exit", {
        instance_id: runtime.instance_id,
        confirm_active: Boolean(confirmed),
      });
      setExiting(true);
      setExitConfirmation(null);
      setExitStatus("已保存并请求安全退出，可关闭本页。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  const ignoreLibraryChange = useCallback(() => {}, []);

  return (
    <div className={dark ? "app-shell dark" : "app-shell"}>
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            学
          </span>
          <div>
            <h1>AI 辅助学习</h1>
            <p>边看，边问，慢慢弄明白。</p>
          </div>
        </div>
        <div className="header-actions">
          <span className={`connection-badge ${health?.ready ? "connected" : ""}`} role="status">
            <span aria-hidden="true" />
            {connecting ? "正在连接后台" : health?.ready ? "后台已连接" : "后台未连接"}
          </span>
          <Button
            variant="ghost"
            onClick={() => {
              setDark((value) => !value);
              localStorage.setItem("learning.theme", dark ? "light" : "dark");
            }}
          >
            {dark ? "浅色" : "深色"}
          </Button>
          <Button
            variant="outline"
            disabled={connecting || exiting}
            onClick={() => {
              setConnecting(true);
              setError("");
              void connect();
            }}
          >
            重新连接
          </Button>
          <Button
            variant="ghost"
            disabled={!health?.ready || busy || exiting}
            onClick={() => void requestExit()}
          >
            安全退出
          </Button>
        </div>
      </header>
      <div className="workspace-toolbar">
        <Button
          variant="ghost"
          onClick={() => {
            setSidebarVisible((value) => !value);
            setImmersive(false);
          }}
        >
          {sidebarVisible && !immersive ? "收起课程目录" : "展开课程目录"}
        </Button>
        <span>
          {courses.find((value) => value.course_id === courseId)?.title || "我的学习空间"}
        </span>
        {!chatVisible && (
          <Button variant="outline" disabled={!courseId} onClick={() => void openChat()}>
            展开聊天
          </Button>
        )}
        <small>关闭网页后，后台继续回答</small>
      </div>
      {error && (
        <div className="global-error" role="alert">
          {error}
        </div>
      )}
      {exitStatus && (
        <div className="global-notice" role="status">
          {exitStatus}
        </div>
      )}
      <div
        className={`workspace learning-workspace ${sidebarVisible && !immersive ? "" : "directory-collapsed"}`}
      >
        <aside className="sidebar" aria-label="课程与对话" hidden={!sidebarVisible || immersive}>
          <section>
            <h2>课程</h2>
            <label className="sr-only" htmlFor="course-select">
              选择课程
            </label>
            <select
              id="course-select"
              value={courseId}
              disabled={!health?.ready || exiting}
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
                disabled={!health?.ready || busy || exiting}
              />
              <Button
                type="submit"
                variant="outline"
                disabled={!health?.ready || busy || !title.trim() || exiting}
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
                disabled={!courseId || busy || exiting}
                onClick={() => void createChat()}
              >
                ＋ 新对话
              </Button>
            </div>
            {!courseId ? (
              <p className="muted">先选择一门课程。</p>
            ) : chats.length ? (
              <ul>
                {chats.map((chat, index) => (
                  <li key={chat.chat_id}>
                    <button
                      className={chatId === chat.chat_id ? "chat-link selected" : "chat-link"}
                      onClick={() => void selectChat(chat.chat_id)}
                    >
                      <span>对话 {index + 1}</span>
                      <small>{chat.chat_id.slice(0, 8)}</small>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">截图或展开问答即可开始新对话，也可稍后从这里重开历史。</p>
            )}
          </section>
          <details className="capabilities">
            <summary>连接与能力</summary>
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
          </details>
        </aside>
        <main
          className={`study-columns ${chatVisible ? "" : "chat-collapsed"}`}
          ref={studyColumns}
          style={{ "--chat-width": `${chatWidth}%` } as CSSProperties}
        >
          <div className="media-panel main-panel">
            {courseId ? (
              <VideoWorkspace
                key={courseId}
                ref={videoRef}
                courseId={courseId}
                connected={Boolean(health?.ready) && !exiting}
                onLibraryChange={ignoreLibraryChange}
                onImmersiveChange={setImmersive}
                onOpenChat={() => void openChat()}
                onCapture={(source) => void openChat(source)}
              />
            ) : (
              <div className="empty-state">
                <span className="empty-mark" aria-hidden="true">
                  ▷
                </span>
                <h2>留在课程里，把问题问清楚</h2>
                <p>选择或新建课程，再关联本地视频。从当前画面截取题目，确认后直接开始问答。</p>
                <div className="empty-detail">
                  <span>原件原地播放</span>
                  <span>确认选区提问</span>
                  <span>随时接续聊天</span>
                </div>
              </div>
            )}
          </div>
          {chatVisible && (
            <div
              className="panel-resizer"
              role="separator"
              aria-label="调整视频与聊天宽度"
              aria-orientation="vertical"
              aria-valuenow={Math.round(chatWidth)}
              aria-valuemin={28}
              aria-valuemax={65}
              tabIndex={0}
              onPointerDown={(event) => {
                const width = studyColumns.current?.getBoundingClientRect().width || 1;
                resizeStart.current = { x: event.clientX, width, percent: chatWidth };
                event.currentTarget.setPointerCapture(event.pointerId);
              }}
              onPointerMove={resize}
              onPointerUp={(event) => {
                resizeStart.current = null;
                event.currentTarget.releasePointerCapture(event.pointerId);
              }}
              onKeyDown={(event) => {
                if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                  event.preventDefault();
                  setChatWidth((value) =>
                    Math.max(28, Math.min(65, value + (event.key === "ArrowLeft" ? 2 : -2))),
                  );
                }
              }}
            >
              <span />
            </div>
          )}
          <div className="chat-panel main-panel" hidden={!chatVisible}>
            <div className="chat-panel-toolbar">
              <span>学习问答</span>
              <Button variant="ghost" onClick={() => void closeChat()}>
                收起聊天
              </Button>
            </div>
            {chatId ? (
              <ChatWorkspace
                key={chatId}
                ref={chatRef}
                chatId={chatId}
                canSubmit={health?.capabilities.question_answering === "ready" && !exiting}
                connected={Boolean(health?.ready)}
                configuredModel={health?.configured_model}
                configuredThinking={health?.configured_thinking}
                incomingCapture={capture}
                onCaptureConsumed={() => setCapture(undefined)}
                onOpenSource={(source) => void videoRef.current?.openSource(source)}
              />
            ) : (
              <div className="chat-welcome">
                <span className="eyebrow">把疑问留在这里</span>
                <h2>这一处，为什么？</h2>
                <p>截图确认题目，或直接输入问题。只会提交你确认的图片、文字与相关聊天历史。</p>
                <Button disabled={!courseId || busy || exiting} onClick={() => void createChat()}>
                  开始问答
                </Button>
              </div>
            )}
          </div>
        </main>
      </div>
      {exitConfirmation && (
        <div className="exit-overlay">
          <section
            className="exit-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="exit-title"
          >
            <h2 id="exit-title">停止回答并退出后台？</h2>
            <p>
              还有 {exitConfirmation.active_generations.length}{" "}
              个回答在进行。确认后会保存已收到的内容，停止所属生成并退出。
            </p>
            <div className="button-row">
              <Button variant="outline" onClick={() => setExitConfirmation(null)}>
                继续学习
              </Button>
              <Button disabled={busy} onClick={() => void requestExit(exitConfirmation)}>
                保存并退出
              </Button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
