import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { ContentBlock } from "./ContentBlock";
import { QuestionPlayer } from "./QuestionPlayer";
import { localDraft, roles } from "./model";
import { finishWorkbenchDraft, saveWorkbenchDraft } from "./drafts";
import { useWorkbenchOperation } from "./useWorkbenchOperation";

const states: Record<string, string> = {
  ready: "可阅读",
  generating: "继续生成中",
  paused: "已暂停",
  unknown: "待核对原执行",
  needs_sources: "等待补齐来源",
  content_removed: "内容已修改或移出",
  excluded: "来源已排除",
  partial: "部分内容保留",
};
type Bundle = { book: Schema<"BookView">; revision: Schema<"ArtifactRevision"> };
type DirectoryDraft = {
  base_revision_id: string;
  chapters: Schema<"BookDirectoryUpdate">["chapters"];
};

export function BookReader({
  courseId,
  artifactId,
  fixedRevision,
  refreshVersion,
  sessionId,
  onOpenSource,
  onEdit,
  onArtifactChanged,
  onTaskCreated,
  onUnstructured,
}: {
  courseId: string;
  artifactId: string;
  fixedRevision?: string;
  refreshVersion?: string;
  sessionId?: string;
  onOpenSource: (source: Schema<"SourceSnapshot">) => void;
  onEdit?: (revision: Schema<"ArtifactRevision">, ids: string[]) => void;
  onArtifactChanged?: (receipt: Schema<"ArtifactReceipt">) => void;
  onTaskCreated?: (task: Schema<"TaskView">) => void;
  onUnstructured?: () => void;
}) {
  const key = `${courseId}.book.reading.${artifactId}`;
  const [saved] = useState(() => localDraft<{ chapter_id?: string; page_id?: string }>(key));
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [chapterId, setChapterId] = useState(saved.value?.chapter_id);
  const [pageId, setPageId] = useState(saved.value?.page_id);
  const [notice, setNotice] = useState(saved.error);
  const directoryKey = key + ".directory.draft";
  const [savedDirectory] = useState(() =>
    fixedRevision ? { value: null, error: "" } : localDraft<DirectoryDraft>(directoryKey),
  );
  const [editingDirectory, setEditingDirectory] = useState(!!savedDirectory.value);
  const [directoryBase, setDirectoryBase] = useState(savedDirectory.value?.base_revision_id);
  const [labels, setLabels] = useState<Schema<"BookDirectoryUpdate">["chapters"]>(
    savedDirectory.value?.chapters ?? [],
  );
  const epoch = useRef(0);
  const invalidate = useCallback(() => {
    ++epoch.current;
  }, []);
  const current = useRef<Bundle | null>(null);
  const path = `/courses/${courseId}/learning/artifacts/${artifactId}`;
  const load = useCallback(async () => {
    const request = ++epoch.current;
    const book = await api.read<Schema<"BookView">>(
      path + "/book" + (fixedRevision ? "?revision_id=" + fixedRevision : ""),
    );
    const revision = await api.read<Schema<"ArtifactRevision">>(
      path + "/versions/" + book.revision_id,
    );
    if (request !== epoch.current) return;
    const next = { book, revision };
    current.current = next;
    setBundle(next);
  }, [path, fixedRevision]);
  useEffect(() => {
    let live = true;
    const read = () =>
      void load().catch((reason) => {
        if (!live) return;
        if (reason instanceof ApiError && reason.code === "BOOK_LAYOUT_NOT_FOUND")
          onUnstructured?.();
        else setNotice(errorMessage(reason));
      });
    read();
    const timer = setInterval(() => {
      if (!fixedRevision && ["queued", "running"].includes(current.current?.book.task_state ?? ""))
        read();
    }, 2500);
    return () => {
      live = false;
      invalidate();
      clearInterval(timer);
    };
  }, [load, fixedRevision, refreshVersion, onUnstructured, invalidate]);
  const changeDirectory = useWorkbenchOperation<Schema<"ArtifactReceipt">>(
    key + ".directory",
    courseId,
    "artifact",
    (receipt) => {
      if (receipt.outcome === "applied") {
        finishWorkbenchDraft(directoryKey);
        setEditingDirectory(false);
        onArtifactChanged?.(receipt);
        void load().catch((e) => setNotice(errorMessage(e)));
      } else setNotice("目录同时发生变化，本次候选与当前版本均已保留。请在版本记录中核对。");
    },
  );
  const updateLabels = (next: typeof labels) => {
    setLabels(next);
    try {
      if (savedDirectory.error)
        throw new ApiError("WORKBENCH_DRAFT_INVALID", "原目录草稿保留，未覆盖");
      if (directoryBase)
        saveWorkbenchDraft(directoryKey, { base_revision_id: directoryBase, chapters: next });
    } catch (reason) {
      setNotice(errorMessage(reason));
    }
  };
  const chapter =
    bundle?.book.chapters.find((c) => c.chapter.chapter_id === chapterId) ??
    bundle?.book.chapters[0];
  const page = chapter?.pages.find((p) => p.layout.page_id === pageId) ?? chapter?.pages[0];
  const select = (nextChapter: string, nextPage?: string) => {
    setChapterId(nextChapter);
    setPageId(nextPage);
    if (!fixedRevision) {
      try {
        saveWorkbenchDraft(key, { chapter_id: nextChapter, page_id: nextPage });
      } catch (reason) {
        setNotice(errorMessage(reason));
      }
    }
  };
  const source = (id: string) =>
    void api
      .read<Schema<"SourceSnapshot">>(`/courses/${courseId}/learning/selections/${id}`)
      .then(onOpenSource)
      .catch((e) => setNotice(errorMessage(e)));
  return (
    <section aria-label="交互书阅读" className="book-reader">
      {bundle && (
        <>
          <div className="workbench-toolbar">
            <span>
              {bundle.book.is_historical
                ? "历史版本"
                : bundle.book.book_status === "ready"
                  ? "本书生成完成"
                  : "已保存内容可先阅读"}
            </span>
            <Button
              variant="ghost"
              onClick={() => void load().catch((e) => setNotice(errorMessage(e)))}
            >
              刷新章节
            </Button>
            {!fixedRevision && (
              <Button
                variant="ghost"
                onClick={() => {
                  if (editingDirectory) {
                    setEditingDirectory(false);
                    return;
                  }
                  const prior = localDraft<DirectoryDraft>(directoryKey);
                  if (prior.error) {
                    setNotice(prior.error);
                    return;
                  }
                  setDirectoryBase(prior.value?.base_revision_id ?? bundle.book.revision_id);
                  setLabels(
                    prior.value?.chapters ??
                      bundle.book.chapters.map((c) => ({
                        chapter_id: c.chapter.chapter_id,
                        title: c.chapter.title,
                        objective: c.chapter.objective ?? "",
                      })),
                  );
                  setEditingDirectory(true);
                }}
              >
                调整目录
              </Button>
            )}
          </div>
          {changeDirectory.pending && !editingDirectory && (
            <Button
              onClick={() => {
                setEditingDirectory(true);
                void changeDirectory.reconcile();
              }}
            >
              核对原目录调整
            </Button>
          )}
          {editingDirectory && (
            <section aria-label="调整书目录">
              {labels.map((label, index) => (
                <div key={label.chapter_id}>
                  <label>
                    章节标题
                    <input
                      disabled={changeDirectory.disabled}
                      value={label.title}
                      onChange={(e) =>
                        updateLabels(
                          labels.map((c, i) => (i === index ? { ...c, title: e.target.value } : c)),
                        )
                      }
                    />
                  </label>
                  {index > 0 && (
                    <Button
                      variant="ghost"
                      disabled={changeDirectory.disabled}
                      onClick={() => {
                        const next = [...labels];
                        [next[index - 1], next[index]] = [next[index], next[index - 1]];
                        updateLabels(next);
                      }}
                    >
                      上移章节
                    </Button>
                  )}
                </div>
              ))}
              <Button
                disabled={changeDirectory.disabled || !directoryBase}
                onClick={() =>
                  void changeDirectory.execute(path + "/book-directory", {
                    expected_revision_id: directoryBase,
                    chapters: labels,
                  })
                }
              >
                保存目录调整
              </Button>
              {changeDirectory.pending && (
                <Button onClick={() => void changeDirectory.reconcile()}>核对原目录调整</Button>
              )}
            </section>
          )}
          <div className="workbench-columns">
            <nav aria-label="书目录">
              {bundle.book.chapters.map((item) => (
                <div key={item.chapter.chapter_id}>
                  <button
                    aria-pressed={chapter?.chapter.chapter_id === item.chapter.chapter_id}
                    onClick={() => select(item.chapter.chapter_id)}
                  >
                    <strong>{item.chapter.title}</strong>
                    <small>{states[item.state] ?? item.state}</small>
                  </button>
                  {chapter?.chapter.chapter_id === item.chapter.chapter_id &&
                    item.pages.map((p, index) => (
                      <button
                        key={p.layout.page_id}
                        aria-pressed={page?.layout.page_id === p.layout.page_id}
                        onClick={() => select(item.chapter.chapter_id, p.layout.page_id)}
                      >
                        第 {index + 1} 页
                      </button>
                    ))}
                </div>
              ))}
            </nav>
            <article aria-label="书页内容">
              {chapter && <h3>{chapter.chapter.title}</h3>}
              {!page && (
                <p>
                  {chapter?.state === "needs_sources"
                    ? "本章来源尚待补齐；已保存的其他章节仍可阅读。"
                    : "本章内容尚未生成，可以先读其他已保存页面。"}
                </p>
              )}
              {page && (
                <>
                  {page.state === "content_removed" && (
                    <p>此页部分内容已修改或移出，旧内容仍在版本记录中。</p>
                  )}
                  {!fixedRevision && onEdit && (
                    <Button
                      variant="ghost"
                      onClick={() => onEdit(bundle.revision, page.layout.block_ids)}
                    >
                      编辑此页
                    </Button>
                  )}
                  {page.layout.block_ids.map((id) => {
                    const block = bundle.revision.body.blocks.find((b) => b.block_id === id);
                    if (!block) return null;
                    const pin = page.layout.question_revisions?.[id];
                    return (
                      <section key={`${id}:${pin ?? "content"}`} className="workbench-block">
                        {block.kind === "question" && pin && !fixedRevision ? (
                          <QuestionPlayer
                            courseId={courseId}
                            artifactId={artifactId}
                            revisionId={pin}
                            block={block}
                            sessionId={sessionId}
                            onTaskCreated={onTaskCreated}
                          />
                        ) : (
                          <ContentBlock block={block} />
                        )}
                        <small>
                          {block.attribution === "ai_supplement"
                            ? "AI补充"
                            : block.attribution === "source_inference"
                              ? "依据资料的推断"
                              : block.attribution === "user_authored"
                                ? "用户内容"
                                : block.attribution === "source_quote"
                                  ? "原文引述"
                                  : block.kind === "question"
                                    ? "资料依据 · 非原题引述"
                                    : "来源归纳"}{" "}
                          · {roles[bundle.revision.content_role]}
                        </small>
                        {(block.source_snapshot_ids ?? []).map((identity, index) => (
                          <Button variant="ghost" key={identity} onClick={() => source(identity)}>
                            来源 {index + 1}
                          </Button>
                        ))}
                      </section>
                    );
                  })}
                </>
              )}
            </article>
          </div>
        </>
      )}
      {(notice || changeDirectory.notice) && (
        <p role="status">{notice || changeDirectory.notice}</p>
      )}
    </section>
  );
}
