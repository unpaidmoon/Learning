import {
  buildCacheKey,
  readCache,
  writeCache,
  clearCache,
} from "../../../third_party/deepstudent/upstream/src/features/chat/components/translationCache";
import { api, ApiError } from "../api/client";
import type { Schema } from "../api/client";
import type { ReadingAction } from "./actions";

type Task = Schema<"TaskView">;
type Source = Schema<"SourceSnapshot">;
type Policy = Schema<"TranslationPolicyView">;
const instruction = "翻译成中文，保留公式和术语。";
const metadata = new Map<
  string,
  { taskId: string; courseId: string; source: Source; settings: string; policyId: string }
>();
const byTask = new Map<string, string>();
const verified = new Map<string, { hash: string; text: string }>();
let currentPolicy = "";

function rangeIdentity(source: Source) {
  const reference = structuredClone(source.reference) as Record<string, unknown>;
  if (reference.kind === "document" || reference.kind === "document_selection") {
    delete reference.regions;
    if (Array.isArray(reference.parts))
      reference.parts = reference.parts.map((part) => {
        const value = { ...part };
        delete value.regions;
        return value;
      });
  }
  const canonical = (value: unknown): unknown =>
    Array.isArray(value)
      ? value.map(canonical)
      : value && typeof value === "object"
        ? Object.fromEntries(
            Object.entries(value)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([key, item]) => [key, canonical(item)]),
          )
        : value;
  return JSON.stringify(
    canonical({
      course: source.course_id,
      role: source.content_role,
      text: source.text,
      reference,
    }),
  );
}

export function clearTranslationMemo() {
  clearCache();
  metadata.clear();
  byTask.clear();
  verified.clear();
  currentPolicy = "";
}
if (typeof window !== "undefined")
  window.addEventListener("model_assignments_changed", clearTranslationMemo);

async function keyFor(source: Source, policy: Policy, textInstruction = instruction) {
  if (!policy.policy_id) return null;
  const partition = JSON.stringify({ policy: policy.policy_id, settings: policy.settings });
  if (currentPolicy && currentPolicy !== partition) clearTranslationMemo();
  currentPolicy = partition;
  const key = await buildCacheKey({
    mode: "streaming",
    modelId: policy.settings.model,
    srcLang: "auto",
    tgtLang: "zh",
    source: source.text,
    contextAfter: "",
    contextBefore: JSON.stringify({
      course: source.course_id,
      chat: policy.chat_id,
      exact_text_range: rangeIdentity(source),
      settings: policy.settings,
      policy: policy.policy_id,
      instruction: textInstruction,
    }),
  });
  // The original exposes a weak fallback; it cannot identify a project result.
  return /^[a-f0-9]{64}$/.test(key.split("|").at(-1) ?? "") ? key : null;
}

async function refreshEntry(key: string) {
  const known = metadata.get(key);
  if (!known) return null;
  const entry = await api.read<Schema<"TranslationCacheEntry">>(
    `/courses/${known.courseId}/learning/tasks/${known.taskId}/translation-cache-entry`,
  );
  if (entry.status === "unknown")
    throw new ApiError("TASK_RECONCILIATION_REQUIRED", "已有相同翻译的结果待核对，未重新发送", 409);
  if (entry.status === "pending") return known.taskId;
  if (
    entry.status === "eligible" &&
    entry.text &&
    entry.source &&
    rangeIdentity(entry.source) === rangeIdentity(known.source) &&
    entry.policy_id === known.policyId &&
    JSON.stringify(entry.settings) === known.settings &&
    entry.instruction === instruction
  ) {
    writeCache(key, { mode: "streaming", text: entry.text });
    return known.taskId;
  }
  metadata.delete(key);
  byTask.delete(known.taskId);
  return null;
}

export async function rememberCompletedTranslation(task: Task) {
  const key = byTask.get(task.task_id);
  if (key && task.state === "completed") await refreshEntry(key).catch(() => null);
}

async function sha(text: string) {
  if (!globalThis.crypto?.subtle) return null;
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(bytes)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function verifiedTranslation(task: Task) {
  const info = task.translation_reuse;
  if (!info || info.kind !== "saved_result") return null;
  const entry = verified.get(info.operation_id);
  return entry && entry.hash === info.text_sha256 ? entry.text : null;
}

export async function translatePdfSelection(
  sessionId: string,
  courseId: string,
  chatId: string,
  action: ReadingAction,
): Promise<Task> {
  let source: Source,
    policy: Policy,
    key: string | null,
    candidate: string | null = null;
  try {
    source = await api.write<Source>(
      `/courses/${action.file.course_id}/learning/selections/pdf`,
      action.selection,
    );
    policy = await api.read<Policy>(
      `/desktop/sessions/${encodeURIComponent(sessionId)}/translation-policy`,
    );
    if (
      source.course_id !== action.file.course_id ||
      policy.course_id !== courseId ||
      policy.chat_id !== chatId
    )
      throw new ApiError("TASK_CHAT_SCOPE_DENIED", "翻译归属与所选资料不符", 409);
    key = await keyFor(source, policy);
    if (key && !action.force_regenerate) {
      const known = metadata.get(key),
        cached = readCache(key);
      candidate = known && cached ? known.taskId : await refreshEntry(key);
    }
  } catch (error) {
    if (error instanceof ApiError && error.status) throw error;
    throw new ApiError("TRANSLATION_PREPARATION_FAILED", "翻译尚未提交，请保留选区后重试", 422);
  }
  const result = await api.write<Task>(
    `/desktop/sessions/${encodeURIComponent(sessionId)}/learning-tasks`,
    {
      operation_id: action.operation_id,
      kind: "translation",
      instruction,
      source_snapshot_ids: [source.snapshot_id],
      ...(candidate ? { cached_task_id: candidate } : {}),
    },
  );
  if (JSON.stringify(result.settings) !== JSON.stringify(policy.settings)) key = null;
  if (key) {
    const previous = metadata.get(key);
    if (previous && previous.taskId !== result.task_id) byTask.delete(previous.taskId);
    metadata.delete(key);
    metadata.set(key, {
      taskId: result.task_id,
      courseId,
      source,
      settings: JSON.stringify(policy.settings),
      policyId: policy.policy_id!,
    });
    byTask.set(result.task_id, key);
    if (metadata.size > 100) {
      const oldest = metadata.keys().next().value;
      if (oldest) {
        byTask.delete(metadata.get(oldest)!.taskId);
        metadata.delete(oldest);
      }
    }
    const cached = readCache(key),
      info = result.translation_reuse;
    if (
      cached?.mode === "streaming" &&
      info?.kind === "saved_result" &&
      (await sha(cached.text)) === info.text_sha256
    ) {
      verified.set(info.operation_id, { hash: info.text_sha256!, text: cached.text });
      if (verified.size > 100) verified.delete(verified.keys().next().value!);
    }
  }
  return result;
}
