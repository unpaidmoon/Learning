import { afterEach, expect, test, vi } from "vitest";
import { PdfReaderAdapter } from "../src/reading/pdfAdapter";
import type { Schema } from "../src/api/client";
import type { SourceJump } from "../src/reading/actions";

afterEach(() => vi.restoreAllMocks());

test("repeated explicit jump identity does not rewind a reader that the user moved", () => {
  const descriptor = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, "sandbox");
  Object.defineProperty(HTMLIFrameElement.prototype, "sandbox", {
    configurable: true,
    value: { add: vi.fn() },
  });
  const scroll = vi.fn();
  const div = document.createElement("div");
  div.scrollIntoView = scroll;
  const file = {
    asset_id: "asset",
    source_version: "file_revision",
    entry: { name: "fixture.pdf" },
  } as Schema<"CourseFileOpenResponse">;
  const adapter = new PdfReaderAdapter(document.createElement("div"), file);
  const viewer = {
    currentPageNumber: 1,
    getPageView: vi.fn(() => ({ div, viewport: { viewBox: [0, 0, 600, 800] } })),
  };
  Reflect.set(adapter, "application", { pdfViewer: viewer });
  const request: SourceJump = {
    request_id: "explicit_request",
    reference: {
      kind: "document",
      asset_id: "asset",
      source_version: "file_revision",
      source_digest: "a".repeat(64),
      page_index: 3,
      offset_unit: "unicode_codepoint",
      start: 0,
      end: 10,
      parser_version: "fixture",
      regions: [],
      page_label: "iv",
    },
  };
  try {
    adapter.jump(request);
    expect(viewer.currentPageNumber).toBe(4);
    viewer.currentPageNumber = 9;
    adapter.jump({ ...request });
    expect(viewer.currentPageNumber).toBe(9);
    expect(scroll).toHaveBeenCalledTimes(1);
    adapter.jump({ ...request, request_id: "new_explicit_click" });
    expect(viewer.currentPageNumber).toBe(4);
    viewer.currentPageNumber = 9;
    adapter.jump({ ...request });
    expect(viewer.currentPageNumber).toBe(9);
    adapter.jump({
      ...request,
      request_id: "wrong_source",
      reference: { ...request.reference, source_version: "other_revision" },
    });
    expect(scroll).toHaveBeenCalledTimes(2);
  } finally {
    window.clearTimeout(Reflect.get(adapter, "highlightTimer"));
    if (descriptor) Object.defineProperty(HTMLIFrameElement.prototype, "sandbox", descriptor);
    else Reflect.deleteProperty(HTMLIFrameElement.prototype, "sandbox");
  }
});
