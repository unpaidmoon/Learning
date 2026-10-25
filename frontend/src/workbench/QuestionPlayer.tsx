import { useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { MarkdownAnswer } from "../chat/MarkdownAnswer";
import { Button } from "../components/ui/button";
import { saveWorkbenchDraft } from "./drafts";
import { localDraft } from "./model";
import { useWorkbenchOperation } from "./useWorkbenchOperation";

const verdictLabels = {
  correct: "正确",
  wrong: "错误",
  partial: "部分正确",
  pending: "待判断",
};
const authors = {
  deterministic: "确定性比较",
  ai_reference: "AI参考评价",
  user_override: "用户更正",
};

export function QuestionPlayer({
  courseId,
  artifactId,
  revisionId,
  block,
  sessionId,
  onTaskCreated,
  reviewSession,
  onAnswered,
}: {
  courseId: string;
  artifactId: string;
  revisionId: string;
  block: Schema<"PracticeQuestion">["block"];
  sessionId?: string;
  onTaskCreated?: (task: Schema<"TaskView">) => void;
  reviewSession?: Schema<"ReviewSession">;
  onAnswered?: () => void;
}) {
  const key = `${courseId}.answer.${artifactId}.${revisionId}.${block.block_id}.${reviewSession?.session_id ?? "practice"}`;
  const [saved] = useState(() => localDraft<{ text: string }>(key));
  const [text, setText] = useState(saved.value?.text ?? "");
  const [actual, setActual] = useState<Schema<"AnswerView"> | null>(null);
  const [history, setHistory] = useState<Schema<"AnswerView">[]>([]);
  const [judgments, setJudgments] = useState<Schema<"AssessmentView">[]>([]);
  const [reason, setReason] = useState("");
  const [verdict, setVerdict] = useState<Schema<"UserVerdict">["verdict"]>("pending");
  const [explanation, setExplanation] = useState(false);
  const [notice, setNotice] = useState(saved.error);
  const [referenceTask, setReferenceTask] = useState<Schema<"TaskView"> | null>(null);
  const base = `/courses/${courseId}/learning`;
  const submission = useWorkbenchOperation<Schema<"AnswerView">>(
    key + ".submit",
    courseId,
    "practice",
    (value) => {
      setActual(value);
      onAnswered?.();
    },
  );
  const correction = useWorkbenchOperation<Schema<"AnswerView">>(
    key + ".override",
    courseId,
    "practice",
    setActual,
  );
  const reference = useWorkbenchOperation<Schema<"TaskView">>(
    key + ".reference",
    courseId,
    "task",
    (task) => {
      setReferenceTask(task);
      onTaskCreated?.(task);
    },
  );
  useEffect(() => {
    if (reviewSession) return;
    let live = true;
    void api
      .read<Schema<"AnswerHistory">>(
        `${base}/artifacts/${artifactId}/answers?block_id=${encodeURIComponent(block.block_id)}&limit=1`,
      )
      .then((page) => {
        if (live && page.answers[0]?.question_revision_id === revisionId)
          setActual(page.answers[0]);
      })
      .catch((error) => {
        if (live) setNotice(errorMessage(error));
      });
    return () => {
      live = false;
    };
  }, [base, artifactId, block.block_id, revisionId, reviewSession]);
  const answerId = actual?.answer_id;
  useEffect(() => {
    if (!referenceTask || !answerId) return;
    let live = true;
    const timer = setInterval(() => {
      void api
        .read<Schema<"AnswerView">>(`${base}/answers/${answerId}`)
        .then((value) => {
          if (live) setActual(value);
        })
        .catch((error) => {
          if (live) setNotice(errorMessage(error));
        });
    }, 2500);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [referenceTask, answerId, base]);
  const loadHistory = async () => {
    try {
      const page = await api.read<Schema<"AnswerHistory">>(
        `${base}/artifacts/${artifactId}/answers?block_id=${encodeURIComponent(block.block_id)}`,
      );
      setHistory(page.answers);
      if (actual)
        setJudgments(
          (
            await api.read<Schema<"AssessmentHistory">>(
              `${base}/answers/${actual.answer_id}/assessments`,
            )
          ).assessments,
        );
    } catch (error) {
      setNotice(errorMessage(error));
    }
  };
  return (
    <section className="practice-question" aria-label="题目作答">
      <small>
        {block.attribution === "user_authored" ? "用户编写的练习" : "AI参考练习 · 资料引用表示依据"}
      </small>
      <MarkdownAnswer text={block.markdown} />
      {(block.question.options ?? []).map((option) => (
        <p key={option.option_id}>
          <strong>{option.option_id}.</strong> {option.text}
        </p>
      ))}
      <label>
        实际作答
        <textarea
          value={text}
          disabled={submission.disabled}
          aria-label="实际作答"
          onChange={(event) => {
            setText(event.target.value);
            try {
              if (!saved.error) saveWorkbenchDraft(key, { text: event.target.value });
            } catch (error) {
              setNotice(errorMessage(error));
            }
          }}
        />
      </label>
      <Button
        disabled={
          submission.disabled ||
          !text.trim() ||
          !!saved.error ||
          reviewSession?.state === "answered"
        }
        onClick={() =>
          void submission.execute(`${base}/artifacts/${artifactId}/answers`, {
            question_revision_id: revisionId,
            block_id: block.block_id,
            text,
            ...(reviewSession
              ? {
                  review_session_id: reviewSession.session_id,
                  expected_review_revision: reviewSession.schedule_revision,
                }
              : {}),
          })
        }
      >
        {actual ? "提交新的作答，保留上次记录" : "提交作答"}
      </Button>
      {submission.pending && (
        <Button disabled={submission.busy} onClick={() => void submission.reconcile()}>
          核对原作答提交
        </Button>
      )}
      {actual && (
        <aside aria-label="有效评价">
          <p>已提交的实际作答：{actual.text}</p>
          <strong>
            {verdictLabels[actual.effective_assessment.verdict]} ·{" "}
            {authors[actual.effective_assessment.author]}
          </strong>
          <MarkdownAnswer text={actual.effective_assessment.reason} />
          {sessionId && (
            <Button
              disabled={reference.disabled}
              onClick={() =>
                void reference.execute(
                  `/desktop/sessions/${encodeURIComponent(sessionId)}/learning-task-intents`,
                  {
                    kind: "answer_review",
                    instruction:
                      "按实际题目版本和实际作答给参考判分、理由和具体问题；无法可靠判断时保持待判断。",
                    target: { mode: "create", title: "作答参考评价" },
                    sources: [],
                    answer_review: {
                      answer_id: actual.answer_id,
                      expected_revision: actual.assessment_revision,
                    },
                  },
                )
              }
            >
              AI参考判分
            </Button>
          )}
          {reference.pending && (
            <Button disabled={reference.busy} onClick={() => void reference.reconcile()}>
              核对原判分任务
            </Button>
          )}
          {referenceTask && <p>参考评价已提交，可在任务中停止或核对。</p>}
          <details>
            <summary>更正此判定</summary>
            <label>
              用户判定
              <select
                value={verdict}
                onChange={(event) => setVerdict(event.target.value as typeof verdict)}
              >
                {Object.entries(verdictLabels).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              更正理由
              <textarea value={reason} onChange={(event) => setReason(event.target.value)} />
            </label>
            <Button
              disabled={correction.disabled || !reason.trim()}
              onClick={() =>
                void correction.execute(`${base}/answers/${actual.answer_id}/override`, {
                  expected_revision: actual.assessment_revision,
                  verdict,
                  reason,
                })
              }
            >
              保存改判并调整后续复习
            </Button>
            {correction.pending && (
              <Button onClick={() => void correction.reconcile()}>核对原改判</Button>
            )}
          </details>
        </aside>
      )}
      <Button variant="ghost" onClick={() => setExplanation(!explanation)}>
        查看参考答案与解析
      </Button>
      <Button variant="ghost" onClick={() => void loadHistory()}>
        作答与评价历史
      </Button>
      {explanation && (
        <aside>
          <p>
            {block.question.answer_authority === "user_confirmed"
              ? "已确认参考答案"
              : "AI参考答案或待确认内容"}
          </p>
          <MarkdownAnswer text={block.question.reference_answer || "尚无参考答案"} />
          <MarkdownAnswer text={block.question.explanation} />
        </aside>
      )}
      {!!history.length && (
        <details open>
          <summary>实际作答历史</summary>
          {history.map((item) => (
            <p key={item.answer_id}>
              第{item.sequence}次：{item.text} · {verdictLabels[item.effective_assessment.verdict]}
              {item.question_revision_id !== revisionId ? " · 旧题目版本" : ""}
            </p>
          ))}
        </details>
      )}
      {!!judgments.length && (
        <details open>
          <summary>本次作答的评价历史</summary>
          {judgments.map((item) => (
            <p key={item.assessment_id}>
              {authors[item.author]}：{verdictLabels[item.verdict]} · {item.reason}
              {item.evidence.disposition === "stale" ? "（保留为迟到结果）" : ""}
            </p>
          ))}
        </details>
      )}
      {[notice, submission.notice, correction.notice, reference.notice]
        .filter(Boolean)
        .map((message, index) => (
          <p key={index} role="status">
            {message}
          </p>
        ))}
    </section>
  );
}
