import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { VideoWorkspace } from "../src/media/VideoWorkspace";
import type { VideoWorkspaceHandle } from "../src/media/VideoWorkspace";
import type { Schema } from "../src/api/client";

const asset: Schema<"MediaAssetResponse"> = {
  asset_id: "asset-synthetic",
  source_version: "source-one",
  display_name: "合成数学课.mp4",
  availability: "available",
  position_ms: 3500,
  position_revision: 4,
  last_opened_at: null,
  media_type: "video/mp4",
  size_bytes: 12345,
};
const answer = (value: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(value), { status }));
const noop = () => {};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(document, "fullscreenElement");
  Reflect.deleteProperty(document, "exitFullscreen");
});

function renderPlayer() {
  const ref = createRef<VideoWorkspaceHandle>();
  render(
    <VideoWorkspace
      ref={ref}
      courseId="course-A"
      connected
      onLibraryChange={noop}
      onCapture={noop}
      onOpenChat={noop}
      onImmersiveChange={noop}
    />,
  );
  return ref;
}

describe("原地视频播放器", () => {
  it("新导入视频身份不在初始清单时读取当前课程清单并打开，不重复打开同标签", async () => {
    let listings = 0;
    const request = vi.fn((url: string) => {
      if (url.endsWith("/videos")) return answer(++listings === 1 ? [] : [asset]);
      return answer(asset);
    });
    vi.stubGlobal("fetch", request);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    const props = {
      courseId: "course-A",
      connected: true,
      onLibraryChange: noop,
      onOpenChat: noop,
      onImmersiveChange: noop,
      assetId: asset.asset_id,
      minimal: true,
    };
    const view = render(<VideoWorkspace {...props} />);
    expect(await screen.findByLabelText("课程视频")).toBeInTheDocument();
    expect(request.mock.calls.filter(([url]) => url.endsWith("/open"))).toHaveLength(1);
    view.rerender(<VideoWorkspace {...props} />);
    expect(request.mock.calls.filter(([url]) => url.endsWith("/open"))).toHaveLength(1);
  });

  it("进入课程只列目录，主动打开才请求来源与恢复播放", async () => {
    const request = vi.fn((url: string) => answer(url.endsWith("/videos") ? [asset] : asset));
    vi.stubGlobal("fetch", request);
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    renderPlayer();
    await screen.findByRole("button", { name: /合成数学课/ });
    expect(screen.queryByLabelText("课程视频")).not.toBeInTheDocument();
    expect(request.mock.calls).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: /合成数学课/ }));
    const player = (await screen.findByLabelText("课程视频")) as HTMLVideoElement;
    Object.defineProperty(player, "duration", { configurable: true, value: 30 });
    fireEvent.loadedMetadata(player);
    expect(player.currentTime).toBe(3.5);
    expect(player.src).toContain(
      "/api/v1/videos/asset-synthetic/content?source_version=source-one",
    );
    expect(player.src.startsWith("blob:")).toBe(false);
    expect(play).toHaveBeenCalledOnce();
  });

  it("暂停与seek并发保存串行使用已接受的位置修订", async () => {
    const writes: Array<{ position_ms: number; expected_revision: number }> = [];
    let release: (() => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, options?: RequestInit) => {
        if (url.endsWith("/videos")) return answer([asset]);
        if (url.endsWith("/open")) return answer(asset);
        if (url.endsWith("/position")) {
          const body = JSON.parse(String(options?.body));
          writes.push(body);
          if (writes.length === 1)
            return new Promise<Response>((resolve) => {
              release = () =>
                resolve(
                  new Response(
                    JSON.stringify({ position_ms: body.position_ms, position_revision: 5 }),
                  ),
                );
            });
          return answer({ position_ms: body.position_ms, position_revision: 6 });
        }
        return answer({});
      }),
    );
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    const ref = renderPlayer();
    await screen.findByRole("button", { name: /合成数学课/ });
    await userEvent.click(screen.getByRole("button", { name: /合成数学课/ }));
    const player = (await screen.findByLabelText("课程视频")) as HTMLVideoElement;
    Object.defineProperty(player, "readyState", { configurable: true, value: 4 });
    player.currentTime = 4;
    fireEvent.seeked(player);
    await waitFor(() => expect(writes).toHaveLength(1));
    player.currentTime = 2;
    fireEvent.pause(player);
    expect(writes).toHaveLength(1);
    await act(async () => {
      release?.();
      await ref.current?.savePosition();
    });
    expect(writes.map((value) => [value.position_ms, value.expected_revision])).toEqual([
      [4000, 4],
      [2000, 5],
    ]);
  });

  it("来源版本失效明确提示，不为旧引用打开另一个版本", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/videos")) return answer([asset]);
        return answer(
          { code: "SOURCE_VERSION_CHANGED", message: "原件内容已变化，请检查版本" },
          409,
        );
      }),
    );
    renderPlayer();
    await userEvent.click(await screen.findByRole("button", { name: /合成数学课/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("SOURCE_VERSION_CHANGED");
    expect(screen.queryByLabelText("课程视频")).not.toBeInTheDocument();
  });

  it("短按方向键跳转两秒，长按右键只临时改变倍速且释放后恢复", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => answer(url.endsWith("/videos") ? [asset] : asset)),
    );
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    renderPlayer();
    await userEvent.click(await screen.findByRole("button", { name: /合成数学课/ }));
    const player = (await screen.findByLabelText("课程视频")) as HTMLVideoElement;
    Object.defineProperty(player, "duration", { configurable: true, value: 60 });
    player.currentTime = 10;
    fireEvent.keyDown(window, { key: "ArrowLeft" });
    expect(player.currentTime).toBe(8);
    fireEvent.keyDown(window, { key: "ArrowRight" });
    fireEvent.keyUp(window, { key: "ArrowRight" });
    expect(player.currentTime).toBe(10);
    player.playbackRate = 1.5;
    fireEvent.keyDown(window, { key: "ArrowRight" });
    await waitFor(() => expect(player.playbackRate).toBe(2), { timeout: 1000 });
    fireEvent.keyUp(window, { key: "ArrowRight" });
    expect(player.playbackRate).toBe(1.5);
    expect(player.currentTime).toBe(10);
  });

  it("文本输入和截图遮罩保留播放器键位，不改变进度或静音", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => answer(url.endsWith("/videos") ? [asset] : asset)),
    );
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    renderPlayer();
    await userEvent.click(await screen.findByRole("button", { name: /合成数学课/ }));
    const player = (await screen.findByLabelText("课程视频")) as HTMLVideoElement;
    player.currentTime = 10;
    const input = screen.getByLabelText("视频路径");
    fireEvent.keyDown(input, { key: "ArrowLeft" });
    fireEvent.keyDown(input, { key: "m" });
    fireEvent.keyDown(input, { key: " ", code: "Space" });
    expect(player.currentTime).toBe(10);
    expect(player.muted).toBe(false);
    expect(play).not.toHaveBeenCalled();
    fireEvent.keyDown(window, { key: "ArrowRight" });
    fireEvent.keyUp(input, { key: "ArrowRight" });
    expect(player.currentTime).toBe(10);
    const overlay = document.createElement("div");
    overlay.dataset.workspaceCaptureOverlay = "true";
    document.body.append(overlay);
    fireEvent.keyDown(window, { key: "ArrowLeft" });
    fireEvent.keyDown(window, { key: "m" });
    expect(player.currentTime).toBe(10);
    expect(player.muted).toBe(false);
    overlay.remove();
  });

  it("滚轮仅在非全屏音量控件或全屏视频区调整音量", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => answer(url.endsWith("/videos") ? [asset] : asset)),
    );
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    renderPlayer();
    await userEvent.click(await screen.findByRole("button", { name: /合成数学课/ }));
    const player = (await screen.findByLabelText("课程视频")) as HTMLVideoElement;
    player.volume = 0.5;
    fireEvent.wheel(screen.getByLabelText("视频画面"), { deltaY: -20 });
    expect(player.volume).toBe(0.5);
    fireEvent.wheel(screen.getByLabelText("视频音量"), { deltaY: -20 });
    expect(player.volume).toBeCloseTo(0.55);
    Object.defineProperty(document, "fullscreenElement", {
      configurable: true,
      value: screen.getByLabelText("视频画面"),
    });
    fireEvent.wheel(screen.getByLabelText("视频画面"), { deltaY: 20 });
    expect(player.volume).toBeCloseTo(0.5);
    const chat = document.createElement("div");
    document.body.append(chat);
    fireEvent.wheel(chat, { deltaY: 20 });
    expect(player.volume).toBeCloseTo(0.5);
    chat.remove();
  });

  it("双击请求整个工作区全屏，不叠加单击播放动作", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => answer(url.endsWith("/videos") ? [asset] : asset)),
    );
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    const root = document.createElement("div");
    root.requestFullscreen = vi.fn().mockResolvedValue(undefined);
    document.body.append(root);
    render(
      <VideoWorkspace
        courseId="course-A"
        connected
        onLibraryChange={noop}
        onOpenChat={noop}
        onImmersiveChange={noop}
        workspaceRef={{ current: root }}
      />,
    );
    await userEvent.click(await screen.findByRole("button", { name: /合成数学课/ }));
    const player = await screen.findByLabelText("课程视频");
    fireEvent.click(player, { detail: 1 });
    fireEvent.click(player, { detail: 2 });
    fireEvent.doubleClick(player);
    await waitFor(() => expect(root.requestFullscreen).toHaveBeenCalledOnce());
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(play).not.toHaveBeenCalled();
    expect(pause).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "截图提问" })).not.toBeInTheDocument();
    root.remove();
  });

  it("ESC显式退出当前工作区全屏，输入焦点不阻止，截图取消优先且不操作其他全屏目标", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => answer([asset])),
    );
    const root = document.createElement("main");
    const other = document.createElement("div");
    const exit = vi.fn().mockResolvedValue(undefined);
    let fullscreen: Element | null = root;
    Object.defineProperty(document, "fullscreenElement", {
      configurable: true,
      get: () => fullscreen,
    });
    Object.defineProperty(document, "exitFullscreen", { configurable: true, value: exit });
    render(
      <VideoWorkspace
        courseId="course-A"
        connected
        onLibraryChange={noop}
        onOpenChat={noop}
        onImmersiveChange={noop}
        workspaceRef={{ current: root }}
      />,
    );
    await screen.findByRole("button", { name: /合成数学课/ });
    const input = screen.getByLabelText("视频路径");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(exit).toHaveBeenCalledOnce();
    const overlay = document.createElement("div");
    overlay.dataset.workspaceCaptureOverlay = "true";
    document.body.append(overlay);
    fireEvent.keyDown(input, { key: "Escape" });
    expect(exit).toHaveBeenCalledOnce();
    overlay.remove();
    fullscreen = other;
    fireEvent.keyDown(input, { key: "Escape" });
    expect(exit).toHaveBeenCalledOnce();
    fullscreen = null;
    fireEvent.keyDown(input, { key: "Escape" });
    expect(exit).toHaveBeenCalledOnce();
  });

  it("ESC退出失败给出稳定代码，保留可重试状态", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => answer([asset])),
    );
    const root = document.createElement("main");
    const exit = vi.fn().mockRejectedValue(new Error("synthetic fullscreen rejection"));
    Object.defineProperty(document, "fullscreenElement", { configurable: true, value: root });
    Object.defineProperty(document, "exitFullscreen", { configurable: true, value: exit });
    render(
      <VideoWorkspace
        courseId="course-A"
        connected
        onLibraryChange={noop}
        onOpenChat={noop}
        onImmersiveChange={noop}
        workspaceRef={{ current: root }}
      />,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(await screen.findByRole("alert")).toHaveTextContent("FULLSCREEN_EXIT_FAILED");
    exit.mockResolvedValue(undefined);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(exit).toHaveBeenCalledTimes(2);
  });

  it("切离标签暂停，切回只恢复此前正在播放的视频", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => answer(url.endsWith("/videos") ? [asset] : asset)),
    );
    let paused = false;
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(async () => {
      paused = false;
    });
    const pause = vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {
      paused = true;
    });
    const props = {
      courseId: "course-A",
      connected: true,
      onLibraryChange: noop,
      onOpenChat: noop,
      onImmersiveChange: noop,
    };
    const view = render(<VideoWorkspace {...props} active />);
    await userEvent.click(await screen.findByRole("button", { name: /合成数学课/ }));
    const player = (await screen.findByLabelText("课程视频")) as HTMLVideoElement;
    Object.defineProperty(player, "paused", { configurable: true, get: () => paused });
    paused = false;
    view.rerender(<VideoWorkspace {...props} active={false} />);
    expect(pause).toHaveBeenCalled();
    expect(paused).toBe(true);
    view.rerender(<VideoWorkspace {...props} active />);
    await waitFor(() => expect(play).toHaveBeenCalledOnce());
    paused = true;
    view.rerender(<VideoWorkspace {...props} active={false} />);
    view.rerender(<VideoWorkspace {...props} active />);
    expect(play).toHaveBeenCalledOnce();
    expect(paused).toBe(true);
  });
});
