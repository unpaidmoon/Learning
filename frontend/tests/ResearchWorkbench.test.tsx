import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { api } from "../src/api/client";
import type { Schema } from "../src/api/client";
import { ResearchPanel } from "../src/workbench/ResearchPanel";
import { SourcePreview } from "../src/workbench/SourcePreview";
import { TaskComposer } from "../src/workbench/TaskComposer";

const task: Schema<"TaskView"> = {
  task_id: "task",
  course_id: "course",
  chat_id: null,
  kind: "research",
  instruction: "Compare given sources",
  coverage_mode: "complete",
  scope_manifest_id: null,
  revision: 4,
  scope_revision: 0,
  state: "completed",
  settings: {
    model: "deepseek-flash",
    thinking: "high",
    max_output_tokens: 8192,
    timeout_seconds: 180,
  },
  sources_only: false,
  allow_external: false,
  outline: [],
  outline_confirmed: true,
  layout_version: 2,
  plan_revision: 2,
  units: [],
  created_at: "2026-10-24T00:00:00Z",
  updated_at: "2026-10-24T00:00:00Z",
};
const source: Schema<"SourceSnapshot"> = {
  snapshot_id: "web_source",
  course_id: "course",
  content_hash: "a".repeat(64),
  content_role: "original",
  text: "Saved public text at the time of research.",
  created_at: "2026-10-24T00:00:00Z",
  reference: {
    kind: "web_evidence",
    evidence_kind: "search_excerpt",
    url: "https://example.com/official",
    final_url: null,
    title: "Official reference",
    retrieved_at: "2026-10-24T00:00:00Z",
    published_at: null,
    response_sha256: "b".repeat(64),
    target_http_status: null,
    parser_version: "search-v1",
  },
};

