import { useCallback, useEffect, useState } from "react";
import { api, ApiError, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { BookOutlinePanel } from "./BookOutlinePanel";
import { finishWorkbenchDraft, saveWorkbenchDraft } from "./drafts";
import { active, localDraft, operationId, states } from "./model";
import type { Task } from "./model";
import { TaskCoveragePanel } from "./TaskCoveragePanel";
import { ResearchPanel } from "./ResearchPanel";
import { AlignedTranslationDraft } from "./AlignedTranslationDraft";

export function TaskDetail({
  courseId,
  taskId,
  indexRevision,
  onOpen,
}: {
  courseId: string;
  taskId: string;
  indexRevision?: number;
  onOpen: (id: string) => void;
}) {
  const key = courseId + ".control." + taskId;
  const [saved] = useState(() => localDraft<{ operation_id: string }>(key));
  const [task, setTask] = useState<Task | null>(null);
  const [notice, setNotice] = useState(saved.error);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<string | null>(saved.value?.operation_id ?? null);
  const [shown, setShown] = useState(100);
  const [draftRun, setDraftRun] = useState<string | null>(null);
  const path = `/courses/${courseId}/learning/tasks/${taskId}`;
  const refresh = useCallback(() => api.read<Task>(path).then(setTask), [path]);
  useEffect(() => {
    void refresh().catch((reason) => setNotice(errorMessage(reason)));
  }, [refresh, indexRevision]);
  const mutate = async (route: string, fields: Record<string, unknown> = {}) => {
    if (!task || busy || pending) return;
    const operation = operationId();
    try {
      if (saved.error) throw new ApiError("WORKBENCH_DRAFT_INVALID", "原操作记录保留，请先核对");
      saveWorkbenchDraft(key, { operation_id: operation });
    } catch (reason) {
      setNotice(errorMessage(reason));
      return;
    }
    setPending(operation);
    setBusy(true);
    try {
      setTask(
        await api.write<Task>(path + route, {
          operation_id: operation,
          expected_revision: task.revision,
          ...fields,
        }),
      );
      finishWorkbenchDraft(key);
      setPending(null);
    } catch (reason) {
      if (
        reason instanceof ApiError &&
        reason.status &&
        reason.status >= 400 &&
        reason.status < 500
      ) {
        finishWorkbenchDraft(key);
        setPending(null);
      }
      setNotice(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  const control = (action: "pause" | "resume" | "cancel") => mutate("/control/" + action);
  const reconcileControl = async () => {
    if (!pending || busy) return;
    setBusy(true);
    try {
      const receipt = await api.read<Schema<"LearningOperationView">>(
        `/courses/${courseId}/learning/operations/${pending}`,
      );
      if (!("task_id" in receipt.result) || receipt.result.task_id !== taskId)
        throw new Error("TASK_RECEIPT_MISMATCH");
      setTask(receipt.result);
      finishWorkbenchDraft(key);
      setPending(null);
      await refresh();
    } catch (reason) {
      setNotice(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  return (
    <article>
      {notice && <p role="status">{notice}</p>}
      {task && (
        <>
          <h2>{states[task.state]}</h2>
          <p>{task.instruction}</p>
          <p>
            {task.settings.model} · {task.settings.thinking}
          </p>
          {task.kind === "book" && task.layout_version === 2 && (
            <BookOutlinePanel courseId={courseId} task={task} onConfirmed={setTask} />
          )}
          {task.kind === "research" && <ResearchPanel courseId={courseId} task={task} />}
          <div className="workbench-toolbar">
            {pending && (
              <Button disabled={busy} onClick={() => void reconcileControl()}>
                核对原控制操作
              </Button>
            )}
            <Button
              variant="outline"
              onClick={() => void refresh().catch((reason) => setNotice(errorMessage(reason)))}
            >
              刷新进度
            </Button>
            {active.has(task.state) && task.state !== "unknown" && (
              <Button disabled={busy || !!pending} onClick={() => void control("pause")}>
                暂停
              </Button>
            )}
            {task.state === "paused" && (
              <Button disabled={busy || !!pending} onClick={() => void control("resume")}>
                继续
              </Button>
            )}
            {(active.has(task.state) || task.state === "paused") && (
              <Button disabled={busy || !!pending} onClick={() => void control("cancel")}>
                取消任务
              </Button>
            )}
            {task.state === "unknown" && (
              <Button
                disabled={busy}
                onClick={() => {
                  setBusy(true);
                  void api
                    .write<Task>(path + "/reconcile")
                    .then(setTask)
                    .catch((reason) => setNotice(errorMessage(reason)))
                    .finally(() => setBusy(false));
                }}
              >
                核对原执行
              </Button>
            )}
          </div>
          {task.layout_version === 2 && (
            <TaskCoveragePanel
              courseId={courseId}
              task={task}
              disabled={busy || !!pending}
              onDecide={(decision, sourceIds, leafIds) =>
                mutate("/source-gap", { decision, source_ids: sourceIds, leaf_ids: leafIds })
              }
            />
          )}
          {task.units.slice(0, shown).map((unit) => (
            <p key={unit.unit_id}>
              {unit.label} ·{" "}
              {unit.state === "completed"
                ? "已完成"
                : unit.state === "unknown"
                  ? "待对账"
                  : ({
                      pending: "未开始",
                      read: "材料已准备",
                      dispatching: "生成中",
                      missing: "来源缺失",
                      failed: "失败",
                    }[unit.state] ?? unit.state)}
              {unit.error_code && <span>（{unit.error_code}）</span>}
              {unit.state === "failed" && ["failed", "needs_sources"].includes(task.state) && (
                <Button
                  disabled={busy || !!pending}
                  onClick={() => void mutate("/work/" + unit.unit_id + "/retry")}
                >
                  {unit.error_code === "TASK_RESULT_LOCAL_RECOVERY_REQUIRED"
                    ? "重新处理原输出"
                    : "重试此项生成"}
                </Button>
              )}
              {unit.result_artifact_id && (
                <Button variant="ghost" onClick={() => onOpen(unit.result_artifact_id!)}>
                  阅读成果
                </Button>
              )}
              {task.kind === "aligned_translation" && unit.run_id && (
                <Button variant="ghost" onClick={() => setDraftRun(unit.run_id!)}>
                  查看本项译文草稿
                </Button>
              )}
            </p>
          ))}
          {draftRun && (
            <AlignedTranslationDraft
              key={draftRun}
              courseId={courseId}
              taskId={taskId}
              runId={draftRun}
              onClose={() => setDraftRun(null)}
            />
          )}
          {task.units.length > shown && (
            <Button variant="ghost" onClick={() => setShown((old) => old + 100)}>
              显示更多生成工作
            </Button>
          )}
        </>
      )}
    </article>
  );
}
