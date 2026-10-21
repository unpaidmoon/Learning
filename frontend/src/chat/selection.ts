import katex from "katex";

export interface QuoteSelection {
  text: string;
  markdown: string;
  /** UTF-16 offsets into the immutable source answer. */
  source_start?: number;
  source_end?: number;
}

interface SourcePosition {
  start?: { offset?: number };
  end?: { offset?: number };
}
interface TreeNode {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: TreeNode[];
  position?: SourcePosition;
}
interface ParseNode {
  type?: string;
  text?: string;
  loc?: { start: number; end: number };
  [key: string]: unknown;
}

/** Delimiter normalization retains a boundary map back to the original answer. */
function normalize(text: string) {
  let normalized = "";
  const offsets: number[] = [];
  const append = (value: string, indexes: number[]) => {
    normalized += value;
    for (const index of indexes) offsets.push(index);
  };
  let previous = 0;
  for (const match of text.matchAll(/\\\[([\s\S]+?)\\\]|\\\(([\s\S]+?)\\\)/g)) {
    const start = match.index;
    append(
      text.slice(previous, start),
      Array.from({ length: start - previous }, (_, i) => previous + i),
    );
    const display = match[1] !== undefined;
    const value = match[1] ?? match[2];
    append(display ? "\n$$" : "$", display ? [start, start, start + 1] : [start]);
    append(
      value,
      Array.from({ length: value.length }, (_, i) => start + 2 + i),
    );
    const close = start + match[0].length - 2;
    append(display ? "$$\n" : "$", display ? [close, close + 1, close + 2] : [close]);
    previous = start + match[0].length;
  }
  append(
    text.slice(previous),
    Array.from({ length: text.length - previous }, (_, i) => previous + i),
  );
  offsets.push(text.length);
  return { normalized, offsets };
}

function leafLocations(latex: string) {
  const parser = katex as typeof katex & {
    __parse: (value: string, settings: object) => ParseNode[];
  };
  const leaves: { start: number; end: number; argument: boolean }[] = [];
  const seen = new Set<object>();
  const visit = (value: unknown, argument = false) => {
    if (!value || typeof value !== "object" || seen.has(value)) return;
    seen.add(value);
    if (Array.isArray(value)) {
      value.forEach((item) => visit(item));
      return;
    }
    const node = value as ParseNode;
    if (
      ["mathord", "textord", "atom", "spacing"].includes(node.type ?? "") &&
      typeof node.text === "string" &&
      node.loc &&
      Number.isInteger(node.loc.start) &&
      Number.isInteger(node.loc.end) &&
      node.loc.start >= 0 &&
      node.loc.end <= latex.length
    ) {
      leaves.push({ start: node.loc.start, end: node.loc.end, argument });
      return;
    }
    for (const [key, child] of Object.entries(node)) {
      if (key !== "loc" && key !== "lexer") visit(child, true);
    }
  };
  visit(parser.__parse(latex, { trust: false, maxExpand: 500, maxSize: 20 }));
  return leaves
    .sort((left, right) => left.start - right.start || left.end - right.end)
    .filter((item, index, all) => !index || item.start >= all[index - 1].end);
}

