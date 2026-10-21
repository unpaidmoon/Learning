import { useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { createSelectionRendering, quoteFromRange, type QuoteSelection } from "./selection";
import "katex/dist/katex.min.css";

export type { QuoteSelection } from "./selection";

export function MarkdownAnswer({
  text,
  onQuote,
}: {
  text: string;
  onQuote?: (selection: QuoteSelection) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const rendering = useMemo(() => createSelectionRendering(text), [text]);
  const [selected, setSelected] = useState<{
    quote?: QuoteSelection;
    error?: string;
    left: number;
    top: number;
    sourceText: string;
  } | null>(null);
  useEffect(() => {
    if (!onQuote) return;
    const inspect = () => {
      const element = root.current;
      const selection = window.getSelection();
      if (!element || !selection || selection.isCollapsed || !selection.rangeCount) {
        setSelected(null);
        return;
      }
      const range = selection.getRangeAt(0);
      if (!element.contains(range.startContainer) || !element.contains(range.endContainer)) {
        setSelected(null);
        return;
      }
      const result = quoteFromRange(element, range, text);
      const rect = range.getBoundingClientRect?.();
      setSelected({
        ...result,
        sourceText: text,
        left: Math.max(8, Math.min(rect?.left ?? 8, window.innerWidth - 164)),
        top: Math.max(8, (rect?.top ?? 42) - 38),
      });
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelected(null);
      else inspect();
    };
    const dismiss = () => setSelected(null);
    document.addEventListener("scroll", dismiss, true);
    window.addEventListener("resize", dismiss);
    document.addEventListener("selectionchange", inspect);
    document.addEventListener("pointerup", inspect);
    document.addEventListener("mouseup", inspect);
    document.addEventListener("keyup", key);
    return () => {
      document.removeEventListener("scroll", dismiss, true);
      window.removeEventListener("resize", dismiss);
      document.removeEventListener("selectionchange", inspect);
      document.removeEventListener("pointerup", inspect);
      document.removeEventListener("mouseup", inspect);
      document.removeEventListener("keyup", key);
    };
  }, [onQuote, text]);
  // A parent polling saved generations must not reparse unchanged mathematics.
  const content = useMemo(
    () => (
      <Markdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[
          rendering.plugin,
          [
            rehypeKatex,
            {
              trust: rendering.trust,
              strict: (code: string) => (code === "htmlExtension" ? "ignore" : "warn"),
              maxExpand: 500,
              maxSize: 20,
            },
          ],
          rendering.cleanPlugin,
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
        {rendering.normalized}
      </Markdown>
    ),
    [rendering],
  );
  return (
    <div className="markdown-answer" ref={root}>
      {content}
      {onQuote && selected?.sourceText === text && selected.quote && (
        <button
          type="button"
          className="selection-quote-button"
          aria-label="添加到对话"
          style={{ position: "fixed", left: selected.left, top: selected.top, zIndex: 80 }}
          onPointerDown={(event) => event.preventDefault()}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => {
            onQuote(selected.quote!);
            window.getSelection()?.removeAllRanges();
            setSelected(null);
          }}
        >
          添加到对话
        </button>
      )}
      {onQuote && selected?.sourceText === text && selected.error && (
        <span
          className="selection-quote-error"
          role="status"
          style={{ position: "fixed", left: selected.left, top: selected.top, zIndex: 80 }}
        >
          {selected.error}
        </span>
      )}
    </div>
  );
}
