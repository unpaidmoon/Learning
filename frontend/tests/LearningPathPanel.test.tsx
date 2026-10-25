import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../src/api/client";
import type { Schema } from "../src/api/client";
import { LearningPathPanel } from "../src/workbench/LearningPathPanel";
import { StructuredBlockEditor } from "../src/workbench/StructuredBlockEditor";

const step = (id: string, order: number): Schema<"PathStepView"> => ({
  block: {
    block_id: id,
    kind: "path_step",
    markdown: "学习说明 " + id,
    attribution: "source_paraphrase",
    source_snapshot_ids: [],
    step: {
      title: "概念 " + id,
      objective: "能说明该概念",
      suggested_order: order,
      recall_prompt: "回忆问题 " + id,
      explanation: "讲解内容 " + id,
      questions: [],
    },
  },
  visited: false,
  skipped: false,
  explanation_seen: false,
  activity_current: false,
  self_assessment: null,
  self_assessment_current: false,
  answers: [],
  review: null,
});
const original: Schema<"PathView"> = {
  artifact_id: "path",
  content_revision_id: "revision",
  revision: 0,
  current_block_id: null,
  steps: [step("one", 0), step("two", 1)],
};
function view() {
  return (
    <LearningPathPanel
      courseId="course"
      artifactId="path"
      contentRevision="revision"
      onOpenSource={vi.fn()}
    />
  );
}
beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
});

test("free navigation and reveal use the frozen route version and never submit answers", async () => {
  vi.spyOn(api, "read").mockResolvedValue(original);
  const write = vi.spyOn(api, "write").mockImplementation(async (_path, payload) => {
    const input = payload as Record<string, unknown>;
    return {
      event: {},
      path: {
        ...original,
        revision: 1,
        current_block_id: input.block_id,
        steps: original.steps.map((s) =>
          s.block.block_id === input.block_id
            ? {
                ...s,
                visited: true,
                activity_current: true,
                explanation_seen: input.action === "view_explanation",
              }
            : s,
        ),
      },
    } as never;
  });
  render(view());
  fireEvent.click(await screen.findByRole("button", { name: /概念 two/ }));
  await screen.findByRole("heading", { name: "概念 two" });
  expect(write.mock.calls[0][1]).toMatchObject({
    action: "navigate",
    content_revision_id: "revision",
    expected_revision: 0,
    block_id: "two",
  });
  fireEvent.click(screen.getByRole("button", { name: "查看完整讲解" }));
  await screen.findByText("讲解内容 two");
  expect(write.mock.calls.every(([path]) => path.endsWith("/path/actions"))).toBe(true);
  expect(screen.getByText("实际作答证据：尚无作答")).toBeInTheDocument();
});

test("a lost action response survives reopen and reconciles without another mutation", async () => {
  const result: Schema<"PathView"> = {
    ...original,
    revision: 1,
    current_block_id: "one",
    steps: [
      { ...original.steps[0], skipped: true, visited: true, activity_current: true },
      original.steps[1],
    ],
  };
  vi.spyOn(api, "read").mockImplementation(async (path) =>
    path.includes("path-operations") ? { result: { path: result } } : original,
  );
  const write = vi
    .spyOn(api, "write")
    .mockRejectedValue(new ApiError("BACKEND_UNREACHABLE", "Lost response"));
  const first = render(view());
  fireEvent.click(await screen.findByRole("button", { name: "暂时跳过" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "核对原学习操作" })).not.toBeDisabled(),
  );
  first.unmount();
  render(view());
  fireEvent.click(await screen.findByRole("button", { name: "核对原学习操作" }));
  await screen.findByText("已跳过，可随时返回");
  expect(write).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: /概念 two/ })).not.toBeDisabled();
});

test("stale user confidence stays labelled as an older content version", async () => {
  vi.spyOn(api, "read").mockResolvedValue({
    ...original,
    steps: [{ ...original.steps[0], self_assessment: "confident", self_assessment_current: false }],
  });
  render(view());
  expect(await screen.findByText(/用户自评：自我感觉熟悉（旧内容版本）/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "自我感觉熟悉" })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
});

test("the point editor changes order and recall separately from its explanation", () => {
  const change = vi.fn();
  render(
    <StructuredBlockEditor block={original.steps[0].block} disabled={false} onChange={change} />,
  );
  fireEvent.change(screen.getByRole("spinbutton", { name: "建议顺序" }), {
    target: { value: "4" },
  });
  expect(change.mock.calls[0][0].step).toMatchObject({
    suggested_order: 4,
    recall_prompt: "回忆问题 one",
    explanation: "讲解内容 one",
  });
});
