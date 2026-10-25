import type { Schema } from "../api/client";
import { errorMessage } from "../api/client";
import { readWorkbenchDraft } from "./drafts";

export type Artifact = Schema<"ArtifactView">;
export type Task = Schema<"TaskView">;
export type Intent = Schema<"TaskIntent">;
export const active = new Set(["queued", "running", "unknown", "needs_sources"]);
export const states: Record<string, string> = {
  queued: "等待处理",
  running: "正在处理",
  paused: "已暂停",
  needs_sources: "等待处理来源缺口",
  unknown: "需要对账",
  completed: "已完成",
  partial: "已接受部分成果",
  failed: "处理失败",
  cancelled: "已取消",
  awaiting_outline: "等待确认目录",
};
export const roles: Record<string, string> = {
  original: "原始资料",
  user_note: "用户笔记",
  ai_generated: "AI生成",
  mixed: "混合修订",
};
export const operationId = () => "workbench_" + crypto.randomUUID().replaceAll("-", "");
export type PickedSource = {
  key: string;
  title: string;
  declaration: NonNullable<Intent["sources"]>[number];
};
export type ComposerDraft = {
  file?: Schema<"CourseFileOpenResponse">;
  media?: Schema<"MediaAssetResponse">;
  scope: string;
  kind:
    | "note"
    | "aligned_translation"
    | "handout"
    | "deep_solution"
    | "question_set"
    | "card_set"
    | "learning_path"
    | "visualization"
    | "book"
    | "research";
  title: string;
  instruction: string;
  sourcesOnly: boolean;
  outlineText?: string;
  includeGenerated?: boolean;
  sourceCourseId?: string;
  pickedSources?: PickedSource[];
  researchTopics?: string;
  researchUrls?: string;
  allowExternal?: boolean;
  pending: string | null;
};
export type ArtifactDraft = {
  base_revision_id: string;
  changes: Record<string, string>;
  blocks?: Record<string, Artifact["revision"]["body"]["blocks"][number]>;
  pending: string | null;
};
export function localDraft<T>(key: string): { value: T | null; error: string } {
  try {
    return { value: readWorkbenchDraft<T>(key), error: "" };
  } catch (reason) {
    return { value: null, error: errorMessage(reason) };
  }
}
