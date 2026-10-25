import { useCallback, useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { MarkdownAnswer } from "../chat/MarkdownAnswer";
import { Button } from "../components/ui/button";
import { useWorkbenchOperation } from "./useWorkbenchOperation";
import { EssayRoundComparison } from "./EssayRoundComparison";

type Block = Schema<"EssayRound">["draft"];

export function EssayPanel({
  courseId,
  artifactId,
  revisionId,
  block,
  sessionId,
  onTaskCreated,
  onRevised,
}: {
  courseId: string;
  artifactId: string;
  revisionId: string;
  block: Block;
  sessionId?: string;
  onTaskCreated?: (task: Schema<"TaskView">) => void;
  onRevised: (receipt: Schema<"ArtifactReceipt">) => void;
}) {
  const base = `/courses/${courseId}/learning`;
  const key = `${courseId}.essay.${artifactId}.${revisionId}.${block.block_id}`;
  const [rounds, setRounds] = useState<Schema<"EssayRoundSummary">[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [selected, setSelected] = useState<Schema<"EssayRound"> | null>(null);
  const [notice, setNotice] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [comparing, setComparing] = useState(false);
  const refresh = useCallback(
    async () => api.read<Schema<"EssayRoundPage">>(`${base}/artifacts/${artifactId}/essay-rounds`),
    [base, artifactId],
  );
  useEffect(() => {
    let live = true;
    void refresh()
      .then((page) => {
        if (live) {
          setRounds(page.items);
          setCursor(page.next_before);
          setLoaded(true);
        }
      })
      .catch((error) => {
        if (live) setNotice(errorMessage(error));
      });
    return () => {
      live = false;
    };
  }, [refresh]);
  const generation = useWorkbenchOperation<Schema<"TaskView">>(
    key + ".review",
    courseId,
    "task",
    (task) => onTaskCreated?.(task),
  );
  return (
    <section aria-label="作文与轮次" className="essay-panel">
      {block.essay.topic && (
        <>
          <h3>题目与要求</h3>
          <MarkdownAnswer text={block.essay.topic} />
        </>
      )}
      <h3>实际作文</h3>
      <MarkdownAnswer text={block.markdown} />
      <details>
        <summary>评分标准 · 参考满分 {block.essay.rubric?.max_total ?? 100}</summary>
        <MarkdownAnswer text={block.essay.rubric?.description ?? ""} />
      </details>
      {sessionId && (
        <Button
          disabled={generation.disabled || !loaded}
          onClick={() =>
            void generation.execute(
              `/desktop/sessions/${encodeURIComponent(sessionId)}/learning-task-intents`,
              {
                kind: "essay_review",
                instruction:
                  "按这份实际稿、题目和评分标准完成一次参考批改，标明理由和实际原文批注；不自动改稿。",
                sources: [],
                target: { mode: "create", title: "作文参考批改" },
                essay_review: {
                  artifact_id: artifactId,
                  revision_id: revisionId,
                  block_id: block.block_id,
                  expected_round_number: rounds[0]?.round_number ?? 0,
                },
              },
            )
          }
        >
          批改当前稿，保存独立轮次
        </Button>
      )}
      {generation.pending && (
        <Button disabled={generation.busy} onClick={() => void generation.reconcile()}>
          核对原批改任务
        </Button>
      )}
      {!!rounds.length && (
        <aside aria-label="作文轮次记录">
          <h3>批改轮次</h3>
          {rounds.length >= 2 && (
            <Button variant="outline" onClick={() => setComparing((old) => !old)}>
              {comparing ? "收起对照" : "对照两轮"}
            </Button>
          )}
          {rounds.map((row) => (
            <Button
              key={row.round_id}
              variant="ghost"
              onClick={() =>
                void api
                  .read<Schema<"EssayRound">>(`${base}/essay-rounds/${row.round_id}`)
                  .then(setSelected)
                  .catch((error) => setNotice(errorMessage(error)))
              }
            >
              第{row.round_number}轮 · {new Date(row.created_at).toLocaleString()}
              {row.draft_revision_id !== revisionId ? " · 历史稿" : " · 当前稿"}
            </Button>
          ))}
          {cursor && (
            <Button
              variant="ghost"
              onClick={() =>
                void api
                  .read<Schema<"EssayRoundPage">>(
                    `${base}/artifacts/${artifactId}/essay-rounds?before=${cursor}`,
                  )
                  .then((page) => {
                    setRounds((old) => [...old, ...page.items]);
                    setCursor(page.next_before);
                  })
                  .catch((error) => setNotice(errorMessage(error)))
              }
            >
              更多轮次
            </Button>
          )}
        </aside>
      )}
      {selected && (
        <EssayRoundDetail
          key={selected.round_id}
          initial={selected}
          currentRevision={revisionId}
          sessionId={sessionId}
          onTaskCreated={onTaskCreated}
          onRevised={onRevised}
        />
      )}
      {comparing && (
        <EssayRoundComparison
          key={artifactId}
          courseId={courseId}
          artifactId={artifactId}
          rounds={rounds}
        />
      )}
      {(notice || generation.notice) && <p role="status">{notice || generation.notice}</p>}
    </section>
  );
}

function EssayRoundDetail({
  initial,
  currentRevision,
  sessionId,
  onTaskCreated,
  onRevised,
}: {
  initial: Schema<"EssayRound">;
  currentRevision: string;
  sessionId?: string;
  onTaskCreated?: (task: Schema<"TaskView">) => void;
  onRevised: (receipt: Schema<"ArtifactReceipt">) => void;
}) {
  const [round, setRound] = useState(initial);
  const [total, setTotal] = useState("");
  const [reason, setReason] = useState("");
  const [origin, setOrigin] = useState<"user_self" | "user_override">("user_override");
  const [indices, setIndices] = useState<number[]>([]);
  const [notice, setNotice] = useState("");
  const base = `/courses/${round.course_id}/learning`;
  const path = `${base}/essay-rounds/${round.round_id}`;
  const key = round.course_id + ".essay.round." + round.round_id;
  const assessment = useWorkbenchOperation<Schema<"EssayRound">>(
    key + ".assessment",
    round.course_id,
    "essay",
    setRound,
  );
  const acceptance = useWorkbenchOperation<Schema<"ArtifactReceipt">>(
    key + ".acceptance",
    round.course_id,
    "artifact",
    onRevised,
  );
  const derivatives = useWorkbenchOperation<Schema<"TaskView">>(
    key + ".derivative",
    round.course_id,
    "task",
    (task) => onTaskCreated?.(task),
  );
  const result = round.result as {
    status?: string;
    diagnostics?: string[];
    annotations?: {
      start: number;
      end: number;
      quote: string;
      replacement: string;
      comment: string;
      kind: string;
    }[];
    reference_evaluation?: { total: number; max_total: number } | null;
  } | null;
  const reference = result?.reference_evaluation;
  const userAssessment = round.assessments.find(
    (item) => item.assessment_id === round.user_assessment_id,
  );
  const generate = async (kind: "question_set" | "card_set") => {
    if (!sessionId || !round.feedback_artifact_id) return;
    try {
      const feedback = await api.read<Schema<"ArtifactView">>(
        `${base}/artifacts/${round.feedback_artifact_id}`,
      );
      await derivatives.execute(
        `/desktop/sessions/${encodeURIComponent(sessionId)}/learning-task-intents`,
        {
          kind,
          instruction:
            kind === "card_set"
              ? "针对本轮原稿与参考批改中的具体问题明确创建复习卡，保留AI参考身份；不冒充用户已掌握。"
              : "针对本轮原稿与参考批改中的具体问题明确创建练习题，保留参考答案与来源。",
          target: {
            mode: "create",
            title: kind === "card_set" ? "作文问题复习卡" : "作文问题练习",
          },
          sources: [
            {
              kind: "artifact",
              artifact_id: round.artifact_id,
              revision_id: round.draft_revision_id,
              block_ids: [round.block_id],
            },
            {
              kind: "artifact",
              artifact_id: feedback.artifact_id,
              revision_id: feedback.head_revision_id,
              block_ids: feedback.revision.body.blocks.map((block) => block.block_id),
            },
          ],
        },
      );
    } catch (error) {
      setNotice(errorMessage(error));
    }
  };
  return (
    <article className="workbench-history" aria-label="作文轮次详情">
      <h3>第{round.round_number}轮</h3>
      {round.draft_revision_id !== currentRevision && (
        <p>此轮绑定历史稿，参考结果不会替换当前稿。</p>
      )}
      <details>
        <summary>查看本轮实际稿</summary>
        <AnnotatedEssay text={round.draft.markdown} annotations={result?.annotations ?? []} />
      </details>
      <p>
        {reference
          ? `AI参考评分：${reference.total} / ${reference.max_total}`
          : "AI参考评分：待判断"}
      </p>
      {userAssessment && (
        <p>
          {userAssessment.origin === "user_self" ? "用户自评" : "用户更正"}：
          {userAssessment.total ?? "待判断"} / {userAssessment.max_total}
        </p>
      )}
      {result?.status === "pending" && (
        <p>原始评分或批注尚不能可靠使用，已保留原输出，可由用户判断。</p>
      )}
      {typeof round.result?.raw_feedback === "string" && (
        <details>
          <summary>完整参考批改</summary>
          <MarkdownAnswer text={round.result.raw_feedback} />
        </details>
      )}
      {(result?.annotations ?? []).map((annotation, index) => (
        <section key={index}>
          <p>
            <mark>{annotation.quote || "插入位置"}</mark> · {annotation.comment}
          </p>
          {annotation.replacement !== annotation.quote && (
            <label>
              <input
                type="checkbox"
                disabled={acceptance.disabled}
                checked={indices.includes(index)}
                onChange={() =>
                  setIndices((old) =>
                    old.includes(index) ? old.filter((value) => value !== index) : [...old, index],
                  )
                }
              />
              采纳为：{annotation.replacement || "删除此处"}
            </label>
          )}
        </section>
      ))}
      <Button
        disabled={acceptance.disabled || !indices.length}
        onClick={() =>
          void acceptance.execute(path + "/apply-suggestions", {
            expected_revision_id: round.draft_revision_id,
            annotation_indices: indices,
          })
        }
      >
        采纳所选，保存新稿
      </Button>
      {acceptance.pending && (
        <Button disabled={acceptance.busy} onClick={() => void acceptance.reconcile()}>
          核对原采纳操作
        </Button>
      )}
      <details>
        <summary>自评或更正参考分</summary>
        <label>
          评价身份
          <select
            value={origin}
            onChange={(event) => setOrigin(event.target.value as typeof origin)}
          >
            <option value="user_override">用户更正</option>
            <option value="user_self">独立自评</option>
          </select>
        </label>
        <label>
          用户参考分
          <input
            type="number"
            min={0}
            max={round.draft.essay.rubric?.max_total ?? 100}
            value={total}
            onChange={(event) => setTotal(event.target.value)}
          />
        </label>
        <label>
          评价理由
          <textarea value={reason} onChange={(event) => setReason(event.target.value)} />
        </label>
        <Button
          disabled={assessment.disabled || !reason.trim()}
          onClick={() =>
            void assessment.execute(path + "/assessments", {
              expected_revision: round.assessment_revision,
              origin,
              total: total === "" ? null : Number(total),
              reason,
            })
          }
        >
          保存用户评价
        </Button>
        {assessment.pending && (
          <Button onClick={() => void assessment.reconcile()}>核对原作文评价</Button>
        )}
      </details>
      <details>
        <summary>评分历史</summary>
        {round.assessments.map((item) => (
          <p key={item.assessment_id}>
            {item.origin === "ai_reference"
              ? "AI参考"
              : item.origin === "user_self"
                ? "用户自评"
                : "用户更正"}
            ：{item.total ?? "待判断"} / {item.max_total} · {item.reason}
          </p>
        ))}
      </details>
      {sessionId && round.feedback_artifact_id && (
        <div className="workbench-toolbar">
          <Button disabled={derivatives.disabled} onClick={() => void generate("question_set")}>
            从本轮原稿与批改生成练习
          </Button>
          <Button disabled={derivatives.disabled} onClick={() => void generate("card_set")}>
            从本轮原稿与批改生成卡片
          </Button>
        </div>
      )}
      {derivatives.pending && (
        <Button onClick={() => void derivatives.reconcile()}>核对原生成任务</Button>
      )}
      {[notice, assessment.notice, acceptance.notice, derivatives.notice]
        .filter(Boolean)
        .map((notice, index) => (
          <p key={index} role="status">
            {notice}
          </p>
        ))}
    </article>
  );
}

function AnnotatedEssay({
  text,
  annotations,
}: {
  text: string;
  annotations: { start: number; end: number; comment: string }[];
}) {
  const codepoints = Array.from(text);
  const ordered = annotations
    .filter(
      (item) =>
        Number.isSafeInteger(item.start) &&
        Number.isSafeInteger(item.end) &&
        item.start >= 0 &&
        item.end >= item.start &&
        item.end <= codepoints.length,
    )
    .sort((a, b) => a.start - b.start || a.end - b.end);
  const pieces = [];
  let position = 0;
  for (const [index, annotation] of ordered.entries()) {
    if (annotation.start < position) continue;
    pieces.push(
      <span key={`text_${index}`}>{codepoints.slice(position, annotation.start).join("")}</span>,
    );
    pieces.push(
      <mark key={`mark_${index}`} title={annotation.comment}>
        {codepoints.slice(annotation.start, annotation.end).join("") || "▏"}
      </mark>,
    );
    position = annotation.end;
  }
  pieces.push(<span key="last">{codepoints.slice(position).join("")}</span>);
  return <div className="essay-original">{pieces}</div>;
}

export function EssayFeedbackPanel({
  courseId,
  roundId,
  sessionId,
  onTaskCreated,
  onOpenArtifact,
}: {
  courseId: string;
  roundId: string;
  sessionId?: string;
  onTaskCreated?: (task: Schema<"TaskView">) => void;
  onOpenArtifact?: (artifactId: string) => void;
}) {
  const [round, setRound] = useState<Schema<"EssayRound"> | null>(null);
  const [current, setCurrent] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    let live = true;
    void api
      .read<Schema<"EssayRound">>(`/courses/${courseId}/learning/essay-rounds/${roundId}`)
      .then(async (value) => {
        const artifact = await api.read<Schema<"ArtifactView">>(
          `/courses/${courseId}/learning/artifacts/${value.artifact_id}`,
        );
        if (live) {
          setRound(value);
          setCurrent(artifact.head_revision_id);
        }
      })
      .catch((error) => {
        if (live) setNotice(errorMessage(error));
      });
    return () => {
      live = false;
    };
  }, [courseId, roundId]);
  return (
    <>
      {round && (
        <>
          <Button variant="ghost" onClick={() => onOpenArtifact?.(round.artifact_id)}>
            回到作文原稿
          </Button>
          <EssayRoundDetail
            initial={round}
            currentRevision={current}
            sessionId={sessionId}
            onTaskCreated={onTaskCreated}
            onRevised={(receipt) => onOpenArtifact?.(receipt.artifact.artifact_id)}
          />
        </>
      )}
      {notice && <p role="status">{notice}</p>}
    </>
  );
}
