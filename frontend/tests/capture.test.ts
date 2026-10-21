import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CaptureError,
  captureVideoFrame,
  checkImageDimensions,
  exportCrop,
  normalizeCrop,
} from "../src/media/capture";
import type { CaptureSource } from "../src/media/capture";

describe("confirmed source-pixel crop", () => {
  it("preserves an exact source rectangle without scaling", () => {
    expect(normalizeCrop({ x: 160, y: 90, width: 320, height: 180 }, 640, 360)).toEqual({
      x: 160,
      y: 90,
      width: 320,
      height: 180,
    });
  });

  it("rounds fractional edges inward so no pixel outside the selection is added", () => {
    expect(normalizeCrop({ x: 10.2, y: 20.8, width: 100.6, height: 40.4 }, 640, 360)).toEqual({
      x: 11,
      y: 21,
      width: 99,
      height: 40,
    });
  });

  it("clips selection to the original and rejects empty or nonfinite ranges", () => {
    expect(normalizeCrop({ x: -20, y: -10, width: 700, height: 400 }, 640, 360)).toEqual({
      x: 0,
      y: 0,
      width: 640,
      height: 360,
    });
    expect(() => normalizeCrop({ x: 640, y: 0, width: 1, height: 1 }, 640, 360)).toThrow(
      CaptureError,
    );
    expect(() => normalizeCrop({ x: 0.5, y: 0, width: 0.2, height: 1 }, 640, 360)).toThrow(
      CaptureError,
    );
    expect(() => normalizeCrop({ x: NaN, y: 0, width: 10, height: 10 }, 640, 360)).toThrow(
      CaptureError,
    );
  });

  it("rejects zero, fractional and excessive input dimensions", () => {
    for (const dimensions of [
      [0, 1],
      [0.5, 2],
      [10_000, 10_000],
    ]) {
      expect(() => checkImageDimensions(dimensions[0], dimensions[1])).toThrow(CaptureError);
    }
  });
});

describe("browser-only capture and export", () => {
  const drawImage = vi.fn();
  const output = "data:image/png;base64,YQ==";
  let naturalWidth = 640;

  beforeEach(() => {
    naturalWidth = 640;
    drawImage.mockClear();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage,
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue(output);
    vi.stubGlobal(
      "Image",
      class SyntheticImage {
        naturalWidth = naturalWidth;
        naturalHeight = 360;
        onload: (() => void) | null = null;
        onerror: (() => void) | null = null;
        set src(_url: string) {
          queueMicrotask(() => this.onload?.());
        }
      },
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("pauses first and records the video element's actual decoded dimensions and time", () => {
    const pause = vi.fn();
    const video = {
      pause,
      readyState: 2,
      currentTime: 12.345,
      videoWidth: 640,
      videoHeight: 360,
    } as unknown as HTMLVideoElement;
    const source = captureVideoFrame(video, {
      asset_id: "asset-synthetic",
      source_version: "v1",
      time_ms: 0,
    });
    expect(pause).toHaveBeenCalledOnce();
    expect(drawImage).toHaveBeenCalledExactlyOnceWith(video, 0, 0, 640, 360);
    expect(source.source?.time_ms).toBe(12_345);
    expect(source.width).toBe(640);
    expect(source.dataUrl).toBe(output);
  });

  it("does not attempt to read a frame that is not decoded", () => {
    const video = {
      pause: vi.fn(),
      readyState: 1,
      currentTime: 0,
    } as unknown as HTMLVideoElement;
    expect(() => captureVideoFrame(video)).toThrow(CaptureError);
    expect(video.pause).toHaveBeenCalledOnce();
    expect(drawImage).not.toHaveBeenCalled();
  });

  it("exports only the exact confirmed pixels and retains the original identity for re-edit", async () => {
    const source: CaptureSource = {
      sourceId: "local-original",
      dataUrl: "data:image/png;base64,c291cmNl",
      width: 640,
      height: 360,
      source: { asset_id: "asset-synthetic", source_version: "v1", time_ms: 2000 },
    };
    const result = await exportCrop(source, { x: 160, y: 90, width: 320, height: 180 });
    expect(drawImage).toHaveBeenCalledExactlyOnceWith(
      expect.anything(),
      160,
      90,
      320,
      180,
      0,
      0,
      320,
      180,
    );
    expect(result.source).toBe(source);
    expect(result.dataUrl).toBe(output);
    expect(result.width).toBe(320);
    expect(result.height).toBe(180);
    expect(source.dataUrl).toBe("data:image/png;base64,c291cmNl");
  });

  it("blocks a stale original with a different decoded size", async () => {
    naturalWidth = 320;
    await expect(
      exportCrop(
        { sourceId: "stale", dataUrl: output, width: 640, height: 360 },
        { x: 160, y: 90, width: 320, height: 180 },
      ),
    ).rejects.toMatchObject({ code: "IMAGE_VERSION_CHANGED" });
    expect(drawImage).not.toHaveBeenCalled();
  });
});
