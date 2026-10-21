import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { VideoWorkspace } from "../src/media/VideoWorkspace";
import type { VideoWorkspaceHandle } from "../src/media/VideoWorkspace";
import type { Schema } from "../src/api/client";

const asset: Schema<"MediaAssetResponse"> = {
  asset_id: "asset-synthetic",
  source_version: "source-one",
  display_name: "合成数学课.mp4",
  availability: "available",
  position_ms: 3500,
  position_revision: 4,
  last_opened_at: null,
  media_type: "video/mp4",
  size_bytes: 12345,
};
const answer = (value: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(value), { status }));
const noop = () => {};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function renderPlayer() {
  const ref = createRef<VideoWorkspaceHandle>();
  render(
    <VideoWorkspace
      ref={ref}
      courseId="course-A"
      connected
      onLibraryChange={noop}
      onCapture={noop}
      onOpenChat={noop}
      onImmersiveChange={noop}
    />,
  );
  return ref;
}

describe("原地视频播放器", () => {
  it("进入课程只列目录，主动打开才请求来源与恢复播放", async () => {
    const request = vi.fn((url: string) => answer(url.endsWith("/videos") ? [asset] : asset));
    vi.stubGlobal("fetch", request);
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    renderPlayer();
    await screen.findByRole("button", { name: /合成数学课/ });
    expect(screen.queryByLabelText("课程视频")).not.toBeInTheDocument();
    expect(request.mock.calls).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: /合成数学课/ }));
    const player = (await screen.findByLabelText("课程视频")) as HTMLVideoElement;
    Object.defineProperty(player, "duration", { configurable: true, value: 30 });
    fireEvent.loadedMetadata(player);
    expect(player.currentTime).toBe(3.5);
    expect(player.src).toContain(
      "/api/v1/videos/asset-synthetic/content?source_version=source-one",
    );
    expect(player.src.startsWith("blob:")).toBe(false);
    expect(play).toHaveBeenCalledOnce();
  });

  it("暂停与seek并发保存串行使用已接受的位置修订", async () => {
    const writes: Array<{ position_ms: number; expected_revision: number }> = [];
    let release: (() => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, options?: RequestInit) => {
        if (url.endsWith("/videos")) return answer([asset]);
        if (url.endsWith("/open")) return answer(asset);
        if (url.endsWith("/position")) {
          const body = JSON.parse(String(options?.body));
          writes.push(body);
          if (writes.length === 1)
            return new Promise<Response>((resolve) => {
              release = () =>
                resolve(
                  new Response(
                    JSON.stringify({ position_ms: body.position_ms, position_revision: 5 }),
                  ),
                );
            });
          return answer({ position_ms: body.position_ms, position_revision: 6 });
        }
        return answer({});
      }),
    );
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    const ref = renderPlayer();
    await screen.findByRole("button", { name: /合成数学课/ });
    await userEvent.click(screen.getByRole("button", { name: /合成数学课/ }));
    const player = (await screen.findByLabelText("课程视频")) as HTMLVideoElement;
    Object.defineProperty(player, "readyState", { configurable: true, value: 4 });
    player.currentTime = 4;
    fireEvent.seeked(player);
    await waitFor(() => expect(writes).toHaveLength(1));
    player.currentTime = 2;
    fireEvent.pause(player);
    expect(writes).toHaveLength(1);
    await act(async () => {
      release?.();
      await ref.current?.savePosition();
    });
    expect(writes.map((value) => [value.position_ms, value.expected_revision])).toEqual([
      [4000, 4],
      [2000, 5],
    ]);
  });

  it("来源版本失效明确提示，不为旧引用打开另一个版本", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/videos")) return answer([asset]);
        return answer(
          { code: "SOURCE_VERSION_CHANGED", message: "原件内容已变化，请检查版本" },
          409,
        );
      }),
    );
    renderPlayer();
    await userEvent.click(await screen.findByRole("button", { name: /合成数学课/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("SOURCE_VERSION_CHANGED");
    expect(screen.queryByLabelText("课程视频")).not.toBeInTheDocument();
  });
});
