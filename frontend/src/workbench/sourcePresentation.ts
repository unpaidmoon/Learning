import {
  buildSelectionDisplayName,
  describeSelectionLocator,
} from "../../../third_party/deepstudent/web/selectionPresentation";
import type { Schema } from "../api/client";
import { formatClock } from "../../../third_party/deepstudent/web/timePresentation";

type Reference = Schema<"SourceSnapshot">["reference"];

// A display label never determines which text or image may be read. In
// particular a non-contiguous page selection must not imply the intervening pages.
export function sourcePresentation(reference: Reference): string {
  if (reference.kind === "document_selection") {
    const pages = reference.parts.map((part) => part.page_index + 1);
    const ranges: [number, number][] = [];
    for (const page of pages) {
      const last = ranges.at(-1);
      if (last && last[1] + 1 === page) last[1] = page;
      else ranges.push([page, page]);
    }
    return ranges
      .map(([from, to], index) => {
        const locator = `page:${from}${from === to ? "" : `-${to}`}`;
        return index === 0
          ? buildSelectionDisplayName({ kind: "pdf", title: "文档选区", locator })
          : describeSelectionLocator(locator);
      })
      .filter(Boolean)
      .join("、");
  }
  if (
    reference.kind === "document" ||
    reference.kind === "document_image" ||
    reference.kind === "document_region_image"
  ) {
    return buildSelectionDisplayName({
      kind: "pdf",
      title:
        reference.kind === "document_region_image"
          ? "文档局部图"
          : reference.kind === "document_image"
            ? "授权文档页图"
            : "文档选区",
      locator:
        reference.kind === "document" && reference.parser_version === "utf8-text-v1"
          ? undefined
          : `page:${reference.page_index + 1}`,
    });
  }
  if (reference.kind === "artifact")
    return buildSelectionDisplayName({ kind: "note", title: "学习成果选区" });
  if (reference.kind === "message")
    return buildSelectionDisplayName({ kind: "message", title: "已提交对话选区" });
  if (reference.kind === "submitted_image") return "用户已确认提交的图片范围";
  if (reference.kind === "subtitle")
    return `已保存字幕片段 ${subtitleTime(reference.start_ms)}–${subtitleTime(reference.end_ms)}`;
  return "已保存来源";
}

function subtitleTime(milliseconds: number): string {
  const fraction = milliseconds % 1000;
  return (
    formatClock(milliseconds / 1000) + (fraction ? `.${String(fraction).padStart(3, "0")}` : "")
  );
}
