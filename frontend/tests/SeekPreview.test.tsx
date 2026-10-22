import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SeekPreview } from "../src/media/SeekPreview";

const source = "/api/v1/videos/synthetic-video/content?source_version=version-one";
const props = { src: source, time: 10, visible: true, left: 90, sourceKey: "synthetic:one" };

beforeEach(() => {
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function decoderAt(video: HTMLVideoElement, duration = 120) {
  let current = 0;
  let seeking = false;
  let ready = 4;
  const writes: number[] = [];
  Object.defineProperty(video, "duration", { configurable: true, get: () => duration });
  Object.defineProperty(video, "readyState", { configurable: true, get: () => ready });
  Object.defineProperty(video, "seeking", { configurable: true, get: () => seeking });
  Object.defineProperty(video, "currentTime", {
    configurable: true,
    get: () => current,
    set: (value) => {
      current = value;
      seeking = true;
      writes.push(value);
    },
  });
  return {
    writes,
    ready: (value: number) => {
      ready = value;
    },
    complete: () => {
      seeking = false;
      fireEvent.seeked(video);
    },
    data: () => {
      ready = 4;
      seeking = false;
      fireEvent.loadedData(video);
    },
  };
}

describe("本地进度预览", () => {
  it("StrictMode挂载清理重放后仍保留受控视频源并能够预览", () => {
    const view = render(
      <StrictMode>
        <SeekPreview {...props} />
      </StrictMode>,
    );
    const video = view.container.querySelector("video")!;
    expect(video).toHaveAttribute("src", new URL(source, window.location.href).href);
    const state = decoderAt(video);
    fireEvent.loadedMetadata(video);
    state.complete();
    expect(screen.getByRole("img")).toHaveAttribute("aria-busy", "false");
  });

  it("隐藏、空身份及外部URL均不创建解码器或播放资源", () => {
    const view = render(<SeekPreview {...props} visible={false} />);
    expect(view.container.querySelector("video")).toBeNull();
    view.rerender(<SeekPreview {...props} src="" />);
    expect(view.container.querySelector("video")).toBeNull();
    view.rerender(<SeekPreview {...props} sourceKey="" />);
    expect(view.container.querySelector("video")).toBeNull();
    view.rerender(<SeekPreview {...props} src="https://example.invalid/video.mp4" />);
    expect(view.container.querySelector("video")).toBeNull();
    view.rerender(<SeekPreview {...props} src="/api/v1/attachments/image" />);
    expect(view.container.querySelector("video")).toBeNull();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    expect(HTMLMediaElement.prototype.load).not.toHaveBeenCalled();
  });

  it("指针时间与位置立即更新，只有一个seek在途，快拖仅保留最新目标", async () => {
    const view = render(<SeekPreview {...props} />);
    const video = view.container.querySelector("video")!;
    const state = decoderAt(video);
    fireEvent.loadedMetadata(video);
    expect(state.writes).toEqual([10]);
    expect(screen.getByRole("img", { name: "视频进度预览" })).toHaveAttribute("aria-busy", "true");

    view.rerender(<SeekPreview {...props} time={20} left={120} />);
    view.rerender(<SeekPreview {...props} time={40} left={170} />);
    expect(screen.getByText("0:40")).toBeInTheDocument();
    expect(screen.getByRole("img")).toHaveStyle({ left: "170px" });
    await act(async () => {});
    expect(state.writes).toEqual([10]);

    state.complete();
    expect(state.writes).toEqual([10, 40]);
    expect(screen.getByRole("img")).toHaveAttribute("data-preview-frame-time", "10");
    expect(screen.getByRole("img")).toHaveAttribute("aria-busy", "true");
    expect(video).not.toHaveClass("ready");
    state.complete();
    expect(screen.getByRole("img")).toHaveAttribute("data-preview-frame-time", "40");
    expect(screen.getByRole("img")).toHaveAttribute("aria-busy", "false");
    expect(video).toHaveClass("ready");
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("新seek仍在进行时的过期seeked事件不会把尚未完成的画面标为就绪", async () => {
    const view = render(<SeekPreview {...props} />);
    const video = view.container.querySelector("video")!;
    const state = decoderAt(video);
    fireEvent.loadedMetadata(video);
    view.rerender(<SeekPreview {...props} time={30} />);
    await act(async () => {});
    state.complete();
    expect(state.writes).toEqual([10, 30]);
    // The first request's delayed media event arrives while the next seek is active.
    fireEvent.seeked(video);
    expect(screen.getByRole("img")).toHaveAttribute("data-preview-frame-time", "10");
    expect(screen.getByRole("img")).toHaveAttribute("aria-busy", "true");
    state.complete();
    expect(screen.getByRole("img")).toHaveAttribute("data-preview-frame-time", "30");
  });

  it("源身份切换销毁旧解码器，旧事件不能将旧画面带进新预览", async () => {
    const view = render(<SeekPreview {...props} />);
    const old = view.container.querySelector("video")!;
    const oldState = decoderAt(old);
    fireEvent.loadedMetadata(old);
    view.rerender(
      <SeekPreview
        {...props}
        sourceKey="synthetic:two"
        src="/api/v1/videos/synthetic-next/content?source_version=two"
        time={70}
      />,
    );
    const next = view.container.querySelector("video")!;
    const nextState = decoderAt(next);
    expect(next).not.toBe(old);
    expect(old).not.toHaveAttribute("src");
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalledOnce();
    expect(HTMLMediaElement.prototype.load).toHaveBeenCalledOnce();
    oldState.complete();
    expect(screen.getByRole("img")).toHaveAttribute("data-preview-frame-time", "");
    fireEvent.loadedMetadata(next);
    nextState.complete();
    expect(nextState.writes).toEqual([70]);
    expect(screen.getByRole("img")).toHaveAttribute("data-preview-frame-time", "70");
  });

  it("预览隐藏时释放自己的资源，未调用播放也不继续后续seek", async () => {
    const view = render(<SeekPreview {...props} />);
    const video = view.container.querySelector("video")!;
    const state = decoderAt(video);
    fireEvent.loadedMetadata(video);
    view.rerender(<SeekPreview {...props} visible={false} time={80} />);
    await act(async () => {});
    state.complete();
    expect(view.container.querySelector("video")).toBeNull();
    expect(video).not.toHaveAttribute("src");
    expect(state.writes).toEqual([10]);
    expect(HTMLMediaElement.prototype.load).toHaveBeenCalledOnce();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("零秒初始帧可由loadeddata完成，不依赖浏览器一定发出零距离seeked", () => {
    const view = render(<SeekPreview {...props} time={0} />);
    const video = view.container.querySelector("video")!;
    const state = decoderAt(video);
    state.ready(1);
    fireEvent.loadedMetadata(video);
    state.data();
    expect(screen.getByRole("img")).toHaveAttribute("aria-busy", "false");
    expect(video).toHaveClass("ready");
  });

  it("媒体解码失败只影响预览，不播放或重试读取其他源", async () => {
    const view = render(<SeekPreview {...props} />);
    const video = view.container.querySelector("video")!;
    const state = decoderAt(video);
    fireEvent.loadedMetadata(video);
    fireEvent.error(video);
    expect(screen.getByText("预览不可用")).toBeInTheDocument();
    view.rerender(<SeekPreview {...props} time={60} />);
    await act(async () => {});
    expect(state.writes).toEqual([10]);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });
});
