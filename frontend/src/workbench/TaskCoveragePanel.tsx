import { useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import type { Task } from "./model";

export function TaskCoveragePanel({
  courseId,
  task,
  disabled,
  onDecide,
}: {
  courseId: string;
  task: Task;
  disabled: boolean;
  onDecide: (
    decision: "retry" | "exclude" | "accept_partial",
    sourceIds: string[],
    leafIds: string[],
  ) => Promise<void>;
}) {
  const [coverage, setCoverage] = useState<Schema<"TaskCoverageView"> | null>(null);
  const [error, setError] = useState("");
  const [sources, setSources] = useState<string[]>([]);
  const [leaves, setLeaves] = useState<string[]>([]);
  const [shown, setShown] = useState(100);
  useEffect(() => {
    let live = true;
    void api
      .read<Schema<"TaskCoverageView">>(
        `/courses/${courseId}/learning/tasks/${task.task_id}/coverage`,
      )
      .then((value) => {
        if (live) {
          setCoverage(value);
          setSources([]);
          setLeaves([]);
        }
      })
      .catch((reason) => {
        if (live) setError(errorMessage(reason));
      });
    return () => {
      live = false;
    };
  }, [courseId, task.task_id, task.revision]);
  if (task.layout_version !== 2) return null;
  const gaps =
    coverage?.leaves.filter(
      (leaf) => leaf.error_code && leaf.excluded_at_scope_revision === null,
    ) ?? [];
  const locked = disabled || !["needs_sources", "failed"].includes(task.state);
  const selected = sources.length + leaves.length > 0;
  const toggle = (old: string[], id: string) =>
    old.includes(id) ? old.filter((value) => value !== id) : [...old, id];
  return (
    <section className="workbench-coverage" aria-label="资料覆盖与缺口">
      <h3>资料范围与缺口</h3>
      {error && <p role="alert">{error}</p>}
      {coverage?.sources.map((source) => (
        <label key={source.source_id}>
          <input
            type="checkbox"
            disabled={locked || source.excluded_at_scope_revision !== null}
            checked={sources.includes(source.source_id)}
            onChange={() => setSources((old) => toggle(old, source.source_id))}
          />
          {source.declaration.kind === "course" ? (
            "本课程成员范围已冻结"
          ) : (
            <>
              {source.label || `资料 ${source.ordinal + 1}`} · 已处理 {source.processed_leaf_count}{" "}
              / {source.expected_leaf_count ?? "范围待确认"}
            </>
          )}
          {source.excluded_at_scope_revision !== null
            ? " · 已明确排除"
            : source.error_code
              ? ` · ${source.error_code}`
              : ""}
        </label>
      ))}
      {gaps.slice(0, shown).map((leaf) => (
        <label key={leaf.leaf_id}>
          <input
            type="checkbox"
            disabled={locked}
            checked={leaves.includes(leaf.leaf_id)}
            onChange={() => setLeaves((old) => toggle(old, leaf.leaf_id))}
          />
          {leaf.label} · {leaf.error_code}
        </label>
      ))}
      {gaps.length > shown && (
        <Button variant="ghost" onClick={() => setShown((old) => old + 100)}>
          显示更多缺口
        </Button>
      )}
      <div className="workbench-toolbar">
        <Button
          disabled={locked || !selected}
          onClick={() => void onDecide("retry", sources, leaves)}
        >
          重试选中来源
        </Button>
        <Button
          disabled={locked || !selected}
          onClick={() => void onDecide("exclude", sources, leaves)}
        >
          明确排除所选范围
        </Button>
        <Button
          variant="outline"
          disabled={locked || !coverage?.target.materialized}
          onClick={() => void onDecide("accept_partial", [], [])}
        >
          接受当前部分成果并结束
        </Button>
      </div>
      {task.state === "unknown" && <p>原执行仍需对账，暂不改变范围或重新生成。</p>}
    </section>
  );
}
