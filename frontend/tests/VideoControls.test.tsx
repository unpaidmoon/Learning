import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { VideoControls } from "../src/media/VideoControls";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const controls = () => ({
  playing: true,
  elapsed: 10,
  duration: 100,
  volume: 1,
  muted: false,
  rate: 1,
  onFullscreen: vi.fn().mockResolvedValue(undefined),
  onSeek: vi.fn(),
  onTogglePlay: vi.fn(),
  onMute: vi.fn(),
  onVolume: vi.fn(),
  onRate: vi.fn(),
});

describe("进度条拖动", () => {
  it("鼠标悬停只更新预览，连续移动的预览目标不改变播放进度", () => {
    vi.stubGlobal("PointerEvent", MouseEvent);
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    const props = controls();
    render(
      <VideoControls
        {...props}
        previewSrc="/api/v1/videos/asset-one/content?source_version=version-one"
        sourceKey="asset-one:version-one"
      />,
    );
    const range = screen.getByRole("slider", { name: "视频播放进度" });
    const box = { left: 10, width: 214, right: 224, top: 0, height: 14, bottom: 14 };
    vi.spyOn(range, "getBoundingClientRect").mockReturnValue(box as DOMRect);
    vi.spyOn(range.parentElement!, "getBoundingClientRect").mockReturnValue(box as DOMRect);
    fireEvent.pointerMove(range, { clientX: 67 });
    expect(screen.getByLabelText("视频进度预览")).toHaveAttribute("data-preview-time", "25");
    expect(range).toHaveValue("10");
    fireEvent.pointerMove(range, { clientX: 167 });
    expect(screen.getByLabelText("视频进度预览")).toHaveAttribute("data-preview-time", "75");
    expect(range).toHaveValue("10");
    expect(props.onSeek).not.toHaveBeenCalled();
  });

  it("指针坐标即时驱动圆点，不受播放timeupdate覆盖，释放位置只提交一次", () => {
    vi.stubGlobal("PointerEvent", MouseEvent);
    const props = controls();
    const view = render(<VideoControls {...props} />);
    const range = screen.getByRole("slider", { name: "视频播放进度" });
    vi.spyOn(range, "getBoundingClientRect").mockReturnValue({
      left: 10,
      width: 214,
    } as DOMRect);
    fireEvent.pointerDown(range, { clientX: 67, button: 0 });
    expect(range).toHaveValue("25");
    fireEvent.pointerMove(range, { clientX: 167 });
    expect(range).toHaveValue("75");
    view.rerender(<VideoControls {...props} elapsed={12} />);
    expect(range).toHaveValue("75");
    expect(props.onSeek).not.toHaveBeenCalled();
    fireEvent.pointerUp(range, { clientX: 167, button: 0 });
    expect(props.onSeek).toHaveBeenCalledExactlyOnceWith(75);
  });

  it("拖动时只更新跟手位置，主视频继续原进度，松开才提交一次跳转", () => {
    const props = controls();
    const view = render(<VideoControls {...props} />);
    const range = screen.getByRole("slider", { name: "视频播放进度" });
    fireEvent.pointerDown(range, { pointerId: 1, button: 0 });
    fireEvent.change(range, { target: { value: "60" } });
    expect(props.onSeek).not.toHaveBeenCalled();
    expect(range).toHaveValue("60");
    view.rerender(<VideoControls {...props} elapsed={12} />);
    expect(range).toHaveValue("60");
    fireEvent.pointerUp(range, { pointerId: 1, button: 0 });
    expect(props.onSeek).toHaveBeenCalledExactlyOnceWith(60);
    expect(props.onTogglePlay).not.toHaveBeenCalled();
  });

  it("取消拖动不跳转也不改变播放状态，进度回到实际播放位置", () => {
    const props = { ...controls(), playing: false };
    const view = render(<VideoControls {...props} />);
    const range = screen.getByRole("slider", { name: "视频播放进度" });
    fireEvent.pointerDown(range, { pointerId: 1, button: 0 });
    fireEvent.change(range, { target: { value: "70" } });
    view.rerender(<VideoControls {...props} elapsed={11} />);
    fireEvent.pointerCancel(range, { pointerId: 1 });
    expect(range).toHaveValue("11");
    expect(props.onSeek).not.toHaveBeenCalled();
    expect(props.onTogglePlay).not.toHaveBeenCalled();
  });

  it("键盘调整进度不进入指针拖动队列", () => {
    const props = controls();
    render(<VideoControls {...props} />);
    fireEvent.change(screen.getByRole("slider", { name: "视频播放进度" }), {
      target: { value: "12" },
    });
    expect(props.onSeek).toHaveBeenCalledExactlyOnceWith(12);
  });
});
