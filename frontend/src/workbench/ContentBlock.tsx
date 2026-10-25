import type { Schema } from "../api/client";
import { MarkdownAnswer } from "../chat/MarkdownAnswer";
import { VisualizationViewer } from "./VisualizationViewer";
import { TranslationParagraph } from "./TranslationParagraph";

type Block = Schema<"ArtifactView">["revision"]["body"]["blocks"][number];

export function ContentBlock({ block }: { block: Block }) {
  if (block.kind === "translation") return <TranslationParagraph block={block} />;
  return (
    <>
      {block.kind === "question" && (
        <small>
          {block.attribution === "user_authored"
            ? "用户编写的练习"
            : "AI参考练习 · 资料引用表示依据"}
        </small>
      )}
      <MarkdownAnswer text={block.markdown} />
      {block.kind === "visualization" && <VisualizationViewer envelope={block.visualization} />}
      {block.kind === "path_step" && (
        <>
          <h3>{block.step.title}</h3>
          <MarkdownAnswer text={block.step.objective} />
          <p>{block.step.recall_prompt}</p>
          <details>
            <summary>知识点讲解</summary>
            <MarkdownAnswer text={block.step.explanation} />
          </details>
        </>
      )}
      {block.kind === "card" && (
        <details>
          <summary>查看卡片背面</summary>
          <MarkdownAnswer text={block.card.back} />
        </details>
      )}
      {block.kind === "question" && (
        <>
          {(block.question.options ?? []).map((option) => (
            <p key={option.option_id}>
              {option.option_id}. {option.text}
            </p>
          ))}
          <details>
            <summary>参考答案与解析</summary>
            <MarkdownAnswer text={block.question.reference_answer} />
            <MarkdownAnswer text={block.question.explanation} />
          </details>
        </>
      )}
      {block.kind === "assessment" && <p>AI参考评价，正式作答与用户更正在对应题目中保留。</p>}
    </>
  );
}
