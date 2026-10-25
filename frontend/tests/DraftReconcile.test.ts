import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const value = {
  text: "保留未发送的消息",
  attachment_ids: ["document_existing"],
  references: [],
  conditions: [],
  corrections: [],
  model: null,
  thinking: null,
};

describe("草稿保存回执丢失后的只读对账", () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
    localStorage.setItem(
      "learning.draft.lost-ack-chat",
      JSON.stringify({ revision: 2, value, dirty: true }),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("后台已经保存相同正文与附件时接回新revision，不再次写入", async () => {
    const request = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(
      async () => new Response(JSON.stringify({ ...value, revision: 3 })),
    );
    vi.stubGlobal("fetch", request);
    const { getDraftController } = await import("../src/chat/draft");
    const controller = getDraftController("lost-ack-chat");
    await controller.load();
    expect(controller.snapshot()).toMatchObject({ ready: true, dirty: false, error: "", value });
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0][1]?.method).not.toBe("POST");
    expect(JSON.parse(localStorage.getItem("learning.draft.lost-ack-chat")!).revision).toBe(3);
  });

  it("后台内容不同则继续保护本地输入，不覆盖、不自动写入", async () => {
    const request = vi.fn(
      async () => new Response(JSON.stringify({ ...value, revision: 3, text: "另一份已保存内容" })),
    );
    vi.stubGlobal("fetch", request);
    const { getDraftController } = await import("../src/chat/draft");
    const controller = getDraftController("lost-ack-chat");
    await controller.load();
    expect(controller.snapshot()).toMatchObject({ ready: false, dirty: true, value });
    expect(controller.snapshot().error).toContain("草稿有较新的保存版本");
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("接回失败读取后只核对旧版本，保留未保存输入直到下一次明确编辑", async () => {
    const request = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockImplementation(
        async (_url: string, init?: RequestInit) =>
          new Response(
            JSON.stringify({
              ...value,
              text: init?.method === "POST" ? JSON.parse(String(init.body)).text : "旧保存内容",
              revision: init?.method === "POST" ? 3 : 2,
            }),
          ),
      );
    vi.stubGlobal("fetch", request);
    const { getDraftController } = await import("../src/chat/draft");
    const controller = getDraftController("lost-ack-chat");
    await controller.load();
    expect(controller.snapshot().ready).toBe(false);
    await controller.reconcile();
    expect(controller.snapshot()).toMatchObject({ ready: true, dirty: true, value });
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(request.mock.calls.every(([, init]) => init?.method !== "POST")).toBe(true);
    controller.update({ text: "用户恢复后新编辑" });
    await controller.flush();
    expect(request.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    expect(controller.snapshot().value.text).toBe("用户恢复后新编辑");
  });

  it("只读接回仍保护不同的新版本，不把冲突变成自动覆盖", async () => {
    const request = vi.fn(
      async () => new Response(JSON.stringify({ ...value, text: "其他窗口的新版本", revision: 3 })),
    );
    vi.stubGlobal("fetch", request);
    const { getDraftController } = await import("../src/chat/draft");
    const controller = getDraftController("lost-ack-chat");
    const remote = await controller.reconcile();
    expect(remote?.text).toBe("其他窗口的新版本");
    expect(controller.snapshot()).toMatchObject({ ready: false, dirty: true, value });
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("已完成接回后，旧读取的迟到失败不能再次冻结草稿", async () => {
    let rejectOld!: (reason: Error) => void;
    const oldRead = new Promise<Response>((_resolve, reject) => {
      rejectOld = reject;
    });
    const request = vi
      .fn()
      .mockImplementationOnce(() => oldRead)
      .mockImplementationOnce(async () => new Response(JSON.stringify({ ...value, revision: 3 })));
    vi.stubGlobal("fetch", request);
    const { getDraftController } = await import("../src/chat/draft");
    const controller = getDraftController("lost-ack-chat");
    const loading = controller.load();
    await controller.reconcile();
    rejectOld(new TypeError("late disconnected response"));
    await loading;
    expect(controller.snapshot()).toMatchObject({ ready: true, dirty: false, error: "", value });
    expect(request.mock.calls.every(([, init]) => init?.method !== "POST")).toBe(true);
  });
});
