import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Schema } from "../src/api/client";
import { ResourceViewer } from "../src/library/ResourceViewer";

const file: Schema<"CourseFileOpenResponse"> = {
  course_id: "course-synthetic",
  asset_id: "asset-synthetic",
  source_version: "version-original",
  viewer: "text",
  content_url: "/api/v1/course-files/asset-synthetic/content?source_version=version-original",
  video: null,
  entry: {
    name: "合成资料.txt",
    path: "合成资料.txt",
    kind: "file",
    viewer: "text",
    size_bytes: 20,
    asset_id: "asset-synthetic",
    source_version: "version-original",
  },
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("资料重新读取", () => {
  it("原件恢复后重新打开同一版本，会清除旧错误并展示恢复内容", async () => {
    const request = vi
      .fn()
      .mockResolvedValueOnce(new Response("", { status: 409 }))
      .mockResolvedValueOnce(new Response("已恢复的合成正文"));
    vi.stubGlobal("fetch", request);
    const page = render(<ResourceViewer file={file} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("SOURCE_UNAVAILABLE");

    page.rerender(<ResourceViewer file={{ ...file }} />);
    expect(await screen.findByLabelText("资料正文")).toHaveTextContent("已恢复的合成正文");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("同一版本新读取完成后，旧请求迟到的正文不能覆盖新结果", async () => {
    let releaseOldBody: (value: string) => void = () => {};
    const oldBody = new Promise<string>((resolve) => {
      releaseOldBody = resolve;
    });
    const oldResponse = new Response("");
    const oldRead = vi.spyOn(oldResponse, "text").mockReturnValue(oldBody);
    const request = vi
      .fn()
      .mockResolvedValueOnce(oldResponse)
      .mockResolvedValueOnce(new Response("最新读取的合成正文"));
    vi.stubGlobal("fetch", request);
    const page = render(<ResourceViewer file={file} />);
    await waitFor(() => expect(oldRead).toHaveBeenCalledOnce());

    page.rerender(<ResourceViewer file={{ ...file }} />);
    expect(await screen.findByLabelText("资料正文")).toHaveTextContent("最新读取的合成正文");
    await act(async () => {
      releaseOldBody("迟到的旧正文");
      await oldBody;
    });
    expect(screen.getByLabelText("资料正文")).toHaveTextContent("最新读取的合成正文");
    expect(screen.queryByText("迟到的旧正文")).not.toBeInTheDocument();
    const oldRequest = request.mock.calls[0][1] as RequestInit;
    expect(oldRequest.signal?.aborted).toBe(true);
  });
});
