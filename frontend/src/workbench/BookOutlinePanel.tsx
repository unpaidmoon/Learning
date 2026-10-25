import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { finishWorkbenchDraft, saveWorkbenchDraft } from "./drafts";
import { localDraft, operationId } from "./model";
import { useWorkbenchOperation } from "./useWorkbenchOperation";

type Proposal = NonNullable<Schema<"BookOutlineView">["proposal"]>;

export function BookOutlinePanel({
  courseId,
  task,
  onConfirmed,
}: {
  courseId: string;
  task: Schema<"TaskView">;
  onConfirmed: (task: Schema<"TaskView">) => void;
}) {
  const key = `${courseId}.book.outline.${task.task_id}`;
  const [saved] = useState(() => localDraft<{ hash: string; proposal: Proposal }>(key));
  const [view, setView] = useState<Schema<"BookOutlineView"> | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(saved.value?.proposal ?? null);
  const [notice, setNotice] = useState(saved.error);
  const dirty = useRef(!!saved.value);
  const draftHash = useRef(saved.value?.hash);
  const path = `/courses/${courseId}/learning/tasks/${task.task_id}/book-outline`;
  const operation = useWorkbenchOperation<Schema<"TaskView">>(
    key + ".confirm",
    courseId,
    "task",
    (value) => {
      finishWorkbenchDraft(key);
      dirty.current = false;
      onConfirmed(value);
    },
  );
  useEffect(() => {
    let live = true;
    void api
      .read<Schema<"BookOutlineView">>(path)
      .then((value) => {
        if (!live) return;
        setView(value);
        if (!dirty.current) {
          setProposal(value.proposal);
          draftHash.current = value.proposal_hash ?? undefined;
        } else if (draftHash.current !== value.proposal_hash)
          setNotice("目录提案已有变化，本地修改保留，请核对当前范围。");
      })
      .catch((reason) => {
        if (live) setNotice(errorMessage(reason));
      });
    return () => {
      live = false;
    };
  }, [path, task.revision, saved]);
  const change = (value: Proposal) => {
    dirty.current = true;
    setProposal(value);
    try {
      if (view?.proposal_hash)
        saveWorkbenchDraft(key, { hash: view.proposal_hash, proposal: value });
    } catch (reason) {
      setNotice(errorMessage(reason));
    }
  };
  const update = (index: number, fields: Partial<Proposal["chapters"][number]>) => {
    if (proposal)
      change({
        ...proposal,
        chapters: proposal.chapters.map((chapter, i) =>
          i === index ? { ...chapter, ...fields } : chapter,
        ),
      });
  };
  if (!view?.proposal)
    return (
      <>
        <p>
          {notice ||
            (view?.supplied
              ? "按已有目录编排章节来源，随后连续生成。"
              : "正在拟定目录，正文将在目录确认后生成。")}
        </p>
        {operation.pending && (
          <Button onClick={() => void operation.reconcile()}>核对原目录确认</Button>
        )}
      </>
    );
  if (view.confirmed)
    return (
      <details open={!!operation.pending}>
        <summary>已确认目录 · 后续章节连续生成</summary>
        <ol>
          {view.proposal.chapters.map((c) => (
            <li key={c.chapter_id}>{c.title}</li>
          ))}
        </ol>
        {operation.pending && (
          <Button onClick={() => void operation.reconcile()}>核对原目录确认</Button>
        )}
        {operation.notice && <p role="status">{operation.notice}</p>}
      </details>
    );
  return (
    <section className="workbench-composer" aria-label="交互书目录确认">
      <h3>确认一次目录，随后连续生成</h3>
      <p>目录建议依据指定资料清单；正文会逐项读取范围，已完成章节可先读。</p>
      {view.proposal.rationale && <p>{view.proposal.rationale}</p>}
      {proposal?.chapters.map((chapter, index) => (
        <fieldset key={chapter.chapter_id} disabled={operation.disabled}>
          <legend>第 {index + 1} 章</legend>
          <label>
            章节标题
            <input
              value={chapter.title}
              onChange={(e) => update(index, { title: e.target.value })}
            />
          </label>
          <label>
            章节目标
            <textarea
              value={chapter.objective ?? ""}
              onChange={(e) => update(index, { objective: e.target.value })}
            />
          </label>
          <details>
            <summary>章节来源 · {(chapter.source_ids ?? []).length || "AI补充"}</summary>
            {view.sources.map((source) => (
              <label key={source.source_id}>
                <input
                  type="checkbox"
                  checked={(chapter.source_ids ?? []).includes(source.source_id)}
                  onChange={(e) =>
                    update(index, {
                      source_ids: e.target.checked
                        ? [...(chapter.source_ids ?? []), source.source_id]
                        : (chapter.source_ids ?? []).filter((id) => id !== source.source_id),
                    })
                  }
                />
                {source.label}
              </label>
            ))}
            {(chapter.source_ids ?? [])
              .filter((id) => !view.sources.some((s) => s.source_id === id))
              .map((id) => (
                <label key={id}>
                  <input
                    type="checkbox"
                    checked
                    onChange={() =>
                      update(index, {
                        source_ids: (chapter.source_ids ?? []).filter((s) => s !== id),
                      })
                    }
                  />
                  此来源已不在当前范围，取消本章关联
                </label>
              ))}
          </details>
          <div className="workbench-toolbar">
            {index > 0 && (
              <Button
                variant="ghost"
                onClick={() => {
                  if (!proposal) return;
                  const chapters = [...proposal.chapters];
                  [chapters[index - 1], chapters[index]] = [chapters[index], chapters[index - 1]];
                  change({ ...proposal, chapters });
                }}
              >
                上移章节
              </Button>
            )}
            {(proposal?.chapters.length ?? 0) > 1 && (
              <Button
                variant="ghost"
                onClick={() =>
                  proposal &&
                  change({ ...proposal, chapters: proposal.chapters.filter((_, i) => i !== index) })
                }
              >
                删除此章
              </Button>
            )}
          </div>
        </fieldset>
      ))}
      <Button
        variant="ghost"
        disabled={operation.disabled}
        onClick={() =>
          proposal &&
          change({
            ...proposal,
            chapters: [
              ...proposal.chapters,
              {
                chapter_id: "chapter_" + operationId(),
                title: "新章节",
                objective: "",
                source_ids: [],
              },
            ],
          })
        }
      >
        新增章节
      </Button>
      <Button
        disabled={operation.disabled || !proposal || !view.proposal_hash || !!saved.error}
        onClick={() =>
          proposal &&
          view.proposal_hash &&
          void operation.execute(path, {
            expected_revision: view.task_revision,
            proposal_hash: view.proposal_hash,
            outline: proposal,
          })
        }
      >
        {task.state === "paused" ? "确认目录，保持暂停" : "确认目录并连续生成"}
      </Button>
      {operation.pending && (
        <Button onClick={() => void operation.reconcile()}>核对原目录确认</Button>
      )}
      {(notice || operation.notice) && <p role="status">{notice || operation.notice}</p>}
    </section>
  );
}
