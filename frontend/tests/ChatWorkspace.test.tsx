import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
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
    if (url.endsWith("/metadata"))
      return answer({
        attachment_id: url.split("/").at(-2),
        media_type: "image/png",
        size_bytes: 4,
        sha256: "fixture-crop-hash",
        confirmed: true,
        kind: "image",
        filename: "图片.png",
        page_count: null,
        character_count: null,
        download_url: url.replace(/\/metadata$/, ""),
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
  it("新空对话初始草稿读取挂起时可立即离开，既不等待读取也不写入空草稿", async () => {
    let release: (response: Response) => void = () => {};
    const request = fixture(emptyChat, (url, options) =>
      url.endsWith("/draft") && options?.method !== "POST"
        ? new Promise((resolve) => {
            release = resolve;
          })
        : undefined,
    );
    const ref = createRef<import("../src/chat/ChatWorkspace").ChatWorkspaceHandle>();
    render(<ChatWorkspace ref={ref} chatId="chat-one" connected canSubmit />);
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => url.endsWith("/chat-one/draft"))).toBe(true),
    );
    expect(screen.getByLabelText("问题或追问")).toBeDisabled();
    try {
      await expect(ref.current!.saveDraft()).resolves.toBeUndefined();
      expect(
        request.mock.calls.filter(
          ([url, options]) => url.endsWith("/draft") && options?.method === "POST",
        ),
      ).toHaveLength(0);
    } finally {
      await act(async () => release(new Response(JSON.stringify(blankDraft), { status: 200 })));
    }
  });

  it("初始草稿读取挂起但存在本地未保存输入时仍阻止离开，保留内容且不覆盖服务器", async () => {
    localStorage.setItem(
      "learning.draft.chat-one",
      JSON.stringify({
        revision: 4,
        dirty: true,
        value: { ...blankDraft, text: "尚未核对版本的本地输入" },
      }),
    );
    let release: (response: Response) => void = () => {};
    const request = fixture(emptyChat, (url, options) =>
      url.endsWith("/draft") && options?.method !== "POST"
        ? new Promise((resolve) => {
            release = resolve;
          })
        : undefined,
    );
    const ref = createRef<import("../src/chat/ChatWorkspace").ChatWorkspaceHandle>();
    render(<ChatWorkspace ref={ref} chatId="chat-one" connected canSubmit />);
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => url.endsWith("/chat-one/draft"))).toBe(true),
    );
    try {
      await expect(ref.current!.saveDraft()).rejects.toThrow("草稿版本未核对");
      expect(screen.getByLabelText("问题或追问")).toHaveValue("尚未核对版本的本地输入");
      expect(
        request.mock.calls.filter(
          ([url, options]) => url.endsWith("/draft") && options?.method === "POST",
        ),
      ).toHaveLength(0);
    } finally {
      await act(async () =>
        release(new Response(JSON.stringify({ ...blankDraft, revision: 5 }), { status: 200 })),
      );
    }
  });

  it("版本冲突的未就绪草稿仍阻止离开，不把本地输入当空稿跳过", async () => {
    localStorage.setItem(
      "learning.draft.chat-one",
      JSON.stringify({
        revision: 4,
        dirty: true,
        value: { ...blankDraft, text: "需要保留的冲突输入" },
      }),
    );
    savedDraft = { ...blankDraft, revision: 5, text: "服务器的新草稿" };
    const request = fixture(emptyChat);
    const ref = createRef<import("../src/chat/ChatWorkspace").ChatWorkspaceHandle>();
    render(<ChatWorkspace ref={ref} chatId="chat-one" connected canSubmit />);
    await screen.findByText(/草稿有较新的保存版本/);
    await expect(ref.current!.saveDraft()).rejects.toThrow("草稿版本未核对");
    expect(screen.getByLabelText("问题或追问")).toHaveValue("需要保留的冲突输入");
    expect(savedDraft.text).toBe("服务器的新草稿");
    expect(
      request.mock.calls.filter(
        ([url, options]) => url.endsWith("/draft") && options?.method === "POST",
      ),
    ).toHaveLength(0);
  });

  it("未就绪空草稿仍保护排队中的确认截图，不借空稿导航分支丢弃附件", async () => {
    let release: (response: Response) => void = () => {};
    const request = fixture(emptyChat, (url, options) =>
      url.endsWith("/draft") && options?.method !== "POST"
        ? new Promise((resolve) => {
            release = resolve;
          })
        : undefined,
    );
    const ref = createRef<import("../src/chat/ChatWorkspace").ChatWorkspaceHandle>();
    render(
      <ChatWorkspace
        ref={ref}
        chatId="chat-one"
        connected
        canSubmit
        incomingConfirmedCapture={{
          source: {
            sourceId: "queued-capture-before-draft-load",
            dataUrl: "data:image/png;base64,T1JJR0lOQUw=",
            width: 100,
            height: 80,
            origin: "workspace",
          },
          crop: { x: 10, y: 5, width: 20, height: 15 },
          dataUrl: "data:image/png;base64,Q1JPUA==",
          width: 20,
          height: 15,
        }}
      />,
    );
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => url.endsWith("/chat-one/draft"))).toBe(true),
    );
    try {
      await expect(ref.current!.saveDraft()).rejects.toThrow("截图尚未加入草稿");
      expect(
        request.mock.calls.some(
          ([url, options]) => options?.method === "POST" && url.endsWith("/attachments"),
        ),
      ).toBe(false);
    } finally {
      cleanup();
      await act(async () => release(new Response(JSON.stringify(blankDraft), { status: 200 })));
    }
  });

  it("空对话直接显示纯加号附件和输入框，自动保存而不显示欢迎页或底部说明", async () => {
    fixture(emptyChat);
    const { container } = render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    expect(screen.getByLabelText("添加附件")).toHaveAttribute("type", "file");
    expect(screen.queryByText("＋ 选择图片")).not.toBeInTheDocument();
    expect(screen.queryByText("从当前不懂的地方问起")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存草稿" })).not.toBeInTheDocument();
    expect(container.querySelector(".draft-save-status")).not.toBeInTheDocument();
    expect(container.querySelector(".composer-hint")).not.toBeInTheDocument();
    expect(container.querySelector(".composer-area")?.lastElementChild).toHaveClass("composer");
    await userEvent.type(screen.getByLabelText("问题或追问"), "自动保存的补充问题");
    await waitFor(() => expect(savedDraft.text).toBe("自动保存的补充问题"));
    expect(screen.queryByText("草稿已保存")).not.toBeInTheDocument();
  });

  it("PDF整份加入草稿卡片，保留原始文件字节且只在发送时提交附件身份", async () => {
    const metadata = {
      attachment_id: "document-pdf",
      filename: "分块矩阵.pdf",
      media_type: "application/pdf",
      size_bytes: 16,
      sha256: "synthetic-pdf-hash",
      confirmed: true,
      kind: "pdf",
      page_count: 3,
      character_count: null,
      download_url: "/api/v1/document-attachments/document-pdf/content",
    };
    const request = fixture(emptyChat, (url) =>
      url.endsWith("/document-attachments") ? answer(metadata) : undefined,
    );
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    const pdf = new File(["%PDF-synthetic-all-pages"], metadata.filename, {
      type: "application/pdf",
    });
    await userEvent.upload(screen.getByLabelText("添加附件"), pdf);
    await screen.findByText("分块矩阵.pdf");
    expect(screen.getByText("整份 PDF · 3 页")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "截图编辑" })).not.toBeInTheDocument();
    expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(false);
    const upload = JSON.parse(
      request.mock.calls.find(([url]) => url.endsWith("/document-attachments"))![1]!.body as string,
    );
    expect(atob(upload.data_base64)).toBe("%PDF-synthetic-all-pages");
    expect(upload.chat_id).toBe("chat-one");
    expect(upload.confirmed).toBe(true);
    await waitFor(() => expect(savedDraft.attachment_ids).toEqual(["document-pdf"]));
    expect(screen.getByLabelText("问题或追问")).toHaveFocus();
    await userEvent.type(screen.getByLabelText("问题或追问"), "解释这份PDF");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(request.mock.calls.filter(([url]) => url === "/api/v1/submissions")).toHaveLength(1),
    );
    const submission = JSON.parse(
      request.mock.calls.find(([url]) => url === "/api/v1/submissions")![1]!.body as string,
    );
    expect(submission.attachment_ids).toEqual(["document-pdf"]);
  });

  it("文档上传失败保留文件并禁发，显式重试沿用同一上传身份", async () => {
    let attempts = 0;
    const request = fixture(emptyChat, (url) => {
      if (!url.endsWith("/document-attachments")) return undefined;
      attempts += 1;
      return attempts === 1
        ? answer({ code: "DOCUMENT_UPLOAD_UNKNOWN", message: "本次附件回执未知" }, 503)
        : answer({
            attachment_id: "retried-document",
            filename: "条件.md",
            media_type: "text/markdown",
            size_bytes: 12,
            sha256: "synthetic-md-hash",
            confirmed: true,
            kind: "markdown",
            page_count: null,
            character_count: 8,
            download_url: "/api/v1/document-attachments/retried-document/content",
          });
    });
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    await userEvent.type(screen.getByLabelText("问题或追问"), "保留问题");
    await userEvent.upload(
      screen.getByLabelText("添加附件"),
      new File(["# 已确认条件"], "条件.md", { type: "text/markdown" }),
    );
    await screen.findByText(/本次附件回执未知/);
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    fireEvent.keyDown(screen.getByLabelText("问题或追问"), { key: "Enter", ctrlKey: true });
    expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "重试附件" }));
    await screen.findByText("Markdown");
    expect(screen.getByLabelText("问题或追问")).toHaveValue("保留问题");
    const uploads = request.mock.calls
      .filter(([url]) => url.endsWith("/document-attachments"))
      .map(([, options]) => JSON.parse(options!.body as string));
    expect(uploads).toHaveLength(2);
    expect(uploads[0].upload_id).toBe(uploads[1].upload_id);
    expect(uploads[0].data_base64).toBe(uploads[1].data_base64);
    await waitFor(() => expect(savedDraft.attachment_ids).toEqual(["retried-document"]));
  });

  it("文档超限明确保留失败状态而不截断上传，取消后可发送原问题", async () => {
    const request = fixture(emptyChat);
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    await userEvent.type(screen.getByLabelText("问题或追问"), "保留文字问题");
    const oversized = new File(["完整原文"], "超限.txt", { type: "text/plain" });
    Object.defineProperty(oversized, "size", { value: 1024 * 1024 + 1 });
    await userEvent.upload(screen.getByLabelText("添加附件"), oversized);
    await screen.findByText(/DOCUMENT_SIZE_LIMIT/);
    expect(request.mock.calls.some(([url]) => url.endsWith("/document-attachments"))).toBe(false);
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "取消附件" }));
    expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
    expect(screen.getByLabelText("问题或追问")).toHaveValue("保留文字问题");
  });

  it("文档上传未完成时阻止切课保存检查，完成后保存原聊天的附件", async () => {
    let release: (value: Response) => void = () => {};
    fixture(emptyChat, (url) =>
      url.endsWith("/document-attachments")
        ? new Promise((resolve) => {
            release = resolve;
          })
        : undefined,
    );
    const ref = createRef<import("../src/chat/ChatWorkspace").ChatWorkspaceHandle>();
    render(<ChatWorkspace ref={ref} chatId="chat-one" connected canSubmit />);
    await ready();
    await userEvent.upload(
      screen.getByLabelText("添加附件"),
      new File(["先保留全部内容"], "条件.txt", { type: "text/plain" }),
    );
    await screen.findByText("正在加入附件…");
    await expect(ref.current!.saveDraft()).rejects.toThrow("附件仍在读取或加入草稿");
    expect(screen.getByRole("button", { name: "处理中…" })).toBeDisabled();
    release(
      new Response(
        JSON.stringify({
          attachment_id: "awaited-document",
          filename: "条件.txt",
          media_type: "text/plain",
          size_bytes: 24,
          sha256: "synthetic-txt-hash",
          confirmed: true,
          kind: "text",
          page_count: null,
          character_count: 8,
          download_url: "/api/v1/document-attachments/awaited-document/content",
        }),
        { status: 200 },
      ),
    );
    await screen.findByText("文本文件");
    await ref.current!.saveDraft();
    expect(savedDraft.attachment_ids).toEqual(["awaited-document"]);
  });

  it("重开带PDF附件的历史消息时按元数据显示文件，不当作图片或重新解析上传", async () => {
    const request = fixture(
      {
        ...emptyChat,
        messages: [
          {
            message_id: "user-document",
            role: "user",
            text: "已提交整份资料",
            reasoning: "",
            status: "completed",
            attachment_ids: ["saved-document"],
          },
        ],
      },
      (url) =>
        url.endsWith("/saved-document/metadata")
          ? answer({
              attachment_id: "saved-document",
              filename: "已保存.pdf",
              media_type: "application/pdf",
              size_bytes: 24,
              sha256: "synthetic-history-hash",
              confirmed: true,
              kind: "pdf",
              page_count: 7,
              character_count: null,
              download_url: "/api/v1/document-attachments/saved-document/content",
            })
          : undefined,
    );
    const { container } = render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    expect(await screen.findByText("整份 PDF · 7 页")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "已保存.pdf" })).toHaveAttribute(
      "href",
      "/api/v1/document-attachments/saved-document/content",
    );
    expect(container.querySelector('img[src*="saved-document"]')).not.toBeInTheDocument();
    expect(request.mock.calls.some(([url]) => url.endsWith("/document-attachments"))).toBe(false);
    expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(false);
  });

  it("附件元数据读取失败仅显式重读原身份，不重新上传或派发模型", async () => {
    savedDraft = { ...blankDraft, attachment_ids: ["metadata-retry-document"] };
    let reads = 0;
    const request = fixture(emptyChat, (url) => {
      if (!url.endsWith("/metadata-retry-document/metadata")) return undefined;
      reads += 1;
      return reads === 1
        ? answer({ code: "METADATA_TEMPORARILY_UNAVAILABLE", message: "附件信息暂不可用" }, 503)
        : answer({
            attachment_id: "metadata-retry-document",
            filename: "恢复后的.txt",
            media_type: "text/plain",
            size_bytes: 24,
            sha256: "synthetic-metadata-retry",
            confirmed: true,
            kind: "text",
            page_count: null,
            character_count: 8,
            download_url: "/api/v1/document-attachments/metadata-retry-document/content",
          });
    });
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    await screen.findByText(/附件信息暂不可用/);
    expect(reads).toBe(1);
    await userEvent.click(screen.getByRole("button", { name: "重新读取附件 1" }));
    expect(await screen.findByText("恢复后的.txt")).toBeInTheDocument();
    expect(reads).toBe(2);
    expect(request.mock.calls.some(([url]) => url.endsWith("/document-attachments"))).toBe(false);
    expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(false);
    expect(savedDraft.attachment_ids).toEqual(["metadata-retry-document"]);
  });

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
    await userEvent.upload(screen.getByLabelText("添加附件"), image);
    await userEvent.click(await screen.findByRole("button", { name: "取消编辑" }));
    expect(screen.getByLabelText("问题或追问")).toHaveValue("原草稿文字");
    expect(request.mock.calls.some(([url]) => url.endsWith("/attachments"))).toBe(false);
    await userEvent.upload(screen.getByLabelText("添加附件"), image);
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
  it("Markdown数学可读、没有常驻整式按钮且禁止回答远图及HTML加载", async () => {
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
    expect(screen.queryByRole("button", { name: "引用整条公式 1" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("问题或追问")).toHaveValue("");
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
  it("拖选确认的截图直接加入草稿并聚焦输入，只上传选区且不弹独立裁剪器", async () => {
    const request = fixture(emptyChat);
    const consumed = vi.fn();
    const result: CaptureResult = {
      source: {
        sourceId: "workspace-capture",
        dataUrl: "data:image/png;base64,T1JJR0lOQUw=",
        width: 100,
        height: 80,
        origin: "workspace",
      },
      crop: { x: 10, y: 5, width: 20, height: 15 },
      dataUrl: "data:image/png;base64,Q1JPUA==",
      width: 20,
      height: 15,
    };
    render(
      <ChatWorkspace
        chatId="chat-one"
        connected
        canSubmit
        incomingConfirmedCapture={result}
        onConfirmedCaptureConsumed={consumed}
      />,
    );
    await screen.findByAltText("待发送的确认图片 1");
    expect(screen.queryByRole("dialog", { name: "截图编辑" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("问题或追问")).toHaveFocus();
    expect(consumed).toHaveBeenCalledTimes(1);
    expect(request.mock.calls.filter(([url]) => url.endsWith("/attachments"))).toHaveLength(1);
    const payload = JSON.parse(
      request.mock.calls.find(([url]) => url.endsWith("/attachments"))![1]!.body as string,
    );
    expect(payload.data_base64).toBe("Q1JPUA==");
    expect(JSON.stringify(request.mock.calls)).not.toContain("T1JJR0lOQUw=");
    const source = JSON.parse(
      request.mock.calls.find(
        ([url, options]) => url.endsWith("/source") && options?.method === "POST",
      )![1]!.body as string,
    );
    expect(source.origin).toBe("workspace");
    expect(request.mock.calls.some(([url]) => url.endsWith("/submissions"))).toBe(false);
  });
  it("截图上传跨过disabled真实DOM提交后才聚焦输入，不在上传锁定期间丢失focus", async () => {
    let releaseUpload!: (value: Response) => void;
    const upload = new Promise<Response>((resolve) => {
      releaseUpload = resolve;
    });
    const request = fixture(emptyChat, (url) =>
      url.endsWith("/attachments") ? upload : undefined,
    );
    render(
      <ChatWorkspace
        chatId="chat-one"
        connected
        canSubmit
        incomingConfirmedCapture={{
          source: {
            sourceId: "deferred-workspace",
            dataUrl: "data:image/png;base64,T1JJR0lOQUw=",
            width: 100,
            height: 80,
            origin: "workspace",
          },
          crop: { x: 10, y: 5, width: 20, height: 15 },
          dataUrl: "data:image/png;base64,Q1JPUA==",
          width: 20,
          height: 15,
        }}
      />,
    );
    await waitFor(() =>
      expect(request.mock.calls.filter(([url]) => url.endsWith("/attachments"))).toHaveLength(1),
    );
    const question = screen.getByLabelText("问题或追问");
    expect(question).toBeDisabled();
    expect(question).not.toHaveFocus();
    releaseUpload(
      new Response(
        JSON.stringify({
          attachment_id: "delayed-crop",
          media_type: "image/png",
          size_bytes: 4,
          sha256: "fixture-crop-hash",
          confirmed: true,
        }),
      ),
    );
    await screen.findByAltText("待发送的确认图片 1");
    await waitFor(() => expect(question).toBeEnabled());
    await waitFor(() => expect(question).toHaveFocus());
    expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(false);
  });
  it("截图加入失败时阻止漏图发送，用户显式取消后才可发送保留的问题", async () => {
    const request = fixture(emptyChat, (url) =>
      url.endsWith("/attachments")
        ? answer({ code: "UPLOAD_UNAVAILABLE", message: "图片暂未保存" }, 503)
        : undefined,
    );
    const consumed = vi.fn();
    render(
      <ChatWorkspace
        chatId="chat-one"
        connected
        canSubmit
        incomingConfirmedCapture={{
          source: {
            sourceId: "failed-workspace",
            dataUrl: "data:image/png;base64,T1JJR0lOQUw=",
            width: 100,
            height: 80,
            origin: "workspace",
          },
          crop: { x: 10, y: 5, width: 20, height: 15 },
          dataUrl: "data:image/png;base64,Q1JPUA==",
          width: 20,
          height: 15,
        }}
        onConfirmedCaptureConsumed={consumed}
      />,
    );
    await screen.findByRole("button", { name: "重新加入截图" });
    await ready();
    await userEvent.type(screen.getByLabelText("问题或追问"), "请解释这个选区");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    fireEvent.keyDown(screen.getByLabelText("问题或追问"), { key: "Enter", ctrlKey: true });
    expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(false);
    expect(consumed).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "取消这张截图" }));
    expect(consumed).toHaveBeenCalledWith("failed-workspace");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(true),
    );
    expect(screen.queryByRole("button", { name: "重新加入截图" })).not.toBeInTheDocument();
  });
  it("持久草稿引用以卡片呈现，不改问题文本，并按引用原身份提交", async () => {
    const reference = {
      reference_id: "quote-one",
      message_id: "assistant-one",
      text: "x",
      markdown: "$x$",
      source_start: 0,
      source_end: 1,
    };
    savedDraft = { ...blankDraft, text: "解释这个符号", references: [reference] };
    const request = fixture(emptyChat);
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    expect(screen.getByLabelText("引用 1")).toBeInTheDocument();
    expect(screen.getByLabelText("问题或追问")).toHaveValue("解释这个符号");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(true),
    );
    const submitted = JSON.parse(
      request.mock.calls.find(([url]) => url === "/api/v1/submissions")![1]!.body as string,
    );
    expect(submitted.references).toEqual([reference]);
    expect(submitted.text).toBe("解释这个符号");
  });
  it("仅有引用时提示补充问题，按钮与快捷键都不发空请求，补问后连同引用提交", async () => {
    const reference = {
      reference_id: "quote-only",
      message_id: "assistant-one",
      text: "x",
      markdown: "$x$",
      source_start: 0,
      source_end: 1,
    };
    savedDraft = { ...blankDraft, references: [reference] };
    const request = fixture(emptyChat);
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    expect(screen.getByLabelText("引用 1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    expect(screen.queryByText("已加入引用，请补充问题后发送。")).not.toBeInTheDocument();
    fireEvent.keyDown(screen.getByLabelText("问题或追问"), { key: "Enter", ctrlKey: true });
    expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(false);
    await userEvent.type(screen.getByLabelText("问题或追问"), "这个符号表示什么？");
    expect(screen.queryByText("已加入引用，请补充问题后发送。")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(true),
    );
    const submitted = JSON.parse(
      request.mock.calls.find(([url]) => url === "/api/v1/submissions")![1]!.body as string,
    );
    expect(submitted.references).toEqual([reference]);
    expect(submitted.text).toBe("这个符号表示什么？");
  });
  it("移除引用只改引用列表，保留问题与已确认附件", async () => {
    savedDraft = {
      ...blankDraft,
      text: "保留补充问题",
      attachment_ids: ["retained-image"],
      references: [
        {
          reference_id: "quote-one",
          message_id: "assistant-one",
          text: "x",
          markdown: "$x$",
          source_start: 0,
          source_end: 1,
        },
      ],
    };
    fixture(emptyChat);
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "移除引用 1" }));
    expect(screen.queryByLabelText("本次引用")).not.toBeInTheDocument();
    expect(screen.getByLabelText("问题或追问")).toHaveValue("保留补充问题");
    expect(screen.getByAltText("待发送的确认图片 1")).toHaveAttribute(
      "src",
      "/api/v1/attachments/retained-image",
    );
    await waitFor(() => expect(savedDraft.references).toEqual([]));
  });
  it("官方目录按所选模型提供思考档位，不自动替换不可用选择", async () => {
    const request = fixture(emptyChat);
    render(
      <ChatWorkspace
        chatId="chat-one"
        connected
        canSubmit
        configuredModel="account-flash"
        configuredThinking="high"
        modelChoices={["account-flash", "account-pro"]}
        modelThinkingChoices={{ "account-flash": ["high", "max"], "account-pro": ["none", "low"] }}
      />,
    );
    await ready();
    await userEvent.type(screen.getByLabelText("问题或追问"), "解释原因");
    fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "account-pro" } });
    expect(screen.getByLabelText("思考强度")).toHaveValue("high");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    expect(screen.getByText("当前模型不支持已选思考强度，请手动重新选择。")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("思考强度"), "low");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => url === "/api/v1/submissions")).toBe(true),
    );
    const submitted = JSON.parse(
      request.mock.calls.find(([url]) => url === "/api/v1/submissions")![1]!.body as string,
    );
    expect(submitted.model).toBe("account-pro");
    expect(submitted.thinking).toBe("low");
  });
  it("空官方模型目录保留草稿和选择并禁发，不悄悄退回API模型", async () => {
    fixture(emptyChat);
    render(<ChatWorkspace chatId="chat-one" connected canSubmit modelChoices={[]} />);
    await ready();
    await userEvent.type(screen.getByLabelText("问题或追问"), "稍后发送");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    expect(screen.getByText("当前接入方式尚未提供可用模型，请检查连接状态。")).toBeInTheDocument();
    expect(screen.getByLabelText("问题或追问")).toHaveValue("稍后发送");
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
