import { act, fireEvent, render, screen, waitFor, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { bindSplitAudio } from "../src/integrations/audioSynchronization";
import { BilibiliImport } from "../src/integrations/BilibiliImport";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("平台媒体与手选导入", () => {
  it("番剧季目录不默认全选，确认后只导入手选分集且不自动保存媒体", async () => {
    const requests: { path: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string, options?: RequestInit) => {
        const body = options?.body
          ? (JSON.parse(String(options.body)) as Record<string, unknown>)
          : {};
        requests.push({ path, body });
        let result: unknown;
        if (path.endsWith("/modules"))
          result = [{ platform: "bilibili", enabled: true, connection_id: "account-a" }];
        else if (path.endsWith("/inspect"))
          result = {
            kind: "video",
            title: "合成番剧",
            plan_id: "programme-plan",
            complete: true,
            parts: [
              { object_id: "bangumi:110:910", page: 1, title: "第一集", availability: "unknown" },
              { object_id: "bangumi:120:920", page: 2, title: "第二集", availability: "unknown" },
            ],
          };
        else if (path.endsWith("/import"))
          result = { state: "completed", items: [{ asset_id: "episode-2" }] };
        else throw new Error("Unexpected request: " + path);
        return new Response(JSON.stringify(result), { status: 200 });
      }),
    );
    render(<BilibiliImport courseId="course-a" connected onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "导入B站链接" }));
    fireEvent.change(screen.getByRole("textbox", { name: "B站视频链接" }), {
      target: { value: "https://www.bilibili.com/bangumi/play/ss100" },
    });
    fireEvent.click(screen.getByRole("button", { name: "读取链接" }));
    const second = await screen.findByRole("checkbox", { name: /P2/ });
    expect(second).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /P1/ })).not.toBeChecked();
    fireEvent.click(second);
    fireEvent.click(screen.getByRole("button", { name: "导入所选分P" }));
    expect(await screen.findByText(/已导入 1/)).toBeVisible();
    expect(requests.find((r) => r.path.endsWith("/inspect"))?.body.url).toBe(
      "https://www.bilibili.com/bangumi/play/ss100",
    );
    expect(requests.find((r) => r.path.endsWith("/import"))?.body.object_ids).toEqual([
      "bangumi:120:920",
    ]);
    expect(
      requests.every(
        (r) =>
          r.path.endsWith("/modules") || r.path.endsWith("/inspect") || r.path.endsWith("/import"),
      ),
    ).toBe(true);
  });
  it("拖动先暂停音轨，seek完成后恢复，同步音量及倍速", async () => {
    const video = document.createElement("video"),
      audio = document.createElement("audio");
    let paused = false;
    Object.defineProperty(video, "paused", { get: () => paused });
    const play = vi.spyOn(audio, "play").mockResolvedValue();
    const pause = vi.spyOn(audio, "pause").mockImplementation(() => {});
    video.currentTime = 75;
    video.volume = 0.4;
    video.playbackRate = 1.5;
    const dispose = bindSplitAudio(video, audio, vi.fn());
    expect(audio.currentTime).toBe(75);
    expect(audio.volume).toBe(0.4);
    expect(audio.playbackRate).toBe(1.5);
    video.dispatchEvent(new Event("seeking"));
    expect(pause).toHaveBeenCalledOnce();
    video.currentTime = 135;
    video.dispatchEvent(new Event("seeked"));
    expect(audio.currentTime).toBe(135);
    expect(play).toHaveBeenCalledTimes(2);
    paused = true;
    video.dispatchEvent(new Event("pause"));
    expect(pause).toHaveBeenCalledTimes(2);
    dispose();
  });

  it("音轨被浏览器拒绝时暂停画面并给可处理错误", async () => {
    const video = document.createElement("video"),
      audio = document.createElement("audio");
    Object.defineProperty(video, "paused", { value: false });
    vi.spyOn(audio, "play").mockRejectedValue(new Error("autoplay blocked"));
    vi.spyOn(audio, "pause").mockImplementation(() => {});
    const pause = vi.spyOn(video, "pause").mockImplementation(() => {}),
      failed = vi.fn();
    const dispose = bindSplitAudio(video, audio, failed);
    await Promise.resolve();
    expect(pause).toHaveBeenCalledOnce();
    expect(failed).toHaveBeenCalledOnce();
    dispose();
  });

  it("分P初始均未勾选，未知结果保留原操作身份再核对", async () => {
    const requests: { path: string; body: Record<string, unknown> }[] = [];
    let count = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string, options?: RequestInit) => {
        const body = options?.body
          ? (JSON.parse(String(options.body)) as Record<string, unknown>)
          : {};
        requests.push({ path, body });
        let value: unknown = [];
        if (path.endsWith("/modules"))
          value = [{ platform: "bilibili", enabled: true, connection_id: "connection_a" }];
        else if (path.endsWith("/inspect"))
          value = {
            title: "合成多P",
            plan_id: "plan_a",
            parts: [
              { object_id: "p1", page: 1, title: "第一课" },
              { object_id: "p2", page: 2, title: "第二课" },
            ],
          };
        else if (path.endsWith("/import")) {
          count++;
          value = { state: count === 1 ? "unknown" : "completed", items: [{ asset_id: "a" }] };
        }
        return new Response(JSON.stringify(value), { status: 200 });
      }),
    );
    render(<BilibiliImport courseId="course_a" connected onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "导入B站链接" }));
    fireEvent.change(screen.getByRole("textbox", { name: "B站视频链接" }), {
      target: { value: "BV1234567890" },
    });
    fireEvent.click(screen.getByRole("button", { name: "读取链接" }));
    const checkbox = await screen.findByRole("checkbox", { name: /P1/ });
    expect(checkbox).not.toBeChecked();
    expect(screen.getByRole("button", { name: "导入所选分P" })).toBeDisabled();
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: "导入所选分P" }));
    await waitFor(() => expect(screen.getByText(/导入结果待核对/)).toBeVisible());
    fireEvent.click(screen.getByRole("button", { name: "核对原导入" }));
    await act(async () => {
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.getByText(/已导入 1/)).toBeVisible());
    const imports = requests.filter((request) => request.path.endsWith("/import"));
    expect(imports).toHaveLength(2);
    expect(imports[1].body).toEqual(imports[0].body);
    expect(imports[0].body.object_ids).toEqual(["p1"]);
  });

  it("合集逐页浏览，手选一个视频才解析分P，确认后只导入所选分P", async () => {
    const requests: { path: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string, options?: RequestInit) => {
        const body = options?.body
          ? (JSON.parse(String(options.body)) as Record<string, unknown>)
          : {};
        requests.push({ path, body });
        let result: unknown = [];
        if (path.endsWith("/modules"))
          result = [{ platform: "bilibili", enabled: true, connection_id: "account_a" }];
        else if (path.endsWith("/inspect") && String(body.url).includes("space.bilibili.com")) {
          const page = body.page || 1;
          result = {
            kind: "catalogue",
            catalogue_id: `page_${page}`,
            course_id: "course_a",
            connection_id: "account_a",
            catalogue_kind: "collection",
            title: "合成分页目录",
            url: "https://space.bilibili.com/10/lists/20?type=season",
            page,
            total: 31,
            has_more: page === 1,
            provider_revision: "123",
            videos:
              page === 1
                ? Array.from({ length: 30 }, (_, index) => ({
                    bvid: `BV${String(index + 1).padStart(10, "0")}`,
                    title: `目录视频${index + 1}`,
                    ordinal: index + 1,
                  }))
                : [{ bvid: "BV0000000031", title: "目录视频31", ordinal: 31 }],
          };
        } else if (path.endsWith("/inspect"))
          result = {
            kind: "video",
            plan_id: "selected_video",
            title: "所选视频分P",
            parts: [
              { object_id: "p1", page: 1, title: "第一节" },
              { object_id: "p2", page: 2, title: "第二节" },
            ],
          };
        else if (path.endsWith("/import"))
          result = { state: "completed", items: [{ asset_id: "a", reused: false }] };
        return new Response(JSON.stringify(result), { status: 200 });
      }),
    );
    render(<BilibiliImport courseId="course_a" connected onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "导入B站链接" }));
    fireEvent.change(screen.getByRole("textbox", { name: "B站视频链接" }), {
      target: { value: "https://space.bilibili.com/10/lists/20" },
    });
    fireEvent.click(screen.getByRole("button", { name: "读取链接" }));
    expect(await screen.findByText("合成分页目录")).toBeVisible();
    expect(requests.filter((value) => value.path.endsWith("/import"))).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    fireEvent.click(await screen.findByRole("button", { name: /31\. 目录视频31/ }));
    const checkbox = await screen.findByRole("checkbox", { name: /P2/ });
    expect(checkbox).not.toBeChecked();
    expect(
      requests.filter(
        (value) =>
          value.path.endsWith("/inspect") && !String(value.body.url).includes("space.bilibili.com"),
      ),
    ).toHaveLength(1);
    const videoRequest = requests.find(
      (value) => value.body.url === "https://www.bilibili.com/video/BV0000000031",
    );
    expect(videoRequest?.body.catalogue_id).toBe("page_2");
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole("button", { name: "导入所选分P" }));
    await screen.findByText(/已导入 1/);
    expect(requests.filter((value) => value.path.endsWith("/import"))).toHaveLength(1);
    expect(requests.find((value) => value.path.endsWith("/import"))?.body.object_ids).toEqual([
      "p2",
    ]);
  });

  it("换课程后忽略旧目录响应", async () => {
    let reply: ((value: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string) => {
        if (path.endsWith("/modules"))
          return new Response(
            JSON.stringify([{ platform: "bilibili", enabled: true, connection_id: "a" }]),
          );
        return new Promise<Response>((resolve) => {
          reply = resolve;
        });
      }),
    );
    const view = render(<BilibiliImport courseId="course_a" connected onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "导入B站链接" }));
    fireEvent.change(screen.getByRole("textbox", { name: "B站视频链接" }), {
      target: { value: "https://space.bilibili.com/10/lists/20" },
    });
    fireEvent.click(screen.getByRole("button", { name: "读取链接" }));
    await waitFor(() => expect(reply).toBeDefined());
    view.rerender(<BilibiliImport courseId="course_b" connected onImported={vi.fn()} />);
    await act(async () => {
      reply?.(new Response(JSON.stringify({ kind: "catalogue", title: "旧课程目录", videos: [] })));
    });
    expect(screen.queryByText("旧课程目录")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "合集与系列目录" })).not.toBeInTheDocument();
  });
});
