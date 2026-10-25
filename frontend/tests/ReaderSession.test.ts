import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Schema } from "../src/api/client";
import { ReaderSession } from "../src/reading/readerSession";

type TestPosition = { page_index: number; scale: string; rotation: 0; scroll_fraction: number };

const mocks = vi.hoisted(() => ({
  adapters: [] as Array<{
    position: (value: {
      page_index: number;
      scale: string;
      rotation: 0;
      scroll_fraction: number;
    }) => void;
    open: ReturnType<typeof vi.fn>;
  }>,
  focus: vi.fn(),
}));

vi.mock("../src/reading/pdfAdapter", () => ({
  PdfReaderAdapter: class {
    open = vi.fn(async (_version, _signal, state) => this.position(state));
    dispose = vi.fn();
    constructor(
      _host: unknown,
      _file: unknown,
      callbacks: { position: (value: TestPosition) => void },
    ) {
      this.position = callbacks.position;
      mocks.adapters.push(this);
    }
    position: (value: TestPosition) => void;
  },
}));
vi.mock("../src/reading/preparation", async (original) => {
  const actual = await original<typeof import("../src/reading/preparation")>();
  return {
    ...actual,
    ReadingPreparation: class {
      focus = mocks.focus;
      setVisible = vi.fn();
      dispose = vi.fn();
    },
  };
});

const callbacks = () => ({
  selection: vi.fn(),
  error: vi.fn(),
  notice: vi.fn(),
  preparation: vi.fn(),
});
const file = (id: string) =>
  ({
    course_id: "course",
    asset_id: id,
    source_version: "version",
  }) as Schema<"CourseFileOpenResponse">;
const state = (epoch: string, revision = 0) => ({
  reader_epoch: epoch,
  revision,
  page_index: 0,
  scale: "page-width",
  rotation: 0 as const,
  scroll_fraction: 0,
  updated_at: "fixture",
});
const json = (value: unknown) => new Response(JSON.stringify(value));
const settle = async () => {
  for (let i = 0; i < 20; i++) await Promise.resolve();
};

describe("阅读页可见性与迟到回执", () => {
  const sessions: ReaderSession[] = [];
  beforeEach(() => {
    vi.useFakeTimers();
    sessionStorage.clear();
    mocks.adapters.length = 0;
    mocks.focus.mockReset().mockResolvedValue(undefined);
  });
  afterEach(() => {
    sessions.splice(0).forEach((s) => s.dispose());
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });
  function make(id: string) {
    const session = new ReaderSession(document.createElement("div"), file(id), callbacks());
    sessions.push(session);
    return session;
  }
  it("新标签先激活、旧标签后隐藏时，释放沿用旧序号", async () => {
    const writes: Array<{ url: string; body: Record<string, unknown> }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (!init?.method) return json({ prepared: true, pdfjs_version: "5.4.624" });
        writes.push({ url, body: JSON.parse(String(init.body)) });
        return json(
          url.endsWith("/reading-state") ? state(url.includes("/a/") ? "epoch_a" : "epoch_b") : {},
        );
      }),
    );
    const a = make("a"),
      b = make("b");
    a.setVisible(true);
    await settle();
    b.setVisible(true);
    a.setVisible(false);
    await settle();
    const release = writes.find((w) => w.url.endsWith("/focus/release"))!.body;
    const newOpen = writes.find(
      (w) => w.url.includes("/b/") && w.url.endsWith("/reading-state"),
    )!.body;
    expect(release.focus_sequence).toBe(2);
    expect(newOpen.view_sequence).toBe(3);
    expect(Number(release.focus_sequence)).toBeLessThan(Number(newOpen.view_sequence));
  });
  it("旧资料打开回执迟到时只释放原身份，不启动旧页识别", async () => {
    let oldReply!: (response: Response) => void;
    const writes: Array<Record<string, unknown>> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (!init?.method) return json({ prepared: true, pdfjs_version: "5.4.624" });
        writes.push({ url, ...JSON.parse(String(init.body)) });
        if (url.includes("/a/") && url.endsWith("/reading-state"))
          return new Promise<Response>((r) => (oldReply = r));
        return json(url.endsWith("/reading-state") ? state("epoch_b") : {});
      }),
    );
    const a = make("a"),
      b = make("b");
    a.setVisible(true);
    a.setVisible(false);
    b.setVisible(true);
    await settle();
    oldReply(json(state("epoch_a")));
    await settle();
    expect(mocks.adapters[0].open).not.toHaveBeenCalled();
    expect(mocks.focus).toHaveBeenCalledTimes(1);
    expect(mocks.focus.mock.calls[0][0].reader_epoch).toBe("epoch_b");
    expect(writes.find((w) => String(w.url).endsWith("/focus/release"))).toMatchObject({
      reader_epoch: "epoch_a",
      focus_sequence: 2,
    });
  });
  it("旧位置保存回执不覆盖重新激活后取得的新epoch与revision", async () => {
    let oldSave!: (response: Response) => void;
    let opened = 0;
    const saves: Array<Record<string, unknown>> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (!init?.method) return json({ prepared: true, pdfjs_version: "5.4.624" });
        if (url.endsWith("/reading-state"))
          return json(state(`epoch_${++opened}`, opened === 1 ? 0 : 8));
        if (url.endsWith("/reading-state/save")) {
          const body = JSON.parse(String(init.body));
          saves.push(body);
          if (saves.length === 1) return new Promise<Response>((r) => (oldSave = r));
          return json({ ...state("epoch_2", 9), ...body });
        }
        return json({});
      }),
    );
    const a = make("a");
    a.setVisible(true);
    await settle();
    mocks.adapters[0].position({ ...state("ignored"), page_index: 1 });
    await vi.advanceTimersByTimeAsync(700);
    a.setVisible(false);
    a.setVisible(true);
    await settle();
    oldSave(json(state("epoch_1", 1)));
    await settle();
    mocks.adapters[0].position({ ...state("ignored"), page_index: 2 });
    await vi.advanceTimersByTimeAsync(700);
    await settle();
    expect(saves.at(-1)).toMatchObject({
      reader_epoch: "epoch_2",
      expected_revision: 8,
      page_index: 2,
    });
  });
});
