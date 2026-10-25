import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CourseExplorer } from "../src/library/CourseExplorer";
import type { Schema } from "../src/api/client";

const folder: Schema<"CourseFileEntry"> = {
  name: "章节",
  path: "章节",
  kind: "directory",
  viewer: "directory",
  size_bytes: null,
  asset_id: null,
  source_version: null,
};
const textFile: Schema<"CourseFileEntry"> = {
  name: "说明.txt",
  path: "说明.txt",
  kind: "file",
  viewer: "text",
  size_bytes: 10,
  asset_id: null,
  source_version: null,
};
const listing = (courseId = "course-a", title = "课程 A"): Schema<"CourseFileListResponse"> => ({
  course_id: courseId,
  title,
  root_path: "D:/synthetic/course-root",
  root_status: "ready",
  legacy_asset_count: 0,
  path: "",
  entries: [folder, textFile],
});
const response = (value: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(value), { status }));
const props = () => ({
  courseId: "course-a",
  connected: true,
  onOpen: vi.fn(),
  onCourseChanged: vi.fn(),
  onDetached: vi.fn(),
  onDirectorySelect: vi.fn(),
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("精简课程侧栏", () => {
  it("后台恢复后的列表读取清除旧连接提示，不派发文件修改", async () => {
    const request = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockImplementation(() => response(listing()));
    vi.stubGlobal("fetch", request);
    const callbacks = props();
    const view = render(<CourseExplorer {...callbacks} />);
    await screen.findByRole("alert");
    view.rerender(<CourseExplorer {...callbacks} revision={1} />);
    await screen.findByRole("button", { name: "说明.txt" });
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(
      request.mock.calls.every(([, options]) => !options?.method || options.method === "GET"),
    ).toBe(true);
  });

  it("缺少原件时重新定位并保留旧版本未匹配提示", async () => {
    let current = {
      ...listing(),
      root_status: "missing" as const,
      entries: [],
    } as Schema<"CourseFileListResponse">;
    const request = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/root/relocate")) {
        const body = JSON.parse(String(options?.body));
        expect(body.expected_root).toBe("D:/synthetic/course-root");
        expect(body.operation_id).toMatch(/^[A-Za-z0-9_-]+$/);
        expect(body.path).toBeUndefined();
        current = listing();
        return response({ state: "completed", unavailable_versions: ["old-version"] });
      }
      return response(current);
    });
    vi.stubGlobal("fetch", request);
    render(<CourseExplorer {...props()} />);
    await userEvent.click(await screen.findByRole("button", { name: "重新定位课程原件" }));
    expect(await screen.findByText(/1 个旧版本仍不可用/)).toBeInTheDocument();
    expect(request.mock.calls.filter(([url]) => url.endsWith("/root/relocate"))).toHaveLength(1);
    expect(request.mock.calls.some(([url]) => url.endsWith("/root"))).toBe(false);
  });

  it("仅保留课程操作和文件浏览，目录选择通知中间文件页", async () => {
    const callbacks = props();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) =>
        response(
          url.includes("path=%E7%AB%A0%E8%8A%82")
            ? { ...listing(), path: "章节", entries: [] }
            : listing(),
        ),
      ),
    );
    render(<CourseExplorer {...callbacks} />);
    await screen.findByRole("button", { name: "重命名课程" });
    expect(screen.queryByRole("button", { name: "导入资料" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "新建文件夹" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("文件操作：说明.txt")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /章节/ }));
    expect(callbacks.onDirectorySelect).toHaveBeenLastCalledWith("章节");
    await userEvent.click(screen.getByRole("button", { name: "课程 A" }));
    expect(callbacks.onDirectorySelect).toHaveBeenLastCalledWith("");
  });

  it("修改课程别名时只提交名称和操作身份", async () => {
    const callbacks = props();
    let current = listing();
    const request = vi.fn((url: string, options?: RequestInit) => {
      if (url.endsWith("/rename-course")) {
        const value = JSON.parse(String(options?.body));
        current = listing("course-a", value.name);
        return response({
          operation_id: value.operation_id,
          state: "completed",
          entry: null,
          root: current,
        });
      }
      return response(current);
    });
    vi.stubGlobal("fetch", request);
    render(<CourseExplorer {...callbacks} />);
    await userEvent.click(await screen.findByRole("button", { name: "重命名课程" }));
    await userEvent.clear(screen.getByLabelText("课程名称"));
    await userEvent.type(screen.getByLabelText("课程名称"), "数学复习");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByRole("button", { name: "数学复习" })).toBeInTheDocument();
    expect(callbacks.onCourseChanged).toHaveBeenCalledTimes(1);
    const mutation = request.mock.calls.find(([url]) => url.endsWith("/rename-course"));
    expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({
      name: "数学复习",
      operation_id: expect.any(String),
    });
  });

  it("未绑定的旧课程直接调用原生目录选择，不提供路径输入", async () => {
    let current: Schema<"CourseFileListResponse"> = {
      ...listing(),
      root_status: "unbound",
      root_path: null,
      entries: [],
    };
    const request = vi.fn((url: string) => {
      if (url.endsWith("/root")) {
        current = {
          ...listing(),
          root_status: "ready",
          root_path: "D:/synthetic/course-root",
          entries: [],
        };
        return response(current);
      }
      return response(current);
    });
    vi.stubGlobal("fetch", request);
    render(<CourseExplorer {...props()} />);
    await userEvent.click(await screen.findByRole("button", { name: "选择课程文件夹" }));
    await screen.findByRole("button", { name: "重命名课程" });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    const mutation = request.mock.calls.find(([url]) => url.endsWith("/root"));
    expect(mutation).toBeTruthy();
  });

  it("别名保存失败保留输入，不自动重复修改", async () => {
    const request = vi.fn((url: string) =>
      url.endsWith("/rename-course")
        ? response({ code: "COURSE_NAME_INVALID", message: "课程名称不可用" }, 422)
        : response(listing()),
    );
    vi.stubGlobal("fetch", request);
    const callbacks = props();
    render(<CourseExplorer {...callbacks} />);
    await userEvent.click(await screen.findByRole("button", { name: "重命名课程" }));
    await userEvent.clear(screen.getByLabelText("课程名称"));
    await userEvent.type(screen.getByLabelText("课程名称"), "未保存名称");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    expect((await screen.findAllByRole("alert"))[0]).toHaveTextContent("COURSE_NAME_INVALID");
    expect(screen.getByLabelText("课程名称")).toHaveValue("未保存名称");
    expect(screen.getByRole("button", { name: "课程 A" })).toBeInTheDocument();
    expect(callbacks.onCourseChanged).not.toHaveBeenCalled();
    expect(request.mock.calls.filter(([url]) => url.endsWith("/rename-course"))).toHaveLength(1);
  });

  it("未知保存结果只核对原操作，不重发", async () => {
    let current = listing();
    const request = vi.fn((url: string) => {
      if (url.endsWith("/rename-course")) {
        current = listing("course-a", "已在后台保存");
        return Promise.reject(new TypeError("lost receipt"));
      }
      if (url.includes("/files/operations/"))
        return response({
          operation_id: "queried",
          kind: "rename_course",
          state: "completed",
          result: {},
          error_code: null,
        });
      return response(current);
    });
    vi.stubGlobal("fetch", request);
    render(<CourseExplorer {...props()} />);
    await userEvent.click(await screen.findByRole("button", { name: "重命名课程" }));
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    await userEvent.click(await screen.findByRole("button", { name: "核对课程操作" }));
    expect(await screen.findByRole("button", { name: "已在后台保存" })).toBeInTheDocument();
    expect(request.mock.calls.filter(([url]) => url.endsWith("/rename-course"))).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "核对课程操作" })).not.toBeInTheDocument();
  });

  it("旧课程的迟到移除回执不会清掉新课程", async () => {
    let resolveDetach: (value: Response) => void = () => {};
    const callbacks = props();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/detach"))
          return new Promise<Response>((resolve) => {
            resolveDetach = resolve;
          });
        return response(url.includes("course-b") ? listing("course-b", "课程 B") : listing());
      }),
    );
    const view = render(<CourseExplorer key="course-a" {...callbacks} />);
    await screen.findByRole("button", { name: "重命名课程" });
    await userEvent.click(screen.getByLabelText("课程设置"));
    await userEvent.click(screen.getByRole("button", { name: "移除课程…" }));
    await userEvent.click(screen.getByRole("button", { name: "移除课程" }));
    view.rerender(<CourseExplorer key="course-b" {...callbacks} courseId="course-b" />);
    await screen.findByRole("button", { name: "课程 B" });
    await act(async () =>
      resolveDetach(new Response(JSON.stringify({ operation_id: "old", state: "completed" }))),
    );
    expect(callbacks.onDetached).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "课程 B" })).toBeInTheDocument();
  });

  it("文件区刷新不会丢弃同课程正在保存的别名操作", async () => {
    let current = listing();
    let resolveRename: (value: Response) => void = () => {};
    const callbacks = props();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/rename-course"))
          return new Promise<Response>((resolve) => {
            resolveRename = resolve;
          });
        return response(current);
      }),
    );
    const view = render(<CourseExplorer {...callbacks} revision={0} />);
    await userEvent.click(await screen.findByRole("button", { name: "重命名课程" }));
    await userEvent.clear(screen.getByLabelText("课程名称"));
    await userEvent.type(screen.getByLabelText("课程名称"), "更新后的课程名");
    await userEvent.click(screen.getByRole("button", { name: "保存" }));
    view.rerender(<CourseExplorer {...callbacks} revision={1} />);
    current = listing("course-a", "更新后的课程名");
    await act(async () =>
      resolveRename(new Response(JSON.stringify({ operation_id: "renamed", state: "completed" }))),
    );
    expect(await screen.findByRole("button", { name: "更新后的课程名" })).toBeInTheDocument();
    expect(callbacks.onCourseChanged).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "重命名课程" })).toBeEnabled();
  });
});
