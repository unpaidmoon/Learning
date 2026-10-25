import { useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { useDraft } from "../chat/draft";
import { Button } from "../components/ui/button";

export function ReadCourseScope({ courseId, sessionId }: { courseId: string; sessionId?: string }) {
  const [binding, setBinding] = useState<Schema<"DesktopSessionResponse"> | null>(null);
  const [courses, setCourses] = useState<Schema<"CourseResponse">[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let live = true;
    if (!sessionId) return;
    void Promise.all([
      api.read<Schema<"DesktopSessionResponse">>(`/desktop/sessions/${sessionId}`),
      api.read<Schema<"CourseResponse">[]>("/courses"),
    ])
      .then(([current, available]) => {
        if (!live) return;
        if (current.course_id !== courseId) throw new Error("读取范围与原对话课程不符");
        setBinding(current);
        setCourses(available);
      })
      .catch((reason) => {
        if (live) setError(errorMessage(reason));
      });
    return () => {
      live = false;
    };
  }, [courseId, sessionId]);
  if (!sessionId) return null;
  return (
    <section aria-label="本对话资料读取范围">
      <h4>本对话资料读取范围</h4>
      <p>默认当前课程。明确选择其他课程后，AI 可按需查阅这些课程；结果仍归当前对话。</p>
      {binding && (
        <BoundScope
          key={binding.chat_id}
          chatId={binding.chat_id}
          courseId={courseId}
          courses={courses}
        />
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}

function BoundScope({
  chatId,
  courseId,
  courses,
}: {
  chatId: string;
  courseId: string;
  courses: Schema<"CourseResponse">[];
}) {
  const draft = useDraft(chatId);
  const [error, setError] = useState("");
  const selected = draft.value.read_course_ids?.length ? draft.value.read_course_ids : [courseId];
  const change = (identity: string, checked: boolean) => {
    const next = checked ? [...selected, identity] : selected.filter((value) => value !== identity);
    if (!next.length || next.length > 4) return;
    draft.controller.update({
      read_course_ids: next.length === 1 && next[0] === courseId ? [] : next,
    });
    void draft.controller.flush().catch((reason) => setError(errorMessage(reason)));
  };
  return (
    <>
      <fieldset disabled={!draft.ready} aria-label="选择可读取的课程">
        {courses.map((course) => (
          <label key={course.course_id}>
            <input
              type="checkbox"
              checked={selected.includes(course.course_id)}
              disabled={
                (selected.length === 1 && selected[0] === course.course_id) ||
                (selected.length >= 4 && !selected.includes(course.course_id))
              }
              onChange={(event) => change(course.course_id, event.target.checked)}
            />
            {course.title}
            {course.course_id === courseId ? "（当前课程）" : ""}
          </label>
        ))}
      </fieldset>
      <Button
        variant="outline"
        disabled={!draft.ready || !draft.value.read_course_ids?.length}
        onClick={() => {
          draft.controller.update({ read_course_ids: [] });
          void draft.controller.flush().catch((reason) => setError(errorMessage(reason)));
        }}
      >
        仅当前课程
      </Button>
      {(error || draft.error) && <p role="alert">{error || draft.error}</p>}
    </>
  );
}
