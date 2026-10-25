import { describe, expect, it } from "vitest";
import { sourcePresentation } from "../src/workbench/sourcePresentation";

describe("固定选区定位显示", () => {
  it("preserves gaps between selected pages instead of implying unread intervening pages", () => {
    const reference = {
      kind: "document_selection" as const,
      offset_unit: "unicode_codepoint" as const,
      asset_id: "a",
      source_version: "v",
      source_digest: "a".repeat(64),
      parts: [0, 1, 4].map((page_index) => ({
        page_index,
        start: 0,
        end: 1,
        regions: [{ x0: 0, y0: 0, x1: 1, y1: 1 }],
        parser_version: "fixture",
      })),
    };
    const frozen = JSON.stringify(reference);
    expect(sourcePresentation(reference)).toBe("文档选区 第 1-2 页、第 5 页");
    expect(JSON.stringify(reference)).toBe(frozen);
  });
  it("distinguishes text selections and authorized document images", () => {
    const common = {
      asset_id: "a",
      source_version: "v",
      source_digest: "a".repeat(64),
      page_index: 2,
      parser_version: "fixture",
    };
    expect(
      sourcePresentation({
        ...common,
        kind: "document_image",
        image_sha256: "b".repeat(64),
        width: 30,
        height: 40,
      }),
    ).toBe("授权文档页图 第 3 页");
    expect(
      sourcePresentation({
        ...common,
        kind: "document",
        offset_unit: "unicode_codepoint",
        start: 3,
        end: 5,
        parser_version: "utf8-text-v1",
      }),
    ).toBe("文档选区");
  });
  it("formats subtitle display time while preserving the exact millisecond reference", () => {
    const reference = {
      kind: "subtitle" as const,
      asset_id: "a",
      source_version: "v",
      source_digest: "a".repeat(64),
      transcript_revision_id: "trraw_v",
      segment_id: "s",
      start_ms: 1_234,
      end_ms: 3_661_567,
    };
    const frozen = JSON.stringify(reference);
    expect(sourcePresentation(reference)).toBe("已保存字幕片段 00:01.234–1:01:01.567");
    expect(JSON.stringify(reference)).toBe(frozen);
  });
});
