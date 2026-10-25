import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { OfflineReader } from "../src/offline/OfflineReader";
import type { OfflineDocument } from "../src/offline/model";

afterEach(cleanup);

function example(): OfflineDocument {
  return {
    format: "learning-offline-v1",
    title: "合成离线资料",
    kind: "book",
    role: "mixed",
    exported_at: "2026-10-24",
    revision_hash: "a".repeat(64),
    coverage: "本版保存内容",
    sources: [
      { id: "s0", label: "原选区", role: "original", text: "只读所选文字。", location: {} },
    ],
    linked_questions: {},
    chapters: [],
    blocks: [
      {
        block_id: "b0",
        kind: "question",
        markdown: "选择正确选项。",
        attribution: "ai_supplement",
        source_snapshot_ids: ["s0"],
        question: {
          question_type: "single_choice",
          reference_answer: "A",
          answer_authority: "ai_reference",
          explanation: "A与条件一致。",
          options: [
            { option_id: "A", text: "正确条件" },
            { option_id: "B", text: "其他条件" },
          ],
        },
      },
      {
        block_id: "b1",
        kind: "card",
        markdown: "先回忆，再翻面。",
        attribution: "user_authored",
        card: { back: "本卡背面" },
      },
    ],
  };
}

describe("独立离线阅读", () => {
  it.each(["jpeg", "webp"])("renders an exact packaged %s source image", (extension) => {
    const value = example();
    value.sources[0].image = `assets/${"a".repeat(64)}.${extension}`;
    const { container } = render(<OfflineReader document={value} />);
    expect(container.querySelector("img")).toHaveAttribute("src", value.sources[0].image);
  });

  it.each([`assets/${"a".repeat(64)}.svg`, "../outside.png", "https://bad.invalid/image.png"])(
    "rejects an unapproved source image path %s",
    (image) => {
      const value = example();
      value.sources[0].image = image;
      const { container } = render(<OfflineReader document={value} />);
      expect(container.querySelector("img")).toBeNull();
    },
  );
  it("retains old scope text with its explicit later scope notice", () => {
    const value = example();
    value.notices = ["当前任务按调整后的范围完成。"];
    value.block_notices = { b0: "本块保留较早范围的内容。" };
    render(<OfflineReader document={value} />);
    expect(screen.getByText("当前任务按调整后的范围完成。")).toBeTruthy();
    expect(screen.getByText("本块保留较早范围的内容。")).toBeTruthy();
    expect(screen.getByText("选择正确选项。")).toBeTruthy();
    expect(screen.getByText("先回忆，再翻面。")).toBeTruthy();
  });
  it("shows local answer comparison without official scores or persisted review state", () => {
    const first = render(<OfflineReader document={example()} />);
    expect(screen.queryByText("A与条件一致。")).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: "B. 其他条件" }));
    fireEvent.click(screen.getByRole("button", { name: "对照参考答案" }));
    expect(screen.getByText("与参考选项不一致，请核对解析。")).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "A. 正确条件" }));
    fireEvent.click(screen.getByRole("button", { name: "对照参考答案" }));
    expect(screen.getByText("与参考选项一致。")).toBeTruthy();
    first.unmount();
    render(<OfflineReader document={example()} />);
    expect(screen.getByRole("radio", { name: "A. 正确条件" })).not.toBeChecked();
    expect(screen.queryByText("与参考选项一致。")).toBeNull();
  });

  it("keeps recall local and exposes the exact saved source without loading remote images", () => {
    const value = example();
    value.blocks.push({
      block_id: "b2",
      kind: "markdown",
      attribution: "user_authored",
      markdown: "<script>window.BAD=1</script>\n![remote](https://bad.invalid/a.png)",
    });
    const { container } = render(<OfflineReader document={value} />);
    fireEvent.click(screen.getByRole("button", { name: "翻面核对" }));
    expect(screen.getByText("本卡背面")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "已回忆" }));
    expect(screen.getByText("本次标为已回忆，不改变正式复习安排。")).toBeTruthy();
    expect(screen.getByText("只读所选文字。")).toBeTruthy();
    expect(container.querySelector("script,iframe,img")).toBeNull();
  });

  it("book navigation shows only existing pinned pages and permits free movement", () => {
    const value = example();
    value.chapters = [
      {
        id: "c0",
        title: "原章",
        objective: "自由阅读",
        pages: [
          { id: "p0", title: "练习页", block_ids: ["b0"] },
          { id: "p1", title: "卡片页", block_ids: ["b1"] },
        ],
      },
    ];
    render(<OfflineReader document={value} />);
    expect(screen.queryByRole("button", { name: "翻面核对" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "卡片页" }));
    expect(screen.getByRole("button", { name: "翻面核对" })).toBeTruthy();
    expect(screen.queryByRole("radio")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    expect(screen.getByRole("radio", { name: "A. 正确条件" })).toBeTruthy();
  });
});
