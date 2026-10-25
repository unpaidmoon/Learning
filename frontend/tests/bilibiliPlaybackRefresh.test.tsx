import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { VideoWorkspace } from "../src/media/VideoWorkspace";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("在线会话刷新改变读取地址，未解码的错误播放器不覆盖已保存进度", async () => {
  const asset = {
    asset_id: "bili-a",
    source_version: "version-a",
    display_name: "合成来源",
    platform: "bilibili",
    availability: "available",
    media_type: "video/mp4",
    position_ms: 3500,
    position_revision: 0,
    size_bytes: null,
    playback_session_id: "one",
  };
  const requests: { url: string; body: Record<string, unknown> }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url: input, body });
      const result = input.endsWith("/videos")
        ? [asset]
        : input.endsWith("/open")
          ? { ...asset, playback_session_id: body.refresh ? "two" : "one" }
          : input.includes("/transcripts")
            ? []
            : {};
      return new Response(JSON.stringify(result), { status: 200 });
    }),
  );
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  render(
    <VideoWorkspace
      courseId="course-a"
      assetId="bili-a"
      connected
      onLibraryChange={() => {}}
      onCapture={() => {}}
      onOpenChat={() => {}}
      onImmersiveChange={() => {}}
    />,
  );
  const video = (await screen.findByLabelText("课程视频")) as HTMLVideoElement;
  expect(video.src).toContain("playback_session=one");
  fireEvent.error(video);
  fireEvent.click(screen.getByRole("button", { name: "重新打开" }));
  await waitFor(() => expect(video.src).toContain("playback_session=two"));
  fireEvent.loadedMetadata(video);
  expect(video.currentTime).toBe(3.5);
  expect(requests.filter((r) => r.url.endsWith("/open")).at(-1)?.body).toEqual({
    source_version: "version-a",
    refresh: true,
  });
  expect(requests.filter((r) => r.url.endsWith("/position"))).toHaveLength(0);
});
