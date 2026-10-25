import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties, PointerEvent } from "react";
import { api, errorMessage } from "./api/client";
import type { Schema } from "./api/client";
import { Button } from "./components/ui/button";
import { ChatWorkspace } from "./chat/ChatWorkspace";
import type { ChatWorkspaceHandle } from "./chat/ChatWorkspace";
import { VideoWorkspace } from "./media/VideoWorkspace";
import type { VideoWorkspaceHandle } from "./media/VideoWorkspace";
import type { CaptureResult, CaptureSource } from "./media/capture";
import { WorkspaceCapture } from "./media/WorkspaceCapture";
import { AccountPanel } from "./account/AccountPanel";
import { CourseExplorer } from "./library/CourseExplorer";
import { FileManager } from "./library/FileManager";
import { ResourceViewer } from "./library/ResourceViewer";
import { Icon } from "./components/ui/Icon";

type StudyTab = {
  id: string;
  title: string;
  file?: Schema<"CourseFileOpenResponse">;
  video?: Schema<"MediaAssetResponse">;
  timeMs?: number;
};

const FILES_TAB = "files";

const capabilityNames: Record<keyof Schema<"Capabilities">, string> = {
  harness: "Harness 执行层",
  question_answering: "图文问答",
  persistence: "业务保存",
  video: "视频学习",
  transcription: "本地字幕",
  knowledge_base: "知识库",
};
const capabilityLabels = {
  available: "可用",
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
  const [executionViewOnly] = useState(
    () => new URLSearchParams(location.search).get("read_only") === "1",
  );
  const [health, setHealth] = useState<Schema<"HealthResponse"> | null>(null);
  const [connecting, setConnecting] = useState(true);
  const [error, setError] = useState("");
  const [courses, setCourses] = useState<Schema<"CourseResponse">[]>([]);
  const [courseId, setCourseId] = useState("");
  const [chats, setChats] = useState<Schema<"ChatResponse">[]>([]);
  const [chatId, setChatId] = useState("");
  const [busy, setBusy] = useState(false);
  const [dark, setDark] = useState(() => localStorage.getItem("learning.theme") === "dark");
  const [chatVisible, setChatVisible] = useState(true);
  const [chatWidth, setChatWidth] = useState(35);
  const [immersive, setImmersive] = useState(false);
  const [capture, setCapture] = useState<CaptureSource>();
  const [confirmedCapture, setConfirmedCapture] = useState<CaptureResult>();
  const [capturing, setCapturing] = useState(false);
  const [captureRoot, setCaptureRoot] = useState<HTMLElement | null>(null);
  const [accountOpen, setAccountOpen] = useState(false);
  const [account, setAccount] = useState<Schema<"AccountResponse"> | null>(null);
  const [courseDialog, setCourseDialog] = useState(false);
  const [folderPath, setFolderPath] = useState("");
  const [courseName, setCourseName] = useState("");
  const [pickingFolder, setPickingFolder] = useState(false);
  const [sidebarVisible, setSidebarVisible] = useState(
    () => localStorage.getItem("learning.sidebar-visible") !== "false",
  );
  const [filePath, setFilePath] = useState("");
  const [tabs, setTabs] = useState<StudyTab[]>([]);
  const [activeTabId, setActiveTabId] = useState(FILES_TAB);
  const tabsRef = useRef<StudyTab[]>([]);
  const [libraryRevision, setLibraryRevision] = useState(0);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const captureTarget = useRef<{ courseId: string; chatId: string } | null>(null);
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
      void api
        .read<Schema<"AccountResponse">>("/account")
        .then(setAccount)
        .catch(() => setAccount(null));
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
          void api
            .read<Schema<"AccountResponse">>("/account", controller.signal)
            .then((state) => {
              if (!controller.signal.aborted) setAccount(state);
            })
            .catch(() => undefined);
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
    if (captureTarget.current) {
      setError("截图正在加入原对话，请完成或取消后切换课程（CAPTURE_CONTEXT_LOCKED）");
      return;
    }
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
    setConfirmedCapture(undefined);
    tabsRef.current = [];
    setTabs([]);
    setActiveTabId(FILES_TAB);
    setFilePath("");
    setImmersive(false);
    setError("");
    if (!id) return;
    try {
      if (!executionViewOnly) await newChat(id);
      const next = await api.read<Schema<"ChatResponse">[]>(`/courses/${id}/chats`);
      if (epoch === courseEpoch.current) {
        setChats(next);
        if (executionViewOnly && next.length) {
          const chosen = courseChats.current.get(id) || next[next.length - 1].chat_id;
          courseChats.current.set(id, chosen);
          setChatId(chosen);
        }
      }
    } catch (reason) {
      if (epoch === courseEpoch.current) setError(errorMessage(reason));
    }
  };

  const createCourse = async () => {
    if (!folderPath || !courseName.trim()) return;
    setBusy(true);
    try {
      const folder = await api.write<Schema<"CourseFolderResponse">>(
        "/courses/folder",
        {
          path: folderPath,
          title: courseName.trim(),
        },
        { timeoutMs: 120_000 },
      );
      if (!folder) return;
      setCourses(await api.read<Schema<"CourseResponse">[]>("/courses"));
      setCourseDialog(false);
      setFolderPath("");
      setCourseName("");
      await chooseCourse(folder.course_id);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const pickCourseFolder = async () => {
    setPickingFolder(true);
    try {
      const selection = await api.write<Schema<"CourseFolderPickResponse"> | null>(
        "/courses/folder/pick",
        undefined,
        { timeoutMs: 180_000 },
      );
      if (selection) {
        setFolderPath(selection.path);
        setCourseName((value) => (value.trim() ? value : selection.name));
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setPickingFolder(false);
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
    if (captureTarget.current) {
      setError("截图正在加入原对话，请完成或取消后新建对话（CAPTURE_CONTEXT_LOCKED）");
      return;
    }
    setBusy(true);
    try {
      await chatRef.current?.saveDraft();
      const existing = courseChats.current.get(courseId);
      if (existing) {
        const current = await api.read<Schema<"ChatResponse">>(`/chats/${existing}`);
        if (!current.messages?.length && !current.generations?.length) {
          setChatVisible(true);
          chatRef.current?.focusInput();
          return;
        }
      }
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
    if (!source && courseChats.current.has(courseId)) {
      setChatVisible(true);
      chatRef.current?.focusInput();
      return;
    }
    try {
      await newChat(courseId);
      if (selectedCourse.current !== courseId) return;
      setChatVisible(true);
      if (source) setCapture(source);
      else window.setTimeout(() => chatRef.current?.focusInput(), 0);
    } catch (reason) {
      setError(errorMessage(reason));
    }
  };

  const selectChat = async (id: string) => {
    const targetCourse = courseId;
    const epoch = courseEpoch.current;
    if (captureTarget.current) {
      setError("截图正在加入原对话，请完成或取消后切换对话（CAPTURE_CONTEXT_LOCKED）");
      return;
    }
    try {
      await chatRef.current?.saveDraft();
      if (selectedCourse.current !== targetCourse || courseEpoch.current !== epoch) return;
      if (captureTarget.current) {
        setError("截图正在加入原对话，请完成或取消后切换对话（CAPTURE_CONTEXT_LOCKED）");
        return;
      }
      courseChats.current.set(targetCourse, id);
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
      if (
        (runtime.active_generations.length ||
          runtime.active_tasks?.length ||
          runtime.active_preparations?.length ||
          runtime.active_data_operations?.length) &&
        !confirmed
      ) {
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
  const activeTab = tabs.find((tab) => tab.id === activeTabId);
  const selectedCourseValue = courses.find((value) => value.course_id === courseId);
  const videoActive = Boolean(activeTab?.video);
  useEffect(() => {
    if (activeTab?.video) void videoRef.current?.openAsset(activeTab.video, activeTab.timeMs);
  }, [activeTab]);

  const refreshAccount = async () => {
    try {
      setAccount(await api.read<Schema<"AccountResponse">>("/account"));
      await connect();
    } catch (reason) {
      setError(errorMessage(reason));
    }
  };
  const refreshCourses = async () => {
    setCourses(await api.read<Schema<"CourseResponse">[]>("/courses"));
    setLibraryRevision((value) => value + 1);
  };
  const openTab = (tab: StudyTab) => {
    const values = tabsRef.current;
    const next = values.some((value) => value.id === tab.id)
      ? values.map((value) => (value.id === tab.id ? tab : value))
      : [...values, tab];
    tabsRef.current = next;
    setTabs(next);
    setActiveTabId(tab.id);
  };
  const openFile = (file: Schema<"CourseFileOpenResponse">) => {
    if (file.course_id !== selectedCourse.current) return;
    openTab({ id: file.asset_id, title: file.entry.name, file, video: file.video ?? undefined });
  };
  const openVideo = (video: Schema<"MediaAssetResponse">) => {
    openTab({ id: video.asset_id, title: video.display_name, video });
  };
  const selectTab = (tab: StudyTab) => {
    setActiveTabId(tab.id);
  };
  const closeTab = async (id: string) => {
    const targetCourse = selectedCourse.current;
    try {
      await videoRef.current?.savePosition();
      if (targetCourse !== selectedCourse.current) return;
      const closing = tabsRef.current.find((tab) => tab.id === id);
      const remaining = tabsRef.current.filter((tab) => tab.id !== id);
      if (closing?.video && !remaining.some((tab) => tab.video))
        await videoRef.current?.closeAsset();
      if (targetCourse !== selectedCourse.current) return;
      tabsRef.current = remaining;
      setTabs(remaining);
      setActiveTabId((current) => (current === id ? (remaining.at(-1)?.id ?? FILES_TAB) : current));
    } catch (reason) {
      setError(errorMessage(reason));
    }
  };
  const openSource = async (source: NonNullable<CaptureSource["source"]>) => {
    try {
      const asset = await api.write<Schema<"MediaAssetResponse">>(
        `/videos/${source.asset_id}/open`,
        {
          source_version: source.source_version,
        },
      );
      openTab({
        id: asset.asset_id,
        title: asset.display_name,
        video: asset,
        timeMs: source.time_ms,
      });
    } catch (reason) {
      setError(errorMessage(reason));
    }
  };
  const finishCapture = async (result: CaptureResult) => {
    const target = captureTarget.current;
    if (!target || selectedCourse.current !== target.courseId)
      throw new Error("截图的原课程已改变，请取消后重新框选（CAPTURE_CONTEXT_CHANGED）");
    try {
      const id = await newChat(target.courseId);
      if (selectedCourse.current !== target.courseId || (target.chatId && target.chatId !== id))
        throw new Error("截图的原对话已改变，请取消后重新框选（CAPTURE_CONTEXT_CHANGED）");
      target.chatId = id;
      setConfirmedCapture(result);
      setChatVisible(true);
      setCapturing(false);
      window.setTimeout(() => chatRef.current?.focusInput(), 0);
    } catch (reason) {
      setError(errorMessage(reason));
      throw reason;
    }
  };
  const cancelCapture = () => {
    captureTarget.current = null;
    setCapturing(false);
    setCaptureRoot(null);
  };
  const beginCapture = () => {
    if (!courseId || capturing || exiting || busy || captureTarget.current) return;
    ++courseEpoch.current;
    captureTarget.current = { courseId, chatId: courseChats.current.get(courseId) ?? "" };
    videoRef.current?.pauseForCapture();
    setCaptureRoot(workspaceRef.current);
    setCapturing(true);
  };
  // Only the focused application document owns these shortcuts.
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (!event.ctrlKey || !event.shiftKey || event.altKey || event.repeat) return;
      if (event.code === "KeyS") {
        event.preventDefault();
        beginCapture();
      }
      if (event.code === "KeyA") {
        event.preventDefault();
        if (chatVisible && immersive) void closeChat();
        else void openChat();
      }
    };
    document.addEventListener("keydown", keydown);
    return () => document.removeEventListener("keydown", keydown);
  });

  return (
    <div
      ref={workspaceRef}
      className={`app-shell${dark ? " dark" : ""}${immersive ? " immersive" : ""}`}
    >
      <header className="app-header">
        <div className="brand">
          <button
            className="icon-button"
            aria-label={sidebarVisible ? "收起课程目录" : "展开课程目录"}
            aria-expanded={sidebarVisible}
            title={sidebarVisible ? "收起课程目录" : "展开课程目录"}
            onClick={() => {
              setSidebarVisible((value) => !value);
              localStorage.setItem("learning.sidebar-visible", String(!sidebarVisible));
            }}
          >
            <Icon name="panel" />
          </button>
          <h1>学习空间</h1>
          <span className="app-divider" />
          <span className="header-course">{selectedCourseValue?.title || "我的课程"}</span>
        </div>
        <div className="header-actions">
          <span className={`connection-badge ${health?.ready ? "connected" : ""}`} role="status">
            <span />
            {connecting ? "正在连接后台" : health?.ready ? "后台已连接" : "后台未连接"}
          </span>
          <button
            className="icon-button"
            aria-label={dark ? "切换浅色" : "切换深色"}
            title={dark ? "切换浅色" : "切换深色"}
            onClick={() => {
              setDark(!dark);
              localStorage.setItem("learning.theme", dark ? "light" : "dark");
            }}
          >
            <Icon name={dark ? "sun" : "moon"} />
          </button>
          <Button
            variant="ghost"
            disabled={connecting || exiting}
            onClick={() => {
              setConnecting(true);
              setError("");
              void connect();
            }}
          >
            重新连接
          </Button>
          <Button variant="ghost" onClick={() => setAccountOpen(true)}>
            <Icon name="settings" />
            账号与模型
          </Button>
        </div>
      </header>
      {executionViewOnly && (
        <div className="global-notice" role="status">
          执行层暂不可用，当前仅查看资料与历史。已有学习数据保留。
          <a href="/desktop/">返回原生学习界面</a>
          <Button
            variant="ghost"
            onClick={() => {
              void api
                .write("/runtime/reconnect", {})
                .then(() => setExitStatus("已完成连接核对，可返回原生学习界面。"))
                .catch((reason) => setError(errorMessage(reason)));
            }}
          >
            核对执行层连接
          </Button>
        </div>
      )}
      {error && (
        <div className="global-error" role="alert">
          {error}
          <button className="icon-button" aria-label="关闭提示" onClick={() => setError("")}>
            <Icon name="close" size={14} />
          </button>
        </div>
      )}
      {exitStatus && (
        <div className="global-notice" role="status">
          {exitStatus}
        </div>
      )}
      <div className="workspace learning-workspace">
        <aside className="sidebar" aria-label="课程与文件" hidden={immersive || !sidebarVisible}>
          <div className="course-switcher">
            <span className="sidebar-kicker">课程</span>
            <label className="sr-only" htmlFor="course-select">
              选择课程
            </label>
            <select
              id="course-select"
              value={courseId}
              disabled={!health?.ready || exiting}
              onChange={(event) => void chooseCourse(event.target.value)}
            >
              <option value="">选择一门课程</option>
              {courses.map((course) => (
                <option key={course.course_id} value={course.course_id}>
                  {course.title}
                </option>
              ))}
            </select>
            <button
              className="sidebar-add"
              disabled={!health?.ready || exiting}
              onClick={() => setCourseDialog(true)}
            >
              <Icon name="plus" size={16} />
              创建课程
            </button>
          </div>
          <div className="sidebar-library">
            {courseId ? (
              <CourseExplorer
                key={courseId}
                courseId={courseId}
                connected={Boolean(health?.ready) && !exiting}
                onOpen={openFile}
                onLegacyVideo={openVideo}
                revision={libraryRevision}
                onDirectorySelect={(path) => {
                  setFilePath(path);
                  setActiveTabId(FILES_TAB);
                }}
                onCourseChanged={() => void refreshCourses()}
                onDetached={() => {
                  void chooseCourse("");
                  void refreshCourses();
                }}
              />
            ) : (
              <div className="sidebar-empty">
                <Icon name="folder" size={24} />
                <p>课程资料，井井有条。</p>
                <small>选择已有课程，或打开本地课程文件夹。</small>
              </div>
            )}
          </div>
          <div className="sidebar-footer">
            <details className="capabilities">
              <summary>连接状态</summary>
              {Object.entries(capabilityNames).map(([name, label]) => {
                const state = health?.capabilities[name as keyof Schema<"Capabilities">];
                return (
                  <div className="capability-row" key={name}>
                    <span>{label}</span>
                    <span>{state ? capabilityLabels[state] : "状态未知"}</span>
                  </div>
                );
              })}
            </details>
            <button
              className="sidebar-settings"
              aria-label="打开账号设置"
              onClick={() => setAccountOpen(true)}
            >
              <Icon name="settings" size={16} />
              账号与模型
            </button>
            <button
              className="sidebar-settings muted"
              disabled={!health?.ready || busy || exiting}
              onClick={() => void requestExit()}
            >
              安全退出
            </button>
          </div>
        </aside>
        <main
          className={`study-columns ${chatVisible ? "" : "chat-collapsed"}`}
          ref={studyColumns}
          style={{ "--chat-width": `${chatWidth}%` } as CSSProperties}
        >
          <section className="media-panel main-panel" aria-label="学习资料">
            <div className="resource-toolbar">
              <div className="resource-tabs" role="tablist" aria-label="资料标签页">
                <div className={`resource-tab ${activeTabId === FILES_TAB ? "selected" : ""}`}>
                  <button
                    role="tab"
                    aria-selected={activeTabId === FILES_TAB}
                    onClick={() => setActiveTabId(FILES_TAB)}
                  >
                    <Icon name="folder" size={15} />
                    <span>文件</span>
                  </button>
                </div>
                {tabs.map((tab) => (
                  <div
                    className={`resource-tab ${activeTabId === tab.id ? "selected" : ""}`}
                    key={tab.id}
                  >
                    <button
                      role="tab"
                      aria-selected={activeTabId === tab.id}
                      onClick={() => selectTab(tab)}
                    >
                      <Icon name={tab.video ? "video" : "file"} size={15} />
                      <span>{tab.title}</span>
                    </button>
                    <button
                      className="tab-close"
                      aria-label={`关闭 ${tab.title}`}
                      onClick={() => void closeTab(tab.id)}
                    >
                      <Icon name="close" size={12} />
                    </button>
                  </div>
                ))}
              </div>
              {!chatVisible && (
                <button
                  className="icon-button"
                  aria-label="展开聊天"
                  title="展开聊天 · Ctrl+Shift+A"
                  disabled={!courseId}
                  onClick={() => void openChat()}
                >
                  <Icon name="chat" />
                </button>
              )}
            </div>
            <div className="resource-content">
              {courseId && (
                <div className="video-content" hidden={!videoActive}>
                  <VideoWorkspace
                    key={courseId}
                    ref={videoRef}
                    courseId={courseId}
                    connected={Boolean(health?.ready) && !exiting}
                    onLibraryChange={ignoreLibraryChange}
                    onImmersiveChange={(value) => {
                      setImmersive(value);
                      if (value) setChatVisible(false);
                    }}
                    onOpenChat={() => void openChat()}
                    onCapture={(source) => void openChat(source)}
                    workspaceRef={workspaceRef}
                    minimal
                    active={videoActive}
                  />
                </div>
              )}
              {activeTab?.file && !activeTab.video && (
                <ResourceViewer
                  key={`${activeTab.id}-${activeTab.file.source_version}`}
                  file={activeTab.file}
                />
              )}
              {courseId && (
                <div className="file-manager-pane" hidden={activeTabId !== FILES_TAB}>
                  <FileManager
                    key={courseId}
                    courseId={courseId}
                    connected={Boolean(health?.ready) && !exiting}
                    path={filePath}
                    onPathChange={setFilePath}
                    onOpen={openFile}
                    revision={libraryRevision}
                    onChanged={() => setLibraryRevision((value) => value + 1)}
                  />
                </div>
              )}
              {!courseId && (
                <div className="empty-state">
                  <Icon name="folder" size={36} />
                  <h2>选择课程文件夹</h2>
                  <Button
                    variant="outline"
                    disabled={!health?.ready || exiting}
                    onClick={() => setCourseDialog(true)}
                  >
                    创建课程
                  </Button>
                </div>
              )}
            </div>
          </section>
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
                resizeStart.current = {
                  x: event.clientX,
                  width: studyColumns.current?.getBoundingClientRect().width || 1,
                  percent: chatWidth,
                };
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
          <section className="chat-panel main-panel" aria-label="学习问答" hidden={!chatVisible}>
            <div className="chat-panel-toolbar">
              <div className="chat-title">
                <Icon name="chat" size={17} />
                <select
                  id="chat-history"
                  aria-label="选择对话"
                  value={chatId}
                  disabled={!courseId || busy}
                  onChange={(event) => {
                    if (event.target.value) void selectChat(event.target.value);
                  }}
                >
                  {!chatId && <option value="">新对话</option>}
                  {chats.map((chat, index) => (
                    <option key={chat.chat_id} value={chat.chat_id}>
                      对话 {index + 1}
                    </option>
                  ))}
                </select>
              </div>
              <div className="chat-heading-actions">
                <button
                  className="icon-button"
                  aria-label="新对话"
                  title="新对话"
                  disabled={executionViewOnly || !courseId || busy || exiting}
                  onClick={() => void createChat()}
                >
                  <Icon name="plus" size={17} />
                </button>
                <button
                  className="icon-button"
                  aria-label="收起聊天"
                  title="收起聊天 · Ctrl+Shift+A"
                  onClick={() => void closeChat()}
                >
                  <Icon name="panel" size={17} />
                </button>
              </div>
            </div>
            {chatId ? (
              <ChatWorkspace
                key={chatId}
                ref={chatRef}
                chatId={chatId}
                canSubmit={
                  !executionViewOnly &&
                  health?.capabilities.question_answering === "ready" &&
                  !exiting
                }
                connected={Boolean(health?.ready)}
                configuredModel={health?.configured_model}
                configuredThinking={health?.configured_thinking}
                modelChoices={account?.models?.map((model) => model.id) ?? []}
                modelImageSupport={
                  account
                    ? Object.fromEntries(
                        (account.models ?? []).map((model) => [model.id, model.supports_images]),
                      )
                    : undefined
                }
                modelThinkingChoices={
                  account
                    ? Object.fromEntries(
                        (account.models ?? []).map((model) => [
                          model.id,
                          model.thinking_levels as Schema<"SubmissionInput">["thinking"][],
                        ]),
                      )
                    : undefined
                }
                incomingCapture={capture}
                onCaptureConsumed={() => setCapture(undefined)}
                incomingConfirmedCapture={confirmedCapture}
                onConfirmedCaptureConsumed={(sourceId) => {
                  if (confirmedCapture?.source.sourceId !== sourceId) return;
                  captureTarget.current = null;
                  setConfirmedCapture(undefined);
                  setCaptureRoot(null);
                }}
                onOpenSource={(source) => void openSource(source)}
              />
            ) : (
              <div className="chat-placeholder">
                <div className="chat-placeholder-space" />
                <div className="chat-placeholder-composer">
                  <textarea
                    aria-label="输入问题"
                    placeholder={courseId ? "正在准备对话…" : "选择课程后开始对话"}
                    disabled
                  />
                  {courseId && error && (
                    <button
                      className="icon-button"
                      aria-label="重试准备对话"
                      onClick={() => void openChat()}
                    >
                      <Icon name="refresh" />
                    </button>
                  )}
                </div>
              </div>
            )}
          </section>
        </main>
      </div>
      {immersive && !chatVisible && (
        <div className="fullscreen-ai-edge">
          <button aria-label="全屏唤起AI" title="打开学习问答" onClick={() => void openChat()}>
            <Icon name="chat" size={20} />
            <span>学习问答</span>
          </button>
        </div>
      )}
      {capturing && captureRoot && (
        <WorkspaceCapture root={captureRoot} onConfirm={finishCapture} onCancel={cancelCapture} />
      )}
      {accountOpen && (
        <div className="dialog-overlay">
          <section
            className="settings-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="account-title"
          >
            <div className="dialog-heading">
              <h2 id="account-title">账号与模型</h2>
              <button
                className="icon-button"
                aria-label="关闭账号设置"
                onClick={() => setAccountOpen(false)}
              >
                <Icon name="close" />
              </button>
            </div>
            <AccountPanel
              connected={Boolean(health?.ready)}
              onChanged={() => void refreshAccount()}
              onClose={() => setAccountOpen(false)}
            />
          </section>
        </div>
      )}
      {courseDialog && (
        <div className="dialog-overlay">
          <form
            className="settings-dialog create-course-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="course-dialog-title"
            onSubmit={(event) => {
              event.preventDefault();
              void createCourse();
            }}
          >
            <div className="dialog-heading">
              <h2 id="course-dialog-title">创建课程</h2>
              <button
                type="button"
                className="icon-button"
                aria-label="取消创建课程"
                disabled={busy || pickingFolder}
                onClick={() => setCourseDialog(false)}
              >
                <Icon name="close" />
              </button>
            </div>
            <label className="course-name-field">
              <Icon name="folder" size={19} />
              <input
                aria-label="课程名称"
                placeholder="课程名称"
                maxLength={200}
                value={courseName}
                onChange={(event) => setCourseName(event.target.value)}
                disabled={busy}
              />
            </label>
            <label className="course-folder-label">课程文件夹</label>
            <div className="course-folder-choice">
              {folderPath ? (
                <span className="chosen-folder" title={folderPath}>
                  <Icon name="folder" />
                  {folderPath.split(/[\\/]/).filter(Boolean).at(-1)}
                </span>
              ) : (
                <span>在此电脑上选择文件夹</span>
              )}
              <Button
                type="button"
                variant="outline"
                disabled={busy || pickingFolder}
                onClick={() => void pickCourseFolder()}
              >
                <Icon name="plus" size={15} />
                {pickingFolder ? "正在选择…" : folderPath ? "更换文件夹" : "选择课程文件夹"}
              </Button>
            </div>
            <div className="dialog-actions">
              <Button
                type="button"
                variant="ghost"
                disabled={busy || pickingFolder}
                onClick={() => setCourseDialog(false)}
              >
                取消
              </Button>
              <Button
                type="submit"
                disabled={busy || pickingFolder || !folderPath || !courseName.trim()}
              >
                {busy ? "正在创建…" : "创建"}
              </Button>
            </div>
          </form>
        </div>
      )}
      {exitConfirmation && (
        <div className="dialog-overlay">
          <section
            className="settings-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="exit-title"
          >
            <h2 id="exit-title">保存并退出后台？</h2>
            <p>
              还有{" "}
              {exitConfirmation.active_generations.length +
                (exitConfirmation.active_tasks?.length ?? 0) +
                (exitConfirmation.active_preparations?.length ?? 0) +
                (exitConfirmation.active_data_operations?.length ?? 0)}{" "}
              个回答、学习任务或数据操作在进行或待核对。确认后会保存已收到的内容，停止所属执行并退出。
            </p>
            <div className="dialog-actions">
              <Button variant="ghost" onClick={() => setExitConfirmation(null)}>
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
