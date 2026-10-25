import { useId, useState } from "react";
import { MarkdownAnswer } from "../chat/MarkdownAnswer";
import { ContentBlock } from "../workbench/ContentBlock";
import type { OfflineBlock, OfflineDocument } from "./model";
import "./offline.css";

const roles: Record<string, string> = {
  original: "原始资料",
  user_note: "用户笔记",
  ai_generated: "AI生成",
  mixed: "混合修订",
};
const attributions: Record<string, string> = {
  source_quote: "原文引述",
  source_paraphrase: "资料归纳",
  source_inference: "依据资料的推断",
  ai_supplement: "AI补充",
  user_authored: "用户编写",
};

export function OfflineReader({ document }: { document: OfflineDocument }) {
  const pages = document.chapters.flatMap((chapter) => chapter.pages);
  const [pageId, setPageId] = useState(pages[0]?.id ?? "");
  const current = pages.find((page) => page.id === pageId);
  const visible = current
    ? document.blocks.filter((block) => current.block_ids.includes(block.block_id))
    : document.blocks;
  const position = pages.findIndex((page) => page.id === pageId);
  return (
    <main className="offline-reader">
      <header>
        <small>学习成果 · 独立离线版</small>
        <h1>{document.title}</h1>
        <p>{roles[document.role] ?? document.role}</p>
        <p role="status">{document.coverage}</p>
        {document.notices?.map((text) => (
          <p key={text} className="offline-scope-note">
            {text}
          </p>
        ))}
        <p className="offline-muted">
          自测仅在本页反馈。关闭或刷新后清空，不回传正式成绩与复习安排。
        </p>
      </header>
      <div className={pages.length ? "offline-book" : "offline-content"}>
        {pages.length > 0 && (
          <nav aria-label="交互书目录">
            {document.chapters.map((chapter) => (
              <section key={chapter.id}>
                <h2>{chapter.title}</h2>
                <p>{chapter.objective}</p>
                {chapter.pages.length === 0 && <p>此修订尚无已保存页。</p>}
                {chapter.pages.map((page) => (
                  <button
                    key={page.id}
                    aria-current={page.id === pageId ? "page" : undefined}
                    onClick={() => setPageId(page.id)}
                  >
                    {page.title}
                  </button>
                ))}
              </section>
            ))}
          </nav>
        )}
        <article aria-label="离线成果正文">
          {current && <h2>{current.title}</h2>}
          {visible.map((block) => (
            <OfflineContent key={block.block_id} block={block} document={document} />
          ))}
          {pages.length > 1 && (
            <div className="offline-pagination">
              <button disabled={position <= 0} onClick={() => setPageId(pages[position - 1].id)}>
                上一页
              </button>
              <span>
                {position + 1} / {pages.length}
              </span>
              <button
                disabled={position >= pages.length - 1}
                onClick={() => setPageId(pages[position + 1].id)}
              >
                下一页
              </button>
            </div>
          )}
        </article>
      </div>
      <footer>
        <details>
          <summary>导出版本与来源说明</summary>
          <p>保存时间：{document.exported_at}</p>
          <p>成果修订摘要：{document.revision_hash}</p>
          <p>包内保留本版引用的文字／图像快照。完整原件不随包附带，回看原件须另行打开。</p>
          <a href="./manifest.json">资源完整性清单</a> · <a href="./notices.txt">开源许可</a>
        </details>
      </footer>
    </main>
  );
}

function OfflineContent({ block, document }: { block: OfflineBlock; document: OfflineDocument }) {
  return (
    <section className="offline-block">
      {document.block_notices?.[block.block_id] && (
        <p className="offline-scope-note">{document.block_notices[block.block_id]}</p>
      )}
      {block.kind === "question" ? (
        <OfflineQuestion block={block} />
      ) : block.kind === "card" ? (
        <OfflineCard block={block} />
      ) : (
        <ContentBlock block={block} />
      )}
      {block.kind === "essay_feedback" && block.feedback.annotated_text && (
        <details>
          <summary>原批注文本</summary>
          <pre>{block.feedback.annotated_text}</pre>
        </details>
      )}
      {block.kind === "essay_draft" && (
        <details>
          <summary>作文题目与评价要求</summary>
          <MarkdownAnswer text={block.essay.topic} />
          <p>{block.essay.rubric?.description}</p>
          {block.essay.rubric?.dimensions?.map((dimension) => (
            <p key={dimension.name}>
              {dimension.name}：{dimension.criteria}（满分{dimension.max_score}）
            </p>
          ))}
        </details>
      )}
      {block.kind === "path_step" && (
        <OfflinePath
          block={block}
          questions={document.linked_questions[block.block_id] ?? []}
          document={document}
        />
      )}
      <small className="offline-muted">{attributions[block.attribution]}</small>
      {(block.source_snapshot_ids ?? []).map((id, index) => {
        const source = document.sources.find((item) => item.id === id);
        return source ? (
          <details key={id} className="offline-source">
            <summary>
              来源 {index + 1} · {source.label} · {roles[source.role] ?? source.role}
            </summary>
            {source.image && /^assets\/[a-f0-9]{64}\.(?:png|jpeg|webp)$/.test(source.image) && (
              <img src={source.image} alt={source.label + " · 已保存的授权图像"} />
            )}
            {source.text && <MarkdownAnswer text={source.text} />}
            <details>
              <summary>冻结位置与版本</summary>
              <pre>{JSON.stringify(source.location, null, 2)}</pre>
            </details>
          </details>
        ) : null;
      })}
    </section>
  );
}

