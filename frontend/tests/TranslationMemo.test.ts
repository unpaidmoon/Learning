import { createHash, webcrypto } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../src/api/client";
import type { Schema } from "../src/api/client";
import {
  clearTranslationMemo,
  rememberCompletedTranslation,
  translatePdfSelection,
  verifiedTranslation,
} from "../src/reading/translationMemo";
import type { ReadingAction } from "../src/reading/actions";

type Task = Schema<"TaskView">;
const text = "这是已保存的真实类型译文 fixture。";
const instruction = "翻译成中文，保留公式和术语。";
function fixtures() {
  let serial = 0;
  const posts: Record<string, unknown>[] = [];
  const source: Schema<"SourceSnapshot"> = {
    snapshot_id: "s",
    course_id: "course",
    content_hash: "a".repeat(64),
    content_role: "original",
    text: "Only this sentence.",
    created_at: "2026-10-24",
    reference: {
      kind: "document",
      asset_id: "a",
      source_version: "v",
      source_digest: "b".repeat(64),
      page_index: 0,
      start: 5,
      end: 24,
      offset_unit: "unicode_codepoint",
      parser_version: "fixture",
    },
  };
  const policy: Schema<"TranslationPolicyView"> = {
    course_id: "course",
    chat_id: "chat",
    policy_id: "fixed_policy",
    settings: {
      model: "deepseek-flash",
      thinking: "high",
      max_output_tokens: 8192,
      timeout_seconds: 180,
    },
  };
  const tasks = new Map<string, Task>();
  const action = (operation: string, force = false) =>
    ({
      intent: "translation",
      operation_id: operation,
      force_regenerate: force,
      file: { course_id: "course" },
      selection: { selected_text_fixture: source.text },
    }) as unknown as ReadingAction;
  vi.stubGlobal("crypto", webcrypto);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const path = String(input);
      let value: unknown;
      if (path.endsWith("/session")) value = { csrf_token: "fixture_csrf" };
      else if (path.endsWith("/selections/pdf")) value = source;
      else if (path.endsWith("/translation-policy")) value = policy;
      else if (path.endsWith("/translation-cache-entry")) {
        const task = [...tasks.values()].find((task) => path.includes(task.task_id))!;
        value = {
          status:
            task.state === "unknown"
              ? "unknown"
              : task.state === "completed"
                ? "eligible"
                : "pending",
          task_id: task.task_id,
          source,
          settings: task.settings,
          policy_id: policy.policy_id,
          instruction,
          text,
        };
      } else if (path.endsWith("/learning-tasks")) {
        const body = JSON.parse(String(init?.body));
        posts.push(body);
        if (body.cached_task_id) {
          const task = tasks.get(body.cached_task_id)!;
          value = {
            ...task,
            translation_reuse: {
              operation_id: body.operation_id,
              kind: "saved_result",
              original_task_id: task.task_id,
              original_run_id: "original_run",
              artifact_id: "artifact",
              revision_id: "revision",
              text_sha256: createHash("sha256").update(text).digest("hex"),
              actual_outbound_attempts: 0,
            },
          };
        } else {
          const task: Task = {
            task_id: "task" + ++serial,
            course_id: "course",
            chat_id: "chat",
            kind: "translation",
            instruction,
            coverage_mode: "selection",
            scope_manifest_id: null,
            revision: 1,
            scope_revision: 0,
            layout_version: 1,
            plan_revision: 0,
            state: "completed",
            settings: { ...policy.settings },
            sources_only: false,
            allow_external: false,
            outline: [],
            outline_confirmed: true,
            created_at: "2026-10-24",
            updated_at: "2026-10-24",
            units: [
              {
                unit_id: "selected",
                ordinal: 0,
                label: "选区 1",
                source_snapshot_id: source.snapshot_id,
                state: "completed",
                run_id: "original_run",
                result_artifact_id: "artifact",
                error_code: null,
              },
            ],
          };
          tasks.set(task.task_id, task);
          value = task;
        }
      } else throw new Error("Unexpected fixture path " + path);
      return new Response(JSON.stringify(value), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return { source, policy, tasks, posts, action };
}

beforeEach(clearTranslationMemo);
afterEach(() => {
  clearTranslationMemo();
  vi.unstubAllGlobals();
});

describe("原始翻译LRU的正式入口", () => {
  it("uses original cached text only after the server confirms the same saved result", async () => {
    const fixture = fixtures();
    await api.connect();
    const first = await translatePdfSelection("sid", "course", "chat", fixture.action("first"));
    await rememberCompletedTranslation(first);
    const again = await translatePdfSelection("sid", "course", "chat", fixture.action("again"));
    expect(fixture.posts).toHaveLength(2);
    expect(fixture.posts[0].cached_task_id).toBeUndefined();
    expect(fixture.posts[1].cached_task_id).toBe(first.task_id);
    expect(await verifiedTranslation(again)).toBe(text);
    expect(fixture.posts[1].source_snapshot_ids).toEqual([fixture.source.snapshot_id]);
    expect(fixture.posts[1].pdf_selection).toBeUndefined();
    const fresh = await translatePdfSelection(
      "sid",
      "course",
      "chat",
      fixture.action("fresh", true),
    );
    expect(fresh.task_id).not.toBe(first.task_id);
    expect(fixture.posts[2].cached_task_id).toBeUndefined();
  });
  it("separates source identity and thinking settings instead of deduplicating equal text", async () => {
    const fixture = fixtures();
    await api.connect();
    const first = await translatePdfSelection("sid", "course", "chat", fixture.action("first"));
    await rememberCompletedTranslation(first);
    fixture.source.snapshot_id = "different_source";
    fixture.source.content_hash = "c".repeat(64);
    fixture.source.reference = {
      ...fixture.source.reference,
      asset_id: "other_asset",
    } as typeof fixture.source.reference;
    await translatePdfSelection("sid", "course", "chat", fixture.action("other"));
    expect(fixture.posts[1].cached_task_id).toBeUndefined();
    fixture.policy.settings.thinking = "low";
    await translatePdfSelection("sid", "course", "chat", fixture.action("low"));
    expect(fixture.posts[2].cached_task_id).toBeUndefined();
  });
  it("reuses the same document character range after display boxes change without mutating snapshots", async () => {
    const fixture = fixtures();
    await api.connect();
    const first = await translatePdfSelection("sid", "course", "chat", fixture.action("first"));
    await rememberCompletedTranslation(first);
    fixture.source.snapshot_id = "new_display_snapshot";
    fixture.source.content_hash = "d".repeat(64);
    fixture.source.reference = {
      ...fixture.source.reference,
      regions: [{ x0: 0.11, y0: 0.1, x1: 0.2, y1: 0.2 }],
    } as typeof fixture.source.reference;
    const frozen = JSON.stringify(fixture.source);
    await translatePdfSelection("sid", "course", "chat", fixture.action("again"));
    expect(fixture.posts[1].cached_task_id).toBe(first.task_id);
    expect(JSON.stringify(fixture.source)).toBe(frozen);
  });
  it("blocks an unknown prior outcome before making another task request", async () => {
    const fixture = fixtures();
    await api.connect();
    const first = await translatePdfSelection("sid", "course", "chat", fixture.action("first"));
    fixture.tasks.get(first.task_id)!.state = "unknown";
    await expect(
      translatePdfSelection("sid", "course", "chat", fixture.action("again")),
    ).rejects.toMatchObject({ code: "TASK_RECONCILIATION_REQUIRED" });
    expect(fixture.posts).toHaveLength(1);
  });
  it("disables reuse when the original cache can only produce a weak hash", async () => {
    const fixture = fixtures();
    await api.connect();
    vi.stubGlobal("crypto", {});
    const first = await translatePdfSelection("sid", "course", "chat", fixture.action("first"));
    await rememberCompletedTranslation(first);
    await translatePdfSelection("sid", "course", "chat", fixture.action("again"));
    expect(fixture.posts[1].cached_task_id).toBeUndefined();
  });
});