beforeEach(() => {
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("two selected files freeze their own versions and submission waits for selection registration", async () => {
  let finishA: (value: unknown) => void = () => {};
  const opened = (path: string) => ({
    course_id: "course",
    viewer: "text",
    entry: { name: path, path, kind: "file", viewer: "text", size_bytes: 10 },
    asset_id: path === "a.txt" ? "a" : "b",
    source_version: path === "a.txt" ? "a_version" : "b_version",
  });
  vi.spyOn(api, "read").mockResolvedValue({
    entries: ["a.txt", "b.txt"].map((path) => ({ name: path, path, kind: "file", viewer: "text" })),
  } as never);
  const write = vi.spyOn(api, "write").mockImplementation(async (path, body) => {
    if (path.endsWith("files/open"))
      return (
        (body as { path: string }).path === "a.txt"
          ? await new Promise<unknown>((resolve) => {
              finishA = resolve;
            })
          : opened("b.txt")
      ) as never;
    return task as never;
  });
  const created = vi.fn();
  render(
    <TaskComposer courseId="course" sessionId="native" onClose={vi.fn()} onCreated={created} />,
  );
  fireEvent.change(screen.getByLabelText("成果类型"), { target: { value: "research" } });
  fireEvent.change(screen.getByLabelText("资料范围"), { target: { value: "custom" } });
  fireEvent.click(await screen.findByLabelText("a.txt"));
  expect((screen.getByLabelText("a.txt") as HTMLInputElement).checked).toBe(true);
  expect(
    (screen.getByRole("button", { name: "开始生成并保存" }) as HTMLButtonElement).disabled,
  ).toBe(true);
  finishA(opened("a.txt"));
  await screen.findByRole("list", { name: "已选来源" });
  fireEvent.click(screen.getByLabelText("b.txt"));
  await waitFor(() => expect(screen.getAllByRole("listitem")).toHaveLength(2));
  fireEvent.click(screen.getByRole("button", { name: "开始生成并保存" }));
  await waitFor(() => expect(created).toHaveBeenCalledWith(task));
  const payload = write.mock.calls.find(([path]) =>
    path.includes("learning-task-intents"),
  )?.[1] as Schema<"TaskIntent">;
  expect(payload.sources).toEqual([
    {
      kind: "document",
      asset_id: "a",
      source_version: "a_version",
      first_page: 0,
      text_only: false,
    },
    {
      kind: "document",
      asset_id: "b",
      source_version: "b_version",
      first_page: 0,
      text_only: false,
    },
  ]);
  expect(payload.allow_external).toBe(false);
  expect(payload).not.toHaveProperty("model");
});

test("external-only research submits only the explicit URL scope and native-selected settings", async () => {
  vi.spyOn(api, "read").mockRejectedValue(new Error("unneeded read"));
  const write = vi.spyOn(api, "write").mockResolvedValue(task as never);
  render(
    <TaskComposer courseId="course" sessionId="native" onClose={vi.fn()} onCreated={vi.fn()} />,
  );
  fireEvent.change(screen.getByLabelText("成果类型"), { target: { value: "research" } });
  expect((screen.getByLabelText("标题") as HTMLInputElement).value).toBe("资料对照报告");
  fireEvent.click(screen.getByLabelText("允许为本次研究检索和读取外部公开文字资料"));
  fireEvent.change(screen.getByLabelText("资料范围"), { target: { value: "external" } });
  fireEvent.change(screen.getByLabelText("指定外部网页（可选，每行一个网址）"), {
    target: { value: "https://example.com/a\nhttps://example.com/b" },
  });
  fireEvent.change(screen.getByLabelText("研究子主题（可选，每行一项）"), {
    target: { value: "条件\n局限" },
  });
  fireEvent.click(screen.getByRole("button", { name: "开始生成并保存" }));
  await waitFor(() => expect(write).toHaveBeenCalledTimes(1));
  const payload = write.mock.calls[0][1] as Schema<"TaskIntent">;
  expect(payload.sources).toEqual([]);
  expect(payload.coverage_mode).toBe("complete");
  expect(payload.research).toEqual({
    topics: ["条件", "局限"],
    urls: ["https://example.com/a", "https://example.com/b"],
  });
  expect(payload.allow_external).toBe(true);
  expect(payload).not.toHaveProperty("settings");
});

test("saved external excerpts show their real origin and never refetch the current website", async () => {
  const read = vi.spyOn(api, "read");
  render(<SourcePreview source={source} />);
  expect(await screen.findByText("Saved public text at the time of research.")).toBeTruthy();
  expect(screen.getByText("外部检索摘要 · 未据此声称读过完整网页")).toBeTruthy();
  expect(screen.getByRole("link", { name: "打开公开网页当前版本" }).getAttribute("href")).toBe(
    "https://example.com/official",
  );
  expect(read).not.toHaveBeenCalled();
});

test("synthesis evidence retains the earlier read attribution without pretending to reread", async () => {
  vi.spyOn(api, "read").mockImplementation(async (path) => {
    if (path.includes("/evidence/"))
      return { items: [{ source, read_in_run_id: "prior_run", from_current_run: false }] } as never;
    return {
      task_id: "task",
      task_revision: 4,
      allow_external: true,
      topics: ["对照"],
      statistics: {},
      reconciliation_required: [],
      source_decision_required: [],
      external_reads: [],
      checkpoints: [
        {
          unit_id: "synthesis",
          topic_id: "topic",
          title: "对照",
          state: "completed",
          phase: "synthesis",
          run_id: "current_run",
          error_code: null,
          evidence_count: 1,
          evidence_from_prior_runs: 1,
        },
      ],
    } as never;
  });
  const write = vi.spyOn(api, "write");
  render(<ResearchPanel courseId="course" task={task} />);
  fireEvent.click(await screen.findByRole("button", { name: "查看依据（沿用已读版本）" }));
  expect(
    await screen.findByText("此版本已在本任务先前步骤读取，本轮沿用其证据引用。"),
  ).toBeTruthy();
  expect(screen.getByText("Saved public text at the time of research.")).toBeTruthy();
  expect(write).not.toHaveBeenCalled();
});
