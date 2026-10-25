import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { roles } from "./model";
import type { PickedSource } from "./model";
import { TaskCourseSelect } from "./TaskCourseSelect";

const audio = /\.(mp3|wav|flac|m4a|aac|ogg|opus)$/i;

export function TaskSourcePicker({
  courseId: ownerCourseId,
  value,
  disabled,
  onChange,
  onBusyChange,
}: {
  courseId: string;
  value: PickedSource[];
  disabled: boolean;
  onChange: (sources: PickedSource[]) => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [courseId, setCourseId] = useState(ownerCourseId);
  const [courseTitle, setCourseTitle] = useState("");
  const fileKey = (path: string) =>
    `file:${courseId === ownerCourseId ? "" : courseId + ":"}${path}`;
  const [directory, setDirectory] = useState("");
  const [listing, setListing] = useState<Schema<"CourseFileListResponse"> | null>(null);
  const [artifacts, setArtifacts] = useState<Schema<"ArtifactIndexPage"> | null>(null);
  const [tab, setTab] = useState<"files" | "artifacts">("files");
  const [busy, setBusy] = useState(false);
  const [pendingFile, setPendingFile] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const owner = useRef(courseId);
  useEffect(() => {
    owner.current = courseId;
    return () => {
      owner.current = "";
    };
  }, [courseId]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);
  useEffect(() => {
    let live = true;
    if (tab === "files")
      void api
        .read<Schema<"CourseFileListResponse">>(
          `/courses/${courseId}/files?path=${encodeURIComponent(directory)}`,
        )
        .then((page) => {
          if (live) setListing(page);
        })
        .catch((reason) => {
          if (live) setNotice(errorMessage(reason));
        });
    else
      void api
        .read<Schema<"ArtifactIndexPage">>(`/courses/${courseId}/learning/artifact-index?limit=100`)
        .then((page) => {
          if (live) setArtifacts(page);
        })
        .catch((reason) => {
          if (live) setNotice(errorMessage(reason));
        });
    return () => {
      live = false;
    };
  }, [courseId, directory, tab]);
  const pickFile = async (entry: Schema<"CourseFileEntry">) => {
    if (disabled || busy) return;
    const key = fileKey(entry.path);
    if (value.some((item) => item.key === key)) {
      onChange(value.filter((item) => item.key !== key));
      return;
    }
    setBusy(true);
    setPendingFile(key);
    onBusyChange?.(true);
    const requestedCourse = courseId;
    try {
      const opened = await api.write<Schema<"CourseFileOpenResponse">>(
        `/courses/${courseId}/files/open`,
        { path: entry.path },
      );
      if (owner.current !== requestedCourse || opened.course_id !== requestedCourse) return;
      const transcript = opened.viewer === "video" || audio.test(entry.path);
      onChange([
        ...value,
        {
          key,
          title:
            (courseId === ownerCourseId ? "" : courseTitle + " · ") +
            entry.path +
            (transcript ? " · 有效字幕" : ""),
          declaration: transcript
            ? {
                kind: "transcript",
                ...(courseId !== ownerCourseId ? { source_course_id: courseId } : {}),
                asset_id: opened.asset_id,
                source_version: opened.source_version,
                start_ms: 0,
              }
            : {
                kind: "document",
                ...(courseId !== ownerCourseId ? { source_course_id: courseId } : {}),
                asset_id: opened.asset_id,
                source_version: opened.source_version,
                first_page: 0,
                text_only: false,
              },
        },
      ]);
    } catch (reason) {
      if (owner.current === requestedCourse) setNotice(errorMessage(reason));
    } finally {
      if (owner.current === requestedCourse) {
        setBusy(false);
        setPendingFile(null);
        onBusyChange?.(false);
      }
    }
  };
  return (
    <fieldset className="workbench-source-picker" disabled={disabled || busy}>
      <legend>明确选择资料与已有成果</legend>
      <TaskCourseSelect
        ownerCourseId={ownerCourseId}
        value={courseId}
        onChange={(id, title) => {
          setCourseId(id);
          setCourseTitle(title);
          setDirectory("");
          setListing(null);
          setArtifacts(null);
          setNotice("");
        }}
      />
      <div className="workbench-toolbar">
        <Button variant="ghost" aria-pressed={tab === "files"} onClick={() => setTab("files")}>
          课程原件与字幕
        </Button>
        <Button
          variant="ghost"
          aria-pressed={tab === "artifacts"}
          onClick={() => setTab("artifacts")}
        >
          笔记与学习成果
        </Button>
      </div>
      {tab === "files" ? (
        <>
          {directory && (
            <Button
              variant="ghost"
              onClick={() => setDirectory(directory.split("/").slice(0, -1).join("/"))}
            >
              返回上级
            </Button>
          )}
          {listing?.entries.map((entry) =>
            entry.kind === "directory" ? (
              <Button variant="ghost" key={entry.path} onClick={() => setDirectory(entry.path)}>
                {entry.name} /
              </Button>
            ) : (["text", "pdf", "video"].includes(entry.viewer) || audio.test(entry.path)) &&
              entry.kind === "file" ? (
              <label key={entry.path}>
                <input
                  type="checkbox"
                  checked={
                    pendingFile === fileKey(entry.path) ||
                    value.some((item) => item.key === fileKey(entry.path))
                  }
                  onChange={() => void pickFile(entry)}
                />
                {entry.name}
                {entry.viewer === "video" || audio.test(entry.path) ? " · 有效字幕" : ""}
              </label>
            ) : null,
          )}
        </>
      ) : (
        <>
          {artifacts?.items.map((item) => (
            <label key={item.artifact_id}>
              <input
                type="checkbox"
                checked={value.some((v) => v.key === `artifact:${item.artifact_id}`)}
                onChange={(e) =>
                  onChange(
                    e.target.checked
                      ? [
                          ...value,
                          {
                            key: `artifact:${item.artifact_id}`,
                            title:
                              (courseId === ownerCourseId ? "" : courseTitle + " · ") + item.title,
                            declaration: {
                              kind: "artifact",
                              ...(courseId !== ownerCourseId ? { source_course_id: courseId } : {}),
                              artifact_id: item.artifact_id,
                              revision_id: item.head_revision_id,
                            },
                          },
                        ]
                      : value.filter((v) => v.key !== `artifact:${item.artifact_id}`),
                  )
                }
              />
              {item.title} · {roles[item.content_role]}
            </label>
          ))}
          {artifacts?.next_cursor && (
            <Button
              variant="ghost"
              onClick={() => {
                const requestedCourse = courseId;
                setBusy(true);
                onBusyChange?.(true);
                void api
                  .read<Schema<"ArtifactIndexPage">>(
                    `/courses/${courseId}/learning/artifact-index?limit=100&cursor=${encodeURIComponent(artifacts.next_cursor!)}`,
                  )
                  .then((page) => {
                    if (owner.current === requestedCourse)
                      setArtifacts({ ...page, items: [...artifacts.items, ...page.items] });
                  })
                  .catch((reason) => {
                    if (owner.current === requestedCourse) setNotice(errorMessage(reason));
                  })
                  .finally(() => {
                    if (owner.current === requestedCourse) {
                      setBusy(false);
                      onBusyChange?.(false);
                    }
                  });
              }}
            >
              更多成果
            </Button>
          )}
        </>
      )}
      {!!value.length && (
        <ul aria-label="已选来源">
          {value.map((item) => (
            <li key={item.key}>
              {item.title}
              <Button
                variant="ghost"
                onClick={() => onChange(value.filter((v) => v.key !== item.key))}
              >
                移除
              </Button>
            </li>
          ))}
        </ul>
      )}
      {notice && <p role="status">{notice}</p>}
      {pendingFile && <p role="status">正在选择资料…</p>}
    </fieldset>
  );
}
