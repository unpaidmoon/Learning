import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "../src/api/client";
import type { Schema } from "../src/api/client";
import { PlatformSettings } from "../src/integrations/PlatformSettings";
import { INTEGRATIONS_CHANGED } from "../src/integrations/bilibiliUi";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const moduleView = (state: "expired" | "connected"): Schema<"ModuleView">[] => [
  {
    platform: "bilibili",
    enabled: true,
    implemented: true,
    connection_id: "account-a",
    account_label: "合成账号",
    connection_state: state,
  },
];

it("显示连接失效及重新连接入口，不自动登录或重发操作", async () => {
  const read = vi.spyOn(api, "read").mockResolvedValue(moduleView("expired"));
  const write = vi.spyOn(api, "write");
  render(<PlatformSettings />);
  expect(await screen.findByText("合成账号 · 登录已失效，请重新连接")).toBeVisible();
  expect(screen.getByRole("button", { name: "连接B站账号" })).toBeVisible();
  expect(read).toHaveBeenCalledTimes(1);
  expect(write).not.toHaveBeenCalled();
});

it("状态变化及窗口重新聚焦只刷新本地状态，迟到旧响应不能覆盖", async () => {
  let stale: (value: Schema<"ModuleView">[]) => void = () => {};
  const read = vi
    .spyOn(api, "read")
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          stale = resolve;
        }),
    )
    .mockResolvedValueOnce(moduleView("expired"))
    .mockResolvedValue(moduleView("connected"));
  const write = vi.spyOn(api, "write");
  render(<PlatformSettings />);
  fireEvent(window, new Event(INTEGRATIONS_CHANGED));
  expect(await screen.findByText("合成账号 · 登录已失效，请重新连接")).toBeVisible();
  await act(async () => stale(moduleView("connected")));
  expect(screen.getByText("合成账号 · 登录已失效，请重新连接")).toBeVisible();
  fireEvent(window, new Event("focus"));
  await waitFor(() => expect(screen.queryByText(/登录已失效/)).not.toBeInTheDocument());
  expect(read.mock.calls.every(([path]) => path === "/integrations/modules")).toBe(true);
  expect(write).not.toHaveBeenCalled();
});

it("断开连接会移除旧二维码并停止该扫码轮询", async () => {
  const scroll = Object.getOwnPropertyDescriptor(Element.prototype, "scrollIntoView");
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: vi.fn(),
  });
  vi.useFakeTimers();
  let modules = moduleView("connected");
  vi.spyOn(api, "read").mockImplementation(async () => modules);
  const write = vi.spyOn(api, "write").mockImplementation(async (path) => {
    if (path === "/integrations/bilibili/login")
      return {
        login_id: "login-a",
        state: "waiting",
        url: "https://passport.bilibili.com/synthetic-login",
      };
    if (path.endsWith("/disconnect")) {
      modules = modules.map((m) => ({
        ...m,
        connection_id: null,
        account_label: null,
        connection_state: "disconnected" as const,
      }));
      return { state: "disconnected" };
    }
    throw new Error("Unexpected poll after disconnect");
  });
  try {
    render(<PlatformSettings />);
    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "连接B站账号" }));
    });
    expect(screen.getByRole("img", { name: "请用B站App扫描二维码连接账号" })).toBeVisible();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "断开连接" }));
    });
    expect(
      screen.queryByRole("img", { name: "请用B站App扫描二维码连接账号" }),
    ).not.toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(write).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
    if (scroll) Object.defineProperty(Element.prototype, "scrollIntoView", scroll);
    else Reflect.deleteProperty(Element.prototype, "scrollIntoView");
  }
});
