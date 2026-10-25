import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type Cropper from "cropperjs";
import type { CaptureResult, CaptureSource, CropRect } from "../src/media/capture";
import type { Schema } from "../src/api/client";

const cropperFixture = vi.hoisted(() => ({ data: { x: 0, y: 0, width: 0, height: 0 } }));
vi.mock("cropperjs", () => ({
  default: class FixtureCropper {
    constructor(
      _image: HTMLImageElement,
      private options: Cropper.Options<HTMLImageElement>,
    ) {
      queueMicrotask(() =>
        options.ready?.(new CustomEvent("ready") as Cropper.ReadyEvent<HTMLImageElement>),
      );
    }
    setData(data: CropRect) {
      cropperFixture.data = { ...data };
      this.options.crop?.(
        new CustomEvent("crop", {
          detail: { ...data, rotate: 0, scaleX: 1, scaleY: 1 },
        }) as Cropper.CropEvent<HTMLImageElement>,
      );
    }
    getData() {
      return cropperFixture.data;
    }
    zoom() {}
    setDragMode() {}
    reset() {}
    destroy() {}
  },
}));
vi.mock("../src/media/capture", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/media/capture")>()),
  exportCrop: vi.fn(async (source: CaptureSource, crop: CropRect): Promise<CaptureResult> => ({
    source,
    crop,
    dataUrl: "data:image/png;base64,Q1JPUA==",
    width: crop.width,
    height: crop.height,
  })),
}));
vi.mock("../src/chat/captureCache", () => ({
  loadCapture: vi.fn(async (): Promise<CaptureResult> => ({
    source: {
      sourceId: "same-original",
      dataUrl: "data:image/png;base64,T1JJR0lOQUw=",
      width: 100,
      height: 80,
    },
    crop: { x: 10, y: 5, width: 20, height: 15 },
    dataUrl: "data:image/png;base64,Q1JPUA==",
    width: 20,
    height: 15,
  })),
  saveCapture: vi.fn(),
}));
const source: CaptureSource = {
  sourceId: "new-synthetic-source",
  dataUrl: "data:image/png;base64,T1JJR0lOQUw=",
  width: 100,
  height: 80,
  initialCrop: { x: 10, y: 5, width: 20, height: 15 },
};
const savedText = "保留文字、题目条件与其他附件";
const migratedText = savedText + "\n\n补充：\nx>0\n\n更正：\n改为x<3";
let remoteDraft: Schema<"DraftResponse">;
let ChatWorkspace: typeof import("../src/chat/ChatWorkspace").ChatWorkspace;
let useDraft: typeof import("../src/chat/draft").useDraft;
const originalShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "showModal");
const originalClose = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "close");
let request: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  vi.resetModules();
  localStorage.clear();
  sessionStorage.clear();
  ChatWorkspace = (await import("../src/chat/ChatWorkspace")).ChatWorkspace;
  useDraft = (await import("../src/chat/draft")).useDraft;
  remoteDraft = {
    revision: 0,
    text: savedText,
    attachment_ids: [],
    conditions: ["x>0"],
    corrections: ["改为x<3"],
    model: null,
    thinking: null,
  };
  request = vi.fn((url: string, options?: RequestInit) => {
    if (url.endsWith("/metadata"))
      return Promise.resolve(
        new Response(
          JSON.stringify({
            attachment_id: url.split("/").at(-2),
            media_type: "image/png",
            size_bytes: 4,
            sha256: "fixture-crop-hash",
            confirmed: true,
            kind: "image",
            filename: "图片.png",
            page_count: null,
            character_count: null,
            download_url: url.replace(/\/metadata$/, ""),
          }),
          { status: 200 },
        ),
      );
    let body: unknown = {
      chat_id: "error-chat",
      course_id: "synthetic-course",
      messages: [],
      generations: [],
    };
    if (url.endsWith("/draft")) {
      if (options?.method === "POST") {
        const { expected_revision, ...value } = JSON.parse(options.body as string);
        remoteDraft = { ...value, revision: expected_revision + 1 };
      }
      body = remoteDraft;
    }
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
  });
  vi.stubGlobal("fetch", request);
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
afterEach(async () => {
  cleanup();
  await new Promise((resolve) => setTimeout(resolve, 0));
  vi.unstubAllGlobals();
  if (originalShowModal)
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", originalShowModal);
  else Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
  if (originalClose) Object.defineProperty(HTMLDialogElement.prototype, "close", originalClose);
  else Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
});
async function ready() {
  await waitFor(() => expect(screen.getByLabelText("问题或追问")).toBeEnabled());
}
async function confirmReady() {
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "确认选区并加入草稿" })).toBeEnabled(),
  );
}
function assertNoUploadOrSubmission() {
  expect(
    request.mock.calls.some(
      ([url]) => url === "/api/v1/attachments" || url === "/api/v1/submissions",
    ),
  ).toBe(false);
}

describe("确认选区时可处理的业务拒绝", () => {
  it("第九张截图显示稳定上限代码与取消移除指引，保留选区和已有草稿", async () => {
    remoteDraft.attachment_ids = Array.from({ length: 8 }, (_, index) => `confirmed-${index}`);
    render(<ChatWorkspace chatId="error-chat" connected canSubmit incomingCapture={source} />);
    await ready();
    await confirmReady();
    fireEvent.click(screen.getByRole("button", { name: "确认选区并加入草稿" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "每次最多加入 8 张确认图片，请取消后移除其他图片。（ATTACHMENT_LIMIT）",
    );
    expect(screen.getByLabelText("问题或追问")).toHaveValue(migratedText);
    expect(screen.queryByLabelText("当前题目条件（每行一项）")).not.toBeInTheDocument();
    expect(screen.getAllByAltText(/待发送的确认图片/)).toHaveLength(8);
    expect(screen.getByLabelText("选区左侧")).toHaveValue(10);
    expect(screen.getByLabelText("选区宽度")).toHaveValue(20);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    assertNoUploadOrSubmission();
  });
  it("重裁期间原附件身份失效时显示明确代码，保留其余附件和编辑范围而不另发", async () => {
    remoteDraft.attachment_ids = ["original-confirmed", "other-confirmed"];
    let invalidateOriginal: (() => void) | undefined;
    function DraftInvalidationFixture() {
      const draft = useDraft("error-chat");
      // Simulate a delayed business-state invalidation while the editor still owns the original.
      invalidateOriginal = () => draft.controller.update({ attachment_ids: ["other-confirmed"] });
      return null;
    }
    render(
      <>
        <ChatWorkspace chatId="error-chat" connected canSubmit />
        <DraftInvalidationFixture />
      </>,
    );
    await ready();
    fireEvent.click(screen.getByRole("button", { name: "重新裁剪图片 1" }));
    await confirmReady();
    act(() => invalidateOriginal?.());
    fireEvent.click(screen.getByRole("button", { name: "确认选区并加入草稿" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "原附件已移除，请取消后重新选择。（ATTACHMENT_REPLACEMENT_STALE）",
    );
    expect(screen.getByLabelText("问题或追问")).toHaveValue(migratedText);
    expect(screen.getByAltText("待发送的确认图片 1")).toHaveAttribute(
      "src",
      "/api/v1/attachments/other-confirmed",
    );
    expect(screen.getByLabelText("选区左侧")).toHaveValue(10);
    expect(screen.getByLabelText("选区顶部")).toHaveValue(5);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    assertNoUploadOrSubmission();
  });
});
