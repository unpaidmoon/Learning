import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { BilibiliFileIcon } from "../src/integrations/BilibiliFileIcon";

afterEach(cleanup);

describe("B站资源图标与本地封面入口", () => {
  it("封面只使用同源课程与版本入口，失败后保留专用图标", () => {
    render(
      <BilibiliFileIcon
        courseId="course-a"
        assetId="asset-a"
        sourceVersion="version-a"
        size={42}
        preview
      />,
    );
    const cover = screen.getByRole("img", { name: "B站视频封面" });
    expect(cover.getAttribute("src")).toBe(
      "/api/v1/courses/course-a/integrations/asset-a/cover?source_version=version-a",
    );
    expect(cover.getAttribute("loading")).toBe("lazy");
    fireEvent.error(cover);
    expect(screen.queryByRole("img", { name: "B站视频封面" })).toBeNull();
    expect(screen.getByRole("img", { name: "B站视频" })).toBeTruthy();
  });

  it("列表视图保留小图标并不读取封面", () => {
    render(
      <BilibiliFileIcon
        courseId="course-a"
        assetId="asset-a"
        sourceVersion="version-a"
        size={19}
        preview={false}
      />,
    );
    expect(screen.queryByRole("img", { name: "B站视频封面" })).toBeNull();
    expect(screen.getByRole("img", { name: "B站视频" }).getAttribute("width")).toBe("19");
  });
});
