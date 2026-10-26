import { cleanup, createEvent, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { FileManager } from "../src/library/FileManager";
import {
  COURSE_FILES_FORMAT,
  installCourseFileDrag,
  readCourseDragFiles,
} from "../src/library/fileDrag";

const entry = (name: string, kind = "file", viewer = "text") => ({
  name,
  path: name,
  kind,
  viewer,
  size_bytes: 4,
  source_version: null,
});
const response = (value: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(value), { status }));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

it("内部拖动保留目录目标，只有外部文件显示导入遮罩，Ctrl 显示复制", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      response({
        course_id: "drag",
        title: "拖动",
        path: "",
        root_status: "ready",
        entries: [entry("原文.txt"), entry("章节", "directory", "directory")],
      }),
    ),
  );
  render(<FileManager courseId="drag" connected onOpen={vi.fn()} />);
  const source = await screen.findByRole("option", { name: "原文.txt" });
  const target = screen.getByRole("option", { name: "章节" });
  const data = {
    types: ["text/uri-list"],
    clearData: vi.fn(),
    setData: vi.fn(),
    effectAllowed: "",
    dropEffect: "",
  };
  fireEvent.dragStart(source, { dataTransfer: data });
  expect(data.clearData).toHaveBeenCalledOnce();
  expect(data.setData).toHaveBeenCalledWith(COURSE_FILES_FORMAT, expect.any(String));
  data.types = [COURSE_FILES_FORMAT];
  fireEvent.dragEnter(target, { dataTransfer: data });
  const dragOver = createEvent.dragOver(target, { dataTransfer: data });
  Object.defineProperty(dragOver, "ctrlKey", { value: true });
  fireEvent(target, dragOver);
  expect(screen.queryByText("复制到当前文件夹")).not.toBeInTheDocument();
  expect(target).toHaveClass("is-drop-target");
  expect(data.dropEffect).toBe("copy");
  fireEvent(window, new Event("dragend"));
  expect(target).not.toHaveClass("is-drop-target");
  fireEvent.dragEnter(target, { dataTransfer: { types: ["Files"] } });
  expect(screen.getByText("复制到当前文件夹")).toBeInTheDocument();
});

it("仅输入框接收内部附件，捕获阶段阻止原生重复接收，外部文件保持原流程", async () => {
  const onAttach = vi.fn(async () => {}),
    onError = vi.fn(),
    nativeDrop = vi.fn();
  render(
    <>
      <section className="file-manager">
        <button>文件夹</button>
      </section>
      <div data-slot="conversation.composer">
        <div data-testid="input" />
      </div>
      <div data-testid="outside" />
    </>,
  );
  const dispose = installCourseFileDrag({ canAttach: () => true, onAttach, onError });
  document.addEventListener("drop", nativeDrop);
  const value = { courseId: "drag", paths: ["原文.txt"] };
  const data = {
    types: [COURSE_FILES_FORMAT, "Files"],
    getData: () => JSON.stringify(value),
    dropEffect: "",
  };
  try {
    fireEvent.dragOver(screen.getByTestId("input"), { dataTransfer: data });
    expect(screen.getByTestId("input").parentElement).toHaveClass("learning-file-drop-target");
    fireEvent.drop(screen.getByTestId("input"), { dataTransfer: data });
    await waitFor(() => expect(onAttach).toHaveBeenCalledExactlyOnceWith(value));
    expect(nativeDrop).not.toHaveBeenCalled();
    fireEvent.drop(screen.getByTestId("outside"), { dataTransfer: data });
    expect(onAttach).toHaveBeenCalledTimes(1);
    fireEvent.drop(screen.getByRole("button"), { dataTransfer: data });
    expect(nativeDrop).toHaveBeenCalledTimes(1);
    fireEvent.drop(screen.getByTestId("input"), { dataTransfer: { types: ["Files"] } });
    expect(nativeDrop).toHaveBeenCalledTimes(2);
    expect(onError).not.toHaveBeenCalled();
  } finally {
    dispose();
    document.removeEventListener("drop", nativeDrop);
  }
});

it("忙碌时拒绝重复落放；清理后释放事件和高亮", async () => {
  let finish!: () => void;
  const onAttach = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        finish = resolve;
      }),
  );
  const onError = vi.fn();
  render(<div data-slot="conversation.composer" data-testid="input" />);
  const dispose = installCourseFileDrag({ canAttach: () => true, onAttach, onError });
  const data = {
    types: [COURSE_FILES_FORMAT],
    getData: () => JSON.stringify({ courseId: "drag", paths: ["原文.txt"] }),
  };
  fireEvent.drop(screen.getByTestId("input"), { dataTransfer: data });
  await waitFor(() => expect(onAttach).toHaveBeenCalledOnce());
  fireEvent.drop(screen.getByTestId("input"), { dataTransfer: data });
  expect(onAttach).toHaveBeenCalledOnce();
  expect(onError).toHaveBeenCalledWith(
    expect.objectContaining({ message: expect.stringContaining("FILE_ATTACHMENT_BUSY") }),
  );
  finish();
  dispose();
  fireEvent.dragOver(screen.getByTestId("input"), { dataTransfer: data });
  expect(screen.getByTestId("input")).not.toHaveClass("learning-file-drop-target");
});

it("整批预检拒绝文件夹、未下载平台源和路径越界，不登记或下载原件", async () => {
  const request = vi.fn<(url: string) => Promise<Response>>(() =>
    response({
      entries: [
        entry("原文.txt"),
        entry("文件夹", "directory", "directory"),
        { ...entry("平台.mp4", "file", "video"), platform: "bilibili" },
      ],
    }),
  );
  vi.stubGlobal("fetch", request);
  for (const path of ["文件夹", "平台.mp4", "../原文.txt"]) {
    await expect(
      readCourseDragFiles({ courseId: "drag", paths: ["原文.txt", path] }),
    ).rejects.toThrow(/FILE_ATTACHMENT_UNSUPPORTED|FILE_DROP_SOURCE_INVALID/);
  }
  expect(request.mock.calls.every((args) => String(args[0]).includes("/files?path="))).toBe(true);
});

it("只读取显式来源的精确版本字节，重复路径只读一次", async () => {
  const item = entry("原文.txt");
  const request = vi.fn((url: string) => {
    if (url.includes("/files?")) return response({ entries: [item] });
    if (url.endsWith("/attachment-source"))
      return response({
        course_id: "other",
        entry: item,
        viewer: "text",
        asset_id: "asset_1",
        source_version: "v1",
        content_url: "/api/v1/course-files/asset_1/content?source_version=v1&download=1",
      });
    return Promise.resolve(new Response("text"));
  });
  vi.stubGlobal("fetch", request);
  const files = await readCourseDragFiles({ courseId: "other", paths: ["原文.txt", "原文.txt"] });
  expect(files).toHaveLength(1);
  expect(files[0]).toMatchObject({ name: "原文.txt", type: "text/plain", size: 4 });
  expect(request).toHaveBeenCalledTimes(3);
});
