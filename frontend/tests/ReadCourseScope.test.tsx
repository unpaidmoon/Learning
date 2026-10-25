import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { api } from "../src/api/client";
import { ReadCourseScope } from "../src/workbench/ReadCourseScope";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.clear();
});

test("course choices save only an explicit draft scope and never submit a question", async () => {
  let revision = 0;
  const original = {
    text: "尚未发送的问题",
    attachment_ids: [],
    references: [],
    source_snapshot_ids: [],
    conditions: [],
    corrections: [],
    model: "deepseek-flash",
    thinking: "high",
    read_course_ids: [],
  };
  vi.spyOn(api, "read").mockImplementation(async (path) => {
    if (path.startsWith("/desktop/sessions/"))
      return { course_id: "owner", chat_id: "scope_choice_chat" } as never;
    if (path === "/courses")
      return [
        { course_id: "owner", title: "当前教材" },
        { course_id: "other", title: "对照资料" },
      ] as never;
    if (path.includes("/draft")) return { ...original, revision } as never;
    throw new Error("Unexpected read " + path);
  });
  const write = vi
    .spyOn(api, "write")
    .mockImplementation(
      async (_path, body) => ({ ...(body as object), revision: ++revision }) as never,
    );
  render(<ReadCourseScope courseId="owner" sessionId="scope_session" />);
  const other = await screen.findByRole("checkbox", { name: "对照资料" });
  await waitFor(() => expect(other).not.toBeDisabled());
  fireEvent.click(other);
  await waitFor(() =>
    expect(write).toHaveBeenLastCalledWith(
      "/chats/scope_choice_chat/draft",
      expect.objectContaining({
        text: original.text,
        read_course_ids: ["owner", "other"],
        model: original.model,
      }),
      expect.anything(),
    ),
  );
  fireEvent.click(screen.getByRole("checkbox", { name: "当前教材（当前课程）" }));
  await waitFor(() =>
    expect(write).toHaveBeenLastCalledWith(
      expect.anything(),
      expect.objectContaining({ read_course_ids: ["other"] }),
      expect.anything(),
    ),
  );
  expect(screen.getByRole("checkbox", { name: "对照资料" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "仅当前课程" }));
  await waitFor(() =>
    expect(write).toHaveBeenLastCalledWith(
      expect.anything(),
      expect.objectContaining({ read_course_ids: [] }),
      expect.anything(),
    ),
  );
  expect(write.mock.calls.every(([path]) => path.endsWith("/draft"))).toBe(true);
});