/** Only nonce-bound source data attributes introduced here receive KaTeX trust. */
export function createSelectionRendering(original: string) {
  const { normalized, offsets } = normalize(original);
  const nonce = crypto.randomUUID();
  const allowed = new Set<string>();
  const trust = (context: { command: string; attributes?: Record<string, string> }) => {
    if (context.command !== "\\htmlData" || !context.attributes) return false;
    const attributes = context.attributes;
    return (
      attributes["data-selection-key"] === nonce &&
      Object.keys(attributes).length === 3 &&
      allowed.has(`${attributes["data-selection-start"]}:${attributes["data-selection-end"]}`)
    );
  };
  const plugin = () => (input: unknown) => {
    const walk = (parent: TreeNode) => {
      if (!parent.children) return;
      parent.children = parent.children.map((node) => {
        const code = node.tagName === "pre" ? node.children?.[0] : node;
        const classes = code?.properties?.className;
        if (
          code?.tagName === "code" &&
          Array.isArray(classes) &&
          classes.some(
            (item) => item === "language-math" || item === "math-inline" || item === "math-display",
          )
        ) {
          const latex = (code.children ?? []).map((child) => child.value ?? "").join("");
          const start = node.position?.start?.offset ?? code.position?.start?.offset;
          const end = node.position?.end?.offset ?? code.position?.end?.offset;
          const found = start == null ? -1 : normalized.indexOf(latex, start);
          if (found < 0 || end == null || found + latex.length > end) return node;
          let instrumented = latex;
          try {
            if (
              /\\(?:def|gdef|edef|xdef|let|futurelet|newcommand|renewcommand|providecommand|csname)\b/.test(
                latex,
              )
            ) {
              throw new Error("Macro-defined source locations are not a unique visible selection");
            }
            for (const leaf of leafLocations(latex).reverse()) {
              const sourceStart = offsets[found + leaf.start];
              const sourceEnd = offsets[found + leaf.end];
              allowed.add(`${sourceStart}:${sourceEnd}`);
              const value = latex.slice(leaf.start, leaf.end);
              const command = `\\htmlData{selection-key=${nonce},selection-start=${sourceStart},selection-end=${sourceEnd}}{${value}}`;
              const wrapped = leaf.argument ? `{${command}}` : command;
              instrumented =
                instrumented.slice(0, leaf.start) + wrapped + instrumented.slice(leaf.end);
            }
          } catch {
            // Unsupported formulas retain safe rendering and full-selection support.
          }
          code.children = [{ type: "text", value: instrumented }];
          return {
            type: "element",
            tagName: node.tagName === "pre" ? "div" : "span",
            properties: {
              className: ["selection-formula"],
              "data-formula-start": offsets[found],
              "data-formula-end": offsets[found + latex.length],
              "data-formula-outer-start": offsets[start!],
              "data-formula-outer-end": offsets[end],
            },
            children: [node],
          };
        }
        if (node.type === "text" && node.value) {
          const start = node.position?.start?.offset;
          const end = node.position?.end?.offset;
          if (
            start != null &&
            end != null &&
            original.slice(offsets[start], offsets[end]) === node.value
          ) {
            return {
              type: "element",
              tagName: "span",
              properties: {
                "data-selection-start": offsets[start],
                "data-selection-end": offsets[end],
              },
              children: [node],
            };
          }
        }
        walk(node);
        return node;
      });
    };
    walk(input as TreeNode);
  };
  const cleanPlugin = () => (input: unknown) => {
    const clean = (node: TreeNode, formulaSource?: string) => {
      const start = node.properties?.["data-formula-start"];
      const end = node.properties?.["data-formula-end"];
      if (typeof start === "number" && typeof end === "number") {
        formulaSource = original.slice(start, end);
      }
      if (node.tagName === "annotation" && formulaSource != null) {
        node.children = [{ type: "text", value: formulaSource }];
      } else node.children?.forEach((child) => clean(child, formulaSource));
    };
    clean(input as TreeNode);
  };
  return { normalized, plugin, trust, cleanPlugin };
}

function textNodes(root: Node) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const result: Text[] = [];
  let next: Node | null;
  while ((next = walker.nextNode())) {
    if (
      !next.parentElement?.closest(
        ".katex-mathml, .selection-quote-button, .selection-quote-error",
      ) &&
      next.textContent &&
      !/^[\s\u200b-\u200f\u2060\ufeff]*$/.test(next.textContent)
    ) {
      result.push(next as Text);
    }
  }
  return result;
}

