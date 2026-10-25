import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { captureWorkspace } from "../src/media/workspaceSnapshot";

const rectangle = (left: number, top: number, width: number, height: number): DOMRect => ({
  left,
  top,
  width,
  height,
  x: left,
  y: top,
  right: left + width,
  bottom: top + height,
  toJSON: () => ({}),
});
const draw = vi.fn();
const rendered: string[] = [];

beforeEach(() => {
  rendered.length = 0;
  draw.mockClear();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    drawImage: draw,
  } as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue(
    "data:image/png;base64,c3ludGhldGlj",
  );
  vi.stubGlobal(
    "Image",
    class {
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      set src(value: string) {
        rendered.push(value);
        queueMicrotask(() => this.onload?.());
      }
    },
  );
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function box(node: Element, left: number, top: number, width: number, height: number) {
  vi.spyOn(node, "getBoundingClientRect").mockReturnValue(rectangle(left, top, width, height));
}

describe("当前软件视口冻结边界", () => {
  it("冻结可见文字和当前视频帧，不读取屏外视频、隐藏内容或截图遮罩", async () => {
    const root = document.createElement("main");
    root.innerHTML =
      "<p>合成可见文字</p><video></video><section><p>滚动区域外的隐藏题文</p><video></video></section><div data-workspace-capture-ignore>截图遮罩</div>";
    document.body.append(root);
    box(root, 0, 0, 320, 200);
    box(root.querySelector("p")!, 0, 0, 200, 30);
    const videos = root.querySelectorAll("video");
    box(videos[0], 0, 30, 320, 170);
    box(root.querySelector("section")!, 0, 400, 320, 200);
    root.querySelector("section")!.style.backgroundImage =
      'url("https://outside.invalid/hidden.png")';
    box(videos[1], 0, 400, 320, 170);
    Object.defineProperties(videos[0], {
      readyState: { value: 2 },
      videoWidth: { value: 640 },
      videoHeight: { value: 340 },
    });
    const result = await captureWorkspace(root);
    expect(result.source.origin).toBe("workspace");
    expect(result.source.source).toBeUndefined();
    expect(result.bounds).toEqual({ left: 0, top: 0, width: 320, height: 200 });
    expect(draw).toHaveBeenNthCalledWith(1, videos[0], 0, 0, 640, 340);
    expect(draw).toHaveBeenCalledTimes(2);
    const svg = decodeURIComponent(rendered[0].slice(rendered[0].indexOf(",") + 1));
    expect(svg).toContain("合成可见文字");
    expect(svg).not.toContain("滚动区域外的隐藏题文");
    expect(svg).not.toContain("截图遮罩");
    expect(svg).not.toContain("<video");
    expect(svg).not.toContain("outside.invalid");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("当前视频未解码时拒绝，不改为读取其他画面", async () => {
    const root = document.createElement("main");
    const video = document.createElement("video");
    root.append(video);
    document.body.append(root);
    box(root, 0, 0, 320, 200);
    box(video, 0, 0, 320, 200);
    Object.defineProperty(video, "readyState", { value: 1 });
    await expect(captureWorkspace(root)).rejects.toMatchObject({ code: "VIDEO_FRAME_UNAVAILABLE" });
    expect(draw).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("只冻结窗口内软件交集，软件完全不可见时拒绝", async () => {
    const root = document.createElement("main");
    document.body.append(root);
    box(root, -40, -20, 320, 200);
    const result = await captureWorkspace(root);
    expect(result.bounds).toEqual({ left: 0, top: 0, width: 280, height: 180 });
    expect(result.source.width).toBe(280);
    expect(result.source.height).toBe(180);
    box(root, -500, 0, 320, 200);
    await expect(captureWorkspace(root)).rejects.toMatchObject({
      code: "CAPTURE_AREA_UNAVAILABLE",
    });
  });

  it("可见外部图片不会联网补取", async () => {
    const root = document.createElement("main");
    const image = document.createElement("img");
    image.src = "https://outside.invalid/private.png";
    root.append(image);
    document.body.append(root);
    box(root, 0, 0, 320, 200);
    box(image, 0, 0, 100, 100);
    await expect(captureWorkspace(root)).rejects.toMatchObject({
      code: "CAPTURE_EXTERNAL_RESOURCE",
    });
    expect(fetch).not.toHaveBeenCalled();
    expect(rendered).toHaveLength(0);
  });

  it("原生题图的本地Blob只冻结已显示像素，不经被CSP禁止的fetch读取", async () => {
    const root = document.createElement("main");
    const image = document.createElement("img");
    image.src = `blob:${location.origin}/confirmed-question`;
    Object.defineProperties(image, {
      complete: { value: true },
      naturalWidth: { value: 480 },
      naturalHeight: { value: 200 },
    });
    root.append(image);
    document.body.append(root);
    box(root, 0, 0, 320, 200);
    box(image, 0, 0, 240, 100);
    vi.mocked(fetch).mockRejectedValue(new TypeError("connect-src self denies blob fetch"));
    await captureWorkspace(root);
    expect(fetch).not.toHaveBeenCalled();
    expect(draw).toHaveBeenNthCalledWith(1, image, 0, 0, 480, 200);
    const svg = decodeURIComponent(rendered[0].slice(rendered[0].indexOf(",") + 1));
    expect(svg).toContain("data:image/png;base64,c3ludGhldGlj");
    expect(svg).not.toContain("blob:");
  });

  it("Blob图片未完成解码时明确拒绝，不补读其他画面", async () => {
    const root = document.createElement("main");
    const image = document.createElement("img");
    image.src = `blob:${location.origin}/pending-question`;
    Object.defineProperty(image, "complete", { value: false });
    root.append(image);
    document.body.append(root);
    box(root, 0, 0, 320, 200);
    box(image, 0, 0, 240, 100);
    vi.mocked(fetch).mockRejectedValue(new TypeError("connect-src self denies blob fetch"));
    await expect(captureWorkspace(root)).rejects.toMatchObject({
      code: "CAPTURE_RESOURCE_UNAVAILABLE",
    });
    expect(fetch).not.toHaveBeenCalled();
    expect(draw).not.toHaveBeenCalled();
  });

  it("可见CSS本地Blob通过图片解码冻结，不放宽连接权限", async () => {
    vi.stubGlobal("Image", function () {
      const image = document.createElement("img");
      Object.defineProperties(image, {
        complete: { value: true },
        naturalWidth: { value: 40 },
        naturalHeight: { value: 20 },
        src: {
          set(value: string) {
            rendered.push(value);
            queueMicrotask(() => image.dispatchEvent(new Event("load")));
          },
        },
      });
      return image;
    });
    const root = document.createElement("main");
    const background = document.createElement("div");
    background.style.backgroundImage = `url("blob:${location.origin}/visible-icon")`;
    root.append(background);
    document.body.append(root);
    box(root, 0, 0, 320, 200);
    box(background, 0, 0, 100, 50);
    await captureWorkspace(root);
    expect(fetch).not.toHaveBeenCalled();
    expect(rendered[0]).toBe(`blob:${location.origin}/visible-icon`);
    const svg = decodeURIComponent(rendered[1].slice(rendered[1].indexOf(",") + 1));
    expect(svg).toContain("data:image/png;base64,c3ludGhldGlj");
    expect(svg).not.toContain("blob:");
    expect(draw).toHaveBeenCalledTimes(2);
  });

  it("外部Blob与屏外Blob都不成为读取权限", async () => {
    const root = document.createElement("main");
    const image = document.createElement("img");
    image.src = "blob:https://outside.invalid/private-picture";
    Object.defineProperties(image, {
      complete: { value: true },
      naturalWidth: { value: 200 },
      naturalHeight: { value: 100 },
    });
    root.append(image);
    document.body.append(root);
    box(root, 0, 0, 320, 200);
    box(image, 0, 0, 200, 100);
    await expect(captureWorkspace(root)).rejects.toMatchObject({
      code: "CAPTURE_EXTERNAL_RESOURCE",
    });
    expect(fetch).not.toHaveBeenCalled();
    expect(draw).not.toHaveBeenCalled();
    box(image, 0, 500, 200, 100);
    await captureWorkspace(root);
    expect(fetch).not.toHaveBeenCalled();
    expect(draw).toHaveBeenCalledTimes(1);
    const svg = decodeURIComponent(rendered[0].slice(rendered[0].indexOf(",") + 1));
    expect(svg).not.toContain("outside.invalid");
  });

  it("零高公式骨架与屏外偏移祖先保留真实可见子孙，不改写基线几何", async () => {
    const root = document.createElement("main");
    root.innerHTML =
      '<span class="vlist" style="display:inline-block;height:0"><span class="numerator">x+1</span><span class="denominator">x−1</span></span><span class="offset"><span class="visible-child">偏移后可见</span></span>';
    document.body.append(root);
    box(root, 0, 0, 320, 200);
    box(root.querySelector(".vlist")!, 30, 90, 60, 0);
    box(root.querySelector(".numerator")!, 35, 50, 50, 20);
    box(root.querySelector(".denominator")!, 35, 95, 50, 20);
    box(root.querySelector(".offset")!, -200, 20, 40, 20);
    box(root.querySelector(".visible-child")!, 100, 20, 100, 20);
    await captureWorkspace(root);
    const svg = decodeURIComponent(rendered[0].slice(rendered[0].indexOf(",") + 1));
    expect(svg).toContain("x+1");
    expect(svg).toContain("x−1");
    expect(svg).toContain("偏移后可见");
    const inlineStyle = svg.match(/class="vlist"[^>]*style="([^"]*)"/)?.[1];
    expect(inlineStyle).toBeDefined();
    expect(inlineStyle).not.toContain("width: 60px");
    expect(draw).toHaveBeenCalledTimes(1);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("继续遍历零高祖先仍不读取屏外视频或图片", async () => {
    const root = document.createElement("main");
    root.innerHTML =
      '<span class="zero"><span class="visible-child">可见公式</span><video></video><img src="https://outside.invalid/offscreen.png"></span>';
    document.body.append(root);
    box(root, 0, 0, 320, 200);
    box(root.querySelector(".zero")!, 0, 40, 100, 0);
    box(root.querySelector(".visible-child")!, 0, 20, 100, 20);
    box(root.querySelector("video")!, 0, 500, 100, 100);
    box(root.querySelector("img")!, 0, 600, 100, 100);
    await captureWorkspace(root);
    const svg = decodeURIComponent(rendered[0].slice(rendered[0].indexOf(",") + 1));
    expect(svg).toContain("可见公式");
    expect(svg).not.toContain("outside.invalid");
    expect(draw).toHaveBeenCalledTimes(1);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("零高且裁切的祖先与完全透明子树仍不包含后代内容", async () => {
    const root = document.createElement("main");
    root.innerHTML =
      '<span class="clipped" style="overflow:hidden;height:0"><span>裁切隐藏文字</span></span><section style="opacity:0"><video></video><p>透明隐藏文字</p></section>';
    document.body.append(root);
    box(root, 0, 0, 320, 200);
    box(root.querySelector(".clipped")!, 0, 50, 100, 0);
    (root.querySelector(".clipped") as HTMLElement).style.overflowY = "hidden";
    box(root.querySelector(".clipped span")!, 0, 20, 100, 20);
    box(root.querySelector("section")!, 0, 100, 200, 100);
    box(root.querySelector("video")!, 0, 100, 100, 100);
    box(root.querySelector("p")!, 0, 100, 100, 20);
    await captureWorkspace(root);
    const svg = decodeURIComponent(rendered[0].slice(rendered[0].indexOf(",") + 1));
    expect(svg).not.toContain("裁切隐藏文字");
    expect(svg).not.toContain("透明隐藏文字");
    expect(draw).toHaveBeenCalledTimes(1);
    expect(fetch).not.toHaveBeenCalled();
  });
});
