import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../src/api/client";
import type { PickedSource } from "../src/workbench/model";
import { TaskComposer } from "../src/workbench/TaskComposer";
import { TaskSourcePicker } from "../src/workbench/TaskSourcePicker";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
});

function reads() {
  vi.spyOn(api, "read").mockImplementation(async (path) => {
    if (path === "/courses")
      return [
        { course_id: "owner", title: "当前教材" },
        { course_id: "origin", title: "对照资料" },
      ] as never;
    if (path.includes("/files?"))
      return {
        entries: [{ path: "definition.md", name: "definition.md", kind: "file", viewer: "text" }],
      } as never;
    throw new Error("Unexpected read " + path);
  });
}

test("same-named A and B files stay separate and preserve the explicit origin", async () => {
  reads();
  let selected: PickedSource[] = [];
  const write = vi.spyOn(api, "write").mockImplementation(async (path) => {
    const origin = path.split("/")[2];
    return {
      course_id: origin,
      asset_id: origin + "_asset",
      source_version: origin + "_version",
      viewer: "text",
    } as never;
  });
  function Picker() {
    const [value, setValue] = useState<PickedSource[]>([]);
    return (
      <TaskSourcePicker
        courseId="owner"
        value={value}
        disabled={false}
        onChange={(items) => {
          selected = items;
          setValue(items);
        }}
      />
    );
  }
  render(<Picker />);
  fireEvent.click(await screen.findByRole("checkbox", { name: "definition.md" }));
  await waitFor(() => expect(selected).toHaveLength(1));
  const course = screen.getByRole("combobox", { name: "任务资料来源课程" });
  await screen.findByRole("option", { name: "对照资料" });
  fireEvent.change(course, { target: { value: "origin" } });
  fireEvent.click(await screen.findByRole("checkbox", { name: "definition.md" }));
  await waitFor(() => expect(selected).toHaveLength(2));
  expect(selected.map((item) => item.key)).toEqual([
    "file:definition.md",
    "file:origin:definition.md",
  ]);
  expect(selected[0].declaration).not.toHaveProperty("source_course_id");
  expect(selected[1]).toMatchObject({
    title: "对照资料 · definition.md",
    declaration: { source_course_id: "origin", asset_id: "origin_asset" },
  });
  expect(write.mock.calls.map(([path]) => path)).toEqual([
    "/courses/owner/files/open",
    "/courses/origin/files/open",
  ]);
  fireEvent.click(screen.getByRole("checkbox", { name: "definition.md" }));
  expect(selected).toHaveLength(1);
  expect(selected[0].declaration).not.toHaveProperty("source_course_id");
});

test("whole B declaration is explicit while submit and saved result remain owner A", async () => {
  reads();
  const created = vi.fn();
  const write = vi
    .spyOn(api, "write")
    .mockResolvedValue({ course_id: "owner", task_id: "owned_task" } as never);
  render(
    <TaskComposer
      courseId="owner"
      sessionId="original_session"
      onClose={vi.fn()}
      onCreated={created}
    />,
  );
  await screen.findByRole("option", { name: "对照资料" });
  fireEvent.change(screen.getByRole("combobox", { name: "任务资料来源课程" }), {
    target: { value: "origin" },
  });
  expect(write).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "开始生成并保存" }));
  await waitFor(() => expect(created).toHaveBeenCalled());
  expect(write).toHaveBeenCalledWith(
    "/desktop/sessions/original_session/learning-task-intents",
    expect.objectContaining({
      sources: [{ kind: "course", include_generated: false, source_course_id: "origin" }],
      target: { mode: "create", title: "学习讲义" },
    }),
  );
});
