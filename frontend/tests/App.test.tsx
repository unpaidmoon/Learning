import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Ref } from "react";
import { App } from "../src/App";
import type { Schema } from "../src/api/client";
import type { ChatWorkspaceHandle } from "../src/chat/ChatWorkspace";
import type { VideoWorkspaceHandle } from "../src/media/VideoWorkspace";

// App's state/callback seam; native, media, files and chat have separate real coverage.
const seams = vi.hoisted(() => ({
  realChat: false,
  saveDraft: vi.fn(async () => {}),
  savePosition: vi.fn(async () => {}),
  closeAsset: vi.fn(async () => {}),
  openAsset: vi.fn<(asset: unknown) => Promise<void>>().mockResolvedValue(undefined),
}));
vi.mock("../src/media/WorkspaceCapture", () => ({
  WorkspaceCapture: ({ onCancel }: { onCancel: () => void }) => (
    <button onClick={onCancel}>取消合成截图</button>
  ),
}));
vi.mock("../src/chat/ChatWorkspace", async (importOriginal) => {
  const { useImperativeHandle } = await import("react");
  const original = await importOriginal<typeof import("../src/chat/ChatWorkspace")>();
  function FixtureChat({ chatId, ref }: { chatId: string; ref?: Ref<ChatWorkspaceHandle> }) {
    useImperativeHandle(ref, () => ({ saveDraft: seams.saveDraft, focusInput: () => {} }));
    return <textarea aria-label="输入问题" data-testid="active-chat" data-chat-id={chatId} />;
  }
  return {
    ChatWorkspace: (props: Parameters<typeof original.ChatWorkspace>[0]) =>
      seams.realChat ? <original.ChatWorkspace {...props} /> : <FixtureChat {...props} />,
  };
});
vi.mock("../src/media/VideoWorkspace", async () => {
  const { useImperativeHandle, useState } = await import("react");
  return {
    VideoWorkspace: ({
      ref,
      onAssetChange,
    }: {
      ref: Ref<VideoWorkspaceHandle>;
      onAssetChange?: (asset: Schema<"MediaAssetResponse"> | null) => void;
    }) => {
      const [asset, setAsset] = useState<Schema<"MediaAssetResponse"> | null>(null);
      useImperativeHandle(ref, () => ({
        savePosition: seams.savePosition,
        openAsset: async (value) => {
          await seams.openAsset(value);
          setAsset(value);
          onAssetChange?.(value);
        },
        closeAsset: async () => {
          await seams.closeAsset();
          setAsset(null);
          onAssetChange?.(null);
        },
        openSource: async () => {},
        resumeFullscreen: async () => {},
        toggleFullscreen: async () => {},
        pauseForCapture: () => undefined,
        getElement: () => null,
      }));
      return <div data-testid="video-workspace">{asset?.display_name ?? "无视频"}</div>;
    },
  };
});
vi.mock("../src/library/CourseExplorer", () => ({
  CourseExplorer: () => <div>合成课程文件树</div>,
}));
vi.mock("../src/library/FileManager", () => ({
  FileManager: ({
    courseId,
    onOpen,
  }: {
    courseId: string;
    onOpen: (value: Schema<"CourseFileOpenResponse">) => void;
  }) => {
    const video: Schema<"MediaAssetResponse"> = {
      asset_id: "synthetic-video",
      source_version: "synthetic-version",
      position_ms: 0,
      position_revision: 0,
      display_name: "合成视频.webm",
      availability: "available",
      media_type: "video/webm",
      size_bytes: 100,
    };
    return (
      <div data-testid="file-manager">
        <button
          onClick={() =>
            onOpen({
              course_id: courseId,
              entry: {
                name: video.display_name,
                path: video.display_name,
                kind: "file",
                viewer: "video",
                size_bytes: 100,
              },
              asset_id: video.asset_id,
              source_version: video.source_version,
              viewer: "video",
              content_url: null,
              video,
            })
          }
        >
          打开合成视频
        </button>
      </div>
    );
  },
}));
const health: Schema<"HealthResponse"> = {
  contract_version: "1",
  app_version: "0.1.0",
  instance_id: "health-test-instance",
  ready: true,
  configured_model: "deepseek-flash",
  configured_thinking: "high",
  capabilities: {
    harness: "not_configured",
    question_answering: "not_configured",
    persistence: "ready",
    video: "not_implemented",
    transcription: "not_implemented",
    knowledge_base: "not_implemented",
  },
};
const answer = (value: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(value), { status }));
const courseA = { course_id: "course-A", title: "课程 A" };
const courseB = { course_id: "course-B", title: "课程 B" };
type Override = (url: string, init?: RequestInit) => Promise<Response> | undefined;
function server(courses: Schema<"CourseResponse">[] = [courseA], override?: Override) {
  const created = new Map<string, Schema<"ChatResponse">[]>();
  return vi.fn((url: string, init?: RequestInit): Promise<Response> => {
    const response = override?.(url, init);
    if (response) return response;
    if (url.endsWith("/health")) return answer(health);
    if (url.endsWith("/session")) return answer({ csrf_token: "test-only" });
    if (url.endsWith("/courses")) return answer(courses);
    if (url.endsWith("/account")) return answer({ models: [] });
    const match = /\/courses\/([^/]+)\/chats$/.exec(url);
    if (match) {
      const id = match[1],
        values = created.get(id) ?? [];
      if (init?.method === "POST") {
        const value = { chat_id: "fresh-" + id + "-" + (values.length + 1), course_id: id };
        created.set(id, [...values, value]);
        return answer(value);
      }
      return answer([{ chat_id: "history-" + id, course_id: id }, ...values]);
    }
    if (/\/chats\/[^/]+\/draft$/.test(url))
      return answer({
        revision: 0,
        text: "",
        attachment_ids: [],
        references: [],
        conditions: [],
        corrections: [],
        model: null,
        thinking: null,
      });
    return answer({});
  });
}
const creations = (request: ReturnType<typeof server>, course = "course-A") =>
  request.mock.calls.filter(
    ([url, init]) => url.endsWith("/courses/" + course + "/chats") && init?.method === "POST",
  );
