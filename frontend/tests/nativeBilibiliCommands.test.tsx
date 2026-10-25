import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../src/api/client";
import type { Schema } from "../src/api/client";
import { BilibiliImport } from "../src/integrations/BilibiliImport";
import {
  closeBilibiliImport,
  INTEGRATIONS_CHANGED,
  requestBilibiliImport,
} from "../src/integrations/bilibiliUi";
import { installBilibiliCommand } from "../src/integrations/nativeCommands";

type Entry = Parameters<Parameters<typeof installBilibiliCommand>[0]["commandUi"]["register"]>[0];
const moduleView = (enabled = true, connection = "account-a"): Schema<"ModuleView"> => ({
  platform: "bilibili",
  enabled,
  implemented: true,
  connection_id: connection,
  connection_state: "connected",
  account_label: "合成账号",
});
const controllers: ReturnType<typeof installBilibiliCommand>[] = [];

function setup(enabled = true) {
  let modules = [moduleView(enabled)];
  let scope = { sessionId: "s1", courseId: "a", navigating: false };
  const entries = new Map<string, Entry>();
  const events = new EventTarget();
  const read = vi.spyOn(api, "read").mockImplementation(async () => modules);
  const commandUi = {
    register: vi.fn((entry: Entry) => {
      entries.set(entry.name, entry);
      return () => {
        entries.delete(entry.name);
      };
    }),
    dismiss: vi.fn(),
  };
  const openImport = vi.fn(),
    onError = vi.fn();
  const controller = installBilibiliCommand({
    commandUi,
    getScope: () => scope,
    openImport,
    onError,
    events,
  });
  controllers.push(controller);
  return {
    controller,
    entries,
    events,
    read,
    commandUi,
    openImport,
    onError,
    setModules: (value: Schema<"ModuleView">[]) => {
      modules = value;
    },
    setScope: (value: typeof scope) => {
      scope = value;
    },
  };
}

afterEach(() => {
  controllers.splice(0).forEach((controller) => controller.dispose());
  cleanup();
  closeBilibiliImport("a");
  closeBilibiliImport("b");
  vi.restoreAllMocks();
});

describe("Harness原生B站入口", () => {
  it("全部关闭时不注册说明，启用且已连接后才注册一个原生action", async () => {
    const env = setup(false);
    await env.controller.ready;
    expect(env.entries.size).toBe(0);
    expect(env.commandUi.register).not.toHaveBeenCalled();
    env.setModules([moduleView()]);
    env.events.dispatchEvent(new Event(INTEGRATIONS_CHANGED));
    await waitFor(() => expect(env.entries.size).toBe(1));
    const entry = env.entries.get("bilibili")!;
    expect(entry.ui.kind).toBe("action");
    expect(entry.available({ sessionId: "s1" })).toBe(true);
    expect(entry.available({ sessionId: "s2" })).toBe(false);
    expect(env.read.mock.calls.every(([path]) => path === "/integrations/modules")).toBe(true);
  });

  it("执行前重读本地状态，停用后旧回调不能打开导入视图", async () => {
    const env = setup();
    await env.controller.ready;
    const old = env.entries.get("bilibili")!;
    env.setModules([moduleView(false)]);
    old.ui.run({ sessionId: "s1" });
    await waitFor(() => expect(env.onError).toHaveBeenCalled());
    expect(env.entries.size).toBe(0);
    expect(env.openImport).not.toHaveBeenCalled();
    expect(env.onError.mock.calls[0][0].code).toBe("BILI_ENTRY_SCOPE_CHANGED");
  });

  it("连接或课程在核对途中改变时拒绝旧入口", async () => {
    const env = setup();
    await env.controller.ready;
    const old = env.entries.get("bilibili")!;
    let finish!: (value: Schema<"ModuleView">[]) => void;
    env.read.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    old.ui.run({ sessionId: "s1" });
    env.setScope({ sessionId: "s2", courseId: "b", navigating: false });
    finish([moduleView(true, "account-b")]);
    await waitFor(() => expect(env.onError).toHaveBeenCalled());
    expect(env.openImport).not.toHaveBeenCalled();
  });

  it("更新或卸载后的迟到回调不会恢复旧注册或打开视图", async () => {
    const env = setup();
    await env.controller.ready;
    const old = env.entries.get("bilibili")!;
    let finish!: (value: Schema<"ModuleView">[]) => void;
    env.read.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    old.ui.run({ sessionId: "s1" });
    env.controller.dispose();
    finish([moduleView()]);
    await Promise.resolve();
    await Promise.resolve();
    expect(env.openImport).not.toHaveBeenCalled();
    expect(env.entries.size).toBe(0);
    expect(env.onError).not.toHaveBeenCalled();
  });

  it("另一次状态核对使执行中的旧核对失效，不能沿用缓存放行", async () => {
    const env = setup();
    await env.controller.ready;
    const old = env.entries.get("bilibili")!;
    let finish!: (value: Schema<"ModuleView">[]) => void;
    env.read.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    old.ui.run({ sessionId: "s1" });
    await env.controller.refresh();
    finish([moduleView()]);
    await waitFor(() => expect(env.onError).toHaveBeenCalled());
    expect(env.openImport).not.toHaveBeenCalled();
  });

  it("原生action复用课程内手选视图，不解析链接或发送问答", async () => {
    const env = setup();
    await env.controller.ready;
    env.openImport.mockImplementation(requestBilibiliImport);
    env.entries.get("bilibili")!.ui.run({ sessionId: "s1" });
    await waitFor(() => expect(env.openImport).toHaveBeenCalledWith("a"));
    render(<BilibiliImport courseId="a" connected onImported={vi.fn()} />);
    expect(screen.getByRole("region", { name: "导入B站资料" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "导入B站链接" }));
    expect(screen.queryByRole("region", { name: "导入B站资料" })).toBeNull();
    act(() => {
      requestBilibiliImport("b");
    });
    expect(screen.queryByRole("region", { name: "导入B站资料" })).toBeNull();
    expect(env.read.mock.calls.every(([path]) => path === "/integrations/modules")).toBe(true);
  });
});
