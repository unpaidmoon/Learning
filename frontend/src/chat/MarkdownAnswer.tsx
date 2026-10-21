import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";

export function MarkdownAnswer({
  text,
  onQuote,
}: {
  text: string;
  onQuote?: (latex: string) => void;
}) {
  const formulas = Array.from(
    text.matchAll(
      /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|(?<!\\)\$(?!\$)([^\n$]+?)(?<!\\)\$|\\\(([\s\S]+?)\\\)/g,
    ),
    (match) => (match[1] || match[2] || match[3] || match[4]).trim(),
  );
  // DeepSeek also emits standard LaTeX delimiters; normalize only the delimiters.
  const normalized = text
    .replace(/\\\[([\s\S]+?)\\\]/g, (_, value: string) => `\n$$${value}$$\n`)
    .replace(/\\\(([\s\S]+?)\\\)/g, (_, value: string) => `$${value}$`);
  return (
    <div className="markdown-answer">
      <Markdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[
          [rehypeKatex, { trust: false, strict: "warn", maxExpand: 500, maxSize: 20 }],
        ]}
        skipHtml
        components={{
          img: ({ alt }) => (
            <span className="blocked-image">[回答图片未自动加载：{alt || "图片"}]</span>
          ),
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {normalized}
      </Markdown>
      {onQuote && formulas.length > 0 && (
        <div className="formula-actions" aria-label="整式引用">
          {formulas.map((latex, index) => (
            <button type="button" key={`${index}:${latex}`} onClick={() => onQuote(latex)}>
              引用整条公式 {index + 1}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
