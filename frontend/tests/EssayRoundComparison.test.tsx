import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../src/api/client";
import type { Schema } from "../src/api/client";
import { EssayRoundComparison } from "../src/workbench/EssayRoundComparison";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function round(number: number, text: string): Schema<"EssayRound"> {
  const id = `round${number}`;
  return {
    round_id: id,
    course_id: "course",
    artifact_id: "essay",
    block_id: "draft",
    draft_revision_id: `revision${number}`,
    draft_content_hash: "a".repeat(64),
    round_number: number,
    task_id: `task${number}`,
    state: "completed",
    feedback_artifact_id: `feedback${number}`,
    assessment_revision: 2,
    reference_assessment_id: `ai${number}`,
    user_assessment_id: `user${number}`,
    created_at: "2026-10-24T00:00:00Z",
    content_role: number === 1 ? "user_note" : "mixed",
    draft: {
      block_id: "draft",
      kind: "essay_draft",
      markdown: text,
      attribution: "user_authored",
      source_snapshot_ids: [],
      essay: {
        topic: "Describe an experience.",
        rubric: {
          max_total: 100,
          description: "Actual standard",
          aggregation: "independent",
          dimensions: [],
        },
      },
    },
    result: {
      raw_feedback: `第${number}轮实际批改`,
      annotations: [{ quote: "has", replacement: "have", comment: "主谓一致" }],
    },
    assessments: [
      {
        assessment_id: `ai${number}`,
        round_id: id,
        revision: 1,
        origin: "ai_reference",
        total: 60 + number,
        max_total: 100,
        reason: "模型参考理由",
        supersedes: null,
        run_id: `run${number}`,
        payload: {},
        created_at: "2026-10-24T00:00:00Z",
      },
      {
        assessment_id: `user${number}`,
        round_id: id,
        revision: 2,
        origin: "user_override",
        total: 80 + number,
        max_total: 100,
        reason: "用户更正理由",
        supersedes: `ai${number}`,
        run_id: null,
        payload: {},
        created_at: "2026-10-24T00:00:00Z",
      },
    ],
  };
}

test("two rounds preserve exact Unicode drafts, original reference and separate user corrections", async () => {
  const older = round(1, "  前🙂 I has a plan.\n末尾  ");
  const newer = round(2, "  前🙂 I have a plan.\n末尾  ");
  newer.draft.essay.rubric!.description = "A different standard";
  vi.spyOn(api, "read").mockImplementation(async (path) =>
    path.endsWith("round1") ? older : newer,
  );
  const write = vi.spyOn(api, "write");
  render(<EssayRoundComparison courseId="course" artifactId="essay" rounds={[newer, older]} />);
  const left = await screen.findByRole("article", { name: "第1轮对照内容" });
  const right = screen.getByRole("article", { name: "第2轮对照内容" });
  expect(left.querySelector("pre")?.textContent).toBe(older.draft.markdown);
  expect(right.querySelector("pre")?.textContent).toBe(newer.draft.markdown);
  expect(left.querySelector("pre mark")?.textContent).toBe("s");
  expect(right.querySelector("pre mark")?.textContent).toBe("ve");
  expect(within(left).getByText("AI参考评分：61 / 100")).toBeInTheDocument();
  expect(within(left).getAllByText(/用户更正：81 \/ 100/)).toHaveLength(2);
  expect(within(right).getByText("AI参考评分：62 / 100")).toBeInTheDocument();
  expect(screen.getByText("题目或评分标准不同，两轮分数不能直接比较。")).toBeInTheDocument();
  expect(write).not.toHaveBeenCalled();
});

test("changing the requested pair keeps late reads from replacing the new pair", async () => {
  const rounds = [round(3, "THIRD_DRAFT"), round(2, "SECOND_DRAFT"), round(1, "FIRST_DRAFT")];
  let finish: (value: Schema<"EssayRound">) => void = () => {};
  vi.spyOn(api, "read").mockImplementation((path) =>
    path.endsWith("round2")
      ? new Promise((resolve) => {
          finish = resolve as typeof finish;
        })
      : Promise.resolve(path.endsWith("round1") ? rounds[2] : rounds[0]),
  );
  render(<EssayRoundComparison courseId="course" artifactId="essay" rounds={rounds} />);
  fireEvent.change(screen.getByRole("combobox", { name: "左侧轮次" }), {
    target: { value: "round1" },
  });
  await screen.findByRole("article", { name: "第1轮对照内容" });
  finish(rounds[1]);
  await waitFor(() =>
    expect(screen.queryByRole("article", { name: "第2轮对照内容" })).not.toBeInTheDocument(),
  );
  expect(screen.getByRole("article", { name: "第3轮对照内容" })).toBeInTheDocument();
});
