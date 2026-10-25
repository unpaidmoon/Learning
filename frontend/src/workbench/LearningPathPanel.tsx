import { useCallback, useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { MarkdownAnswer } from "../chat/MarkdownAnswer";
import { Button } from "../components/ui/button";
import { QuestionPlayer } from "./QuestionPlayer";
import { useWorkbenchOperation } from "./useWorkbenchOperation";

const selfLabels = { uncertain: "还不确定", needs_practice: "需要练习", confident: "自我感觉熟悉" };
const verdictLabels = { correct: "正确", wrong: "错误", partial: "部分正确", pending: "待判断" };
type Path = Schema<"PathView">;
type Step = Schema<"PathStepView">;

export function LearningPathPanel({
  courseId,
  artifactId,
  contentRevision,
  sessionId,
  onTaskCreated,
  onOpenSource,
}: {
  courseId: string;
  artifactId: string;
  contentRevision: string;
  sessionId?: string;
  onTaskCreated?: (task: Schema<"TaskView">) => void;
  onOpenSource: (source: Schema<"SourceSnapshot">) => void;
}) {
  const [path, setPath] = useState<Path | null>(null);
  const [notice, setNotice] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [history, setHistory] = useState<Schema<"PathEvent">[]>([]);
  const [before, setBefore] = useState<number | null>(null);
  const base = `/courses/${courseId}/learning/artifacts/${artifactId}/path`;
  const apply = (receipt: Schema<"PathReceipt">) => {
    setPath(receipt.path);
    if (receipt.path.current_block_id) setSelected(receipt.path.current_block_id);
  };
  const operation = useWorkbenchOperation<Schema<"PathReceipt">>(
    `${courseId}.path.${artifactId}`,
    courseId,
    "path",
    apply,
  );
  const refresh = useCallback(async () => {
    const value = await api.read<Path>(base);
    if (value.content_revision_id !== contentRevision)
      throw new Error("路线已修改，请重新打开当前成果。");
    return value;
  }, [base, contentRevision]);
  useEffect(() => {
    let live = true;
    void refresh()
      .then((value) => {
        if (live) setPath(value);
      })
      .catch((reason) => {
        if (live) setNotice(errorMessage(reason));
      });
    return () => {
      live = false;
    };
  }, [refresh]);
  const act = (block: string, action: Schema<"PathAction">["action"], extra = {}) => {
    if (!path) return;
    void operation.execute(base + "/actions", {
      expected_revision: path.revision,
      content_revision_id: path.content_revision_id,
      block_id: block,
      action,
      ...extra,
    });
  };
  const readEvents = async (cursor?: number) => {
    try {
      const page = await api.read<Schema<"PathEventPage">>(
        base + "/events" + (cursor ? `?before=${cursor}` : ""),
      );
      setHistory((old) => (cursor ? [...old, ...page.events] : page.events));
      setBefore(page.next_before);
    } catch (reason) {
      setNotice(errorMessage(reason));
    }
  };
  const step =
    path?.steps.find((s) => s.block.block_id === selected) ??
    path?.steps.find((s) => s.block.block_id === path.current_block_id) ??
    path?.steps[0];
  return (
    <section aria-label="建议学习路线" className="learning-path">
      <p>按目标安排的建议顺序，可随时选择知识点。浏览、自评和实际作答分别记录。</p>
      <div className="workbench-toolbar">
        <Button
          variant="ghost"
          onClick={() =>
            void refresh()
              .then(setPath)
              .catch((e) => setNotice(errorMessage(e)))
          }
        >
          刷新学习证据
        </Button>
        <Button variant="ghost" onClick={() => void readEvents()}>
          学习记录
        </Button>
      </div>
      <div className="workbench-columns">
        <nav aria-label="路线知识点">
          {(path?.steps ?? []).map((item) => (
            <button
              key={item.block.block_id}
              aria-pressed={item.block.block_id === step?.block.block_id}
              disabled={operation.disabled}
              onClick={() => {
                setSelected(item.block.block_id);
                act(item.block.block_id, "navigate");
              }}
            >
              <strong>{item.block.step.title}</strong>
              <small>
                {item.skipped ? "已跳过，可随时返回" : item.visited ? "已浏览" : "待学习"}
                {(item.visited || item.skipped) && !item.activity_current ? " · 内容已有修改" : ""}
              </small>
            </button>
          ))}
        </nav>
        {step && (
          <article aria-label="路线当前知识点" key={step.block.block_id}>
            <h3>{step.block.step.title}</h3>
            <MarkdownAnswer text={step.block.step.objective} />
            <MarkdownAnswer text={step.block.markdown} />
            <p>{step.block.step.recall_prompt}</p>
            <div className="workbench-toolbar">
              <Button
                disabled={operation.disabled}
                onClick={() => act(step.block.block_id, "view_explanation")}
              >
                查看完整讲解
              </Button>
              <Button
                variant="ghost"
                disabled={operation.disabled}
                onClick={() => act(step.block.block_id, "skip")}
              >
                暂时跳过
              </Button>
            </div>
            {step.explanation_seen && step.activity_current && (
              <MarkdownAnswer text={step.block.step.explanation} />
            )}
            <fieldset disabled={operation.disabled}>
              <legend>我的自评</legend>
              {Object.entries(selfLabels).map(([value, label]) => (
                <Button
                  key={value}
                  variant="ghost"
                  aria-pressed={step.self_assessment === value && step.self_assessment_current}
                  onClick={() =>
                    act(step.block.block_id, "self_assess", { self_assessment: value })
                  }
                >
                  {label}
                </Button>
              ))}
            </fieldset>
            {step.self_assessment && (
              <p>
                用户自评：{selfLabels[step.self_assessment]}
                {!step.self_assessment_current && "（旧内容版本）"}。这条自评不等同测试通过。
              </p>
            )}
            {(step.block.source_snapshot_ids ?? []).map((id, index) => (
              <Button
                key={id}
                variant="ghost"
                onClick={() => {
                  void api
                    .read<Schema<"SourceSnapshot">>(
                      `/courses/${courseId}/learning/selections/${id}`,
                    )
                    .then(onOpenSource)
                    .catch((e) => setNotice(errorMessage(e)));
                }}
              >
                知识点来源 {index + 1}
              </Button>
            ))}
            <PathEvidence step={step} />
            {step.answers.map((evidence) => (
              <LinkedPractice
                key={`${evidence.question.revision_id}:${evidence.question.block_id}`}
                courseId={courseId}
                link={evidence.question}
                sessionId={sessionId}
                onTaskCreated={onTaskCreated}
                onAnswered={() =>
                  void refresh()
                    .then(setPath)
                    .catch((e) => setNotice(errorMessage(e)))
                }
              />
            ))}
            {step.review && (
              <p>
                知识点复习：
                {step.review.queue_state === "paused"
                  ? "已暂停"
                  : step.review.queue_state === "removed"
                    ? "已移出"
                    : "与卡片、错题共用今日总量"}
                。 下次到期：{new Date(step.review.due_ms).toLocaleString()}
              </p>
            )}
          </article>
        )}
      </div>
      {!!history.length && (
        <details open>
          <summary>已保存的学习操作</summary>
          {history.map((event) => (
            <p key={event.event_id}>
              {new Date(event.created_at).toLocaleString()} ·{" "}
              {(
                {
                  navigate: "打开知识点",
                  skip: "暂时跳过",
                  view_explanation: "查看讲解",
                  self_assess: "用户自评",
                } as Record<string, string>
              )[event.action] ?? event.action}
            </p>
          ))}
          {before && (
            <Button variant="ghost" onClick={() => void readEvents(before)}>
              更早的学习记录
            </Button>
          )}
        </details>
      )}
      {operation.pending && (
        <Button onClick={() => void operation.reconcile()}>核对原学习操作</Button>
      )}
      {(notice || operation.notice) && <p role="status">{notice || operation.notice}</p>}
    </section>
  );
}

function PathEvidence({ step }: { step: Step }) {
  const actual = step.answers.flatMap((e) => (e.latest_answer ? [e.latest_answer] : []));
  return (
    <section aria-label="知识点实际作答证据">
      <p>实际作答证据：{!actual.length && "尚无作答"}</p>
      {actual.map((answer) => (
        <p key={answer.answer_id}>
          第{answer.sequence}次作答 · {verdictLabels[answer.effective_assessment.verdict]} ·{" "}
          {answer.effective_assessment.author === "ai_reference"
            ? "AI参考判分"
            : answer.effective_assessment.author === "user_override"
              ? "用户改判"
              : "按已确认答案判分"}
        </p>
      ))}
    </section>
  );
}

function LinkedPractice({
  courseId,
  link,
  sessionId,
  onTaskCreated,
  onAnswered,
}: {
  courseId: string;
  link: Schema<"PathQuestionLink">;
  sessionId?: string;
  onTaskCreated?: (task: Schema<"TaskView">) => void;
  onAnswered: () => void;
}) {
  const [block, setBlock] = useState<Schema<"PracticeQuestion">["block"] | null>(null);
  const [open, setOpen] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    if (!open) return;
    let live = true;
    void api
      .read<Schema<"ArtifactRevision">>(
        `/courses/${courseId}/learning/artifacts/${link.artifact_id}/versions/${link.revision_id}`,
      )
      .then((revision) => {
        const item = revision.body.blocks.find((b) => b.block_id === link.block_id);
        if (!item || item.kind !== "question") throw new Error("关联的原版本题目无法核对");
        if (live) setBlock(item);
      })
      .catch((e) => {
        if (live) setNotice(errorMessage(e));
      });
    return () => {
      live = false;
    };
  }, [courseId, link.artifact_id, link.revision_id, link.block_id, open]);
  return (
    <section>
      <Button variant="outline" onClick={() => setOpen((v) => !v)}>
        {open ? "收起关联练习" : "打开关联练习"}
      </Button>
      {open && block && (
        <QuestionPlayer
          courseId={courseId}
          artifactId={link.artifact_id}
          revisionId={link.revision_id}
          block={block}
          sessionId={sessionId}
          onTaskCreated={onTaskCreated}
          onAnswered={onAnswered}
        />
      )}
      {notice && <p role="status">{notice}</p>}
    </section>
  );
}
