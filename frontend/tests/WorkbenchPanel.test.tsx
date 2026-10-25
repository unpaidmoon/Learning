import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../src/api/client";
import type { Schema } from "../src/api/client";
import { WorkbenchPanel } from "../src/workbench/WorkbenchPanel";

const revision = {
  revision_id: "revision_base",
  artifact_id: "artifact_note",
  parent_revision_id: null,
  title: "独立笔记",
  body: {
    blocks: [
      {
        block_id: "paragraph",
        kind: "markdown" as const,
        markdown: "原来的文字",
        attribution: "user_authored" as const,
        source_snapshot_ids: [],
      },
    ],
  },
  content_role: "user_note" as const,
  author: "user" as const,
  task_id: null,
  disposition: "published" as const,
  created_at: "2026-10-23T00:00:00.000+00:00",
};
const base: Schema<"ArtifactView"> = {
  artifact_id: "artifact_note",
  course_id: "course",
  kind: "note",
  title: "独立笔记",
  head_revision_id: revision.revision_id,
  content_role: "user_note",
  indexed: true,
  revision,
  created_at: revision.created_at,
  updated_at: revision.created_at,
};
let current = base;

beforeEach(() => {
  localStorage.clear();
  current = base;
  vi.spyOn(api, "read").mockImplementation(async (path) => {
    if (path.includes("artifact-index")) return { items: [current], next_cursor: null } as never;
    if (path.includes("task-index")) return { items: [], next_cursor: null } as never;
    if (path.endsWith("/versions/revision_base")) return revision as never;
    if (path.endsWith("/artifacts/artifact_note")) return current as never;
    if (path.includes("/operations/"))
      return {
        kind: "task_create",
        result: {
          task_id: "created_task",
          course_id: "course",
          state: "queued",
          instruction: "已保留的要求",
          revision: 0,
          units: [],
          settings: { model: "deepseek-flash", thinking: "high" },
        },
      } as never;
    if (path.endsWith("/tasks/created_task"))
      return {
        task_id: "created_task",
        course_id: "course",
        state: "queued",
        instruction: "已保留的要求",
        revision: 0,
        units: [],
        settings: { model: "deepseek-flash", thinking: "high" },
      } as never;
    throw new Error("Unexpected fixture read: " + path);
  });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  localStorage.clear();
});

test("background head refreshes the list role while local typing stays protected", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  render(<WorkbenchPanel courseId="course" sessionId="native_session" />);
  fireEvent.click(await screen.findByRole("button", { name: /独立笔记/ }));
  await screen.findByText("原来的文字");
  current = {
    ...base,
    content_role: "mixed",
    head_revision_id: "background_revision",
    revision: {
      ...revision,
      revision_id: "background_revision",
      content_role: "mixed",
      author: "model",
      body: { blocks: [{ ...revision.body.blocks[0], markdown: "后台完成的新正文" }] },
    },
  };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2600);
  });
  expect(screen.getByRole("button", { name: /独立笔记.*混合修订/ })).toBeInTheDocument();
  expect(screen.getByText("后台完成的新正文")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByRole("textbox", { name: "编辑内容块" }), {
    target: { value: "正在手动编辑" },
  });
  current = {
    ...current,
    head_revision_id: "later_revision",
    revision: {
      ...current.revision,
      revision_id: "later_revision",
      body: { blocks: [{ ...revision.body.blocks[0], markdown: "更迟的后台正文" }] },
    },
  };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(screen.getByRole("textbox", { name: "编辑内容块" })).toHaveValue("正在手动编辑");
  expect(screen.queryByText("更迟的后台正文")).not.toBeInTheDocument();
});

test("unknown task receipt survives reopening and reconciles without another submit", async () => {
  const write = vi
    .spyOn(api, "write")
    .mockRejectedValue(new ApiError("BACKEND_UNREACHABLE", "连接中断"));
  let view = render(<WorkbenchPanel courseId="course" sessionId="native_session" />);
  fireEvent.click(screen.getByRole("button", { name: "整理资料" }));
  fireEvent.change(screen.getByRole("textbox", { name: "具体要求" }), {
    target: { value: "已保留的要求" },
  });
  fireEvent.click(screen.getByRole("button", { name: "开始生成并保存" }));
  await screen.findByRole("button", { name: "核对原操作" });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "核对原操作" })).not.toBeDisabled(),
  );
  expect(write).toHaveBeenCalledTimes(1);
  view.unmount();
  view = render(<WorkbenchPanel courseId="course" sessionId="another_native_session" />);
  fireEvent.click(screen.getByRole("button", { name: "整理资料" }));
  expect(screen.getByRole("textbox", { name: "具体要求" })).toHaveValue("已保留的要求");
  fireEvent.click(screen.getByRole("button", { name: "核对原操作" }));
  await screen.findByRole("heading", { name: "等待处理" });
  expect(write).toHaveBeenCalledTimes(1);
  expect(api.read).toHaveBeenCalledWith(expect.stringContaining("/learning/operations/"));
  view.unmount();
});

test("unsaved artifact restores its original base and preserves concurrent revision conflict", async () => {
  let view = render(<WorkbenchPanel courseId="course" sessionId="native_session" />);
  fireEvent.click(await screen.findByRole("button", { name: /独立笔记/ }));
  fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByRole("textbox", { name: "编辑内容块" }), {
    target: { value: "尚未保存的修改" },
  });
  view.unmount();
  current = {
    ...base,
    head_revision_id: "revision_parallel",
    revision: {
      ...revision,
      revision_id: "revision_parallel",
      body: { blocks: [{ ...revision.body.blocks[0], markdown: "并发的新文字" }] },
    },
  };
  const write = vi.spyOn(api, "write").mockResolvedValue({
    outcome: "conflict",
    artifact: current,
    conflict: {
      conflict_id: "conflict",
      expected_revision_id: revision.revision_id,
      current_revision_id: "revision_parallel",
      proposed_revision_id: "revision_candidate",
    },
  });
  view = render(<WorkbenchPanel courseId="course" sessionId="native_session" />);
  fireEvent.click(await screen.findByRole("button", { name: /独立笔记/ }));
  expect(await screen.findByRole("textbox", { name: "编辑内容块" })).toHaveValue("尚未保存的修改");
  fireEvent.click(screen.getByRole("button", { name: "保存修改" }));
  await screen.findByText(/当前版本与本次候选均已保留/);
  expect(write).toHaveBeenCalledWith(
    expect.stringContaining("/artifacts/artifact_note/update"),
    expect.objectContaining({
      expected_revision_id: "revision_base",
      changes: [
        expect.objectContaining({
          block_id: "paragraph",
          replacement: expect.objectContaining({
            markdown: "尚未保存的修改",
            attribution: "user_authored",
          }),
        }),
      ],
    }),
  );
  view.unmount();
});
