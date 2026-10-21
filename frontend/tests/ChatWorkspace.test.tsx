import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Schema } from "../src/api/client";
import type { CaptureResult, CaptureSource } from "../src/media/capture";

const captureState = vi.hoisted(() => ({ originals: new Map<string, CaptureResult>() }));
vi.mock("../src/media/capture", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/media/capture")>()),
  readImageFile: vi.fn(async () => ({
    sourceId: "synthetic-source",
    dataUrl: "data:image/png;base64,T1JJR0lOQUw=",
    width: 100,
    height: 80,
  })),
}));
vi.mock("../src/chat/captureCache", () => ({
  saveCapture: vi.fn(async (id: string, value: CaptureResult) =>
    captureState.originals.set(id, value),
  ),
  loadCapture: vi.fn(async (id: string) => captureState.originals.get(id)),
}));
// Crop geometry/export itself is covered by capture.test.ts and the real browser.
// This UI fixture isolates the attachment confirmation boundary.
vi.mock("../src/media/CaptureEditor", () => ({
  CaptureEditor: ({
    source,
    onConfirm,
    onCancel,
  }: {
    source: CaptureSource;
    onConfirm: (value: CaptureResult) => Promise<void>;
    onCancel: () => void;
  }) => (
    <div role="dialog" aria-label="截图编辑">
      <button
        onClick={() =>
          void onConfirm({
            source,
            crop: { x: 10, y: 5, width: 20, height: 15 },
            dataUrl: "data:image/png;base64,Q1JPUA==",
            width: 20,
            height: 15,
          }).catch(() => undefined)
        }
      >
        确认选区加入草稿
      </button>
      <button onClick={onCancel}>取消编辑</button>
    </div>
  ),
}));

const generation: Schema<"GenerationResponse"> = {
  generation_id: "generation-one",
  submission_id: "submission-one",
  chat_id: "chat-one",
  state: "running",
  body: "",
  reasoning: "先检查题目条件。",
  cancel_requested: false,
  event_watermark: 3,
  usage: { status: "unknown" },
};
const message: Schema<"MessageResponse"> = {
  message_id: "assistant-one",
  generation_id: "generation-one",
  role: "assistant",
  status: "running",
  text: "",
  reasoning: "",
};
const chat: Schema<"ChatResponse"> = {
  chat_id: "chat-one",
  course_id: "course-one",
  messages: [message],
  generations: [generation],
};
const blankDraft: Schema<"DraftResponse"> = {
  revision: 0,
  text: "",
  attachment_ids: [],
  conditions: [],
  corrections: [],
  model: null,
  thinking: null,
};
const answer = (value: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(value), { status }));
let ChatWorkspace: typeof import("../src/chat/ChatWorkspace").ChatWorkspace;
let savedDraft: Schema<"DraftResponse">;
function fixture(
  initialChat = chat,
  extra?: (url: string, options?: RequestInit) => Promise<Response> | undefined,
) {
  let currentChat = initialChat;
  let attachmentCounter = 0;
  const request = vi.fn((url: string, options?: RequestInit) => {
    const override = extra?.(url, options);
    if (override) return override;
    if (url.endsWith("/draft/clear")) {
      savedDraft = {
        ...blankDraft,
        revision: savedDraft.revision + 1,
        model: savedDraft.model,
        thinking: savedDraft.thinking,
      };
      return answer(savedDraft);
    }
    if (url.endsWith("/draft")) {
      if (options?.method === "POST") {
        const { expected_revision, ...value } = JSON.parse(options.body as string);
        if (expected_revision !== savedDraft.revision)
          return answer({ code: "DRAFT_REVISION_CONFLICT", message: "较新草稿已保存" }, 409);
        savedDraft = { ...value, revision: expected_revision + 1 };
      }
      return answer(savedDraft);
    }
    if (url.endsWith("/attachments"))
      return answer({
        attachment_id: `attachment-${++attachmentCounter}`,
        media_type: "image/png",
        size_bytes: 4,
        sha256: "fixture-crop-hash",
        confirmed: true,
      });
    if (url.endsWith("/source"))
      return options?.method === "POST"
        ? answer(JSON.parse(options.body as string))
        : answer({ code: "ATTACHMENT_SOURCE_NOT_FOUND", message: "无来源" }, 404);
    if (url.endsWith("/cancel")) {
      const stopped = { ...generation, body: "部分解答", state: "cancelled" };
      currentChat = { ...currentChat, generations: [stopped] };
      return answer(stopped);
    }
    if (url.endsWith("/reconcile")) return answer({ ...generation, state: "unknown" });
    if (url.includes("/submissions"))
      return answer({
        submission_id: "submitted",
        generation_id: "new-generation",
        chat_id: "chat-one",
        state: "completed",
        created: true,
      });
    return answer(currentChat);
  });
  vi.stubGlobal("fetch", request);
  return request;
}
beforeEach(async () => {
  vi.resetModules();
  sessionStorage.clear();
  localStorage.clear();
  captureState.originals.clear();
  savedDraft = structuredClone(blankDraft);
  ChatWorkspace = (await import("../src/chat/ChatWorkspace")).ChatWorkspace;
});
afterEach(async () => {
  cleanup();
  await new Promise((resolve) => setTimeout(resolve, 0));
  vi.unstubAllGlobals();
});
const emptyChat = { ...chat, messages: [], generations: [] };
async function ready() {
  await waitFor(() => expect(screen.getByLabelText("问题或追问")).toBeEnabled());
}

