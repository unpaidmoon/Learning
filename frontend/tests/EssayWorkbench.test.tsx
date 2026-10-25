import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api } from "../src/api/client";
import type { Schema } from "../src/api/client";
import { EssayComposer } from "../src/workbench/EssayComposer";
import { EssayFeedbackPanel } from "../src/workbench/EssayPanel";

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
});

test("a late draft receipt cannot navigate a changed view and reopens for reconciliation", async () => {
  let finish: (receipt: unknown) => void = () => {};
  const write = vi.spyOn(api, "write").mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const created = vi.fn();
  let view = render(<EssayComposer courseId="course" onClose={vi.fn()} onCreated={created} />);
  const text = "  My actual words 🙂\nKeep the exact whitespace.  ";
  fireEvent.change(screen.getByRole("textbox", { name: "实际作文" }), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "保存原稿" }));
  await waitFor(() => expect(write).toHaveBeenCalledTimes(1));
  expect(write.mock.calls[0][1]).toMatchObject({
    kind: "essay",
    body: {
      blocks: [
        expect.objectContaining({
          markdown: text,
          kind: "essay_draft",
          attribution: "user_authored",
        }),
      ],
    },
  });
  view.unmount();
  const receipt = { outcome: "applied", artifact: { artifact_id: "essay" } };
  finish(receipt);
  await Promise.resolve();
  expect(created).not.toHaveBeenCalled();
  vi.spyOn(api, "read").mockResolvedValue({ result: receipt });
  view = render(<EssayComposer courseId="course" onClose={vi.fn()} onCreated={created} />);
  expect(screen.getByRole("textbox", { name: "实际作文" })).toHaveValue(text);
  fireEvent.click(screen.getByRole("button", { name: "核对原稿保存" }));
  await waitFor(() => expect(created).toHaveBeenCalledWith(receipt.artifact));
  expect(write).toHaveBeenCalledTimes(1);
  view.unmount();
});

test("over-limit original is retained and never silently shortened for submission", () => {
  const write = vi.spyOn(api, "write");
  render(<EssayComposer courseId="course" onClose={vi.fn()} onCreated={vi.fn()} />);
  const text = "字".repeat(50001);
  fireEvent.change(screen.getByRole("textbox", { name: "实际作文" }), { target: { value: text } });
  expect(screen.getByRole("textbox", { name: "实际作文" })).toHaveValue(text);
  expect(screen.getByRole("button", { name: "保存原稿" })).toBeDisabled();
  expect(screen.getByRole("status")).toHaveTextContent("输入仍保留");
  expect(write).not.toHaveBeenCalled();
});

test("essay annotations use the exact codepoint range and show reference separately from user evaluation", async () => {
  const block: Schema<"EssayRound">["draft"] = {
    kind: "essay_draft",
    block_id: "draft",
    markdown: "前🙂 bad 后 bad 末",
    attribution: "user_authored",
    source_snapshot_ids: [],
    essay: {
      topic: "Actual prompt",
      rubric: {
        max_total: 100,
        aggregation: "independent",
        dimensions: [],
        description: "Actual grading standard",
      },
    },
  };
  const round: Schema<"EssayRound"> = {
    round_id: "round1",
    course_id: "course",
    artifact_id: "essay",
    block_id: "draft",
    draft_revision_id: "revision1",
    draft_content_hash: "a".repeat(64),
    round_number: 1,
    task_id: "task",
    state: "completed",
    feedback_artifact_id: "feedback",
    assessment_revision: 2,
    reference_assessment_id: "ai",
    user_assessment_id: "user",
    created_at: "2026-10-23T00:00:00Z",
    draft: block,
    content_role: "user_note",
    result: {
      status: "reference_ready",
      raw_feedback: "Explain the grammar issue.",
      reference_evaluation: { total: 70, max_total: 100 },
      annotations: [
        {
          start: 9,
          end: 12,
          quote: "bad",
          replacement: "good",
          comment: "Only the second occurrence",
          kind: "replace",
        },
      ],
    },
    assessments: [
      {
        assessment_id: "ai",
        round_id: "round1",
        revision: 1,
        origin: "ai_reference",
        total: 70,
        max_total: 100,
        reason: "Original reference",
        supersedes: null,
        run_id: "run",
        payload: {},
        created_at: "2026-10-23T00:00:00Z",
      },
      {
        assessment_id: "user",
        round_id: "round1",
        revision: 2,
        origin: "user_override",
        total: 85,
        max_total: 100,
        reason: "My correction",
        supersedes: "ai",
        run_id: null,
        payload: {},
        created_at: "2026-10-23T00:00:01Z",
      },
    ],
  };
  vi.spyOn(api, "read").mockImplementation(async (path) =>
    path.includes("/essay-rounds/")
      ? (round as never)
      : ({ head_revision_id: "revision2" } as never),
  );
  const view = render(<EssayFeedbackPanel courseId="course" roundId="round1" />);
  await screen.findByText("AI参考评分：70 / 100");
  expect(screen.getByText(/此轮绑定历史稿/)).toBeInTheDocument();
  const original = view.container.querySelector(".essay-original");
  expect(original?.textContent).toBe(block.markdown);
  expect(original?.querySelector("mark")?.previousSibling?.textContent).toBe("前🙂 bad 后 ");
  expect(original?.querySelector("mark")?.textContent).toBe("bad");
  expect(screen.getByText("用户更正：85 / 100", { exact: true })).toBeInTheDocument();
});
