import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FileManager } from "../src/library/FileManager";
import { droppedFiles, droppedItems } from "../src/library/dropFiles";

const file = (name: string, path = name) => ({
  name,
  path,
  kind: "file",
  viewer: "text",
  size_bytes: 4,
  asset_id: null,
  source_version: null,
  modified_at: null,
  created_at: null,
});
const folder = (name: string, path = name) => ({
  ...file(name, path),
  kind: "directory",
  viewer: "directory",
  size_bytes: null,
});
const response = (value: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(value), { status }));
function listing(course: string, path: string, entries: unknown[]) {
  return {
    course_id: course,
    title: "数学课程",
    root_path: "C:\\synthetic",
    root_status: "ready",
    legacy_asset_count: 0,
    path,
    entries,
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("中间文件资源管理器", () => {
  it("默认图标显示，双击进入目录，后退及搜索只作用当前目录", async () => {
    const course = "manager-navigation";
    const request = vi.fn((url: string) => {
      const path = new URL(url, "http://localhost").searchParams.get("path") || "";
      return response(
        listing(
          course,
          path,
          path ? [file("子资料.txt", "章节/子资料.txt")] : [folder("章节"), file("说明.txt")],
        ),
      );
    });
    vi.stubGlobal("fetch", request);
    render(<FileManager courseId={course} connected onOpen={vi.fn()} />);
    expect(await screen.findByRole("option", { name: "说明.txt" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "图标视图" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await userEvent.dblClick(screen.getByRole("option", { name: "章节" }));
    expect(await screen.findByRole("option", { name: "子资料.txt" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("搜索当前文件夹"), "无匹配");
    expect(
      within(screen.getByRole("listbox", { name: "文件列表" })).queryByRole("option"),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "后退" }));
    expect(await screen.findByRole("option", { name: "说明.txt" })).toBeInTheDocument();
    expect(screen.getByLabelText("搜索当前文件夹")).toHaveValue("");
  });

  it("内部复制经文件剪贴板传输，目标不会提供外部移动或冲突选择", async () => {
    const course = "manager-copy";
    const request = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/clipboard"))
        return response({ code: "CLIPBOARD_FILES_EMPTY", message: "剪贴板没有外部文件" }, 422);
      if (url.endsWith("/transfer"))
        return response({
          operation_id: JSON.parse(String(options?.body)).operation_id,
          state: "completed",
          entries: [],
          source_paths: [],
          conflicts: [],
        });
      return response(listing(course, "", [file("说明.txt")]));
    });
    vi.stubGlobal("fetch", request);
    render(<FileManager courseId={course} connected onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByRole("option", { name: "说明.txt" }));
    await userEvent.click(screen.getByRole("button", { name: "复制" }));
    await userEvent.click(screen.getByRole("button", { name: "粘贴" }));
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => url.endsWith("/transfer"))).toBe(true),
    );
    const call = request.mock.calls.find(([url]) => url.endsWith("/transfer"))!;
    expect(JSON.parse(String(call[1]?.body))).toMatchObject({
      paths: ["说明.txt"],
      destination: "",
      mode: "copy",
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("外部拖入只上传用户给的File字节，不出现模式或确认弹窗", async () => {
    const course = "manager-drop";
    const dropped = new File(["synthetic drop"], "新增.txt", { type: "text/plain" });
    const request = vi.fn((url: string, options?: RequestInit) => {
      if (url.includes("/upload?")) {
        expect(options?.method).toBe("POST");
        return response({
          operation_id: new URL(url, "http://localhost").searchParams.get("operation_id"),
          state: "completed",
          entries: [],
          source_paths: [],
          conflicts: [],
        });
      }
      return response(listing(course, "", []));
    });
    vi.stubGlobal("fetch", request);
    render(<FileManager courseId={course} connected onOpen={vi.fn()} />);
    await screen.findByText("此文件夹为空");
    const manager = screen.getByRole("region", { name: "文件资源管理器" });
    // The host's chat composer listens on document. A consumed library drop
    // must not activate its overlay or attach the same file to the chat.
    const hostDrag = vi.fn();
    const hostDrop = vi.fn();
    const dragEnd = vi.fn();
    document.addEventListener("dragenter", hostDrag);
    document.addEventListener("dragover", hostDrag);
    document.addEventListener("dragleave", hostDrag);
    document.addEventListener("drop", hostDrop);
    window.addEventListener("dragend", dragEnd);
    const dataTransfer = { getData: () => "", types: ["Files"], items: [], files: [dropped] };
    try {
      fireEvent.dragEnter(manager, { dataTransfer });
      fireEvent.dragOver(manager, { dataTransfer });
      fireEvent.dragLeave(manager, { dataTransfer });
      fireEvent.dragEnter(manager, { dataTransfer });
      fireEvent.drop(manager, { dataTransfer });
      expect(hostDrag).not.toHaveBeenCalled();
      expect(hostDrop).not.toHaveBeenCalled();
      // Clear an already visible host overlay if the drag entered elsewhere.
      expect(dragEnd).toHaveBeenCalledTimes(1);
    } finally {
      document.removeEventListener("dragenter", hostDrag);
      document.removeEventListener("dragover", hostDrag);
      document.removeEventListener("dragleave", hostDrag);
      document.removeEventListener("drop", hostDrop);
      window.removeEventListener("dragend", dragEnd);
    }
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => url.includes("/upload?"))).toBe(true),
    );
    const [url, options] = request.mock.calls.find(([url]) => url.includes("/upload?"))!;
    expect(options?.body).toBe(dropped);
    expect(request.mock.calls.filter(([url]) => url.includes("/upload?"))).toHaveLength(1);
    expect(new URL(url, "http://localhost").searchParams.get("path")).toBe("新增.txt");
    expect(new URL(url, "http://localhost").searchParams.has("mode")).toBe(false);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it.each(["blur", "copy"])("%s 后不会把已失效的内部文件剪贴板当系统粘贴", async (event) => {
    const course = `manager-clipboard-invalid-${event}`;
    const request = vi.fn((url: string) => {
      if (url.endsWith("/clipboard"))
        return response(
          { code: "CLIPBOARD_FILES_EMPTY", message: "剪贴板中没有可复制的文件" },
          422,
        );
      if (url.endsWith("/transfer")) throw new Error("stale internal clipboard must not transfer");
      return response(listing(course, "", [file("说明.txt")]));
    });
    vi.stubGlobal("fetch", request);
    render(<FileManager courseId={course} connected onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByRole("option", { name: "说明.txt" }));
    await userEvent.click(screen.getByRole("button", { name: "复制" }));
    act(() => {
      (event === "blur" ? window : document).dispatchEvent(new Event(event));
    });
    await userEvent.click(screen.getByRole("button", { name: "粘贴" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("CLIPBOARD_FILES_EMPTY");
    expect(request.mock.calls.filter(([url]) => url.endsWith("/transfer"))).toHaveLength(0);
  });

  it("丢回执后保留操作身份，重挂只对账，不自动再次复制", async () => {
    const course = "manager-unknown";
    let operationId = "";
    const request = vi.fn((url: string, options?: RequestInit) => {
      if (url.includes("/upload?")) {
        expect(options?.method).toBe("POST");
        operationId = new URL(url, "http://localhost").searchParams.get("operation_id") || "";
        return Promise.reject(new TypeError("lost receipt"));
      }
      if (url.includes("/operations/"))
        return response({
          operation_id: operationId,
          kind: "upload",
          state: "completed",
          error_code: null,
          result: {
            operation_id: operationId,
            state: "completed",
            entries: [],
            source_paths: [],
            conflicts: [],
          },
        });
      return response(listing(course, "", []));
    });
    vi.stubGlobal("fetch", request);
    const first = render(<FileManager courseId={course} connected onOpen={vi.fn()} />);
    await screen.findByText("此文件夹为空");
    fireEvent.drop(screen.getByRole("region", { name: "文件资源管理器" }), {
      dataTransfer: {
        getData: () => "",
        types: ["Files"],
        items: [],
        files: [new File(["data"], "新增.txt")],
      },
    });
    await screen.findByRole("button", { name: "核对文件操作" });
    first.unmount();
    render(<FileManager courseId={course} connected onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "核对文件操作" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "核对文件操作" })).not.toBeInTheDocument(),
    );
    expect(request.mock.calls.filter(([url]) => url.includes("/upload?"))).toHaveLength(1);
    expect(
      request.mock.calls.filter(([url]) => url.includes(`/operations/${operationId}`)),
    ).toHaveLength(1);
  });

  it("按Delete走回收站route，原件操作未知前不会叠加下一项", async () => {
    const course = "manager-recycle";
    const request = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/delete"))
        return response({
          operation_id: JSON.parse(String(options?.body)).operation_id,
          state: "needs_review",
          entry: null,
          root: null,
        });
      return response(listing(course, "", [file("甲.txt"), file("乙.txt")]));
    });
    vi.stubGlobal("fetch", request);
    render(<FileManager courseId={course} connected onOpen={vi.fn()} />);
    const first = await screen.findByRole("option", { name: "甲.txt" });
    await userEvent.click(first);
    fireEvent.click(screen.getByRole("option", { name: "乙.txt" }), { ctrlKey: true });
    first.focus();
    fireEvent.keyDown(first, { key: "Delete" });
    await screen.findByRole("button", { name: "核对文件操作" });
    expect(request.mock.calls.filter(([url]) => url.endsWith("/delete"))).toHaveLength(1);
    expect(
      JSON.parse(String(request.mock.calls.find(([url]) => url.endsWith("/delete"))![1]?.body)),
    ).toMatchObject({ path: "甲.txt", confirm: true });
  });

  it("离开课程后的旧文件打开回调不污染新页面", async () => {
    const course = "manager-stale-open";
    let complete: (value: Response) => void = () => {};
    const opened = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        url.endsWith("/open")
          ? new Promise<Response>((resolve) => {
              complete = resolve;
            })
          : response(listing(course, "", [file("说明.txt")])),
      ),
    );
    const view = render(<FileManager courseId={course} connected onOpen={opened} />);
    await userEvent.dblClick(await screen.findByRole("option", { name: "说明.txt" }));
    view.unmount();
    await act(async () => {
      complete(new Response(JSON.stringify({ course_id: course, entry: file("说明.txt") })));
    });
    expect(opened).not.toHaveBeenCalled();
  });

  it("属性对话框内的Delete不会穿透成文件删除，Escape只关闭对话框", async () => {
    const course = "manager-properties-keyboard";
    const request = vi.fn((url: string) =>
      url.endsWith("/properties")
        ? response({ ...file("说明.txt"), root_path: "C:\\synthetic" })
        : response(listing(course, "", [file("说明.txt")])),
    );
    vi.stubGlobal("fetch", request);
    render(<FileManager courseId={course} connected onOpen={vi.fn()} />);
    await userEvent.click(await screen.findByRole("option", { name: "说明.txt" }));
    await userEvent.click(screen.getByRole("button", { name: "属性" }));
    const dialog = await screen.findByRole("dialog", { name: "属性" });
    const close = within(dialog).getByRole("button", { name: "关闭" });
    close.focus();
    fireEvent.keyDown(close, { key: "Delete" });
    expect(request.mock.calls.filter(([url]) => url.endsWith("/delete"))).toHaveLength(0);
    fireEvent.keyDown(close, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: "说明.txt" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });
});