describe("学习聊天与正式执行边界", () => {
  it("用户消息保持用户输入；正文活动停止保留部分回答后可以追问", async () => {
    const request = fixture({
      ...chat,
      messages: [
        { ...message, message_id: "user-one", role: "user", text: "这是我的题目条件" },
        message,
      ],
      generations: [{ ...generation, body: "这是回答正文" }],
    });
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    expect(await screen.findByText("这是我的题目条件")).toBeInTheDocument();
    expect(screen.getAllByText("这是回答正文")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "停止回答" }));
    expect(await screen.findByText("已停止，保留部分内容")).toBeInTheDocument();
    expect(screen.getByText("部分解答")).toBeInTheDocument();
    expect(screen.getByText("用量未知")).toBeInTheDocument();
    await ready();
    await userEvent.type(screen.getByLabelText("问题或追问"), "解释第二步");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(request.mock.calls.filter(([url]) => url === "/api/v1/submissions")).toHaveLength(1),
    );
  });
  it("执行未知只发核对命令，不触发新派发", async () => {
    const request = fixture({ ...chat, generations: [{ ...generation, state: "unknown" }] });
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await screen.findByText("执行结果未知，需核对");
    await ready();
    await userEvent.type(screen.getByLabelText("问题或追问"), "继续解释");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "核对执行结果" }));
    expect(request.mock.calls.some(([url]) => url.endsWith("/reconcile"))).toBe(true);
    expect(request.mock.calls.some(([url]) => url.endsWith("/submissions"))).toBe(false);
  });
  it("取消编辑不上传且保留文字；确认只上传选区字节并保存来源；重裁沿原图", async () => {
    const request = fixture(emptyChat);
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    await userEvent.type(screen.getByLabelText("问题或追问"), "原草稿文字");
    const image = new File(["fixture"], "synthetic.png", { type: "image/png" });
    await userEvent.upload(screen.getByLabelText("选择待确认的图片"), image);
    await userEvent.click(await screen.findByRole("button", { name: "取消编辑" }));
    expect(screen.getByLabelText("问题或追问")).toHaveValue("原草稿文字");
    expect(request.mock.calls.some(([url]) => url.endsWith("/attachments"))).toBe(false);
    await userEvent.upload(screen.getByLabelText("选择待确认的图片"), image);
    await userEvent.click(await screen.findByRole("button", { name: "确认选区加入草稿" }));
    expect(await screen.findByAltText("待发送的确认图片 1")).toBeInTheDocument();
    const upload = request.mock.calls.find(([url]) => url.endsWith("/attachments"));
    expect(JSON.parse(upload![1]!.body as string)).toEqual({
      data_base64: "Q1JPUA==",
      media_type: "image/png",
      confirmed: true,
    });
    expect(JSON.stringify(request.mock.calls)).not.toContain("T1JJR0lOQUw=");
    await userEvent.click(screen.getByRole("button", { name: "重新裁剪图片 1" }));
    await userEvent.click(await screen.findByRole("button", { name: "确认选区加入草稿" }));
    await waitFor(() => expect(savedDraft.attachment_ids).toEqual(["attachment-2"]));
    const sourceCalls = request.mock.calls.filter(
      ([url, options]) => url.endsWith("/source") && options?.method === "POST",
    );
    expect(JSON.parse(sourceCalls[1][1]!.body as string).parent_attachment_id).toBe("attachment-1");
    expect(screen.getByLabelText("问题或追问")).toHaveValue("原草稿文字");
  });
  it("回执丢失与界面重开保留提交身份，只读核对不自动重发", async () => {
    const request = fixture(emptyChat, (url) =>
      url === "/api/v1/submissions" ? Promise.reject(new TypeError("ack lost")) : undefined,
    );
    const first = render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    await userEvent.type(screen.getByLabelText("问题或追问"), "只提交一次");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByRole("button", { name: "核对提交" });
    const stored = JSON.parse(localStorage.getItem("learning.pending.chat-one")!);
    expect(stored.submission_id).toBeTruthy();
    first.unmount();
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await userEvent.click(screen.getByRole("button", { name: "核对提交" }));
    await waitFor(() =>
      expect(
        request.mock.calls.some(([url]) => url.endsWith(`/submissions/${stored.submission_id}`)),
      ).toBe(true),
    );
    expect(request.mock.calls.filter(([url]) => url === "/api/v1/submissions")).toHaveLength(1);
  });
  it("Markdown数学可读、整式引用保留LaTeX且禁止回答远图及HTML加载", async () => {
    fixture({
      ...chat,
      generations: [
        {
          ...generation,
          state: "completed",
          body: "中文推导\n\n$$x^2+1=2$$\n\n![remote](https://example.invalid/test.png)\n<script>alert(1)</script>",
        },
      ],
    });
    const view = render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    await userEvent.click(await screen.findByRole("button", { name: "引用整条公式 1" }));
    expect(screen.getByLabelText("问题或追问")).toHaveValue("$$\nx^2+1=2\n$$\n");
    expect(view.container.querySelector(".katex")).toBeInTheDocument();
    expect(view.container.querySelector('img[src^="https:"]')).toBeNull();
    expect(view.container.querySelector("script")).toBeNull();
  });
  it("中文组合输入Ctrl+Enter不误发；选定模型和思考逐次提交", async () => {
    const request = fixture(emptyChat);
    render(
      <ChatWorkspace
        chatId="chat-one"
        connected
        canSubmit
        configuredModel="deepseek-v4-pro"
        configuredThinking="max"
      />,
    );
    await ready();
    const question = screen.getByLabelText("问题或追问");
    await userEvent.type(question, "问题");
    fireEvent.compositionStart(question);
    fireEvent.keyDown(question, { key: "Enter", ctrlKey: true, isComposing: true });
    expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(false);
    fireEvent.compositionEnd(question);
    expect(screen.getByLabelText("模型名称")).toHaveValue("deepseek-v4-pro");
    await userEvent.selectOptions(screen.getByLabelText("思考强度"), "low");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(true),
    );
    const submitted = JSON.parse(
      request.mock.calls.find(([url]) => url === "/api/v1/submissions")![1]!.body as string,
    );
    expect(submitted.model).toBe("deepseek-v4-pro");
    expect(submitted.thinking).toBe("low");
  });
  it("持久保存草稿文本、条件、更正和附件，重开接回", async () => {
    savedDraft = {
      ...blankDraft,
      attachment_ids: ["existing-confirmed"],
      text: "初始文字",
      conditions: ["x>0"],
      corrections: ["改为x<2"],
    };
    fixture(emptyChat);
    const first = render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    await userEvent.type(screen.getByLabelText("问题或追问"), "追加");
    await userEvent.click(screen.getByRole("button", { name: "保存草稿" }));
    await waitFor(() => expect(savedDraft.text).toBe("初始文字追加"));
    first.unmount();
    vi.resetModules();
    ChatWorkspace = (await import("../src/chat/ChatWorkspace")).ChatWorkspace;
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    expect(screen.getByLabelText("问题或追问")).toHaveValue("初始文字追加");
    expect(screen.getByLabelText("当前题目条件（每行一项）")).toHaveValue("x>0");
    expect(screen.getByLabelText("对前文的更正（每行一项）")).toHaveValue("改为x<2");
    expect(screen.getByAltText("待发送的确认图片 1")).toHaveAttribute(
      "src",
      "/api/v1/attachments/existing-confirmed",
    );
  });
  it("文字模型显示能力提示，图片请求被拒后保留用户选择与题图，不自动换模型或新建对话", async () => {
    savedDraft = {
      ...blankDraft,
      text: "请解释这张题图",
      attachment_ids: ["confirmed-question"],
      model: "deepseek-v4-pro",
    };
    const request = fixture(emptyChat, (url) =>
      url === "/api/v1/submissions"
        ? answer(
            { code: "MODEL_IMAGE_UNSUPPORTED", message: "所选模型仅支持文字，题图已保留" },
            409,
          )
        : undefined,
    );
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    expect(
      screen.getByText("仅支持文字；包含题图的对话请使用支持图片的模型。"),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText("所选模型仅支持文字，题图已保留（MODEL_IMAGE_UNSUPPORTED）");
    expect(screen.getByLabelText("模型名称")).toHaveValue("deepseek-v4-pro");
    expect(screen.getByLabelText("问题或追问")).toHaveValue("请解释这张题图");
    expect(screen.getByAltText("待发送的确认图片 1")).toHaveAttribute(
      "src",
      "/api/v1/attachments/confirmed-question",
    );
    expect(request.mock.calls.filter(([url]) => url === "/api/v1/submissions")).toHaveLength(1);
    expect(
      request.mock.calls.some(
        ([url, options]) => url.endsWith("/chats") && options?.method === "POST",
      ),
    ).toBe(false);
    fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "deepseek-flash" } });
    expect(
      screen.queryByText("仅支持文字；包含题图的对话请使用支持图片的模型。"),
    ).not.toBeInTheDocument();
  });
  it("已知提交回执后草稿清理失败仍禁止重复提交，并提供核对", async () => {
    const request = fixture(emptyChat, (url) =>
      url.endsWith("/draft/clear")
        ? answer({ code: "DRAFT_REVISION_CONFLICT", message: "保留新草稿" }, 409)
        : undefined,
    );
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    await userEvent.type(screen.getByLabelText("问题或追问"), "一次接受");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByText("这次提交已接受，草稿清理尚待核对。不会重新发送。");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    expect(localStorage.getItem("learning.pending.chat-one")).not.toBeNull();
    expect(request.mock.calls.filter(([url]) => url === "/api/v1/submissions")).toHaveLength(1);
  });
});