function selectedText(range: Range, node: Text) {
  if (!range.intersectsNode(node)) return null;
  if (range.comparePoint(node, node.length) < 0 || range.comparePoint(node, 0) > 0) return null;
  const start = range.startContainer === node ? range.startOffset : 0;
  const end = range.endContainer === node ? range.endOffset : node.length;
  return end > start ? { start, end, value: node.data.slice(start, end) } : null;
}

export function quoteFromRange(
  root: HTMLElement,
  range: Range,
  source: string,
): {
  quote?: QuoteSelection;
  error?: string;
} {
  const formulaError = "此公式选区暂不能精确对应源码，请缩小选区（FORMULA_SELECTION_UNMAPPED）";
  const textError = "此选区暂不能精确对应原文，请重新选择（TEXT_SELECTION_UNMAPPED）";
  const selected = textNodes(root)
    .map((node) => ({ node, selected: selectedText(range, node) }))
    .filter((item) => item.selected != null);
  if (!selected.length) return {};
  const formulas = new Map<HTMLElement, boolean>();
  for (const { node } of selected) {
    const formula = node.parentElement?.closest<HTMLElement>(".selection-formula");
    if (formula && !formulas.has(formula)) {
      const visual = formula.querySelector(".katex-html") ?? formula;
      const visualRange = document.createRange();
      visualRange.selectNodeContents(visual);
      formulas.set(
        formula,
        range.compareBoundaryPoints(Range.START_TO_START, visualRange) <= 0 &&
          range.compareBoundaryPoints(Range.END_TO_END, visualRange) >= 0,
      );
    }
  }
  const locations: { start: number; end: number; formula: HTMLElement | null }[] = [];
  for (const { node, selected: part } of selected) {
    const element = node.parentElement;
    const formula = element?.closest<HTMLElement>(".selection-formula") ?? null;
    if (formula && formulas.get(formula)) {
      locations.push({
        start: Number(formula.dataset.formulaStart),
        end: Number(formula.dataset.formulaEnd),
        formula,
      });
      continue;
    }
    const mapped = element?.closest<HTMLElement>("[data-selection-start]");
    if (!mapped) return { error: formula ? formulaError : textError };
    let start = Number(mapped.dataset.selectionStart);
    let end = Number(mapped.dataset.selectionEnd);
    if (formula) {
      if (part!.start !== 0 || part!.end !== node.length) return { error: formulaError };
    } else {
      if (source.slice(start, end) !== node.data) return { error: textError };
      start += part!.start;
      end = start + part!.value.length;
    }
    locations.push({ start, end, formula });
  }
  const onlyFormula =
    locations.every((item) => item.formula === locations[0].formula) && locations[0].formula;
  let start = Math.min(...locations.map((item) => item.start));
  let end = Math.max(...locations.map((item) => item.end));
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end > source.length)
    return { error: textError };
  if (onlyFormula) {
    // A visual-order selection must not silently include skipped source tokens.
    for (const element of onlyFormula.querySelectorAll<HTMLElement>("[data-selection-start]")) {
      const a = Number(element.dataset.selectionStart);
      const b = Number(element.dataset.selectionEnd);
      if (
        a >= start &&
        b <= end &&
        !textNodes(element).every((node) => selectedText(range, node))
      ) {
        return { error: formulaError };
      }
    }
    try {
      katex.renderToString(source.slice(start, end), {
        trust: false,
        throwOnError: true,
        maxExpand: 500,
        maxSize: 20,
      });
    } catch {
      return { error: formulaError };
    }
  } else {
    for (const [formula, full] of formulas) {
      if (!full) return { error: formulaError };
      start = Math.min(start, Number(formula.dataset.formulaOuterStart));
      end = Math.max(end, Number(formula.dataset.formulaOuterEnd));
    }
  }
  const value = selected
    .map((item) => item.selected!.value)
    .join("")
    .trim();
  if (!value) return {};
  return {
    quote: {
      text: value,
      markdown: onlyFormula ? `$${source.slice(start, end)}$` : source.slice(start, end),
      source_start: start,
      source_end: end,
    },
  };
}
