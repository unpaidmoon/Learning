import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api, ApiError } from "../src/api/client";
import type { Schema } from "../src/api/client";
import { BookOutlinePanel } from "../src/workbench/BookOutlinePanel";
import { BookReader } from "../src/workbench/BookReader";

const hash = "a".repeat(64);
const proposal = {
  chapters: [
    {
      chapter_id: "c1",
      title: "First chapter",
      objective: "Recall the material",
      source_ids: ["source"],
    },
  ],
  rationale: "From the complete source inventory.",
};
const task: Schema<"TaskView"> = {
  task_id: "task",
  course_id: "course",
  chat_id: null,
  kind: "book",
  instruction: "Make a book",
  coverage_mode: "complete",
  scope_manifest_id: null,
  revision: 3,
  scope_revision: 0,
  state: "awaiting_outline",
  settings: {
    model: "deepseek-flash",
    thinking: "high",
    max_output_tokens: 8192,
    timeout_seconds: 180,
  },
  sources_only: true,
  allow_external: false,
  outline: [],
  outline_confirmed: false,
  layout_version: 2,
  plan_revision: 1,
  units: [],
  created_at: "2026-10-24T00:00:00Z",
  updated_at: "2026-10-24T00:00:00Z",
};
const outline: Schema<"BookOutlineView"> = {
  task_id: "task",
  task_revision: 3,
  proposal_hash: hash,
  proposal,
  confirmed: false,
  confirmation_count: 0,
  supplied: false,
  sources: [{ source_id: "source", kind: "document", label: "My material", declaration: {} }],
};
const question: Schema<"PracticeQuestion">["block"] = {
  block_id: "q1",
  kind: "question",
  markdown: "My question",
  attribution: "source_paraphrase",
  source_snapshot_ids: ["source"],
  question: {
    question_type: "short_answer",
    options: [],
    reference_answer: "Reference",
    explanation: "Explanation",
    answer_authority: "ai_reference",
  },
};
const book: Schema<"BookView"> = {
  artifact_id: "book",
  revision_id: "rev2",
  title: "Book",
  layout_hash: hash,
  origin_task_id: "task",
  task_state: "completed",
  book_status: "ready",
  is_historical: false,
  chapters: [
    {
      chapter: proposal.chapters[0],
      state: "ready",
      pages: [
        {
          state: "ready",
          layout: {
            page_id: "p1",
            chapter_id: "c1",
            unit_id: "u1",
            title: "Page",
            order_key: [0, 0, 0],
            block_ids: ["q1"],
            block_hashes: { q1: hash },
            question_revisions: { q1: "rev1" },
            source_snapshot_ids: ["source"],
          },
        },
      ],
    },
  ],
};
const revision: Schema<"ArtifactRevision"> = {
  revision_id: "rev2",
  artifact_id: "book",
  parent_revision_id: "rev1",
  title: "Book",
  body: { blocks: [question] },
  content_role: "ai_generated",
  author: "model",
  task_id: "task",
  disposition: "published",
  created_at: "2026-10-24T00:00:00Z",
};
beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
});

test("lost outline confirmation reopens its original receipt even after the server confirms", async () => {
  let committed = false;
  const confirmed = { ...task, revision: 4, outline_confirmed: true, state: "queued" as const };
  vi.spyOn(api, "read").mockImplementation(async (path) =>
    path.includes("/operations/") ? { result: confirmed } : { ...outline, confirmed: committed },
  );
  const write = vi.spyOn(api, "write").mockImplementation(async () => {
    committed = true;
    throw new ApiError("BACKEND_UNREACHABLE", "Lost response");
  });
  const done = vi.fn();
  const first = render(<BookOutlinePanel courseId="course" task={task} onConfirmed={done} />);
  fireEvent.click(await screen.findByRole("button", { name: "确认目录并连续生成" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "核对原目录确认" })).not.toBeDisabled(),
  );
  first.unmount();
  render(<BookOutlinePanel courseId="course" task={task} onConfirmed={done} />);
  await screen.findByText("已确认目录 · 后续章节连续生成");
  fireEvent.click(screen.getByRole("button", { name: "核对原目录确认" }));
  await waitFor(() => expect(done).toHaveBeenCalledWith(confirmed));
  expect(write).toHaveBeenCalledTimes(1);
});

test("a new page revision keeps an unchanged question's actual answer tied to its earlier pin", async () => {
  vi.spyOn(api, "read").mockImplementation(async (path) =>
    path.endsWith("/book") ? book : path.includes("/versions/") ? revision : { answers: [] },
  );
  const write = vi
    .spyOn(api, "write")
    .mockRejectedValue(new ApiError("BACKEND_UNREACHABLE", "Lost response"));
  render(<BookReader courseId="course" artifactId="book" onOpenSource={vi.fn()} />);
  fireEvent.change(await screen.findByRole("textbox", { name: "实际作答" }), {
    target: { value: "My actual answer" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交作答" }));
  await waitFor(() => expect(write).toHaveBeenCalled());
  expect(write.mock.calls[0][1]).toMatchObject({
    question_revision_id: "rev1",
    block_id: "q1",
    text: "My actual answer",
  });
});

test("an unfinished directory draft retains its original base across reopen and newer pages", async () => {
  let current = book;
  vi.spyOn(api, "read").mockImplementation(async (path) =>
    path.endsWith("/book")
      ? current
      : path.includes("/versions/")
        ? { ...revision, revision_id: current.revision_id }
        : { answers: [] },
  );
  const write = vi.spyOn(api, "write").mockResolvedValue({ outcome: "conflict" });
  const first = render(<BookReader courseId="course" artifactId="book" onOpenSource={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "调整目录" }));
  fireEvent.change(screen.getByRole("textbox", { name: "章节标题" }), {
    target: { value: "My saved heading" },
  });
  first.unmount();
  current = { ...book, revision_id: "rev3" };
  render(<BookReader courseId="course" artifactId="book" onOpenSource={vi.fn()} />);
  expect(await screen.findByRole("textbox", { name: "章节标题" })).toHaveValue("My saved heading");
  fireEvent.click(screen.getByRole("button", { name: "保存目录调整" }));
  await screen.findByText(/目录同时发生变化/);
  expect(write.mock.calls[0][1]).toMatchObject({
    expected_revision_id: "rev2",
    chapters: [expect.objectContaining({ title: "My saved heading" })],
  });
});

test("reading a historical book version offers the content without submitting practice", async () => {
  vi.spyOn(api, "read").mockImplementation(async (path) =>
    path.includes("/book?") ? { ...book, is_historical: true } : revision,
  );
  const write = vi.spyOn(api, "write");
  render(
    <BookReader courseId="course" artifactId="book" fixedRevision="rev2" onOpenSource={vi.fn()} />,
  );
  await screen.findByText("My question");
  expect(screen.queryByRole("textbox", { name: "实际作答" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "调整目录" })).not.toBeInTheDocument();
  expect(write).not.toHaveBeenCalled();
});
