import { ApiError } from "../api/client";

const prefix = "learning.workbench.v1.";

/** Local unsaved input and receipt identity; never a second artifact store. */
export function readWorkbenchDraft<T>(key: string): T | null {
  const raw = localStorage.getItem(prefix + key);
  if (raw === null) return null;
  try {
    const envelope = JSON.parse(raw) as { version?: unknown; value?: T };
    if (envelope.version !== 1 || envelope.value === undefined) throw new Error();
    return envelope.value;
  } catch {
    throw new ApiError("WORKBENCH_DRAFT_INVALID", "本地草稿无法读取，原记录仍保留");
  }
}

export function saveWorkbenchDraft(key: string, value: unknown): void {
  try {
    localStorage.setItem(prefix + key, JSON.stringify({ version: 1, value }));
  } catch {
    throw new ApiError(
      "WORKBENCH_DRAFT_UNAVAILABLE",
      "本地草稿未能保存，请保留当前页面并复制未保存内容",
    );
  }
}

export function finishWorkbenchDraft(key: string): void {
  try {
    localStorage.removeItem(prefix + key);
  } catch {
    throw new ApiError("WORKBENCH_DRAFT_UNAVAILABLE", "原操作已保留，可稍后核对已保存回执");
  }
}
