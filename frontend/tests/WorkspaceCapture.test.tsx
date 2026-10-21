import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkspaceCapture } from "../src/media/WorkspaceCapture";
import type { CaptureSource } from "../src/media/capture";

const { snapshot, exportSelection } = vi.hoisted(() => ({
  snapshot: vi.fn(),
  exportSelection: vi.fn(),
}));
vi.mock("../src/media/workspaceSnapshot", () => ({ captureWorkspace: snapshot }));
vi.mock("../src/media/capture", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/media/capture")>()),
  exportCrop: exportSelection,
}));

const source: CaptureSource = {
  sourceId: "synthetic-workspace",
  origin: "workspace",
  dataUrl: "data:image/png;base64,c3ludGhldGlj",
  width: 800,
  height: 600,
};
const bounds = { left: 30, top: 40, width: 400, height: 300 };
const frozen = { source, bounds };

beforeEach(() => {
  vi.stubGlobal("PointerEvent", MouseEvent);
  snapshot.mockResolvedValue(frozen);
  exportSelection.mockImplementation(async (original, crop) => ({
    source: original,
    crop,
    dataUrl: "data:image/png;base64,c2VsZWN0ZWQ=",
    width: crop.width,
    height: crop.height,
  }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function mount(onConfirm = vi.fn(), onCancel = vi.fn()) {
  const root = document.createElement("main");
  const value = render(<WorkspaceCapture root={root} onConfirm={onConfirm} onCancel={onCancel} />);
  return { root, onConfirm, onCancel, ...value };
}

async function surface() {
  const target = await screen.findByLabelText("截图选区画布");
  vi.spyOn(target, "getBoundingClientRect").mockReturnValue({
    ...bounds,
    x: bounds.left,
    y: bounds.top,
    right: bounds.left + bounds.width,
    bottom: bounds.top + bounds.height,
    toJSON: () => ({}),
  });
  return target;
}

describe("软件内拖拽截图", () => {
  it("显式启动时冻结一次，未框选不会导出或提交", async () => {
    const { root, onConfirm } = mount();
    await surface();
    expect(snapshot).toHaveBeenCalledExactlyOnceWith(root);
    expect(exportSelection).not.toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "框选软件内截图" })).toHaveAttribute(
      "data-workspace-capture-ignore",
    );
    expect(screen.getByRole("dialog", { name: "框选软件内截图" })).toHaveAttribute(
      "data-workspace-capture-overlay",
      "true",
    );
  });

  it("松开时只导出选区，CSS坐标按实际源像素倍率映射", async () => {
    const { onConfirm } = mount();
    const target = await surface();
    fireEvent.pointerDown(target, { button: 0, clientX: 80.25, clientY: 90.5 });
    fireEvent.pointerMove(target, { clientX: 180.75, clientY: 160.25 });
    expect(exportSelection).not.toHaveBeenCalled();
    fireEvent.pointerUp(target, { clientX: 180.75, clientY: 160.25 });
    await waitFor(() => expect(onConfirm).toHaveBeenCalledOnce());
    expect(exportSelection).toHaveBeenCalledExactlyOnceWith(source, {
      x: 100.5,
      y: 101,
      width: 201,
      height: 139.5,
    });
    expect(onConfirm.mock.calls[0][0].dataUrl).toBe("data:image/png;base64,c2VsZWN0ZWQ=");
    expect(snapshot).toHaveBeenCalledOnce();
  });

  it("反向拖拽并超出软件边界时只保留软件内范围", async () => {
    const { onConfirm } = mount();
    const target = await surface();
    fireEvent.pointerDown(target, { button: 0, clientX: 330, clientY: 240 });
    fireEvent.pointerUp(target, { clientX: -200, clientY: -100 });
    await waitFor(() => expect(onConfirm).toHaveBeenCalledOnce());
    expect(exportSelection).toHaveBeenCalledExactlyOnceWith(source, {
      x: 0,
      y: 0,
      width: 600,
      height: 400,
    });
  });

  it("单击不导出，Esc取消也不提交", async () => {
    const { onConfirm, onCancel } = mount();
    const target = await surface();
    fireEvent.pointerDown(target, { button: 0, clientX: 80, clientY: 90 });
    fireEvent.pointerUp(target, { clientX: 80, clientY: 90 });
    expect(await screen.findByRole("alert")).toHaveTextContent("CROP_EMPTY");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledOnce();
    expect(exportSelection).not.toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("导出失败保留冻结源，重新拖选不读取新画面", async () => {
    exportSelection.mockRejectedValueOnce(new Error("synthetic save failure"));
    const { onConfirm } = mount();
    const target = await surface();
    fireEvent.pointerDown(target, { button: 0, clientX: 80, clientY: 90 });
    fireEvent.pointerUp(target, { clientX: 180, clientY: 190 });
    expect(await screen.findByRole("alert")).toHaveTextContent("CAPTURE_SAVE_FAILED");
    fireEvent.pointerDown(target, { button: 0, clientX: 90, clientY: 100 });
    fireEvent.pointerUp(target, { clientX: 180, clientY: 190 });
    await waitFor(() => expect(onConfirm).toHaveBeenCalledOnce());
    expect(snapshot).toHaveBeenCalledOnce();
    expect(exportSelection).toHaveBeenCalledTimes(2);
  });

  it("冻结失败明确提示，只有用户点击重试才重新冻结", async () => {
    snapshot.mockRejectedValueOnce(new Error("synthetic render failure"));
    const { onConfirm } = mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("CAPTURE_RENDER_FAILED");
    expect(onConfirm).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "重新截图" }));
    });
    await surface();
    expect(snapshot).toHaveBeenCalledTimes(2);
    expect(exportSelection).not.toHaveBeenCalled();
  });
});
