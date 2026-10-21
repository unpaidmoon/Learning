import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MarkdownAnswer } from "../src/chat/MarkdownAnswer";
import { quoteFromRange } from "../src/chat/selection";

afterEach(() => {
  window.getSelection()?.removeAllRanges();
  cleanup();
});

function rangeBetween(start: Text, from: number, end = start, to = end.length) {
  const range = document.createRange();
  range.setStart(start, from);
  range.setEnd(end, to);
  const selection = window.getSelection()!;
  selection.removeAllRanges();
  selection.addRange(range);
  return range;
}

function visibleText(element: Element, value: string) {
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
  let next: Node | null;
  while ((next = walker.nextNode())) {
    if (next.textContent === value && !next.parentElement?.closest(".katex-mathml"))
      return next as Text;
  }
  throw new Error(`Missing visible text ${value}`);
}

describe("鼠标真实选区引用", () => {
  it("普通中文选区只浮出入口，点击才引用所选范围且保留UTF-16来源", () => {
    const onQuote = vi.fn();
    const view = render(<MarkdownAnswer text="第一步😀检查条件，再进行计算。" onQuote={onQuote} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const node = visibleText(root, "第一步😀检查条件，再进行计算。");
    rangeBetween(node, 5, node, 9);
    fireEvent(document, new Event("selectionchange"));
    expect(onQuote).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "添加到对话" }));
    expect(onQuote).toHaveBeenCalledWith({
      text: "检查条件",
      markdown: "检查条件",
      source_start: 5,
      source_end: 9,
    });
    expect(screen.queryByRole("button", { name: "添加到对话" })).toBeNull();
  });

  it("只选公式中的x平方，不能引用其余等式", () => {
    const source = "得到 $x^2+1=2$。";
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const math = root.querySelector(".katex-html")!;
    expect(math, root.innerHTML).not.toBeNull();
    const x = visibleText(math, "x");
    const squared = visibleText(math, "2");
    const range = rangeBetween(x, 0, squared, 1);
    expect(quoteFromRange(root, range, source)).toEqual({
      quote: {
        text: "x2",
        markdown: "$x^2$",
        source_start: 4,
        source_end: 7,
      },
    });
    expect(root.querySelector(".formula-actions")).toBeNull();
  });

  it("分式中只选分子时保留分子，不补入分母", () => {
    const source = "比值 $\\frac{x+1}{y^2}$";
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const math = root.querySelector(".katex-html")!;
    const x = visibleText(math, "x");
    const one = visibleText(math, "1");
    const result = quoteFromRange(root, rangeBetween(x, 0, one, 1), source);
    expect(result.quote?.markdown).toBe("$x+1$");
    expect(result.quote?.markdown).not.toContain("y");
  });

  it("完整分式和跨段文字公式保留原始连续源码", () => {
    const source = "先看条件。\n\n使用 $\\frac{x+1}{y^2}$ 继续推导。";
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const formula = root.querySelector<HTMLElement>(".selection-formula")!;
    const range = document.createRange();
    range.selectNodeContents(formula);
    expect(quoteFromRange(root, range, source).quote?.markdown).toBe("$\\frac{x+1}{y^2}$");
    const start = visibleText(root, "先看条件。");
    const end = visibleText(root, " 继续推导。");
    const result = quoteFromRange(root, rangeBetween(start, 0, end, end.length), source);
    expect(result.quote?.markdown).toBe(source);
    expect(result.quote?.source_start).toBe(0);
    expect(result.quote?.source_end).toBe(source.length);
  });

  it("标准LaTeX定界符归一化后仍定位原始回答", () => {
    const source = "结果 \\(x^2+1\\) 成立。";
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const math = root.querySelector(".katex-html")!;
    const x = visibleText(math, "x");
    const squared = visibleText(math, "2");
    const result = quoteFromRange(root, rangeBetween(x, 0, squared, 1), source);
    expect(result.quote).toEqual({ text: "x2", markdown: "$x^2$", source_start: 5, source_end: 8 });
  });

  it("只选根号下的字母不能因为选完所有文本而补入根号", () => {
    const source = "结果 $\\sqrt{x}$";
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const x = visibleText(root.querySelector(".katex-html")!, "x");
    expect(quoteFromRange(root, rangeBetween(x, 0, x, 1), source).quote?.markdown).toBe("$x$");
  });

  it("跨分子和分母的破碎源码拒绝引用，不补成整个分式", () => {
    const source = "比值 $\\frac{x+1}{y^2}$";
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const math = root.querySelector(".katex-html")!;
    const nodes = Array.from(math.querySelectorAll<HTMLElement>("[data-selection-start]"));
    const first = nodes[0].firstChild as Text;
    const last = nodes[nodes.length - 1].firstChild as Text;
    const result = quoteFromRange(root, rangeBetween(first, 0, last, last.length), source);
    expect(result.quote).toBeUndefined();
    expect(result.error).toContain("FORMULA_SELECTION_UNMAPPED");
  });

  it("外部图片、原始HTML和用户注入的KaTeX HTML扩展不获得信任", () => {
    const view = render(
      <MarkdownAnswer
        text={
          "![远图](https://example.invalid/a.png)\n\n<script>alert(1)</script>\n\n$\\htmlData{selection-key=forged,selection-start=0,selection-end=1}{x}$\n\n$\\href{https://example.invalid}{x}$"
        }
      />,
    );
    expect(view.container.querySelector('img[src^="https:"]')).toBeNull();
    expect(view.container.querySelector("script")).toBeNull();
    expect(view.container.querySelector('[data-selection-key="forged"]')).toBeNull();
    expect(view.container.querySelector('a[href="https://example.invalid"]')).toBeNull();
  });

  it("可访问数学源码不含内部映射标记", () => {
    const view = render(<MarkdownAnswer text="结果 $x^2+1$" />);
    expect(view.container.querySelector("annotation")?.textContent).toBe("x^2+1");
    expect(view.container.querySelector("annotation")?.textContent).not.toContain("htmlData");
  });
});
