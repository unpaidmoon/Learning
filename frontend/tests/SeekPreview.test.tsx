import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SeekPreview } from "../src/media/SeekPreview";

type Frame = { index: number; time_ms: number; url: string };
type Manifest = { status: string; frames: Frame[]; strategy?: "thumbnails" | "media" };
let sequence = 0;
let identity: string;
let manifest: Manifest;
let images: WarmImage[];
let decoders: HTMLVideoElement[];
let fetchMock: ReturnType<typeof vi.fn>;

class WarmImage {
  src = "";
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    images.push(this);
  }
}
function frame(index: number, time: number, asset = identity): Frame {
  return {
    index,
    time_ms: time * 1000,
    url: `/api/v1/videos/${asset}/previews/${index}.jpg?source_version=version-one`,
  };
}
function properties() {
  return {
    src: `/api/v1/videos/${identity}/content?source_version=version-one`,
    time: 10,
    visible: true,
    left: 90,
    sourceKey: identity,
  };
}
function response(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}
async function settle() {
  await act(async () => {});
}
function loadImage(index: number) {
  const request = images.find((image) =>
    image.src.endsWith(`/${index}.jpg?source_version=version-one`),
  );
  expect(request, `thumbnail ${index} was prefetched`).toBeDefined();
  act(() => request!.onload?.());
}

