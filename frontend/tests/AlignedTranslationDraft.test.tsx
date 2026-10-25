import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../src/api/client";
import { AlignedTranslationDraft } from "../src/workbench/AlignedTranslationDraft";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("a valid prefix followed by broken JSON remains an explicitly unverified draft", async () => {
  const read = vi.spyOn(api, "read").mockResolvedValue({
    run_id: "run",
    task_id: "task",
    state: "completed",
    body: '{"src":"First 🙂 paragraph.","tgt":"首段。"}\n{"src":"broken',
  });
  const write = vi.spyOn(api, "write");
  render(<AlignedTranslationDraft courseId="course" taskId="task" runId="run" onClose={vi.fn()} />);
  await screen.findByText("首段。");
  expect(screen.getByRole("region", { name: "待核原文" })).toHaveTextContent("First 🙂 paragraph.");
  expect(screen.getByText(/尚未证明完整覆盖/)).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "译文草稿" })).toBeInTheDocument();
  expect(read).toHaveBeenCalledTimes(1);
  expect(write).not.toHaveBeenCalled();
});

test("a late read from an old run cannot replace the newly selected draft", async () => {
  let finish: (value: unknown) => void = () => {};
  vi.spyOn(api, "read").mockImplementation((path) =>
    path.endsWith("old")
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : Promise.resolve({
          run_id: "new",
          task_id: "task",
          state: "completed",
          body: '{"src":"New.","tgt":"新的。"}\n{"done":true}',
        }),
  );
  const view = render(
    <AlignedTranslationDraft courseId="course" taskId="task" runId="old" onClose={vi.fn()} />,
  );
  view.rerender(
    <AlignedTranslationDraft courseId="course" taskId="task" runId="new" onClose={vi.fn()} />,
  );
  await screen.findByText("新的。");
  finish({
    run_id: "old",
    task_id: "task",
    state: "completed",
    body: '{"src":"Old.","tgt":"旧的。"}',
  });
  await Promise.resolve();
  expect(screen.queryByText("旧的。")).not.toBeInTheDocument();
});
