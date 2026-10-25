import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { MarkdownAnswer } from "../chat/MarkdownAnswer";
import { ContentBlock } from "../workbench/ContentBlock";
import type { ReadingActionHandler, SourceJump } from "./actions";
import { PdfReaderAdapter } from "./pdfAdapter";
import { ReaderSession } from "./readerSession";
import { rememberCompletedTranslation, verifiedTranslation } from "./translationMemo";
import type { PreparationOverview } from "./preparation";
import "./reading.css";

export function PdfReader({
  file,
  onSourceAction,
  sourceJump,
  visible = true,
}: {
  file: Schema<"CourseFileOpenResponse">;
  onSourceAction?: ReadingActionHandler;
  sourceJump?: SourceJump;
  visible?: boolean;
}) {
  const host = useRef<HTMLDivElement>(null);
  const adapterRef = useRef<PdfReaderAdapter | null>(null);
  const sessionRef = useRef<ReaderSession | null>(null);
  const visibility = useRef(visible);
  useLayoutEffect(() => {
    visibility.current = visible;
    sessionRef.current?.setVisible(visible);
  }, [visible]);
  const [preparation, setPreparation] = useState<PreparationOverview | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [selection, setSelection] = useState<Schema<"PdfSelectionInput"> | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [task, setTask] = useState<Schema<"TaskView"> | null>(null);
  const [lastTranslationSelection, setLastTranslationSelection] =
    useState<Schema<"PdfSelectionInput"> | null>(null);
  const [translation, setTranslation] = useState("");
  const [aligned, setAligned] = useState<Schema<"ArtifactView"> | null>(null);
  const operationKey = `learning.reader.operation.${file.course_id}.${file.asset_id}.${file.source_version}`;
  const [pendingOperation, setPendingOperation] = useState<string | null>(() =>
    localStorage.getItem(operationKey),
  );
  const active = useRef(true);
  useEffect(() => {
    if (!host.current) return;
    active.current = true;
    const session = new ReaderSession(host.current, file, {
      selection: setSelection,
      error: (reason) => setError(reason instanceof Error ? reason.message : errorMessage(reason)),
      notice: (reason) =>
        setNotice(reason instanceof Error ? reason.message : errorMessage(reason)),
      preparation: setPreparation,
    });
    sessionRef.current = session;
    adapterRef.current = session.adapter;
    session.setVisible(visibility.current);
    return () => {
      active.current = false;
      session.dispose();
      if (adapterRef.current === session.adapter) adapterRef.current = null;
      if (sessionRef.current === session) sessionRef.current = null;
    };
  }, [file, retry]);
  useEffect(() => {
    if (sourceJump) adapterRef.current?.jump(sourceJump);
  }, [sourceJump]);
  useEffect(() => {
    if (!task) return;
    let stopped = false;
    let timer = 0;
    const update = async () => {
      try {
        const reused = task.translation_reuse;
        if (reused?.kind === "saved_result" && reused.artifact_id && reused.revision_id) {
          let text = await verifiedTranslation(task);
          if (text === null) {
            const version = await api.read<Schema<"ArtifactRevision">>(
              `/courses/${file.course_id}/learning/artifacts/${reused.artifact_id}/versions/${reused.revision_id}`,
            );
            text = version.body.blocks.map((block) => block.markdown).join("\n\n");
          }
          if (!stopped) {
            setTranslation(text);
            setNotice("沿用已保存的翻译，本次没有新增模型请求。原执行用量保留在原任务中。");
          }
          return;
        }
        const current = await api.read<Schema<"TaskView">>(
          `/courses/${file.course_id}/learning/tasks/${task.task_id}`,
        );
        if (stopped) return;
        if (current.kind === "aligned_translation") {
          const id = current.units.find((unit) => unit.result_artifact_id)?.result_artifact_id;
          if (id) {
            const artifact = await api.read<Schema<"ArtifactView">>(
              `/courses/${file.course_id}/learning/artifacts/${id}`,
            );
            if (stopped) return;
            setAligned(artifact);
          }
          if (["completed", "partial"].includes(current.state)) {
            setNotice(
              current.state === "completed"
                ? "段落对照已保存到当前课程"
                : "已保存当前部分对照，未覆盖范围保留在任务中",
            );
            return;
          }
          if (id) setNotice("已完成的对照段落可先读，任务尚未全部完成。");
        } else if (["completed", "partial"].includes(current.state)) {
          const id = current.units.find((unit) => unit.result_artifact_id)?.result_artifact_id;
          if (id) {
            const artifact = await api.read<Schema<"ArtifactView">>(
              `/courses/${file.course_id}/learning/artifacts/${id}`,
            );
            if (stopped) return;
            setTranslation(
              artifact.revision.body.blocks.map((block) => block.markdown).join("\n\n"),
            );
          }
          await rememberCompletedTranslation(current);
          if (!stopped) setNotice("译文已保存到当前课程");
          return;
        }
        if (["failed", "cancelled", "unknown", "needs_sources", "paused"].includes(current.state)) {
          setNotice(
            current.state === "unknown"
              ? "翻译执行结果待对账，未重新发送"
              : current.state === "paused"
                ? "翻译已暂停，可在学习任务中明确恢复。"
                : "翻译已停止；可在学习任务中查看留稿与原因",
          );
          return;
        }
        timer = window.setTimeout(() => void update(), 1000);
      } catch (reason) {
        if (!stopped) setNotice(errorMessage(reason));
      }
    };
    void update();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [task, file.course_id]);
  const perform = async (
    intent: "quote" | "translation" | "aligned_translation",
    actualSelection = selection,
    forceRegenerate = false,
  ) => {
    if (!actualSelection || !onSourceAction || busy || (intent !== "quote" && pendingOperation))
      return;
    setBusy(true);
    adapterRef.current?.freezeSelection(true);
    setNotice("");
    const operationId = crypto.randomUUID();
    try {
      if (intent !== "quote") {
        if (intent === "translation") setLastTranslationSelection(actualSelection);
        // Save reconciliation identity before the first potentially mutating
        // request. A reload or missing HTTP reply must never make a new attempt.
        localStorage.setItem(operationKey, operationId);
        setPendingOperation(operationId);
      }
      const result = await onSourceAction({
        intent,
        selection: actualSelection,
        file,
        operation_id: operationId,
        force_regenerate: forceRegenerate,
      });
      if (!active.current) return;
      if ("task_id" in result) {
        localStorage.removeItem(operationKey);
        setPendingOperation(null);
        setTask(result);
        setTranslation("");
        setAligned(null);
        setNotice("正在翻译所选文字…");
      } else setNotice("所选文字已加入 AI 草稿");
      setSelection(null);
    } catch (reason) {
      const status = (reason as { status?: number })?.status;
      if (intent !== "quote" && status && status >= 400 && status < 500) {
        localStorage.removeItem(operationKey);
        if (active.current) setPendingOperation(null);
      }
      if (active.current)
        setNotice(reason instanceof Error ? reason.message : errorMessage(reason));
    } finally {
      adapterRef.current?.freezeSelection(false);
      if (active.current) setBusy(false);
    }
  };
  const controlPreparation = async (action: "pause" | "resume" | "retry") => {
    if (preparing) return;
    setPreparing(true);
    try {
      await sessionRef.current?.preparation.control(action);
    } catch (reason) {
      if (active.current) setNotice(errorMessage(reason));
    } finally {
      if (active.current) setPreparing(false);
    }
  };
  const reconcileOperation = async () => {
    if (!pendingOperation || busy) return;
    setBusy(true);
    try {
      const receipt = await api.read<Schema<"LearningOperationView">>(
        `/courses/${file.course_id}/learning/operations/${pendingOperation}`,
      );
      if (receipt.kind !== "task_create" || !("task_id" in receipt.result))
        throw new Error("回执类型不符，未重新发送（LEARNING_OPERATION_CONFLICT）");
      localStorage.removeItem(operationKey);
      setPendingOperation(null);
      setTask(receipt.result);
      setNotice("已找到原任务，正在核对进度");
    } catch (reason) {
      setNotice(`原提交尚未确认，请稍后核对；没有重新发送。${errorMessage(reason)}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="pdf-reader" data-source-version={file.source_version}>
      <div className="pdf-reader-host" ref={host} />
      {preparation && (
        <div className="pdf-preparation" aria-label="PDF文字识别进度">
          <span>
            文字层 {preparation.batch?.units_completed ?? preparation.current.units_completed}/
            {preparation.batch?.units_total ?? preparation.current.units_total}
            {preparation.batch?.state === "paused" ? " · 整份已暂停" : ""}
            {preparation.batch?.state === "completed" ? " · 已保存" : ""}
          </span>
          {preparation.batch && ["queued", "running"].includes(preparation.batch.state) && (
            <Button
              variant="outline"
              disabled={preparing}
              onClick={() => void controlPreparation("pause")}
            >
              暂停整份
            </Button>
          )}
          {preparation.batch && ["paused", "cancelled"].includes(preparation.batch.state) && (
            <Button
              variant="outline"
              disabled={preparing}
              onClick={() => void controlPreparation("resume")}
            >
              恢复整份
            </Button>
          )}
          {preparation.batch && ["failed", "needs_source"].includes(preparation.batch.state) && (
            <Button
              variant="outline"
              disabled={preparing}
              onClick={() => void controlPreparation("retry")}
            >
              重试失败页
            </Button>
          )}
          {preparation.limitation_code && <span>当前仅处理本页，整份超过处理上限</span>}
          {preparation.batch &&
            preparation.batch.units_failed + preparation.batch.units_missing > 0 && (
              <span>
                有 {preparation.batch.units_failed + preparation.batch.units_missing} 页待处理
              </span>
            )}
        </div>
      )}
      {onSourceAction && selection && (
        <div className="pdf-selection-actions" aria-label="PDF选区操作">
          <span>
            {selection.pages.length > 1 ? `跨 ${selection.pages.length} 页选区` : "已选择文字"}
          </span>
          <Button
            variant="outline"
            disabled={busy || !!pendingOperation}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => void perform("translation")}
          >
            翻译
          </Button>
          <Button
            variant="outline"
            disabled={busy || !!pendingOperation}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => void perform("aligned_translation")}
          >
            逐段对照
          </Button>
          <Button
            variant="outline"
            disabled={busy}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => void perform("quote")}
          >
            加入 AI
          </Button>
        </div>
      )}
      {(notice || translation || aligned || pendingOperation) && (
        <aside className="pdf-selection-result" aria-live="polite">
          {pendingOperation && (
            <p>
              翻译提交待核对。
              <Button variant="outline" disabled={busy} onClick={() => void reconcileOperation()}>
                核对原提交
              </Button>
            </p>
          )}
          {notice && <p>{notice}</p>}
          {translation && <MarkdownAnswer text={translation} />}
          {aligned?.revision.body.blocks.map((block) => (
            <ContentBlock key={block.block_id} block={block} />
          ))}
          {task?.kind === "aligned_translation" && (
            <Button
              variant="outline"
              onClick={() => {
                window.dispatchEvent(
                  new CustomEvent("learning-open-task", {
                    detail: { courseId: file.course_id, taskId: task.task_id },
                  }),
                );
              }}
            >
              查看对照任务
            </Button>
          )}
          {translation && lastTranslationSelection && onSourceAction && (
            <Button
              variant="outline"
              disabled={busy || !!pendingOperation}
              onClick={() => void perform("translation", lastTranslationSelection, true)}
            >
              重新生成译文
            </Button>
          )}
          <Button
            variant="outline"
            onClick={() => {
              setNotice("");
              setTranslation("");
              setAligned(null);
              setTask(null);
            }}
          >
            收起
          </Button>
        </aside>
      )}
      {error && (
        <div className="pdf-reader-error" role="alert">
          <p>{error}</p>
          <Button
            variant="outline"
            onClick={() => {
              setError("");
              setRetry((value) => value + 1);
            }}
          >
            重试阅读
          </Button>
        </div>
      )}
    </div>
  );
}
