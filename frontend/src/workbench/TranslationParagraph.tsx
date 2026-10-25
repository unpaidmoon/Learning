import type { Schema } from "../api/client";
import { MarkdownAnswer } from "../chat/MarkdownAnswer";
import "./translation.css";

type Block = Extract<
  Schema<"ArtifactView">["revision"]["body"]["blocks"][number],
  { kind: "translation" }
>;

export function TranslationParagraph({ block }: { block: Block }) {
  return (
    <section className="translation-paragraph" aria-label="段落对照">
      <TranslationPair sourceText={block.translation.source_text} translation={block.markdown} />
    </section>
  );
}

export function TranslationPair({
  sourceText,
  translation,
  unverified = false,
}: {
  sourceText: string;
  translation: string;
  unverified?: boolean;
}) {
  return (
    <div className="translation-columns">
      <section aria-label={unverified ? "待核原文" : "原文"}>
        <h4>{unverified ? "待核原文" : "原文"}</h4>
        <MarkdownAnswer text={sourceText} />
      </section>
      <section aria-label={unverified ? "译文草稿" : "译文"}>
        <h4>{unverified ? "译文草稿" : "译文"}</h4>
        <MarkdownAnswer text={translation} />
      </section>
    </div>
  );
}
