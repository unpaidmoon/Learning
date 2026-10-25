import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Schema } from "../src/api/client";
import { SubtitlePanel } from "../src/media/SubtitlePanel";
import type { SubtitleState } from "../src/media/SubtitleSession";

const harness = vi.hoisted(() => ({
  changed: null as ((value: SubtitleState) => void) | null,
  refresh: vi.fn(),
}));
vi.mock("../src/media/SubtitleSession", () => ({
  SubtitleSession: class {
    constructor(_course: unknown, _asset: unknown, changed: (value: SubtitleState) => void) {
      harness.changed = changed;
    }
    setVisible = vi.fn();
    setPosition = vi.fn();
    dispose = vi.fn();
    refresh = harness.refresh;
  },
}));

const segment: Schema<"SubtitleSegment"> = {
  segment_id: "segment_a",
  revision_id: "trraw_a",
  raw_revision_id: "trraw_a",
  start_ms: 1000,
  end_ms: 3000,
  text: "Original caption",
  raw_text: "Original caption",
  origin: "original",
};
const track: Schema<"TranscriptView"> = {
  track_id: "track_a",
  course_id: "course_a",
  asset_id: "asset_a",
  source_version: "version_a",
  source_digest: "a".repeat(64),
  edit_head_id: null,
  audio_start_ms: 0,
  audio_end_ms: 5000,
  units_total: 1,
  units_ready: [0],
  missing_units: [],
  segments: [segment],
};
const asset = {
  asset_id: "asset_a",
  source_version: "version_a",
  position_ms: 0,
} as Schema<"MediaAssetResponse">;
const json = (body: unknown) => new Response(JSON.stringify(body));

async function open() {
  const view = render(
    <SubtitlePanel courseId="course_a" asset={asset} positionMs={0} active onSeek={vi.fn()} />,
  );
  act(() => harness.changed?.({ preparation: null, transcript: track }));
  fireEvent.click(screen.getByRole("button", { name: "修改" }));
  return view;
}

describe("字幕保存对账与编辑版本", () => {
  beforeEach(() => {
    localStorage.clear();
    harness.changed = null;
    harness.refresh.mockClear();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });
  it("保存回执丢失后只查原操作，不再次写字幕", async () => {
    const requests: Array<{ url: string; method: string; body?: Schema<"TranscriptEdit"> }> = [];
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      requests.push({
        url,
        method: init?.method || "GET",
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      if (init?.method === "POST") {
        const pending = JSON.parse(
          localStorage.getItem("learning.subtitle.edit.track_a.segment_a")!,
        );
        expect(pending.operation.operation_id).toBe(requests.at(-1)?.body?.operation_id);
        throw new TypeError("fixture response lost after server commit");
      }
      return json({
        revision_id: "edit_saved",
        head_revision_id: "edit_saved",
        disposition: "published",
      });
    });
    vi.stubGlobal("fetch", fetcher);
    await open();
    fireEvent.change(screen.getByRole("textbox", { name: "字幕修订正文" }), {
      target: { value: "Corrected caption" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存字幕" }));
    await screen.findByRole("button", { name: "核对字幕保存" });
    expect(requests.filter((r) => r.method === "POST")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "核对字幕保存" }));
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "字幕修订正文" })).not.toBeInTheDocument(),
    );
    expect(requests.filter((r) => r.method === "POST")).toHaveLength(1);
    expect(requests[1].url).toContain(`/operations/${requests[0].body!.operation_id}`);
    expect(localStorage.getItem("learning.subtitle.edit.track_a.segment_a")).toBeNull();
  });
  it("后台展示换到新track时，正在编辑的旧稿仍写原track", async () => {
    const written = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () =>
      json({ revision_id: "edit_a", head_revision_id: "edit_a", disposition: "published" }),
    );
    vi.stubGlobal("fetch", written);
    await open();
    act(() =>
      harness.changed?.({
        preparation: null,
        transcript: {
          ...track,
          track_id: "track_b",
          edit_head_id: "head_b",
          segments: [{ ...segment, segment_id: "segment_b" }],
        },
      }),
    );
    fireEvent.change(screen.getByRole("textbox", { name: "字幕修订正文" }), {
      target: { value: "Keep original target" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存字幕" }));
    await waitFor(() => expect(written).toHaveBeenCalledTimes(1));
    expect(String(written.mock.calls[0][0])).toContain("/transcripts/track_a/edit");
  });
  it("冲突稿保留，用户明确改用新基准后才以新head提交", async () => {
    const operations: Schema<"TranscriptEdit">[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init?: RequestInit) => {
        if (init?.method !== "POST") return json({ ...track, edit_head_id: "manual_new_head" });
        operations.push(JSON.parse(String(init.body)));
        return json(
          operations.length === 1
            ? {
                revision_id: "conflict_a",
                head_revision_id: "manual_new_head",
                disposition: "conflict",
              }
            : {
                revision_id: "published_a",
                head_revision_id: "published_a",
                disposition: "published",
              },
        );
      }),
    );
    await open();
    fireEvent.change(screen.getByRole("textbox", { name: "字幕修订正文" }), {
      target: { value: "My saved proposal" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存字幕" }));
    await screen.findByRole("button", { name: "基于当前字幕继续编辑" });
    expect(operations).toHaveLength(1);
    expect(screen.getByRole("textbox", { name: "字幕修订正文" })).toHaveValue("My saved proposal");
    fireEvent.click(screen.getByRole("button", { name: "基于当前字幕继续编辑" }));
    await screen.findByRole("button", { name: "保存字幕" });
    fireEvent.click(screen.getByRole("button", { name: "保存字幕" }));
    await waitFor(() => expect(operations).toHaveLength(2));
    expect(operations[1]).toMatchObject({
      expected_edit_head_id: "manual_new_head",
      changes: [{ text: "My saved proposal" }],
    });
    expect(operations[0].operation_id).not.toBe(operations[1].operation_id);
  });
});
