import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { TaskComposer } from "./TaskComposer";
import { ArtifactEditor } from "./ArtifactEditor";
import { TaskDetail } from "./TaskDetail";
import { ReviewPanel } from "./ReviewPanel";
import { EssayComposer } from "./EssayComposer";
import { CorpusPanel } from "./CorpusPanel";
import { EvidencePanel } from "./EvidencePanel";
import { NoteComposer } from "./NoteComposer";
import { roles, states } from "./model";
import "./workbench.css";

export function WorkbenchPanel(props: {
  courseId: string;
  sessionId: string;
  file?: Schema<"CourseFileOpenResponse">;
  media?: Schema<"MediaAssetResponse">;
  onOpenSource?: (source: Schema<"SourceSnapshot">) => Promise<void>;
  visible?: boolean;
}) {
  return <CourseWorkbench key={props.courseId} {...props} />;
}

function CourseWorkbench({
  courseId,
  sessionId,
  file,
  media,
  onOpenSource,
  visible = true,
}: {
  courseId: string;
  sessionId: string;
  file?: Schema<"CourseFileOpenResponse">;
  media?: Schema<"MediaAssetResponse">;
  onOpenSource?: (source: Schema<"SourceSnapshot">) => Promise<void>;
  visible?: boolean;
}) {
  const [tab, setTab] = useState<"assets" | "tasks" | "reviews">("assets");
  const [assets, setAssets] = useState<Schema<"ArtifactIndexItem">[]>([]);
  const [tasks, setTasks] = useState<Schema<"TaskIndexItem">[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [referenceRevision, setReferenceRevision] = useState<string | null>(null);
  const [compose, setCompose] = useState(false);
  const [composeEssay, setComposeEssay] = useState(false);
  const [composeNote, setComposeNote] = useState(false);
  const [showCorpus, setShowCorpus] = useState(false);
  const [showEvidence, setShowEvidence] = useState(false);
  const requestEpoch = useRef(0);
  useEffect(() => {
    let live = true;
    const open = (event: Event) => {
      const detail = (event as CustomEvent<{ courseId: string; taskId: string }>).detail;
      if (!detail || detail.courseId !== courseId || !/^[A-Za-z0-9_-]{1,160}$/.test(detail.taskId))
        return;
      void api
        .read<Schema<"TaskView">>(`/courses/${courseId}/learning/tasks/${detail.taskId}`)
        .then((task) => {
          if (live && task.course_id === courseId) {
            setTab("tasks");
            setSelected(task.task_id);
            setReferenceRevision(null);
          }
        })
        .catch((reason) => {
          if (live) setError(errorMessage(reason));
        });
    };
    window.addEventListener("learning-open-task", open);
    return () => {
      live = false;
      window.removeEventListener("learning-open-task", open);
    };
  }, [courseId]);
  const sourceJumpEpoch = useRef(0);
  const invalidateSourceJump = useCallback(() => {
    ++sourceJumpEpoch.current;
  }, []);
  useEffect(() => {
    invalidateSourceJump();
    return invalidateSourceJump;
  }, [
    invalidateSourceJump,
    selected,
    sessionId,
    visible,
    file?.asset_id,
    file?.source_version,
    media?.asset_id,
  ]);
  const openSource = async (originalSource: Schema<"SourceSnapshot">) => {
    const epoch = ++sourceJumpEpoch.current;
    if (originalSource.course_id !== courseId) {
      if (onOpenSource) {
        await onOpenSource(originalSource);
        return;
      }
      throw new ApiError("SOURCE_NAVIGATION_UNAVAILABLE", "请通过已保存的跨课程来源快照核对原文");
    }
    let source = originalSource;
    for (let depth = 0; source.reference.kind === "snapshot_slice"; depth++) {
      if (depth >= 16) throw new ApiError("SOURCE_REFERENCE_DEPTH", "来源分段层次无法核对");
      source = await api.read<Schema<"SourceSnapshot">>(
        `/courses/${courseId}/learning/selections/${source.reference.parent_snapshot_id}`,
      );
    }
    if (epoch !== sourceJumpEpoch.current) return;
    if (source.course_id !== courseId)
      throw new ApiError("SOURCE_COURSE_DENIED", "来源不属于当前课程");
    if (source.reference.kind === "artifact") {
      setTab("assets");
      setSelected(source.reference.artifact_id);
      setReferenceRevision(source.reference.revision_id);
      setShowEvidence(false);
    } else if (onOpenSource) await onOpenSource(originalSource);
    else throw new ApiError("SOURCE_NAVIGATION_UNAVAILABLE", "请通过来源快照核对原文");
  };
  const invalidateRequest = useCallback(() => {
    ++requestEpoch.current;
  }, []);
  const observeHead = useCallback(
    (artifact: Schema<"ArtifactView">) => {
      if (artifact.course_id !== courseId) return;
      setAssets((old) => {
        const found = old.find((item) => item.artifact_id === artifact.artifact_id);
        if (
          !found ||
          (found.head_revision_id === artifact.head_revision_id &&
            found.content_role === artifact.content_role &&
            found.title === artifact.title)
        )
          return old;
        return old.map((item) =>
          item.artifact_id === artifact.artifact_id
            ? {
                ...item,
                head_revision_id: artifact.head_revision_id,
                content_role: artifact.content_role,
                title: artifact.title,
                updated_at: artifact.updated_at,
              }
            : item,
        );
      });
    },
    [courseId],
  );
  const refresh = useCallback(
    async (next?: string) => {
      if (tab === "reviews") return;
      const epoch = ++requestEpoch.current;
      const params = new URLSearchParams({ query, limit: "30" });
      if (next) params.set("cursor", next);
      if (tab === "assets") {
        const page = await api.read<Schema<"ArtifactIndexPage">>(
          `/courses/${courseId}/learning/artifact-index?${params}`,
        );
        if (epoch !== requestEpoch.current) return;
        setAssets((old) => (next ? [...old, ...page.items] : page.items));
        setCursor(page.next_cursor);
      } else {
        const page = await api.read<Schema<"TaskIndexPage">>(
          `/courses/${courseId}/learning/task-index?${params}`,
        );
        if (epoch !== requestEpoch.current) return;
        setTasks((old) => (next ? [...old, ...page.items] : page.items));
        setCursor(page.next_cursor);
      }
    },
    [courseId, query, tab],
  );
  useEffect(() => {
    if (!visible) return;
    let live = true;
    const read = () => {
      if (live)
        void refresh().catch((reason) => {
          if (live) setError(errorMessage(reason));
        });
    };
    read();
    const timer = setInterval(() => {
      if (tab === "tasks") read();
    }, 2500);
    return () => {
      live = false;
      invalidateRequest();
      clearInterval(timer);
    };
  }, [refresh, tab, visible, invalidateRequest]);
  return (
    <section className="workbench" aria-label="学习成果与任务">
      <div className="workbench-toolbar">
        <Button
          variant={tab === "assets" ? "outline" : "ghost"}
          onClick={() => {
            setTab("assets");
            setSelected(null);
          }}
        >
          学习成果
        </Button>
        <Button
          variant={tab === "tasks" ? "outline" : "ghost"}
          onClick={() => {
            setTab("tasks");
            setSelected(null);
          }}
        >
          任务
        </Button>
        <Button onClick={() => setCompose(true)}>整理资料</Button>
        <Button onClick={() => setComposeNote(true)}>新建笔记</Button>
        <Button onClick={() => setComposeEssay(true)}>写作文</Button>
        <Button onClick={() => setShowCorpus((old) => !old)}>课程资料</Button>
        <Button onClick={() => setShowEvidence((old) => !old)}>问答来源</Button>
        <Button
          variant={tab === "reviews" ? "outline" : "ghost"}
          onClick={() => {
            setTab("reviews");
            setSelected(null);
          }}
        >
          今日复习
        </Button>
        <input
          aria-label="搜索学习成果或任务"
          placeholder="搜索"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      {error && <p role="alert">{error}</p>}
      {showEvidence && visible && (
        <EvidencePanel
          key={sessionId}
          courseId={courseId}
          sessionId={sessionId}
          onOpenSource={openSource}
          onClose={() => setShowEvidence(false)}
        />
      )}
      {showCorpus && visible && (
        <CorpusPanel
          courseId={courseId}
          sessionId={sessionId}
          onClose={() => setShowCorpus(false)}
        />
      )}
      {composeNote && (
        <NoteComposer
          courseId={courseId}
          onClose={() => setComposeNote(false)}
          onCreated={(artifact) => {
            setComposeNote(false);
            setTab("assets");
            setSelected(artifact.artifact_id);
            setReferenceRevision(null);
            void refresh().catch((reason) => setError(errorMessage(reason)));
          }}
        />
      )}
      {composeEssay && (
        <EssayComposer
          courseId={courseId}
          onClose={() => setComposeEssay(false)}
          onCreated={(artifact) => {
            setComposeEssay(false);
            setTab("assets");
            setSelected(artifact.artifact_id);
            setReferenceRevision(null);
            void refresh().catch((reason) => setError(errorMessage(reason)));
          }}
        />
      )}
      {compose && (
        <TaskComposer
          courseId={courseId}
          sessionId={sessionId}
          file={file}
          media={media}
          onClose={() => setCompose(false)}
          onCreated={(task) => {
            setCompose(false);
            setTab("tasks");
            setSelected(task.task_id);
            void refresh().catch((reason) => setError(errorMessage(reason)));
          }}
        />
      )}
      {tab === "reviews" ? (
        <ReviewPanel
          courseId={courseId}
          sessionId={sessionId}
          onTaskCreated={(task) => {
            setTab("tasks");
            setSelected(task.task_id);
          }}
        />
      ) : (
        <div className="workbench-columns">
          <nav aria-label={tab === "assets" ? "学习成果列表" : "任务列表"}>
            {tab === "assets"
              ? assets.map((asset) => (
                  <button
                    key={asset.artifact_id}
                    onClick={() => {
                      setSelected(asset.artifact_id);
                      setReferenceRevision(null);
                    }}
                    aria-pressed={selected === asset.artifact_id}
                  >
                    <strong>{asset.title}</strong>
                    <small>{roles[asset.content_role]}</small>
                  </button>
                ))
              : tasks.map((task) => (
                  <button
                    key={task.task_id}
                    onClick={() => setSelected(task.task_id)}
                    aria-pressed={selected === task.task_id}
                  >
                    <strong>{task.title}</strong>
                    <small>
                      {states[task.state]} · {task.completed_work}/{task.total_work} 项生成工作
                    </small>
                  </button>
                ))}
            {cursor && (
              <Button
                variant="ghost"
                onClick={() =>
                  void refresh(cursor).catch((reason) => setError(errorMessage(reason)))
                }
              >
                加载更多
              </Button>
            )}
          </nav>
          <div className="workbench-detail">
            {selected ? (
              tab === "assets" ? (
                <ArtifactEditor
                  visible={visible}
                  key={`${courseId}:${selected}:${referenceRevision ?? "head"}`}
                  courseId={courseId}
                  artifactId={selected}
                  onSaved={() => void refresh()}
                  onHeadObserved={observeHead}
                  onOpenArtifact={(id) => {
                    setSelected(id);
                    setReferenceRevision(null);
                  }}
                  sessionId={sessionId}
                  onTaskCreated={(task) => {
                    setTab("tasks");
                    setSelected(task.task_id);
                  }}
                  requestedRevision={referenceRevision ?? undefined}
                  onOpenSource={openSource}
                />
              ) : (
                <TaskDetail
                  key={`${courseId}:${selected}`}
                  courseId={courseId}
                  taskId={selected}
                  indexRevision={tasks.find((task) => task.task_id === selected)?.revision}
                  onOpen={(id) => {
                    setTab("assets");
                    setSelected(id);
                    setReferenceRevision(null);
                  }}
                />
              )
            ) : (
              <p>选择成果继续阅读或修改，也可以明确发起资料整理。普通问答不会自动创建成果。</p>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
