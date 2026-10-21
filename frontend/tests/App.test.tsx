import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import type { Schema } from "../src/api/client";

vi.mock("../src/media/WorkspaceCapture", () => ({
  WorkspaceCapture: ({ onCancel }: { onCancel: () => void }) => (
    <button onClick={onCancel}>取消合成截图</button>
  ),
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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("后台连接", () => {
  it("显示真实健康能力，不将后台就绪当成问答可用", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/health")) return answer(health);
        if (url.endsWith("/session")) return answer({ csrf_token: "test-only" });
        return answer([]);
      }),
    );
    render(<App />);
    expect(await screen.findByText("后台已连接")).toBeInTheDocument();
    expect(screen.getAllByText("未配置")).toHaveLength(2);
    expect(screen.getAllByText("未接入")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "发送" })).not.toBeInTheDocument();
  });

  it("连接失败后，用户重试会重新获取后台状态", async () => {
    const request = vi.fn().mockRejectedValue(new TypeError("offline"));
    vi.stubGlobal("fetch", request);
    render(<App />);
    expect(await screen.findByText("后台未连接")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("BACKEND_UNREACHABLE");
    request.mockImplementation((url: string) => {
      if (url.endsWith("/health")) return answer(health);
      if (url.endsWith("/session")) return answer({ csrf_token: "test-only" });
      return answer([]);
    });
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

  it("课程切换后，迟到的旧课程列表不会覆盖当前课程", async () => {
    let releaseA: (value: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/health")) return answer(health);
        if (url.endsWith("/session")) return answer({ csrf_token: "test-only" });
        if (url.endsWith("/courses")) {
          return answer([
            { course_id: "course-A", title: "课程 A" },
            { course_id: "course-B", title: "课程 B" },
          ] satisfies Schema<"CourseResponse">[]);
        }
        if (url.endsWith("/course-A/chats")) {
          return new Promise<Response>((resolve) => {
            releaseA = resolve;
          });
        }
        if (url.endsWith("/course-B/chats")) {
          return answer([{ chat_id: "chat-B123", course_id: "course-B" }]);
        }
        return answer([]);
      }),
    );
    render(<App />);
    await screen.findByText("后台已连接");
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-A");
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-B");
    expect(await screen.findByRole("option", { name: "对话 1" })).toHaveValue("chat-B123");
    await act(async () => {
      releaseA(new Response(JSON.stringify([{ chat_id: "chat-A123", course_id: "course-A" }])));
    });
    expect(screen.getByRole("option", { name: "对话 1" })).toHaveValue("chat-B123");
    expect(screen.queryByDisplayValue("chat-A123")).not.toBeInTheDocument();
  });

  it("进入课程不自动打开视频或恢复历史，收放聊天不创建对话", async () => {
    const request = vi.fn((url: string) => {
      if (url.endsWith("/health")) return answer(health);
      if (url.endsWith("/session")) return answer({ csrf_token: "test-only" });
      if (url.endsWith("/courses")) return answer([{ course_id: "course-A", title: "课程 A" }]);
      if (url.endsWith("/course-A/chats"))
        return answer([{ chat_id: "history-chat", course_id: "course-A" }]);
      if (url.endsWith("/course-A/videos")) return answer([]);
      return answer({});
    });
    vi.stubGlobal("fetch", request);
    render(<App />);
    await screen.findByText("后台已连接");
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-A");
    expect(await screen.findByRole("option", { name: "对话 1" })).toHaveValue("history-chat");
    expect(screen.queryByLabelText("课程视频")).not.toBeInTheDocument();
    expect(request.mock.calls.some(([url]) => url.includes("/chats/history-chat"))).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "收起聊天" }));
    expect(screen.getByRole("button", { name: "展开聊天" })).toBeInTheDocument();
    expect(request.mock.calls.filter(([url]) => url.endsWith("/course-A/chats"))).toHaveLength(1);
    expect(screen.getByLabelText("课程与文件")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "展开聊天" }));
    expect(screen.getByRole("button", { name: "收起聊天" })).toBeInTheDocument();
    expect(request.mock.calls.filter(([url]) => url.endsWith("/course-A/chats"))).toHaveLength(1);
  });
  it("截图绑定触发时的课程，完成或取消前不能被课程切换改写", async () => {
    const request = vi.fn((url: string) => {
      if (url.endsWith("/health")) return answer(health);
      if (url.endsWith("/session")) return answer({ csrf_token: "test-only" });
      if (url.endsWith("/courses"))
        return answer([
          { course_id: "course-A", title: "课程 A" },
          { course_id: "course-B", title: "课程 B" },
        ]);
      return answer([]);
    });
    vi.stubGlobal("fetch", request);
    render(<App />);
    await screen.findByText("后台已连接");
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-A");
    fireEvent.keyDown(document, { code: "KeyS", ctrlKey: true, shiftKey: true });
    await screen.findByRole("button", { name: "取消合成截图" });
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-B");
    expect(screen.getByLabelText("选择课程")).toHaveValue("course-A");
    expect(screen.getByRole("alert")).toHaveTextContent("CAPTURE_CONTEXT_LOCKED");
    expect(request.mock.calls.filter(([url]) => url.endsWith("/course-B/chats"))).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "取消合成截图" }));
    await userEvent.selectOptions(screen.getByLabelText("选择课程"), "course-B");
    expect(screen.getByLabelText("选择课程")).toHaveValue("course-B");
  });
  it("账号状态读取失败不会把已就绪的本地学习后台降为离线", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/health")) return answer(health);
        if (url.endsWith("/session")) return answer({ csrf_token: "test-only" });
        if (url.endsWith("/account"))
          return answer({ code: "ACCOUNT_UNAVAILABLE", message: "账号暂不可用" }, 503);
        return answer([]);
      }),
    );
    render(<App />);
    await screen.findByText("后台已连接");
    await userEvent.click(screen.getByRole("button", { name: "重新连接" }));
    expect(await screen.findByText("后台已连接")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开课程文件夹" })).toBeEnabled();
  });
});
