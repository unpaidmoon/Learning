import { act, cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import type { Schema } from "../src/api/client";

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
    expect(await screen.findByText("chat-B12")).toBeInTheDocument();
    await act(async () => {
      releaseA(new Response(JSON.stringify([{ chat_id: "chat-A123", course_id: "course-A" }])));
    });
    expect(screen.getByText("chat-B12")).toBeInTheDocument();
    expect(screen.queryByText("chat-A12")).not.toBeInTheDocument();
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
    await screen.findByText("history-");
    expect(screen.queryByLabelText("课程视频")).not.toBeInTheDocument();
    expect(request.mock.calls.some(([url]) => url.includes("/chats/history-chat"))).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "收起聊天" }));
    expect(screen.getByRole("button", { name: "展开聊天" })).toBeInTheDocument();
    expect(request.mock.calls.filter(([url]) => url.endsWith("/course-A/chats"))).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "收起课程目录" }));
    expect(screen.getByLabelText("课程与对话")).not.toBeVisible();
    expect(screen.getByRole("button", { name: "展开课程目录" })).toBeInTheDocument();
  });
});
