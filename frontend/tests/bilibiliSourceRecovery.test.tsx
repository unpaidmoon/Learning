import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, expect, it, vi } from "vitest";
import { api, ApiError } from "../src/api/client";
import type { Schema } from "../src/api/client";
import { BilibiliSourceRecovery } from "../src/integrations/BilibiliSourceRecovery";

const entry: Schema<"CourseFileEntry"> = {
  name: "原资料.bili",
  path: "原资料.bili",
  kind: "file",
  viewer: "video",
  size_bytes: null,
  asset_id: "original-asset",
  source_version: "original-version",
  platform: "bilibili",
};
let scroll: PropertyDescriptor | undefined;
beforeAll(() => {
  scroll = Object.getOwnPropertyDescriptor(Element.prototype, "scrollIntoView");
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: vi.fn(),
  });
});
afterAll(() => {
  if (scroll) Object.defineProperty(Element.prototype, "scrollIntoView", scroll);
  else Reflect.deleteProperty(Element.prototype, "scrollIntoView");
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it("匿名来源只恢复原条目，不导入或自动播放", async () => {
  const write = vi.spyOn(api, "write").mockResolvedValue({
    kind: "anonymous",
    connection: { connection_id: "original-guest", state: "guest", account_label: "匿名访问" },
  });
  const restored = vi.fn();
  render(
    <BilibiliSourceRecovery courseId="a" entry={entry} disabled={false} onRestored={restored} />,
  );
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "恢复来源连接" }));
  });
  expect(write).toHaveBeenCalledTimes(1);
  expect(write.mock.calls[0][0]).toBe("/courses/a/integrations/original-asset/reconnect");
  expect(write.mock.calls[0][1]).toMatchObject({ source_version: "original-version" });
  expect(restored).toHaveBeenCalledOnce();
  expect(screen.getByText(/原来源连接已恢复/)).toBeVisible();
});

it("结果未知保留原操作核对，新来源不会接收旧回调", async () => {
  const write = vi
    .spyOn(api, "write")
    .mockRejectedValueOnce(new ApiError("BACKEND_UNREACHABLE", "synthetic unknown"));
  const restored = vi.fn();
  const view = render(
    <BilibiliSourceRecovery courseId="a" entry={entry} disabled={false} onRestored={restored} />,
  );
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "恢复来源连接" }));
  });
  let complete: (value: unknown) => void = () => {};
  write.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        complete = resolve;
      }),
  );
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "核对原恢复请求" }));
  });
  expect(write.mock.calls[1]).toEqual(write.mock.calls[0]);
  view.rerender(
    <BilibiliSourceRecovery
      courseId="b"
      entry={{ ...entry, asset_id: "other-asset" }}
      disabled={false}
      onRestored={restored}
    />,
  );
  await act(async () => {
    complete({
      kind: "anonymous",
      connection: { connection_id: "old", state: "guest", account_label: "old" },
    });
  });
  expect(restored).not.toHaveBeenCalled();
  expect(screen.queryByText(/原来源连接已恢复/)).not.toBeInTheDocument();
});

it("账号恢复显示原账号，失败检查不自动重发", async () => {
  vi.useFakeTimers();
  const write = vi
    .spyOn(api, "write")
    .mockResolvedValueOnce({
      kind: "login",
      account_label: "原账号10",
      login: {
        login_id: "source-login",
        state: "waiting",
        url: "https://passport.bilibili.com/synthetic-login",
      },
    })
    .mockRejectedValueOnce(new ApiError("BILI_ACCOUNT_MISMATCH", "扫码账号与原来源不同"));
  render(
    <BilibiliSourceRecovery courseId="a" entry={entry} disabled={false} onRestored={vi.fn()} />,
  );
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "恢复来源连接" }));
  });
  expect(screen.getByText(/原来源账号：原账号10/)).toBeVisible();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "检查连接结果" }));
  });
  expect(screen.getByText("扫码账号与原来源不同（BILI_ACCOUNT_MISMATCH）")).toBeVisible();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(6000);
  });
  expect(write).toHaveBeenCalledTimes(2);
});