async function chooseA() {
  await screen.findByText("后台已连接");
  await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-A");
  await screen.findByTestId("active-chat");
}
beforeEach(() => {
  seams.realChat = false;
  seams.saveDraft.mockReset().mockResolvedValue(undefined);
  seams.savePosition.mockReset().mockResolvedValue(undefined);
  seams.closeAsset.mockReset().mockResolvedValue(undefined);
  seams.openAsset.mockReset().mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});
describe("后台连接和课程状态", () => {
  it("显示真实健康能力，不将后台就绪当成问答可用", async () => {
    vi.stubGlobal("fetch", server([]));
    render(<App />);
    expect(await screen.findByText("后台已连接")).toBeInTheDocument();
    expect(screen.getAllByText("未配置")).toHaveLength(2);
    expect(screen.getAllByText("未接入")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "发送" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "输入问题" })).toBeDisabled();
  });
  it("连接失败后，用户重试会重新获取后台状态", async () => {
    const request = vi.fn().mockRejectedValue(new TypeError("offline"));
    vi.stubGlobal("fetch", request);
    render(<App />);
    expect(await screen.findByText("后台未连接")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("BACKEND_UNREACHABLE");
    request.mockImplementation(server([]));
    await userEvent.click(screen.getByRole("button", { name: "重新连接" }));
    expect(await screen.findByText("后台已连接")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
  it("非就绪 HTTP 状态不会显示已连接", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => answer({ ...health, ready: false }, 503)),
    );
    render(<App />);
    expect(await screen.findByText("后台未连接")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("BACKEND_NOT_READY");
  });
  it("课程切换后，迟到的旧课程历史列表不会覆盖当前课程", async () => {
    let releaseA: (value: Response) => void = () => {};
    const request = server([courseA, courseB], (url, init) => {
      if (url.endsWith("/courses/course-A/chats") && init?.method !== "POST")
        return new Promise<Response>((resolve) => {
          releaseA = resolve;
        });
    });
    vi.stubGlobal("fetch", request);
    render(<App />);
    await chooseA();
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-B");
    await waitFor(() => expect(screen.getByLabelText("选择对话")).toHaveValue("fresh-course-B-1"));
    await act(async () => {
      releaseA(
        new Response(JSON.stringify([{ chat_id: "late-history-A", course_id: "course-A" }])),
      );
    });
    expect(screen.getByLabelText("选择对话")).toHaveValue("fresh-course-B-1");
    expect(screen.queryByDisplayValue("late-history-A")).not.toBeInTheDocument();
    expect(screen.getByTestId("active-chat")).toHaveAttribute("data-chat-id", "fresh-course-B-1");
  });
  it("进入课程只建当次空对话，收放AI不再创建，也不自动打开旧资料或历史", async () => {
    const request = server();
    vi.stubGlobal("fetch", request);
    render(<App />);
    await chooseA();
    expect(screen.getByTestId("active-chat")).toHaveAttribute("data-chat-id", "fresh-course-A-1");
    expect(creations(request)).toHaveLength(1);
    expect(seams.openAsset).not.toHaveBeenCalled();
    expect(request.mock.calls.some(([url]) => url.endsWith("/chats/history-course-A"))).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "收起聊天" }));
    await userEvent.click(screen.getByRole("button", { name: "展开聊天" }));
    expect(creations(request)).toHaveLength(1);
    expect(screen.getByTestId("active-chat")).toHaveAttribute("data-chat-id", "fresh-course-A-1");
  });
  it("截图绑定原课程，完成或取消前不能被课程切换改写", async () => {
    const request = server([courseA, courseB]);
    vi.stubGlobal("fetch", request);
    render(<App />);
    await chooseA();
    fireEvent.keyDown(document, { code: "KeyS", ctrlKey: true, shiftKey: true });
    await screen.findByRole("button", { name: "取消合成截图" });
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-B");
    expect(screen.getByLabelText("选择课程")).toHaveValue("course-A");
    expect(screen.getByRole("alert")).toHaveTextContent("CAPTURE_CONTEXT_LOCKED");
    expect(creations(request, "course-B")).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "取消合成截图" }));
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-B");
    await waitFor(() => expect(screen.getByLabelText("选择课程")).toHaveValue("course-B"));
  });

  it("历史切换等待保存期间换课程，旧回调不覆盖新课程或对话记忆", async () => {
    const request = server([courseA, courseB]);
    vi.stubGlobal("fetch", request);
    render(<App />);
    await chooseA();
    let releaseOldSave: () => void = () => {};
    seams.saveDraft.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          releaseOldSave = resolve;
        }),
    );
    await userEvent.selectOptions(screen.getByLabelText("选择对话"), "history-course-A");
    await waitFor(() => expect(seams.saveDraft).toHaveBeenCalledTimes(1));
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-B");
    await waitFor(() => expect(screen.getByLabelText("选择对话")).toHaveValue("fresh-course-B-1"));
    await act(async () => {
      releaseOldSave();
    });
    expect(screen.getByLabelText("选择课程")).toHaveValue("course-B");
    expect(screen.getByLabelText("选择对话")).toHaveValue("fresh-course-B-1");
    expect(screen.getByTestId("active-chat")).toHaveAttribute("data-chat-id", "fresh-course-B-1");
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-A");
    await waitFor(() => expect(screen.getByLabelText("选择对话")).toHaveValue("fresh-course-A-1"));
    expect(creations(request, "course-A")).toHaveLength(1);
  });
  it("保存草稿失败时保留原课程和对话", async () => {
    const request = server([courseA, courseB]);
    vi.stubGlobal("fetch", request);
    render(<App />);
    await chooseA();
    seams.saveDraft.mockRejectedValueOnce(new Error("合成草稿保存失败"));
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-B");
    expect(await screen.findByRole("alert")).toHaveTextContent("合成草稿保存失败");
    expect(screen.getByLabelText("选择课程")).toHaveValue("course-A");
    expect(screen.getByTestId("active-chat")).toHaveAttribute("data-chat-id", "fresh-course-A-1");
    expect(creations(request, "course-B")).toHaveLength(0);
  });
  it("账号读取失败不会把本地学习后台降为离线", async () => {
    vi.stubGlobal(
      "fetch",
      server([], (url) =>
        url.endsWith("/account")
          ? answer({ code: "ACCOUNT_UNAVAILABLE", message: "账号暂不可用" }, 503)
          : undefined,
      ),
    );
    render(<App />);
    await screen.findByText("后台已连接");
    await userEvent.click(screen.getByRole("button", { name: "重新连接" }));
    expect(await screen.findByText("后台已连接")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "创建课程" })[0]).toBeEnabled();
  });
});
describe("本轮工作区交互契约", () => {
  it("自动空聊草稿读取未返回时立即选历史，标题正文草稿都归属历史且不空写新聊", async () => {
    seams.realChat = true;
    const course = { course_id: "nav-race-course-22", title: "合成历史切换竞态课程" };
    const fresh = "nav-race-fresh-22";
    const history = "nav-race-history-22";
    const storedDraft = {
      revision: 7,
      text: "历史草稿",
      attachment_ids: [],
      references: [],
      conditions: [],
      corrections: [],
      model: "deepseek-flash",
      thinking: "high",
    };
    let releaseFreshDraft: (response: Response) => void = () => {};
    const request = server([course], (url, init) => {
      if (url.endsWith("/account"))
        return answer({
          models: [{ id: "deepseek-flash", supports_images: true, thinking_levels: ["high"] }],
        });
      if (url.endsWith(`/courses/${course.course_id}/chats`))
        return answer(
          init?.method === "POST"
            ? { chat_id: fresh, course_id: course.course_id }
            : [history, fresh].map((chat_id) => ({ chat_id, course_id: course.course_id })),
        );
      if (url.endsWith(`/chats/${fresh}/draft`) && init?.method !== "POST")
        return new Promise<Response>((resolve) => {
          releaseFreshDraft = resolve;
        });
      if (url.endsWith(`/chats/${history}/draft`)) {
        if (init?.method === "POST") {
          const submitted = JSON.parse(String(init.body));
          return answer({ ...submitted, revision: submitted.expected_revision + 1 });
        }
        return answer(storedDraft);
      }
      if (url.endsWith(`/chats/${fresh}`) || url.endsWith(`/chats/${history}`)) {
        const id = url.endsWith(`/chats/${history}`) ? history : fresh;
        return answer({
          chat_id: id,
          course_id: course.course_id,
          generations: [],
          messages:
            id === history
              ? [
                  {
                    message_id: "nav-history-answer-22",
                    role: "assistant",
                    text: "这是历史对话的已保存回答。",
                  },
                ]
              : [],
        });
      }
    });
    vi.stubGlobal("fetch", request);
    render(<App />);
    await screen.findByText("后台已连接");
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), course.course_id);
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => url.endsWith(`/chats/${fresh}/draft`))).toBe(true),
    );
    expect(screen.getByLabelText("选择对话")).toHaveValue(fresh);
    await userEvent.selectOptions(screen.getByLabelText("选择对话"), history);
    await waitFor(() => expect(screen.getByLabelText("选择对话")).toHaveValue(history));
    expect(await screen.findByText("这是历史对话的已保存回答。")).toBeVisible();
    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: "问题或追问" })).toBeEnabled();
      expect(screen.getByRole("textbox", { name: "问题或追问" })).toHaveValue("历史草稿");
    });
    await act(async () => {
      releaseFreshDraft(new Response(JSON.stringify({ ...storedDraft, revision: 0, text: "" })));
    });
    expect(screen.getByLabelText("选择对话")).toHaveValue(history);
    expect(screen.getByRole("textbox", { name: "问题或追问" })).toHaveValue("历史草稿");
    await userEvent.type(screen.getByRole("textbox", { name: "问题或追问" }), "；继续追问");
    await waitFor(() =>
      expect(
        request.mock.calls.some(
          ([url, init]) => url.endsWith(`/chats/${history}/draft`) && init?.method === "POST",
        ),
      ).toBe(true),
    );
    const saved = request.mock.calls.find(
      ([url, init]) => url.endsWith(`/chats/${history}/draft`) && init?.method === "POST",
    )!;
    expect(JSON.parse(String(saved[1]?.body)).text).toBe("历史草稿；继续追问");
    expect(
      request.mock.calls.filter(
        ([url, init]) => url.endsWith(`/chats/${fresh}/draft`) && init?.method === "POST",
      ),
    ).toHaveLength(0);
    expect(request.mock.calls.filter(([url]) => url.endsWith("/submissions"))).toHaveLength(0);
  });

  it("最后一个视频关闭后返回固定文件页且不被媒体回调重新打开", async () => {
    vi.stubGlobal("fetch", server());
    render(<App />);
    await chooseA();
    await userEvent.click(screen.getByRole("button", { name: "打开合成视频" }));
    expect(await screen.findByRole("tab", { name: "合成视频.webm" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await waitFor(() => expect(seams.openAsset).toHaveBeenCalledTimes(1));
    await userEvent.click(screen.getByRole("button", { name: "关闭 合成视频.webm" }));
    await waitFor(() =>
      expect(screen.queryByRole("tab", { name: "合成视频.webm" })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("tab", { name: "文件" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("file-manager")).toBeVisible();
    expect(seams.closeAsset).toHaveBeenCalledTimes(1);
    expect(seams.openAsset).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "关闭 文件" })).not.toBeInTheDocument();
  });
  it("收放整个左栏仅改变布局，课程、资料和对话身份保持", async () => {
    const request = server();
    vi.stubGlobal("fetch", request);
    render(<App />);
    await chooseA();
    await userEvent.click(screen.getByRole("button", { name: "打开合成视频" }));
    await userEvent.click(screen.getByRole("button", { name: "收起课程目录" }));
    expect(screen.getByLabelText("课程与文件")).not.toBeVisible();
    expect(screen.getByRole("tab", { name: "合成视频.webm" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByTestId("active-chat")).toHaveAttribute("data-chat-id", "fresh-course-A-1");
    await userEvent.click(screen.getByRole("button", { name: "展开课程目录" }));
    expect(screen.getByLabelText("选择课程")).toHaveValue("course-A");
    expect(screen.getByLabelText("课程与文件")).toBeVisible();
    expect(creations(request)).toHaveLength(1);
  });
  it("课程弹窗原生选目录后再确认创建，课程名独立且没有路径输入框", async () => {
    const courses: Schema<"CourseResponse">[] = [];
    const request = server(courses, (url, init) => {
      if (url.endsWith("/courses/folder/pick"))
        return answer({ path: "D:\\Synthetic\\原目录", name: "原目录" });
      if (url.endsWith("/courses/folder") && init?.method === "POST") {
        courses.push({ course_id: "course-new", title: "我的数学" });
        return answer({
          course_id: "course-new",
          title: "我的数学",
          root_path: "D:\\Synthetic\\原目录",
          root_status: "ready",
          legacy_asset_count: 0,
        });
      }
    });
    vi.stubGlobal("fetch", request);
    render(<App />);
    await screen.findByText("后台已连接");
    await userEvent.click(screen.getAllByRole("button", { name: "创建课程" })[0]);
    const dialog = screen.getByRole("dialog", { name: "创建课程" });
    expect(within(dialog).getAllByRole("textbox")).toHaveLength(1);
    expect(within(dialog).getByRole("button", { name: "创建" })).toBeDisabled();
    await userEvent.click(within(dialog).getByRole("button", { name: "选择课程文件夹" }));
    await waitFor(() => expect(within(dialog).getByLabelText("课程名称")).toHaveValue("原目录"));
    expect(request.mock.calls.filter(([url]) => url.endsWith("/courses/folder"))).toHaveLength(0);
    await userEvent.clear(within(dialog).getByLabelText("课程名称"));
    await userEvent.type(within(dialog).getByLabelText("课程名称"), "我的数学");
    await userEvent.click(within(dialog).getByRole("button", { name: "创建" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "创建课程" })).not.toBeInTheDocument(),
    );
    const submitted = request.mock.calls.find(([url]) => url.endsWith("/courses/folder"))!;
    expect(JSON.parse(String(submitted[1]?.body))).toEqual({
      path: "D:\\Synthetic\\原目录",
      title: "我的数学",
    });
    expect(screen.getByLabelText("选择课程")).toHaveValue("course-new");
  });
  it("右AI直接提供输入框，标题下拉选择对话且没有历史占位或开始问答页", async () => {
    vi.stubGlobal("fetch", server());
    render(<App />);
    await screen.findByText("后台已连接");
    expect(screen.getByRole("textbox", { name: "输入问题" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "开始问答" })).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-A");
    await screen.findByTestId("active-chat");
    expect(screen.getByRole("textbox", { name: "输入问题" })).toBeEnabled();
    expect(screen.getByRole("combobox", { name: "选择对话" })).toHaveValue("fresh-course-A-1");
    expect(screen.queryByRole("option", { name: "历史对话" })).not.toBeInTheDocument();
  });
});