beforeEach(() => {
  identity = `cache-test-${++sequence}`;
  manifest = { status: "ready", frames: [frame(0, 0), frame(1, 10), frame(2, 20)] };
  images = [];
  decoders = [];
  fetchMock = vi.fn(async (_url, init?: RequestInit) =>
    response(init?.method === "POST" ? {} : manifest),
  );
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("Image", WarmImage);
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(function (
    this: HTMLVideoElement,
  ) {
    if (!decoders.includes(this)) decoders.push(this);
  });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("本地缓存进度预览", () => {
  it("隐藏时提前预载持久图片，首次悬停直接显示已解码的邻近帧", async () => {
    const props = properties();
    const view = render(<SeekPreview {...props} visible={false} />);
    await settle();
    expect(fetchMock.mock.calls[0][0]).toContain(
      `/videos/${identity}/previews?source_version=version-one`,
    );
    expect(images).toHaveLength(3);
    loadImage(1);
    expect(screen.queryByLabelText("视频进度预览")).not.toBeInTheDocument();
    view.rerender(<SeekPreview {...props} time={14} />);
    expect(screen.getByTestId("video-preview-image")).toHaveAttribute("src", frame(1, 10).url);
    expect(screen.getByLabelText("视频进度预览")).toHaveAttribute("data-preview-frame-time", "10");
    expect(screen.getByText("0:14")).toBeInTheDocument();
    expect(screen.queryByText(/正在预览|预览不可用/)).not.toBeInTheDocument();
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("空缓存不把加载占位或仅时间标签冒充缩略图", async () => {
    manifest = { status: "unavailable", frames: [] };
    render(<SeekPreview {...properties()} />);
    await settle();
    expect(screen.queryByLabelText("视频进度预览")).not.toBeInTheDocument();
    expect(screen.queryByText("0:10")).not.toBeInTheDocument();
    expect(screen.queryByText(/正在预览/)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.every(([url]) => !String(url).includes("attachment"))).toBe(true);
  });

  it("指针即时更新目标和位置，较近帧解码期间保留已有真实邻近图且不seek视频", async () => {
    const props = properties();
    const view = render(<SeekPreview {...props} />);
    await settle();
    loadImage(1);
    expect(decoders).toHaveLength(0);
    view.rerender(<SeekPreview {...props} time={19} left={170} />);
    expect(screen.getByText("0:19")).toBeInTheDocument();
    expect(screen.getByLabelText("视频进度预览")).toHaveStyle({ left: "170px" });
    expect(screen.getByTestId("video-preview-image")).toHaveAttribute("src", frame(1, 10).url);
    loadImage(2);
    expect(screen.getByTestId("video-preview-image")).toHaveAttribute("src", frame(2, 20).url);
    expect(screen.getByLabelText("视频进度预览")).toHaveAttribute("data-preview-frame-time", "20");
    expect(decoders).toHaveLength(0);
  });

  it("只预载同一业务视频和来源版本的缩略图，拒绝外部、错版本及带凭据地址", async () => {
    manifest.frames = [
      frame(0, 0),
      frame(1, 10, "different-asset"),
      { ...frame(2, 20), url: frame(2, 20).url.replace("version-one", "version-two") },
      { ...frame(3, 30), url: "https://external.invalid/secret.jpg" },
      {
        ...frame(4, 40),
        url: `${location.protocol}//user:secret@${location.host}${frame(4, 40).url}`,
      },
    ];
    render(<SeekPreview {...properties()} />);
    await settle();
    expect(images.map((image) => image.src)).toEqual([frame(0, 0).url]);
  });

  it("无身份或受控视频源时不请求manifest也不启动本地温热解码", async () => {
    const props = properties();
    const view = render(<SeekPreview {...props} sourceKey="" />);
    for (const src of [
      "",
      "https://external.invalid/video.mp4",
      "/api/v1/attachments/picture",
      `/api/v1/videos/${identity}/content`,
    ]) {
      view.rerender(<SeekPreview {...props} src={src} />);
    }
    await settle();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(decoders).toHaveLength(0);
    expect(screen.queryByLabelText("视频进度预览")).not.toBeInTheDocument();
  });

  it("来源切换后旧manifest和图片迟到不能进入新的预览", async () => {
    const props = properties();
    const oldManifest = manifest;
    let resolveOld: (value: Response) => void = () => {};
    fetchMock.mockImplementation((url, init?: RequestInit) => {
      if (init?.method === "POST") return Promise.resolve(response({}));
      if (String(url).includes(`/videos/${identity}/`))
        return new Promise<Response>((resolve) => {
          resolveOld = resolve;
        });
      return Promise.resolve(response({ status: "ready", frames: [frame(7, 70, "next-source")] }));
    });
    const view = render(<SeekPreview {...props} />);
    view.rerender(
      <SeekPreview
        {...props}
        src="/api/v1/videos/next-source/content?source_version=version-one"
        sourceKey="next-source"
        time={70}
      />,
    );
    await settle();
    await act(async () => resolveOld(response(oldManifest)));
    loadImage(0);
    expect(screen.queryByLabelText("视频进度预览")).not.toBeInTheDocument();
    loadImage(7);
    expect(screen.getByTestId("video-preview-image")).toHaveAttribute(
      "src",
      frame(7, 70, "next-source").url,
    );
  });

  it("StrictMode清理重放保持图片缓存且不额外解码本地视频", async () => {
    render(
      <StrictMode>
        <SeekPreview {...properties()} />
      </StrictMode>,
    );
    await settle();
    expect(decoders).toHaveLength(0);
    expect(screen.queryByLabelText("视频进度预览")).not.toBeInTheDocument();
    loadImage(1);
    expect(screen.getByTestId("video-preview-image")).toHaveAttribute("src", frame(1, 10).url);
  });

  it("悬停提交目标时间并在隐藏时释放预览租约，缓存下次仍可使用", async () => {
    vi.useFakeTimers();
    const props = properties();
    const view = render(<SeekPreview {...props} />);
    await settle();
    loadImage(1);
    const gets = () => fetchMock.mock.calls.filter(([, init]) => init?.method !== "POST").length;
    await act(async () => vi.advanceTimersByTime(90));
    const before = gets();
    view.rerender(<SeekPreview {...props} visible={false} />);
    await settle();
    const priority = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes("/video-previews/priority"),
    );
    expect(priority.map(([, init]) => JSON.parse(init!.body as string).active)).toEqual([
      true,
      false,
    ]);
    expect(JSON.parse(priority[0][1]!.body as string)).toMatchObject({
      kind: "preview",
      asset_id: identity,
      source_version: "version-one",
      time_ms: 10000,
    });
    expect(screen.queryByLabelText("视频进度预览")).not.toBeInTheDocument();
    view.rerender(<SeekPreview {...props} />);
    expect(screen.getByTestId("video-preview-image")).toBeInTheDocument();
    expect(gets()).toBe(before);
  });

  it("卸载取消building轮询；迟到响应不再排入下一轮请求", async () => {
    vi.useFakeTimers();
    manifest.status = "building";
    const view = render(<SeekPreview {...properties()} visible={false} />);
    await settle();
    const initial = fetchMock.mock.calls.length;
    view.unmount();
    await act(async () => vi.advanceTimersByTime(2000));
    expect(fetchMock.mock.calls).toHaveLength(initial);
    expect(decoders).toHaveLength(0);
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });

  it("长视频预热的图片请求数受控，而不是一次加载全部缩略图", async () => {
    manifest.frames = Array.from({ length: 600 }, (_, index) => frame(index, index * 5));
    render(<SeekPreview {...properties()} visible={false} />);
    await settle();
    expect(images.length).toBeGreaterThan(1);
    expect(images.length).toBeLessThanOrEqual(3);
  });

  it("在线预览只在悬停未命中时解码，追随最新目标并在隐藏时释放", async () => {
    vi.useFakeTimers();
    manifest = { status: "ready", strategy: "media", frames: [] };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      fillRect: vi.fn(),
      drawImage: vi.fn(),
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue(
      "data:image/jpeg;base64,c3ludGhldGlj",
    );
    const props = { ...properties(), time: 12 };
    const view = render(<SeekPreview {...props} visible={false} />);
    await settle();
    expect(decoders).toHaveLength(0);
    view.rerender(<SeekPreview {...props} />);
    await act(async () => vi.advanceTimersByTime(90));
    expect(decoders).toHaveLength(1);
    const video = decoders[0];
    for (const [key, value] of Object.entries({
      duration: 60,
      readyState: 2,
      videoWidth: 160,
      videoHeight: 90,
    }))
      Object.defineProperty(video, key, { configurable: true, value });
    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(12);
    view.rerender(<SeekPreview {...props} time={30} />);
    await act(async () => vi.advanceTimersByTime(90));
    expect(screen.queryByLabelText("视频进度预览")).not.toBeInTheDocument();
    fireEvent.seeked(video);
    expect(video.currentTime).toBe(30);
    fireEvent.seeked(video);
    expect(screen.getByLabelText("视频进度预览")).toHaveAttribute("data-preview-frame-time", "30");
    view.rerender(<SeekPreview {...props} time={30} visible={false} />);
    expect(video).not.toHaveAttribute("src");
    view.rerender(<SeekPreview {...props} time={30} />);
    await act(async () => vi.advanceTimersByTime(90));
    expect(decoders).toHaveLength(1); // A cached time does not reopen a decoder.
    expect(fetchMock.mock.calls.every(([, init]) => init?.method !== "POST")).toBe(true);
  });

  it("在线视频解码失败后释放资源，指针移动不隐式重发媒体请求", async () => {
    vi.useFakeTimers();
    manifest = { status: "ready", strategy: "media", frames: [] };
    const props = properties();
    const view = render(<SeekPreview {...props} />);
    await settle();
    await act(async () => vi.advanceTimersByTime(90));
    expect(decoders).toHaveLength(1);
    fireEvent.error(decoders[0]);
    expect(decoders[0]).not.toHaveAttribute("src");
    view.rerender(<SeekPreview {...props} time={40} />);
    await act(async () => vi.advanceTimersByTime(200));
    expect(decoders).toHaveLength(1);
    expect(screen.queryByLabelText("视频进度预览")).not.toBeInTheDocument();
  });
});
