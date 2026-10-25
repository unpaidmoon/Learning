import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../src/api/client";
import { CorpusPanel } from "../src/workbench/CorpusPanel";
import { EvidencePanel } from "../src/workbench/EvidencePanel";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
});

test("policy receipt loss retains the exact draft and only reconciles the original operation", async () => {
  const status = {
    revision: 3,
    exclusions: ["old.md"],
    scan_state: "completed",
    scan_error: null,
    documents: [],
    has_more: false,
  };
  const read = vi.spyOn(api, "read").mockResolvedValue(status);
  const write = vi.spyOn(api, "write").mockRejectedValue(new TypeError("synthetic response lost"));
  let view = render(<CorpusPanel courseId="course" onClose={vi.fn()} />);
  const input = await screen.findByRole("textbox", { name: "索引排除项" });
  await waitFor(() => expect(input).toHaveValue("old.md"));
  fireEvent.change(input, { target: { value: "private\nbook/appendix.pdf" } });
  fireEvent.click(screen.getByRole("button", { name: "保存排除项" }));
  await waitFor(() => expect(write).toHaveBeenCalledTimes(1));
  expect(write.mock.calls[0][1]).toMatchObject({
    expected_revision: 3,
    exclusions: ["private", "book/appendix.pdf"],
  });
  const operation = (write.mock.calls[0][1] as { operation_id: string }).operation_id;
  view.unmount();
  view = render(<CorpusPanel courseId="course" onClose={vi.fn()} />);
  expect(screen.getByRole("textbox", { name: "索引排除项" })).toHaveValue(
    "private\nbook/appendix.pdf",
  );
  expect(screen.getByRole("button", { name: "保存排除项" })).toBeDisabled();
  read.mockImplementation(async (path) =>
    path.endsWith(operation)
      ? ({ result: { revision: 4, exclusions: ["private", "book/appendix.pdf"] } } as never)
      : ({ ...status, revision: 4, exclusions: ["private", "book/appendix.pdf"] } as never),
  );
  fireEvent.click(screen.getByRole("button", { name: "核对原保存" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "核对原保存" })).toBeNull());
  expect(write).toHaveBeenCalledTimes(1);
  expect(read).toHaveBeenCalledWith(`/courses/course/learning/operations/${operation}`);
  view.unmount();
});

test("a late page cannot replace a new course view", async () => {
  let finish: (value: unknown) => void = () => {};
  vi.spyOn(api, "read").mockImplementation((path) =>
    path.includes("courseA")
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : (Promise.resolve({
          revision: 0,
          exclusions: [],
          scan_state: "pending",
          scan_error: null,
          documents: [],
          has_more: false,
        }) as never),
  );
  const old = render(<CorpusPanel courseId="courseA" onClose={vi.fn()} />);
  old.unmount();
  render(<CorpusPanel courseId="courseB" onClose={vi.fn()} />);
  finish({
    revision: 9,
    exclusions: ["WRONG_COURSE_SENTINEL"],
    scan_state: "completed",
    documents: [],
    has_more: false,
  });
  await Promise.resolve();
  expect(screen.getByRole("textbox", { name: "索引排除项" })).toHaveValue("");
});

test("evidence navigation uses the immutable program source and preserves AI identity", async () => {
  const source = {
    snapshot_id: "selection_a",
    course_id: "course",
    content_role: "ai_generated",
    text: "AI参考解释",
    content_hash: "a".repeat(64),
    created_at: "2026-10-23T00:00:00Z",
    reference: {
      kind: "artifact",
      artifact_id: "asset",
      revision_id: "original_revision",
      block_id: "block",
    },
  };
  vi.spyOn(api, "read").mockResolvedValue({
    course_id: "course",
    chat_id: "chat",
    generation_limit: 20,
    next_cursor: null,
    items: [{ generation_id: "generation", question: "原问题", label: "学习成果", source }],
  });
  const open = vi.fn().mockResolvedValue(undefined);
  render(
    <EvidencePanel courseId="course" sessionId="native" onClose={vi.fn()} onOpenSource={open} />,
  );
  await screen.findByText("AI参考解释");
  expect(screen.getByRole("region", { name: "问答资料来源" })).toHaveTextContent("AI生成");
  fireEvent.click(screen.getByRole("button", { name: "打开原文" }));
  expect(open).toHaveBeenCalledWith(source);
});
