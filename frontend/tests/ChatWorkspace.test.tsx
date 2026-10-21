import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Schema } from "../src/api/client";
import { ChatWorkspace } from "../src/chat/ChatWorkspace";

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
const answer = (value: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(value), { status }));

beforeEach(() => {
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("真实执行边界", () => {
  it("同一generation的用户消息仍显示用户输入，不能被回答正文替换", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        answer({
          ...chat,
          messages: [
            { ...message, message_id: "user-one", role: "user", text: "这是我的题目条件" },
            message,
          ],
          generations: [{ ...generation, body: "这是回答正文", state: "completed" }],
        }),
      ),
    );
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    expect(await screen.findByText("这是我的题目条件")).toBeInTheDocument();
    expect(screen.getAllByText("这是回答正文")).toHaveLength(1);
  });

  it("思考增量已活动时可以停止，并保留部分内容与未知用量", async () => {
    const request = vi.fn((url: string) =>
      answer(url.endsWith("/cancel") ? { ...generation, state: "cancelled" } : chat),
    );
    vi.stubGlobal("fetch", request);
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    expect(await screen.findByText("先检查题目条件。")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "停止回答" }));
    expect(await screen.findByText("已停止，保留部分内容")).toBeInTheDocument();
    expect(screen.getByText("先检查题目条件。")).toBeInTheDocument();
    expect(screen.getByText("用量未知")).toBeInTheDocument();
    expect(request.mock.calls.filter(([url]) => url.endsWith("/cancel"))).toHaveLength(1);
  });

  it("执行未知只发核对命令，输入不会触发新派发", async () => {
    const unknown = { ...generation, state: "unknown" };
    const request = vi.fn((url: string) =>
      answer(url.endsWith("/reconcile") ? unknown : { ...chat, generations: [unknown] }),
    );
    vi.stubGlobal("fetch", request);
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await screen.findByText("执行结果未知，需核对");
    await userEvent.type(screen.getByLabelText("问题或追问"), "继续解释");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "核对执行结果" }));
    expect(request.mock.calls.some(([url]) => url.endsWith("/reconcile"))).toBe(true);
    expect(request.mock.calls.some(([url]) => url.endsWith("/submissions"))).toBe(false);
  });

  it("附件必须主动确认；选择与取消均不上传", async () => {
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:test-only"),
      revokeObjectURL: vi.fn(),
    });
    const request = vi.fn((url: string) =>
      answer(
        url.endsWith("/attachments")
          ? {
              attachment_id: "attachment-one",
              media_type: "image/png",
              size_bytes: 5,
              sha256: "test-only",
              confirmed: true,
            }
          : { ...chat, messages: [], generations: [] },
      ),
    );
    vi.stubGlobal("fetch", request);
    render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    const input = screen.getByLabelText("选择待确认的图片");
    const image = new File(["image"], "synthetic.png", { type: "image/png" });
    await userEvent.upload(input, image);
    expect(request.mock.calls.some(([url]) => url.endsWith("/attachments"))).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(request.mock.calls.some(([url]) => url.endsWith("/attachments"))).toBe(false);
    await userEvent.upload(input, image);
    await userEvent.click(screen.getByRole("button", { name: "确认加入" }));
    expect(await screen.findByAltText("待发送的确认图片 1")).toBeInTheDocument();
    expect(request.mock.calls.filter(([url]) => url.endsWith("/attachments"))).toHaveLength(1);
  });

  it("回执丢失与界面重开保留提交身份，只读核对而不自动重发", async () => {
    const request = vi.fn((url: string) => {
      if (url === "/api/v1/submissions") return Promise.reject(new TypeError("ack lost"));
      return answer({ ...chat, messages: [], generations: [] });
    });
    vi.stubGlobal("fetch", request);
    const first = render(<ChatWorkspace chatId="chat-one" connected canSubmit />);
    await userEvent.type(screen.getByLabelText("问题或追问"), "只提交一次");
    await userEvent.click(screen.getByRole("button", { name: "发送" }));
    await screen.findByRole("button", { name: "核对提交" });
    const stored = JSON.parse(sessionStorage.getItem("learning.pending.chat-one")!);
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
});