describe("浏览器拖入目录", () => {
  it("保留空目录并通过有身份的创建操作复制，不静默丢弃", async () => {
    const course = "manager-empty-directory";
    const request = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/directory"))
        return response({
          operation_id: JSON.parse(String(options?.body)).operation_id,
          state: "completed",
          entry: folder("空目录"),
        });
      return response(listing(course, "", []));
    });
    vi.stubGlobal("fetch", request);
    render(<FileManager courseId={course} connected onOpen={vi.fn()} />);
    await screen.findByText("此文件夹为空");
    const entry = {
      name: "空目录",
      isFile: false,
      isDirectory: true,
      createReader: () => ({ readEntries: (done: (entries: unknown[]) => void) => done([]) }),
    };
    const data = {
      getData: () => "",
      types: ["Files"],
      items: [{ kind: "file", webkitGetAsEntry: () => entry }],
      files: [],
    } as unknown as DataTransfer;
    expect((await droppedItems(data)).emptyDirectories).toEqual(["空目录"]);
    fireEvent.drop(screen.getByRole("region", { name: "文件资源管理器" }), { dataTransfer: data });
    await waitFor(() =>
      expect(request.mock.calls.filter(([url]) => url.endsWith("/directory"))).toHaveLength(1),
    );
    expect(
      JSON.parse(String(request.mock.calls.find(([url]) => url.endsWith("/directory"))![1]?.body)),
    ).toMatchObject({ path: "", name: "空目录" });
    expect(request.mock.calls.filter(([url]) => url.includes("/upload?"))).toHaveLength(0);
  });
  it("读取所有readEntries分页并保留根内相对层次", async () => {
    const a = new File(["a"], "甲.txt");
    const b = new File(["b"], "乙.txt");
    const pages = [
      [
        {
          name: "甲.txt",
          isFile: true,
          isDirectory: false,
          file: (done: (file: File) => void) => done(a),
        },
      ],
      [
        {
          name: "乙.txt",
          isFile: true,
          isDirectory: false,
          file: (done: (file: File) => void) => done(b),
        },
      ],
      [],
    ];
    const entry = {
      name: "章节",
      isFile: false,
      isDirectory: true,
      createReader: () => ({
        readEntries: (done: (entries: unknown[]) => void) => done(pages.shift() || []),
      }),
    };
    const result = await droppedFiles({
      items: [{ kind: "file", webkitGetAsEntry: () => entry }],
      files: [],
    } as unknown as DataTransfer);
    expect(result.map((item) => item.path)).toEqual(["章节/甲.txt", "章节/乙.txt"]);
    expect(result.map((item) => item.file)).toEqual([a, b]);
  });
  it("拒绝带上级范围的拖入路径，不开始任何网络复制", async () => {
    const item = new File(["x"], "../越界.txt");
    await expect(
      droppedFiles({ items: [], files: [item] } as unknown as DataTransfer),
    ).rejects.toThrow("FILE_DROP_PATH_INVALID");
  });
});
