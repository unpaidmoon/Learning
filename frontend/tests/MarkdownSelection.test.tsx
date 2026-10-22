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
  it.each(["sin", "cos", "log", "ln", "lim", "max", "arctan"])(
    "常见命名算符%s的局部引用保留原命令",
    (name) => {
      const source = `得到 $\\${name} x + 1$。`;
      const view = render(<MarkdownAnswer text={source} />);
      const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
      const math = root.querySelector(".katex-html")!;
      const operator = math.querySelector(
        `[data-selection-start="${source.indexOf(`\\${name}`)}"]`,
      )!;
      const first = document.createTreeWalker(operator, NodeFilter.SHOW_TEXT).nextNode() as Text;
      const result = quoteFromRange(
        root,
        rangeBetween(first, 0, visibleText(math, "x"), 1),
        source,
      );
      expect(result.error).toBeUndefined();
      expect(result.quote?.markdown).toBe(`$\\${name} x$`);
    },
  );

  it("只选det中的de仍拒绝，不把未选的t补入引用", () => {
    const source = String.raw`$\det M + 1$`;
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const det = visibleText(root.querySelector(".katex-html")!, "det");
    const result = quoteFromRange(root, rangeBetween(det, 0, det, 2), source);
    expect(result.quote).toBeUndefined();
    expect(result.error).toContain("FORMULA_SELECTION_UNMAPPED");
  });

  it("上下分式内的重复det按原始词法位置映射，不按可见DOM次序猜测", () => {
    const source = String.raw`$\frac{\det A}{\det B}+1$`;
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const start = source.lastIndexOf(String.raw`\det`);
    const op = root.querySelector(`.katex-html [data-selection-start="${start}"]`)!;
    const det = document.createTreeWalker(op, NodeFilter.SHOW_TEXT).nextNode() as Text;
    const b = visibleText(root.querySelector(".katex-html")!, "B");
    const result = quoteFromRange(root, rangeBetween(det, 0, b, 1), source);
    expect(result.quote?.markdown).toBe(String.raw`$\det B$`);
    expect(result.quote?.source_start).toBe(start);
  });

  it("注释中的同名命令不映射成可见算符，标准定界符及UTF16位置保持", () => {
    const latex = "\\det M % \\det hidden\n + \\det A";
    const source = `😀结果\\[\n${latex}\n\\]。`;
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const start = source.lastIndexOf(String.raw`\det`);
    const op = root.querySelector(`.katex-html [data-selection-start="${start}"]`)!;
    const det = visibleText(op, "det");
    const a = visibleText(root.querySelector(".katex-html")!, "A");
    const result = quoteFromRange(root, rangeBetween(det, 0, a, 1), source);
    expect(result.quote?.markdown).toBe(String.raw`$\det A$`);
    expect(result.quote?.source_start).toBe(start);
    expect(result.quote?.source_end).toBe(start + String.raw`\det A`.length);
  });

  it("截图中的局部行列式链能从第一个det引用到第三个det而不加入B", () => {
    const latex = String.raw`\det M = \det A \det B = 3\cdot(-1)=-3.`;
    const source = `合成 $${latex}$ 后文`;
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const nodes: Text[] = [];
    const walker = document.createTreeWalker(
      root.querySelector(".katex-html")!,
      NodeFilter.SHOW_TEXT,
    );
    let next: Node | null;
    while ((next = walker.nextNode())) if (next.textContent === "det") nodes.push(next as Text);
    expect(nodes).toHaveLength(3);
    const result = quoteFromRange(root, rangeBetween(nodes[0], 0, nodes[2], 3), source);
    expect(result.error).toBeUndefined();
    expect(result.quote?.markdown).toBe(String.raw`$\det M = \det A \det$`);
    expect(source.slice(result.quote!.source_start, result.quote!.source_end)).toBe(
      String.raw`\det M = \det A \det`,
    );
  });

  it("局部选择包含det算符时只引用所选前半等式", () => {
    const source = String.raw`$\det M = \det A \det B = -3$`;
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const math = root.querySelector(".katex-html")!;
    const result = quoteFromRange(
      root,
      rangeBetween(visibleText(math, "M"), 0, visibleText(math, "A"), 1),
      source,
    );
    expect(result.error).toBeUndefined();
    expect(result.quote?.markdown).toBe(String.raw`$M = \det A$`);
  });

  it("从可见M拖到句点选择完整分块矩阵时保留矩阵结构", () => {
    const latex = String.raw`M=\begin{pmatrix}O&A\\B&E\end{pmatrix}.`;
    const source = `分块矩阵 $${latex}$`;
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const math = root.querySelector(".katex-html")!;
    const first = visibleText(math, "M");
    const last = visibleText(math, ".");
    const whole = document.createRange();
    whole.selectNodeContents(math);
    expect(quoteFromRange(root, whole, source).quote?.markdown).toBe(`$${latex}$`);
    const result = quoteFromRange(root, rangeBetween(first, 0, last, last.length), source);
    expect(result.error).toBeUndefined();
    expect(result.quote?.markdown).toBe(`$${latex}$`);
    expect(result.quote?.source_start).toBe(source.indexOf("M="));
    expect(result.quote?.source_end).toBe(source.lastIndexOf("$"));
  });

  it("整条分块矩阵连同公式外句号可按真实可见边界引用", () => {
    const latex = String.raw`M=\begin{pmatrix}0&A\\B&E\end{pmatrix}`;
    const source = `令 $${latex}$。`;
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const first = visibleText(root.querySelector(".katex-html")!, "M");
    const last = visibleText(root, "。");
    const result = quoteFromRange(root, rangeBetween(first, 0, last, last.length), source);
    expect(result.error).toBeUndefined();
    expect(result.quote?.markdown).toBe(`$${latex}$。`);
  });

  it("显示公式的真实可见全选定位原始矩阵源码", () => {
    const latex = String.raw`M=\begin{pmatrix}O&A\\B&E\end{pmatrix}.`;
    const source = `已知\n\\[\n${latex}\n\\]\n交换两行。`;
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const math = root.querySelector(".katex-html")!;
    const first = visibleText(math, "M");
    const last = visibleText(math, ".");
    const result = quoteFromRange(root, rangeBetween(first, 0, last, 1), source);
    expect(result.error).toBeUndefined();
    expect(result.quote?.markdown).toBe(`$${latex}$`);
    expect(source.slice(result.quote!.source_start, result.quote!.source_end)).toBe(latex);
  });

  it("矩阵中只选A仍只引用A，不补矩阵环境或其他单元格", () => {
    const source = String.raw`矩阵 $M=\begin{pmatrix}O&A\\B&E\end{pmatrix}.$`;
    const view = render(<MarkdownAnswer text={source} />);
    const root = view.container.querySelector<HTMLElement>(".markdown-answer")!;
    const a = visibleText(root.querySelector(".katex-html")!, "A");
    const result = quoteFromRange(root, rangeBetween(a, 0, a, 1), source);
    expect(result.quote?.markdown).toBe("$A$");
    expect(result.quote?.source_start).toBe(source.indexOf("A"));
    expect(result.quote?.source_end).toBe(source.indexOf("A") + 1);
  });

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
