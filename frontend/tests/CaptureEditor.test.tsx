import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type Cropper from "cropperjs";
import { CaptureEditor } from "../src/media/CaptureEditor";
import { CaptureError, exportCrop } from "../src/media/capture";
import type { CaptureResult, CaptureSource, CropRect } from "../src/media/capture";

const cropperHarness = vi.hoisted(() => ({
  data: { x: 0, y: 0, width: 0, height: 0 },
  sourceUrls: [] as string[],
  destroyed: 0,
}));

vi.mock("cropperjs", () => ({
  default: class FixtureCropper {
    constructor(
      image: HTMLImageElement,
      private options: Cropper.Options<HTMLImageElement>,
    ) {
      cropperHarness.sourceUrls.push(image.src);
      queueMicrotask(() => {
        options.ready?.(new CustomEvent("ready") as Cropper.ReadyEvent<HTMLImageElement>);
      });
    }
    setData(data: CropRect) {
      cropperHarness.data = { ...data };
      this.options.crop?.(
        new CustomEvent("crop", {
          detail: { ...data, rotate: 0, scaleX: 1, scaleY: 1 },
        }) as Cropper.CropEvent<HTMLImageElement>,
      );
    }
    getData() {
      return cropperHarness.data;
    }
    zoom() {
      // A CSS zoom changes displayed pixels; the source-pixel selection stays authoritative.
    }
    setDragMode() {}
    reset() {}
    destroy() {
      cropperHarness.destroyed += 1;
    }
  },
}));

vi.mock("../src/media/capture", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/media/capture")>()),
  exportCrop: vi.fn(),
}));

const source: CaptureSource = {
  sourceId: "same-browser-original",
  dataUrl: "data:image/png;base64,b3JpZ2luYWw=",
  width: 640,
  height: 360,
  source: { asset_id: "synthetic-video", source_version: "v1", time_ms: 1250 },
  initialCrop: { x: 160, y: 90, width: 320, height: 180 },
};
const output: CaptureResult = {
  source,
  crop: { x: 160, y: 90, width: 320, height: 180 },
  dataUrl: "data:image/png;base64,Y29uZmlybWVk",
  width: 320,
  height: 180,
};
const originalShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "showModal");
const originalClose = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "close");

describe("capture confirmation lifecycle", () => {
  beforeEach(() => {
    cropperHarness.sourceUrls = [];
    cropperHarness.destroyed = 0;
    vi.mocked(exportCrop).mockReset().mockResolvedValue(output);
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: function (this: HTMLDialogElement) {
        this.open = true;
      },
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: function (this: HTMLDialogElement) {
        this.open = false;
      },
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    for (const [name, descriptor] of [
      ["showModal", originalShowModal],
      ["close", originalClose],
    ] as const) {
      if (descriptor) Object.defineProperty(HTMLDialogElement.prototype, name, descriptor);
      else Reflect.deleteProperty(HTMLDialogElement.prototype, name);
    }
  });

  const ready = () =>
    waitFor(() => expect(screen.getByRole("button", { name: "确认选区并加入草稿" })).toBeEnabled());

  it("reopens the same local original and selection; cancel never exports or confirms", async () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    const rendered = render(
      <CaptureEditor source={source} onConfirm={onConfirm} onCancel={onCancel} />,
    );
    await ready();
    expect(screen.getByRole("dialog", { name: "编辑截图" })).toBeVisible();
    expect(screen.getByLabelText("选区左侧")).toHaveValue(160);
    expect(screen.getByLabelText("选区宽度")).toHaveValue(320);
    fireEvent.click(screen.getByLabelText("放大截图"));
    fireEvent.click(screen.getByRole("button", { name: "移动画面" }));
    expect(screen.getByRole("button", { name: "移动画面" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "重新框选" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onCancel).toHaveBeenCalledOnce();
    expect(exportCrop).not.toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(source.initialCrop).toEqual({ x: 160, y: 90, width: 320, height: 180 });
    expect(cropperHarness.sourceUrls).toEqual([source.dataUrl]);
    rendered.unmount();
    expect(cropperHarness.destroyed).toBe(1);
  });

  it("submits source coordinates after zoom and numeric adjustment", async () => {
    const onConfirm = vi.fn();
    render(<CaptureEditor source={source} onConfirm={onConfirm} onCancel={vi.fn()} />);
    await ready();
    fireEvent.click(screen.getByLabelText("放大截图"));
    fireEvent.change(screen.getByLabelText("选区左侧"), { target: { value: "200" } });
    fireEvent.click(screen.getByRole("button", { name: "确认选区并加入草稿" }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledExactlyOnceWith(output));
    expect(exportCrop).toHaveBeenCalledExactlyOnceWith(source, {
      x: 200,
      y: 90,
      width: 320,
      height: 180,
    });
  });

  it("retains the original and selection after a failed confirmation so the user can retry", async () => {
    const onConfirm = vi
      .fn()
      .mockRejectedValueOnce(new Error("fixture publication failed"))
      .mockResolvedValueOnce(undefined);
    render(<CaptureEditor source={source} onConfirm={onConfirm} onCancel={vi.fn()} />);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "确认选区并加入草稿" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("图片尚未加入草稿"));
    expect(screen.getByLabelText("选区左侧")).toHaveValue(160);
    expect(cropperHarness.sourceUrls).toEqual([source.dataUrl]);
    fireEvent.click(screen.getByRole("button", { name: "确认选区并加入草稿" }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(2));
    expect(exportCrop).toHaveBeenNthCalledWith(2, source, source.initialCrop);
  });

  it("blocks repeat confirmation and cancellation while the confirmed crop is being published", async () => {
    let finish: (() => void) | undefined;
    const pending = new Promise<void>((resolve) => {
      finish = resolve;
    });
    const onCancel = vi.fn();
    const onConfirm = vi.fn().mockReturnValue(pending);
    render(<CaptureEditor source={source} onConfirm={onConfirm} onCancel={onCancel} />);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "确认选区并加入草稿" }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: "正在加入…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
    expect(onCancel).not.toHaveBeenCalled();
    expect(exportCrop).toHaveBeenCalledOnce();
    await act(async () => finish?.());
  });

  it("keeps an invalid or subpixel selection local instead of accepting it", async () => {
    vi.mocked(exportCrop).mockRejectedValueOnce(new CaptureError("CROP_EMPTY", "请重新选择区域"));
    const onConfirm = vi.fn();
    render(<CaptureEditor source={source} onConfirm={onConfirm} onCancel={vi.fn()} />);
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "确认选区并加入草稿" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("CROP_EMPTY"));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "确认选区并加入草稿" })).toBeEnabled();
  });
});
