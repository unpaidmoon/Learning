import type { Schema } from "../api/client";

export const terminalStates = new Set([
  "completed",
  "cancelled",
  "failed",
  "interrupted",
  "truncated",
]);

export function generationActive(generation: Schema<"GenerationResponse">): boolean {
  return generation.state !== "unknown" && !terminalStates.has(generation.state);
}

export function stateLabel(state: string): string {
  return (
    {
      accepted: "已接受，等待执行",
      pending: "等待执行",
      dispatching: "正在派发",
      running: "生成中",
      active: "生成中",
      streaming: "生成中",
      cancelling: "正在停止",
      completed: "已完成",
      cancelled: "已停止，保留部分内容",
      failed: "生成失败",
      interrupted: "执行已中断",
      truncated: "输出已截断，可继续追问",
      unknown: "执行结果未知，需核对",
    }[state] || state
  );
}
