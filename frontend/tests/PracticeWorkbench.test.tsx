import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../src/api/client";
import type { Schema } from "../src/api/client";
import { QuestionPlayer } from "../src/workbench/QuestionPlayer";
import { ReviewPanel } from "../src/workbench/ReviewPanel";

const block: Schema<"PracticeQuestion">["block"] = {
  block_id: "q1",
  kind: "question",
  markdown: "Actual question",
  attribution: "ai_supplement",
  source_snapshot_ids: [],
  question: {
    question_type: "short_answer",
    options: [],
    reference_answer: "Reference only",
    explanation: "Explain the reference.",
    answer_authority: "ai_reference",
  },
};
const actual: Schema<"AnswerView"> = {
  answer_id: "answer",
  course_id: "course",
  artifact_id: "paper",
  question_revision_id: "revision",
  block_id: "q1",
  text: "My actual words",
  sequence: 1,
  submitted_at: "2026-10-23T00:00:00Z",
  assessment_revision: 1,
  effective_assessment: {
    assessment_id: "assessment",
    answer_id: "answer",
    revision: 1,
    parent_assessment_id: null,
    verdict: "pending",
    author: "deterministic",
    reason: "Needs reference assessment",
    score: null,
    run_id: null,
    evidence: {},
    created_at: "2026-10-23T00:00:00Z",
  },
};
beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
});

test("lost actual-answer response reopens the same receipt without another submission", async () => {
  vi.spyOn(api, "read").mockImplementation(async (path) => {
    if (path.includes("practice-operations"))
      return { kind: "answer_submit", result: actual } as never;
    if (path.includes("/answers?")) return { answers: [] } as never;
    throw new Error(path);
  });
  const write = vi
    .spyOn(api, "write")
    .mockRejectedValue(new ApiError("BACKEND_UNREACHABLE", "Lost response"));
  let view = render(
    <QuestionPlayer
      courseId="course"
      artifactId="paper"
      revisionId="revision"
      block={block}
      sessionId="native"
    />,
  );
  fireEvent.change(screen.getByRole("textbox", { name: "实际作答" }), {
    target: { value: actual.text },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交作答" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "核对原作答提交" })).not.toBeDisabled(),
  );
  view.unmount();
  view = render(
    <QuestionPlayer
      courseId="course"
      artifactId="paper"
      revisionId="revision"
      block={block}
      sessionId="native_2"
    />,
  );
  expect(screen.getByRole("textbox", { name: "实际作答" })).toHaveValue(actual.text);
  fireEvent.click(screen.getByRole("button", { name: "核对原作答提交" }));
  await screen.findByText("待判断 · 确定性比较");
  fireEvent.click(screen.getByRole("button", { name: "查看参考答案与解析" }));
  expect(screen.getByText("Reference only")).toBeInTheDocument();
  expect(write).toHaveBeenCalledTimes(1);
  view.unmount();
});

test("AI reference requests the stored answer using the native choice without model settings from UI", async () => {
  vi.spyOn(api, "read").mockResolvedValue({ answers: [actual] });
  const write = vi.spyOn(api, "write").mockResolvedValue({ task_id: "review_task" });
  const created = vi.fn();
  render(
    <QuestionPlayer
      courseId="course"
      artifactId="paper"
      revisionId="revision"
      block={block}
      sessionId="native"
      onTaskCreated={created}
    />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "AI参考判分" }));
  await waitFor(() => expect(created).toHaveBeenCalled());
  expect(write).toHaveBeenCalledWith(
    "/desktop/sessions/native/learning-task-intents",
    expect.objectContaining({
      kind: "answer_review",
      sources: [],
      answer_review: { answer_id: "answer", expected_revision: 1 },
    }),
  );
  expect(write.mock.calls[0][1]).not.toHaveProperty("settings");
  expect(write.mock.calls[0][1]).not.toHaveProperty("user_answer");
});

test("card rating requires recall and reveal; a lost rating reconciles without repeating the rating", async () => {
  const stamp = "2026-10-23T00:00:00Z";
  const item: Schema<"ReviewItem"> = {
    review_id: "review",
    course_id: "course",
    kind: "card",
    artifact_id: "deck",
    block_id: "c1",
    content_revision_id: "card_revision",
    source_answer_id: null,
    assessment_id: null,
    queue_state: "active",
    eligible: true,
    inactive_reason: null,
    schedule_revision: 1,
    due_ms: 0,
    scheduler: "fsrs",
    scheduler_state: { phase: "new", reps: 0 },
    scheduler_config: {},
    created_at: stamp,
    updated_at: stamp,
    label: "Recall this fact",
  };
  const opened: Schema<"ReviewSession"> = {
    session_id: "review_session",
    review_id: "review",
    logical_day: "2026-10-23",
    schedule_revision: 1,
    content_revision_id: "card_revision",
    state: "opened",
    opened_at: stamp,
    revealed_at: null,
    rating_event_id: null,
  };
  const revealed: Schema<"ReviewSession"> = { ...opened, state: "revealed", revealed_at: stamp };
  const receipt: Schema<"ReviewRatingReceipt"> = {
    event_id: "rated",
    item: {
      ...item,
      schedule_revision: 2,
      due_ms: 9999999999999,
      scheduler_state: { phase: "learning", reps: 1 },
    },
    session: { ...revealed, state: "rated", rating_event_id: "rated" },
    audit: {},
  };
  let rated = false;
  vi.spyOn(api, "read").mockImplementation(async (path) => {
    if (path.includes("practice-operations"))
      return { kind: "review_rating", result: receipt } as never;
    if (path.includes("/today"))
      return {
        logical_day: "2026-10-23",
        daily_limit: 2,
        admitted_count: 1,
        completed_count: rated ? 1 : 0,
        items: [item],
      } as never;
    if (path.endsWith("/preferences")) return { revision: 0, daily_limit: 2 } as never;
    if (path.includes("/versions/"))
      return {
        body: {
          blocks: [
            {
              block_id: "c1",
              kind: "card",
              markdown: "Recall this fact",
              card: { back: "The hidden answer" },
            },
          ],
        },
      } as never;
    if (path.endsWith("/reviews/review")) return (rated ? receipt.item : item) as never;
    throw new Error(path);
  });
  const write = vi.spyOn(api, "write").mockImplementation(async (path) => {
    if (path.endsWith("/sessions")) return opened as never;
    if (path.endsWith("/reveal")) return revealed as never;
    if (path.endsWith("/rating")) {
      rated = true;
      throw new ApiError("BACKEND_UNREACHABLE", "Lost rating response");
    }
    throw new Error(path);
  });
  let view = render(<ReviewPanel courseId="course" sessionId="native" onTaskCreated={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: /Recall this fact/ }));
  expect(screen.queryByText("The hidden answer")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "记得" })).not.toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "开始本次复习" }));
  fireEvent.click(await screen.findByRole("button", { name: "回忆后翻面" }));
  await screen.findByText("The hidden answer");
  fireEvent.click(screen.getByRole("button", { name: "记得" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "核对原复习操作" })).not.toBeDisabled(),
  );
  view.unmount();
  view = render(<ReviewPanel courseId="course" sessionId="native" onTaskCreated={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: /Recall this fact/ }));
  fireEvent.click(await screen.findByRole("button", { name: "核对原复习操作" }));
  await screen.findByText(/已记录本次用户评分/);
  expect(write.mock.calls.filter(([path]) => path.endsWith("/rating"))).toHaveLength(1);
  expect(screen.getByRole("button", { name: "撤销本次评分" })).toBeEnabled();
  view.unmount();
});
