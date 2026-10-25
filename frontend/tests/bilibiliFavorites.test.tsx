import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BilibiliImport } from "../src/integrations/BilibiliImport";
import { FavoriteBrowser } from "../src/integrations/FavoriteBrowser";
import { INTEGRATIONS_CHANGED } from "../src/integrations/bilibiliUi";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function fixture() {
  let connection = "account-a";
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input),
      body = JSON.parse(String(init?.body || "{}"));
    calls.push({ path, body });
    let value: unknown = {};
    if (path.endsWith("/integrations/modules"))
      value = [
        {
          platform: "bilibili",
          enabled: true,
          implemented: true,
          connection_id: connection,
          connection_state: "connected",
        },
      ];
    else if (path.endsWith("/favorites/folders")) {
      const collected = body.kind === "collected";
      value = {
        kind: body.kind,
        page: body.page,
        total: collected ? 2 : 21,
        has_more: !collected && body.page === 1,
        items: collected
          ? [
              { folder_id: "500", title: "合成关注收藏夹", count: 21, resource_type: "folder" },
              { folder_id: "500", title: "合成关注合集", count: 21, resource_type: "collection" },
            ]
          : Array.from({ length: body.page === 1 ? 20 : 1 }, (_, i) => ({
              folder_id: String(body.page === 1 ? i + 1 : 21),
              title: `合成收藏夹${body.page === 1 ? i + 1 : 21}`,
              count: 21,
              resource_type: "folder",
            })),
      };
    } else if (path.endsWith("/favorites/browse"))
      value = {
        kind: body.kind,
        folder_id: body.folder_id,
        title: "合成视频目录",
        page: body.page,
        total: 21,
        has_more: body.page === 1,
        items:
          body.page === 1
            ? [
                { resource_id: "v1", bvid: "BV0000000001", title: "正常视频", available: true },
                {
                  resource_id: "v2",
                  bvid: null,
                  title: "失效视频",
                  available: false,
                  unavailable_reason: "removed",
                },
              ]
            : [
                {
                  resource_id: "v21",
                  bvid: "BV0000000021",
                  title: "合成收藏视频21",
                  available: true,
                },
              ],
      };
    else if (path.endsWith("/inspect"))
      value = {
        kind: "video",
        plan_id: "plan-selected",
        title: "合成多P",
        parts: [
          { object_id: "p1", page: 1, title: "第一P" },
          { object_id: "p2", page: 2, title: "第二P" },
        ],
      };
    else if (path.endsWith("/import"))
      value = { state: "completed", items: [{ asset_id: "one-only" }] };
    return new Response(JSON.stringify(value), { status: 200 });
  });
  vi.stubGlobal("fetch", fetch);
  return {
    calls,
    fetch,
    changeConnection: (value: string) => {
      connection = value;
    },
  };
}

describe("收藏来源分页与手选", () => {
  it("先选目录与视频页，失效项不可选；只确认P2才导入", async () => {
    const env = fixture(),
      imported = vi.fn();
    render(<BilibiliImport courseId="a" connected onImported={imported} />);
    fireEvent.click(screen.getByRole("button", { name: "导入B站链接" }));
    fireEvent.click(screen.getByRole("button", { name: "浏览账号收藏夹" }));
    await screen.findByRole("button", { name: "合成收藏夹1（21）" });
    fireEvent.click(screen.getByRole("button", { name: "下一页目录" }));
    fireEvent.click(await screen.findByRole("button", { name: "合成收藏夹21（21）" }));
    expect(await screen.findByRole("button", { name: "失效视频（已失效）" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "下一页视频" }));
    fireEvent.click(await screen.findByRole("button", { name: "合成收藏视频21" }));
    expect(env.calls.some((c) => c.path.endsWith("/inspect"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "读取链接" }));
    const second = await screen.findByRole("checkbox", { name: /P2/ });
    expect(second).not.toBeChecked();
    fireEvent.click(second);
    fireEvent.click(screen.getByRole("button", { name: "导入所选分P" }));
    await waitFor(() => expect(imported).toHaveBeenCalledOnce());
    expect(env.calls.find((c) => c.path.endsWith("/import"))?.body.object_ids).toEqual(["p2"]);
    expect(
      env.calls
        .filter((c) => c.path.endsWith("/favorites/browse"))
        .map((c) => [c.body.folder_page, c.body.page]),
    ).toEqual([
      [2, 1],
      [2, 2],
    ]);
    expect(env.calls.some((c) => /download|subtitles|media/.test(c.path))).toBe(false);
  });

  it("我收藏的目录保留相同数值ID的收藏夹与合集类型", async () => {
    const env = fixture();
    render(<FavoriteBrowser disabled={false} onSelect={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "浏览账号收藏夹" }));
    await screen.findByRole("button", { name: "合成收藏夹1（21）" });
    fireEvent.click(screen.getByRole("button", { name: "我收藏的合集和收藏夹" }));
    fireEvent.click(await screen.findByRole("button", { name: "合集 · 合成关注合集（21）" }));
    await screen.findByRole("region", { name: "收藏目录视频" });
    expect(env.calls.find((c) => c.path.endsWith("/favorites/browse"))?.body).toMatchObject({
      kind: "collected",
      folder_id: "500",
      resource_type: "collection",
      folder_page: 1,
    });
  });

  it("连接变化使旧页回调失效，未自动重读或保留旧视频按钮", async () => {
    const env = fixture();
    let finish!: (value: Response) => void;
    env.fetch.mockImplementationOnce(
      async () =>
        new Response(
          JSON.stringify([
            {
              platform: "bilibili",
              enabled: true,
              connection_id: "account-a",
              connection_state: "connected",
            },
          ]),
        ),
    );
    env.fetch.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(<FavoriteBrowser disabled={false} onSelect={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "浏览账号收藏夹" }));
    await waitFor(() => expect(finish).toBeTruthy());
    act(() => {
      window.dispatchEvent(new Event(INTEGRATIONS_CHANGED));
    });
    await act(async () => {
      finish(
        new Response(
          JSON.stringify({
            kind: "created",
            page: 1,
            total: 1,
            has_more: false,
            items: [{ folder_id: "old", title: "过期目录", count: 1 }],
          }),
        ),
      );
    });
    expect(screen.queryByText(/过期目录/)).toBeNull();
    expect(screen.getByRole("alert").textContent).toContain("重新浏览收藏夹");
    expect(env.fetch).toHaveBeenCalledTimes(2);
  });

  it("收藏选择绑定原连接，换账号后不能用新账号读取旧选择", async () => {
    const env = fixture();
    render(<BilibiliImport courseId="a" connected onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "导入B站链接" }));
    fireEvent.click(screen.getByRole("button", { name: "浏览账号收藏夹" }));
    fireEvent.click(await screen.findByRole("button", { name: "合成收藏夹1（21）" }));
    const videos = await screen.findByRole("region", { name: "收藏目录视频" });
    fireEvent.click(within(videos).getByRole("button", { name: "正常视频" }));
    env.changeConnection("account-b");
    fireEvent.click(screen.getByRole("button", { name: "读取链接" }));
    await screen.findByText(/BILI_FAVORITE_CONNECTION_CHANGED/);
    expect(env.calls.some((c) => c.path.endsWith("/inspect"))).toBe(false);
  });
});
