import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { OfflineChatReader } from "../src/offline-chat/OfflineChatReader";
import { localAttachment, localChat, localSibling } from "../src/offline-chat/model";
import type { OfflineChatPage } from "../src/offline-chat/model";

afterEach(cleanup);

it("keeps partial answer status, literal unsafe text and only canonical local images", () => {
  const hash = "a".repeat(64);
  render(
    <OfflineChatReader
      document={{
        format: "learning-offline-chat-v1",
        course: "课程",
        title: "聊天",
        messages: [
          {
            id: "m1",
            role: "assistant",
            text: "保留部分 $x^2$。 <script>BAD()</script>",
            status: "unknown",
            created_at: "2026-10-25",
            sources: ["教材.pdf · PDF第2页 · 原始资料"],
            attachments: [
              {
                filename: "确认图",
                media_type: "image/png",
                path: `../../data/attachments/objects/aa/${hash}`,
              },
              {
                filename: "外部图",
                media_type: "image/png",
                path: "https://invalid.example/image.png",
              },
            ],
          },
        ],
      }}
    />,
  );
  expect(screen.getByText(/原结果待核对/)).toBeTruthy();
  expect(screen.getAllByRole("img")).toHaveLength(1);
  expect(document.querySelector(".katex")).toBeTruthy();
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getByText("外部图（附件路径不可用）")).toBeTruthy();
});

it("moves across message windows without losing the final saved message", () => {
  const value: OfflineChatPage = {
    format: "learning-offline-chat-v1",
    course: "课",
    title: "长聊天",
    messages: Array.from({ length: 205 }, (_, index) => ({
      id: `m${index}`,
      role: "user",
      text: `消息${index}`,
      status: "published",
      created_at: "2026-10-25",
      sources: [],
      attachments: [],
    })),
  };
  render(<OfflineChatReader document={value} />);
  expect(screen.queryByText("消息204")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "最新消息" }));
  expect(screen.getByText("消息204")).toBeTruthy();
  expect(screen.queryByText("消息0")).toBeNull();
});

it("rejects remote, traversal, ADS and digest-prefix-confused resource paths", () => {
  expect(localAttachment(`../../data/attachments/objects/ab/${"a".repeat(64)}`)).toBeUndefined();
  for (const path of [
    "https://example.com/a.png",
    "../../secret.png",
    "data:image/png;base64,abc",
    "../../data/attachments/objects/aa/a:stream",
  ]) {
    expect(localAttachment(path)).toBeUndefined();
  }
  expect(localChat("chats/../other.html")).toBeUndefined();
  expect(localSibling("../../other.html")).toBeUndefined();
});
