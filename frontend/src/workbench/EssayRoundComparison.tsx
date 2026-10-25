import { useEffect, useMemo, useState } from "react";
import { api, ApiError, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { MarkdownAnswer } from "../chat/MarkdownAnswer";
import { roles, states } from "./model";

type Round = Schema<"EssayRound">;
type Summary = Schema<"EssayRoundSummary">;

function difference(left: string, right: string) {
  const a = [...left],
    b = [...right];
  let start = 0,
    suffix = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  while (
    suffix < a.length - start &&
    suffix < b.length - start &&
    a[a.length - suffix - 1] === b[b.length - suffix - 1]
  )
    suffix++;
  return { start, leftEnd: a.length - suffix, rightEnd: b.length - suffix, same: left === right };
}

function Draft({ text, start, end }: { text: string; start: number; end: number }) {
  const chars = [...text];
  return (
    <pre className="essay-comparison-draft">
      {chars.slice(0, start).join("")}
      {end > start && <mark>{chars.slice(start, end).join("")}</mark>}
      {chars.slice(end).join("")}
    </pre>
  );
}

function RoundColumn({ round, start, end }: { round: Round; start: number; end: number }) {
  const reference = round.assessments.find(
    (item) =>
      item.assessment_id === round.reference_assessment_id && item.origin === "ai_reference",
  );
  const user = round.assessments.find((item) => item.assessment_id === round.user_assessment_id);
  const annotations = (
    Array.isArray(round.result?.annotations) ? round.result.annotations : []
  ) as {
    quote: string;
    replacement: string;
    comment: string;
  }[];
  return (
    <article aria-label={`第${round.round_number}轮对照内容`}>
      <h4>
        第{round.round_number}轮 · {states[round.state] ?? round.state}
      </h4>
      <p>
        {roles[round.content_role]} · {new Date(round.created_at).toLocaleString("zh-CN")}
      </p>
      <h5>本轮实际稿</h5>
      <Draft text={round.draft.markdown} start={start} end={end} />
      <p>
        AI参考评分：{reference?.total ?? "待判断"} /{" "}
        {reference?.max_total ?? round.draft.essay.rubric?.max_total ?? 100}
      </p>
      {user && (
        <p>
          {user.origin === "user_self" ? "用户自评" : "用户更正"}：{user.total ?? "待判断"} /{" "}
          {user.max_total} · {user.reason}
        </p>
      )}
      <details>
        <summary>本轮题目与评分标准</summary>
        <MarkdownAnswer text={round.draft.essay.topic ?? ""} />
        <MarkdownAnswer text={round.draft.essay.rubric?.description ?? ""} />
      </details>
      <h5>本轮参考批改</h5>
      {round.result?.status === "pending" && (
        <p>此轮评分或批注尚不能可靠使用，保留原输出供核对。</p>
      )}
      {typeof round.result?.raw_feedback === "string" ? (
        <MarkdownAnswer text={round.result.raw_feedback} />
      ) : (
        <p>此轮尚无可用完整批改，保留实际稿和任务状态。</p>
      )}
      {annotations.length > 0 && (
        <ul aria-label="本轮批注">
          {annotations.map((item, index) => (
            <li key={index}>
              <mark>{item.quote || "插入位置"}</mark> → {item.replacement || "删除此处"}：
              {item.comment}
            </li>
          ))}
        </ul>
      )}
      <details>
        <summary>本轮评分历史</summary>
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
    </article>
  );
}

export function EssayRoundComparison({
  courseId,
  artifactId,
  rounds,
}: {
  courseId: string;
  artifactId: string;
  rounds: Summary[];
}) {
  const [leftId, setLeftId] = useState(rounds[1]?.round_id ?? "");
  const [rightId, setRightId] = useState(rounds[0]?.round_id ?? "");
  const [pair, setPair] = useState<{ owner: string; left: Round; right: Round } | null>(null);
  const [error, setError] = useState<{ owner: string; message: string } | null>(null);
  const owner = `${courseId}:${artifactId}:${leftId}:${rightId}`;
  const notice = error?.owner === owner ? error.message : "";
  useEffect(() => {
    let live = true;
    if (!leftId || !rightId || leftId === rightId) return;
    const base = `/courses/${courseId}/learning/essay-rounds/`;
    void Promise.all([api.read<Round>(base + leftId), api.read<Round>(base + rightId)])
      .then(([left, right]) => {
        if (!live) return;
        if (
          [left, right].some(
            (round) => round.course_id !== courseId || round.artifact_id !== artifactId,
          ) ||
          left.round_id !== leftId ||
          right.round_id !== rightId
        )
          throw new ApiError("ESSAY_COMPARISON_SCOPE", "批改轮次与指定作文不符");
        setPair({ owner, left, right });
        setError(null);
      })
      .catch((reason) => {
        if (live) setError({ owner, message: errorMessage(reason) });
      });
    return () => {
      live = false;
    };
  }, [courseId, artifactId, leftId, rightId, owner]);
  const current = pair?.owner === owner ? pair : null;
  const changed = useMemo(
    () => (current ? difference(current.left.draft.markdown, current.right.draft.markdown) : null),
    [current],
  );
  const sameStandard =
    current &&
    current.left.draft.essay.topic === current.right.draft.essay.topic &&
    JSON.stringify(current.left.draft.essay.rubric) ===
      JSON.stringify(current.right.draft.essay.rubric);
  return (
    <section className="essay-comparison" aria-label="两轮作文对照">
      <h3>两轮对照</h3>
      {(["左侧轮次", "右侧轮次"] as const).map((label, index) => (
        <label key={label}>
          {label}
          <select
            value={index === 0 ? leftId : rightId}
            onChange={(event) => (index === 0 ? setLeftId : setRightId)(event.target.value)}
          >
            {rounds.map((round) => (
              <option key={round.round_id} value={round.round_id}>
                第{round.round_number}轮
              </option>
            ))}
          </select>
        </label>
      ))}
      {leftId === rightId ? (
        <p>请选择两个不同轮次。</p>
      ) : (
        !current && !notice && <p role="status">正在读取两轮已保存的实际稿与批改。</p>
      )}
      {current && changed && (
        <>
          <p>
            {changed.same ? "两轮使用相同文字稿。" : "高亮从两稿开始分歧到再次一致的文字范围。"}
          </p>
          {!sameStandard && <p>题目或评分标准不同，两轮分数不能直接比较。</p>}
          <p>各轮评分与批注保留原稿归属；对照不采纳建议或记录学习成绩。</p>
          <div className="essay-comparison-columns">
            <RoundColumn round={current.left} start={changed.start} end={changed.leftEnd} />
            <RoundColumn round={current.right} start={changed.start} end={changed.rightEnd} />
          </div>
        </>
      )}
      {notice && <p role="alert">{notice}</p>}
    </section>
  );
}
