import { useCallback, useEffect, useState } from "react";
import { api, ApiError, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { finishWorkbenchDraft, saveWorkbenchDraft } from "./drafts";
import { localDraft, operationId, roles } from "./model";
import type { Artifact, ArtifactDraft } from "./model";
import { ContentBlock } from "./ContentBlock";
import { SourcePreview } from "./SourcePreview";
import { QuestionPlayer } from "./QuestionPlayer";
import { StructuredBlockEditor } from "./StructuredBlockEditor";
import { EssayPanel, EssayFeedbackPanel } from "./EssayPanel";
import { LearningPathPanel } from "./LearningPathPanel";
import { BookReader } from "./BookReader";
import { useArtifactScope } from "./useArtifactScope";
import { getDraftController } from "../chat/draft";

export function ArtifactEditor({
  courseId,
  artifactId,
  onSaved,
  onHeadObserved,
  requestedRevision,
  onOpenSource,
  sessionId,
  onTaskCreated,
  onOpenArtifact,
  visible = true,
}: {
  courseId: string;
  artifactId: string;
  onSaved: () => void;
  onHeadObserved?: (artifact: Artifact) => void;
  requestedRevision?: string;
  onOpenSource?: (source: Schema<"SourceSnapshot">) => Promise<void>;
  sessionId?: string;
  onTaskCreated?: (task: Schema<"TaskView">) => void;
  onOpenArtifact?: (artifactId: string) => void;
  visible?: boolean;
}) {
  const key = courseId + ".artifact." + artifactId;
  const [saved] = useState(() => localDraft<ArtifactDraft>(key));
  const [value, setValue] = useState<Artifact | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>(saved.value?.changes ?? {});
  const [blocks, setBlocks] = useState<NonNullable<ArtifactDraft["blocks"]>>(
    saved.value?.blocks ?? {},
  );
  const [editing, setEditing] = useState(!!saved.value);
  const [editingIds, setEditingIds] = useState<string[] | null>(() =>
    saved.value
      ? [
          ...new Set([
            ...Object.keys(saved.value.changes),
            ...Object.keys(saved.value.blocks ?? {}),
          ]),
        ]
      : null,
  );
  const [structuredBook, setStructuredBook] = useState(true);
  const unstructuredBook = useCallback(() => setStructuredBook(false), []);
  const [pending, setPending] = useState<string | null>(saved.value?.pending ?? null);
  const [notice, setNotice] = useState(saved.error);
  const [busy, setBusy] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [versions, setVersions] = useState<Schema<"RevisionIndexItem">[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [historical, setHistorical] = useState<Schema<"ArtifactRevision"> | null>(null);
  const [source, setSource] = useState<Schema<"SourceSnapshot"> | null>(null);
  const scope = useArtifactScope(courseId, artifactId, value?.head_revision_id);
  const historicalScope = useArtifactScope(courseId, artifactId, historical?.revision_id);
  const path = `/courses/${courseId}/learning/artifacts/${artifactId}`;
  useEffect(() => {
    if (value && !editing && !pending && !historical && !requestedRevision) onHeadObserved?.(value);
  }, [value, editing, pending, historical, requestedRevision, onHeadObserved]);
  const quoteBlock = async (blockId: string) => {
    if (!value || !sessionId || editing || busy) return;
    try {
      const binding = await api.read<Schema<"DesktopSessionResponse">>(
        `/desktop/sessions/${sessionId}`,
      );
      if (binding.course_id !== courseId)
        throw new ApiError("SOURCE_COURSE_DENIED", "成果不属于此对话课程");
      const controller = getDraftController(binding.chat_id);
      if (!controller.snapshot().ready) throw new ApiError("DRAFT_NOT_READY", "原对话草稿尚未就绪");
      const source = await api.write<Schema<"SourceSnapshot">>(
        `/courses/${courseId}/learning/selections/artifact`,
        {
          artifact_id: artifactId,
          revision_id: value.head_revision_id,
          block_id: blockId,
        },
      );
      const identities = controller.snapshot().value.source_snapshot_ids ?? [];
      if (identities.length >= 100 && !identities.includes(source.snapshot_id))
        throw new ApiError("SOURCE_SELECTION_LIMIT", "当前引用已达上限");
      controller.update({ source_snapshot_ids: [...new Set([...identities, source.snapshot_id])] });
      await controller.flush();
      setNotice("此内容块已加入原对话草稿。可继续提问，或明确要求修改这段；发送后才会执行。");
    } catch (reason) {
      setNotice(errorMessage(reason));
    }
  };
  const exportOffline = async (revisionId: string) => {
    if (exporting) return;
    setExporting(true);
    try {
      const blob = await api.download(path + "/offline-export", { revision_id: revisionId });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "学习成果-离线包.zip";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
      setNotice("已导出。解压整个文件夹后双击index.html，可断网阅读与自测。");
    } catch (reason) {
      setNotice(errorMessage(reason));
    } finally {
      setExporting(false);
    }
  };
  const beginEdit = async () => {
    if (!value || busy || pending) return;
    if (value.kind === "book") {
      try {
        setValue(await api.read<Artifact>(path));
      } catch (reason) {
        setNotice(errorMessage(reason));
        return;
      }
    }
    setEditingIds(null);
    setEditing(true);
  };
  useEffect(() => {
    let live = true;
    void api
      .read<Artifact>(path)
      .then(async (item) => {
        if (saved.value && saved.value.base_revision_id !== item.head_revision_id) {
          const base = await api.read<Schema<"ArtifactRevision">>(
            path + "/versions/" + saved.value.base_revision_id,
          );
          item = {
            ...item,
            head_revision_id: base.revision_id,
            title: base.title,
            revision: base,
            content_role: base.content_role,
          };
        }
        if (live) setValue(item);
        if (requestedRevision) {
          const referenced = await api.read<Schema<"ArtifactRevision">>(
            path + "/versions/" + requestedRevision,
          );
          if (live) setHistorical(referenced);
        }
      })
      .catch((reason) => {
        if (live) setNotice(errorMessage(reason));
      });
    return () => {
      live = false;
    };
  }, [path, saved, requestedRevision]);
  useEffect(() => {
    const expectedHead = value?.head_revision_id;
    if (!visible || !expectedHead || editing || pending || requestedRevision) return;
    let live = true;
    const timer = setInterval(() => {
      void api
        .read<Artifact>(path)
        .then((current) => {
          if (live)
            setValue((old) =>
              old?.head_revision_id === expectedHead && current.head_revision_id !== expectedHead
                ? current
                : old,
            );
        })
        .catch((reason) => {
          if (live) setNotice(errorMessage(reason));
        });
    }, 2500);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [path, value?.head_revision_id, visible, editing, pending, requestedRevision]);
  const apply = (receipt: Schema<"ArtifactReceipt">) => {
    setPending(null);
    if (receipt.outcome === "conflict") {
      if (value)
        saveWorkbenchDraft(key, {
          base_revision_id: value.head_revision_id,
          changes: draft,
          blocks,
          pending: null,
        });
      setNotice("同时发生了修改，当前版本与本次候选均已保留。请在版本记录中核对后采用。");
      return;
    }
    finishWorkbenchDraft(key);
    setValue(receipt.artifact);
    setDraft({});
    setBlocks({});
    setEditing(false);
    setHistorical(null);
    setNotice("已保存新版本");
    onSaved();
  };
  const save = async () => {
    if (!value || busy || pending) return;
    const changes = value.revision.body.blocks
      .filter(
        (block) =>
          (draft[block.block_id] !== undefined && draft[block.block_id] !== block.markdown) ||
          (blocks[block.block_id] !== undefined &&
            JSON.stringify(blocks[block.block_id]) !== JSON.stringify(block)),
      )
      .map((block) => ({
        block_id: block.block_id,
        replacement: {
          ...(blocks[block.block_id] ?? block),
          markdown: draft[block.block_id] ?? block.markdown,
          attribution: "user_authored" as const,
        },
      }));
    if (!changes.length) {
      setEditing(false);
      return;
    }
    const operation = operationId();
    try {
      if (saved.error)
        throw new ApiError("WORKBENCH_DRAFT_INVALID", "原本地草稿保留，未覆盖原记录");
      saveWorkbenchDraft(key, {
        base_revision_id: value.head_revision_id,
        changes: draft,
        blocks,
        pending: operation,
      });
    } catch (reason) {
      setNotice(errorMessage(reason));
      return;
    }
    setPending(operation);
    setBusy(true);
    try {
      apply(
        await api.write<Schema<"ArtifactReceipt">>(path + "/update", {
          operation_id: operation,
          expected_revision_id: value.head_revision_id,
          changes,
        }),
      );
    } catch (reason) {
      if (
        reason instanceof ApiError &&
        reason.status &&
        reason.status >= 400 &&
        reason.status < 500
      ) {
        saveWorkbenchDraft(key, {
          base_revision_id: value.head_revision_id,
          changes: draft,
          blocks,
          pending: null,
        });
        setPending(null);
      }
      setNotice(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  const reconcile = async () => {
    if (!pending || busy) return;
    setBusy(true);
    try {
      const receipt = await api.read<Schema<"LearningOperationView">>(
        `/courses/${courseId}/learning/operations/${pending}`,
      );
      if ("artifact" in receipt.result) apply(receipt.result);
    } catch {
      setNotice("尚未取得原保存回执，修改仍保留，不会自动重新提交。");
    } finally {
      setBusy(false);
    }
  };
  const history = async (cursor?: string) => {
    try {
      const suffix = cursor ? "?cursor=" + encodeURIComponent(cursor) : "";
      const page = await api.read<Schema<"RevisionIndexPage">>(path + "/versions" + suffix);
      setVersions((old) => (cursor ? [...old, ...page.items] : page.items));
      setHistoryCursor(page.next_cursor);
    } catch (reason) {
      setNotice(errorMessage(reason));
    }
  };
  const restore = async () => {
    if (!historical || !value || busy || pending) return;
    const dirty = value.revision.body.blocks.some(
      (block) =>
        (Object.hasOwn(draft, block.block_id) && draft[block.block_id] !== block.markdown) ||
        (blocks[block.block_id] !== undefined &&
          JSON.stringify(blocks[block.block_id]) !== JSON.stringify(block)),
    );
    if (dirty) {
      setNotice("请先保存当前修改；未保存内容仍保留，尚未恢复其他版本。");
      return;
    }
    const operation = operationId();
    try {
      saveWorkbenchDraft(key, {
        base_revision_id: value.head_revision_id,
        changes: draft,
        blocks,
        pending: operation,
      });
      setPending(operation);
      setBusy(true);
      apply(
        await api.write<Schema<"ArtifactReceipt">>(path + "/restore", {
          operation_id: operation,
          expected_revision_id: value.head_revision_id,
          restore_revision_id: historical.revision_id,
        }),
      );
    } catch (reason) {
      if (
        reason instanceof ApiError &&
        reason.status &&
        reason.status >= 400 &&
        reason.status < 500
      ) {
        saveWorkbenchDraft(key, {
          base_revision_id: value.head_revision_id,
          changes: draft,
          blocks,
          pending: null,
        });
        setPending(null);
      }
      setNotice(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  return (
    <article>
      {notice && <p role="status">{notice}</p>}
      {value && (
        <>
          <h2>{value.title}</h2>
          <p>{roles[value.content_role]}</p>
          {scope?.notices?.map((text) => (
            <p key={text} className="workbench-scope-notice">
              {text}
            </p>
          ))}
          <Button variant="ghost" disabled={busy} onClick={() => void history()}>
            版本记录
          </Button>
          <Button
            variant="ghost"
            disabled={busy || exporting || editing || !!pending}
            onClick={() => void exportOffline(value.head_revision_id)}
          >
            {exporting ? "正在导出…" : "导出离线包"}
          </Button>
          {pending ? (
            <Button disabled={busy} onClick={() => void reconcile()}>
              核对原保存
            </Button>
          ) : editing ? (
            <Button disabled={busy} onClick={() => void save()}>
              保存修改
            </Button>
          ) : (
            <Button onClick={() => void beginEdit()}>
              {value.kind === "book" ? "编辑整书内容" : "编辑"}
            </Button>
          )}
          {!!versions.length && (
            <aside className="workbench-versions" aria-label="版本记录">
              {versions.map((item) => (
                <Button
                  key={item.revision_id}
                  variant="ghost"
                  onClick={() => {
                    void api
                      .read<Schema<"ArtifactRevision">>(path + "/versions/" + item.revision_id)
                      .then(setHistorical)
                      .catch((reason) => setNotice(errorMessage(reason)));
                  }}
                >
                  {new Date(item.created_at).toLocaleString()} ·{" "}
                  {item.author === "model" ? "AI" : "用户"}
                  {item.disposition === "conflict" ? " · 并发候选" : ""}
                </Button>
              ))}
              {historyCursor && (
                <Button variant="ghost" onClick={() => void history(historyCursor)}>
                  更多版本
                </Button>
              )}
            </aside>
          )}
          {historical && (
            <aside aria-label="历史版本内容" className="workbench-history">
              <h3>{historical.title}</h3>
              {historicalScope?.notices?.map((text) => (
                <p key={text} className="workbench-scope-notice">
                  {text}
                </p>
              ))}
              {value.kind === "book" && structuredBook ? (
                <BookReader
                  key={historical.revision_id}
                  courseId={courseId}
                  artifactId={artifactId}
                  fixedRevision={historical.revision_id}
                  onOpenSource={setSource}
                  onUnstructured={unstructuredBook}
                />
              ) : (
                historical.body.blocks.map((block) => (
                  <div key={block.block_id}>
                    {historicalScope?.block_notices?.[block.block_id] && (
                      <p className="workbench-scope-notice">
                        {historicalScope.block_notices[block.block_id]}
                      </p>
                    )}
                    <ContentBlock block={block} />
                  </div>
                ))
              )}
              <Button
                disabled={busy || !!pending || historical.revision_id === value.head_revision_id}
                onClick={() => void restore()}
              >
                采用此版本，保留历史
              </Button>
              <Button variant="ghost" onClick={() => setHistorical(null)}>
                关闭版本查看
              </Button>
              <Button
                variant="ghost"
                disabled={exporting || historical.disposition !== "published"}
                onClick={() => void exportOffline(historical.revision_id)}
              >
                导出此历史版本
              </Button>
            </aside>
          )}
          {source && (
            <aside aria-label="来源快照" className="workbench-history">
              <SourcePreview source={source} onOpenSource={onOpenSource} />
              <Button variant="ghost" onClick={() => setSource(null)}>
                关闭来源
              </Button>
            </aside>
          )}
          {!editing && value.kind === "learning_path" && (
            <LearningPathPanel
              key={value.head_revision_id}
              courseId={courseId}
              artifactId={artifactId}
              contentRevision={value.head_revision_id}
              sessionId={sessionId}
              onTaskCreated={onTaskCreated}
              onOpenSource={setSource}
            />
          )}
          {!editing && value.kind === "book" && structuredBook && (
            <BookReader
              courseId={courseId}
              artifactId={artifactId}
              refreshVersion={value.head_revision_id}
              sessionId={sessionId}
              onOpenSource={setSource}
              onTaskCreated={onTaskCreated}
              onArtifactChanged={apply}
              onUnstructured={unstructuredBook}
              onEdit={(revision, ids) => {
                if (busy || pending) return;
                setValue({
                  ...value,
                  head_revision_id: revision.revision_id,
                  revision,
                  title: revision.title,
                  content_role: revision.content_role,
                });
                setEditingIds(ids);
                setEditing(true);
              }}
            />
          )}
          {value.revision.body.blocks
            .filter((block) =>
              editing
                ? value.kind !== "book" || !editingIds || editingIds.includes(block.block_id)
                : block.kind !== "path_step" && (value.kind !== "book" || !structuredBook),
            )
            .map((block) => (
              <section key={block.block_id} className="workbench-block">
                {scope?.block_notices?.[block.block_id] && (
                  <p className="workbench-scope-notice">{scope.block_notices[block.block_id]}</p>
                )}
                {editing ? (
                  <>
                    <textarea
                      aria-label="编辑内容块"
                      disabled={busy || !!pending}
                      value={draft[block.block_id] ?? block.markdown}
                      onChange={(event) => {
                        const next = { ...draft, [block.block_id]: event.target.value };
                        setDraft(next);
                        try {
                          if (!saved.error)
                            saveWorkbenchDraft(key, {
                              base_revision_id: value.head_revision_id,
                              changes: next,
                              blocks,
                              pending,
                            });
                        } catch (reason) {
                          setNotice(errorMessage(reason));
                        }
                      }}
                    />
                    <StructuredBlockEditor
                      block={blocks[block.block_id] ?? block}
                      disabled={busy || !!pending}
                      onChange={(changed) => {
                        const next = { ...blocks, [block.block_id]: changed };
                        setBlocks(next);
                        try {
                          if (!saved.error)
                            saveWorkbenchDraft(key, {
                              base_revision_id: value.head_revision_id,
                              changes: draft,
                              blocks: next,
                              pending,
                            });
                        } catch (reason) {
                          setNotice(errorMessage(reason));
                        }
                      }}
                    />
                  </>
                ) : block.kind === "question" ? (
                  <QuestionPlayer
                    key={`${block.block_id}:${value.head_revision_id}`}
                    courseId={courseId}
                    artifactId={artifactId}
                    revisionId={value.head_revision_id}
                    block={block}
                    sessionId={sessionId}
                    onTaskCreated={onTaskCreated}
                  />
                ) : block.kind === "essay_draft" ? (
                  <EssayPanel
                    key={`${block.block_id}:${value.head_revision_id}`}
                    courseId={courseId}
                    artifactId={artifactId}
                    revisionId={value.head_revision_id}
                    block={block}
                    sessionId={sessionId}
                    onTaskCreated={onTaskCreated}
                    onRevised={apply}
                  />
                ) : block.kind === "essay_feedback" ? (
                  <EssayFeedbackPanel
                    key={block.feedback.round_id}
                    courseId={courseId}
                    roundId={block.feedback.round_id}
                    sessionId={sessionId}
                    onTaskCreated={onTaskCreated}
                    onOpenArtifact={onOpenArtifact}
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
                            : "来源归纳"}
                </small>
                {!editing && sessionId && (
                  <Button variant="ghost" onClick={() => void quoteBlock(block.block_id)}>
                    引用到问题
                  </Button>
                )}
                {!!block.source_snapshot_ids?.length && (
                  <div className="workbench-citations">
                    {(block.source_snapshot_ids ?? []).map((identity, index) => (
                      <Button
                        key={identity}
                        variant="ghost"
                        onClick={() => {
                          void api
                            .read<Schema<"SourceSnapshot">>(
                              `/courses/${courseId}/learning/selections/${identity}`,
                            )
                            .then(setSource)
                            .catch((reason) => setNotice(errorMessage(reason)));
                        }}
                      >
                        来源 {index + 1}
                      </Button>
                    ))}
                  </div>
                )}
                {block.kind === "translation" &&
                  block.translation.source_parts.map((part, index) => (
                    <Button
                      key={`${part.snapshot_id}:${part.start}:${part.end}`}
                      variant="ghost"
                      onClick={() =>
                        void api
                          .write<Schema<"SourceSnapshot">>(
                            `/courses/${courseId}/learning/selections/${part.snapshot_id}/slice`,
                            { start: part.start, end: part.end },
                          )
                          .then(setSource)
                          .catch((reason) => setNotice(errorMessage(reason)))
                      }
                    >
                      原文段落位置 {index + 1}
                    </Button>
                  ))}
              </section>
            ))}
        </>
      )}
    </article>
  );
}