function OfflineQuestion({ block }: { block: Extract<OfflineBlock, { kind: "question" }> }) {
  const groupId = useId();
  const [selected, setSelected] = useState<string[]>([]);
  const [answer, setAnswer] = useState("");
  const [checked, setChecked] = useState(false);
  const question = block.question;
  const options = question.options ?? [];
  const choice = ["single_choice", "multiple_choice"].includes(question.question_type);
  const reference = question.reference_answer.trim().toUpperCase();
  const expected = reference
    .split(/[,，、\s]+/)
    .filter(Boolean)
    .sort();
  const deterministic =
    choice &&
    expected.length > 0 &&
    expected.every((id) => options.some((option) => option.option_id === id));
  const matches = JSON.stringify([...selected].sort()) === JSON.stringify(expected);
  return (
    <section aria-label="离线自测">
      <small>
        {question.answer_authority === "user_confirmed" ? "用户确认的参考答案" : "AI参考练习"}
      </small>
      <MarkdownAnswer text={block.markdown} />
      {choice ? (
        options.map((option) => (
          <label className="offline-option" key={option.option_id}>
            <input
              type={question.question_type === "multiple_choice" ? "checkbox" : "radio"}
              name={groupId}
              checked={selected.includes(option.option_id)}
              onChange={(event) => {
                setChecked(false);
                setSelected(
                  question.question_type === "single_choice"
                    ? [option.option_id]
                    : event.target.checked
                      ? [...selected, option.option_id]
                      : selected.filter((id) => id !== option.option_id),
                );
              }}
            />
            {option.option_id}. {option.text}
          </label>
        ))
      ) : (
        <textarea
          aria-label="本次离线作答"
          value={answer}
          onChange={(event) => {
            setAnswer(event.target.value);
            setChecked(false);
          }}
        />
      )}
      <button
        onClick={() => setChecked(true)}
        disabled={choice ? !selected.length : !answer.trim()}
      >
        对照参考答案
      </button>
      {checked && (
        <div role="status">
          <p>
            {question.answer_authority === "missing"
              ? "没有可靠参考答案，留待判断。"
              : deterministic
                ? matches
                  ? "与参考选项一致。"
                  : "与参考选项不一致，请核对解析。"
                : "请自行对照理由与条件，本页不作正式判分。"}
          </p>
          <MarkdownAnswer text={question.reference_answer} />
          <MarkdownAnswer text={question.explanation} />
        </div>
      )}
    </section>
  );
}

function OfflineCard({ block }: { block: Extract<OfflineBlock, { kind: "card" }> }) {
  const [flipped, setFlipped] = useState(false);
  const [recall, setRecall] = useState("");
  return (
    <section aria-label="离线回忆卡片">
      <MarkdownAnswer text={block.markdown} />
      <button onClick={() => setFlipped((value) => !value)}>
        {flipped ? "收起背面" : "翻面核对"}
      </button>
      {flipped && (
        <>
          <MarkdownAnswer text={block.card.back} />
          <button onClick={() => setRecall("本次标为已回忆，不改变正式复习安排。")}>已回忆</button>
          <button onClick={() => setRecall("本次标为再想一遍，不改变正式复习安排。")}>
            再想一遍
          </button>
          {recall && <p role="status">{recall}</p>}
        </>
      )}
    </section>
  );
}

function OfflinePath({
  block,
  questions,
  document,
}: {
  block: Extract<OfflineBlock, { kind: "path_step" }>;
  questions: OfflineBlock[];
  document: OfflineDocument;
}) {
  const [action, setAction] = useState("");
  return (
    <section aria-label="离线路线回忆">
      <button onClick={() => setAction("已在本页跳过，可随时返回；不记掌握。")}>本次跳过</button>
      {action && <p role="status">{action}</p>}
      {questions
        .filter((question) => question.kind === "question")
        .map((question) => (
          <OfflineContent key={question.block_id} block={question} document={document} />
        ))}
      <small>{block.step.title}的顺序仅作建议。</small>
    </section>
  );
}
