import { useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";

export function TaskCourseSelect({
  ownerCourseId,
  value,
  onChange,
  disabled = false,
}: {
  ownerCourseId: string;
  value: string;
  onChange: (courseId: string, title: string) => void;
  disabled?: boolean;
}) {
  const [courses, setCourses] = useState<Schema<"CourseResponse">[] | null>(null);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    let live = true;
    void api.read<Schema<"CourseResponse">[]>("/courses").then(
      (items) => {
        if (live) {
          if (Array.isArray(items)) setCourses(items);
          else setNotice("课程列表格式不符，请稍后重试。");
        }
      },
      (reason) => {
        if (live) setNotice(errorMessage(reason));
      },
    );
    return () => {
      live = false;
    };
  }, [ownerCourseId]);
  return (
    <label>
      任务资料来源课程
      <select
        disabled={disabled}
        value={value}
        onChange={(event) => {
          const id = event.target.value;
          const course = courses?.find((item) => item.course_id === id);
          if (course) onChange(id, course.title);
        }}
      >
        {!courses?.some((item) => item.course_id === value) && (
          <option value={value}>{courses ? "原选择课程不可用" : "读取课程信息…"}</option>
        )}
        {courses?.map((course) => (
          <option key={course.course_id} value={course.course_id}>
            {course.title}
            {course.course_id === ownerCourseId ? "（当前课程）" : ""}
          </option>
        ))}
      </select>
      {notice && <span role="status">{notice}</span>}
    </label>
  );
}
