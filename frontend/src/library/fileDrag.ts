import { api } from "../api/client";
import type { Schema } from "../api/client";

export const COURSE_FILES_FORMAT = "application/x-learning-course-files";
export type CourseFileDrag = { courseId: string; paths: string[] };

const attachmentType = (name: string) => {
  const extension = name.split(".").at(-1)?.toLowerCase() || "";
  return (
    {
      png: "image/png",
      jpg: "image/jpeg",
      jpeg: "image/jpeg",
      webp: "image/webp",
      pdf: "application/pdf",
      txt: "text/plain",
      md: "text/markdown",
      markdown: "text/markdown",
    } as Record<string, string>
  )[extension];
};

/** Read only the explicitly dropped files through existing version-bound routes. */
export async function readCourseDragFiles(value: CourseFileDrag): Promise<File[]> {
  if (
    !value ||
    typeof value.courseId !== "string" ||
    !value.courseId ||
    !Array.isArray(value.paths) ||
    !value.paths.length ||
    value.paths.length > 8 ||
    value.paths.some(
      (path) =>
        typeof path !== "string" ||
        !path ||
        path.split("/").some((part) => !part || part === "." || part === "..") ||
        path.includes("\\"),
    )
  )
    throw new Error("拖动来源无效，请重新选择文件（FILE_DROP_SOURCE_INVALID）");
  const prefix = `/courses/${encodeURIComponent(value.courseId)}/files`;
  const listings = new Map<string, Schema<"CourseFileListResponse">>();
  const entries: Schema<"CourseFileEntry">[] = [];
  for (const path of new Set(value.paths)) {
    const parent = path.split("/").slice(0, -1).join("/");
    if (!listings.has(parent))
      listings.set(parent, await api.read(`${prefix}?path=${encodeURIComponent(parent)}`));
    const entry = listings.get(parent)!.entries.find((entry) => entry.path === path);
    if (
      !entry ||
      entry.kind !== "file" ||
      !attachmentType(entry.name) ||
      !["text", "pdf", "image"].includes(entry.viewer)
    )
      throw new Error(
        "请添加图片、PDF、TXT 或 Markdown 文件；文件夹和视频不能作为附件（FILE_ATTACHMENT_UNSUPPORTED）",
      );
    const maximum =
      entry.viewer === "pdf"
        ? 20 * 1024 * 1024
        : entry.viewer === "image"
          ? 10_000_000
          : 1024 * 1024;
    if (!entry.size_bytes || entry.size_bytes > maximum)
      throw new Error("文件为空或超过附件大小上限（FILE_ATTACHMENT_SIZE_LIMIT）");
    entries.push(entry);
  }
  const files: File[] = [];
  for (const entry of entries) {
    const opened = await api.write<Schema<"CourseFileOpenResponse">>(`${prefix}/open`, {
      path: entry.path,
    });
    const expected = `/api/v1/course-files/${encodeURIComponent(opened.asset_id)}/content?source_version=${encodeURIComponent(opened.source_version)}`;
    if (
      opened.course_id !== value.courseId ||
      opened.entry.path !== entry.path ||
      opened.content_url !== expected ||
      opened.viewer !== entry.viewer ||
      (entry.source_version && opened.source_version !== entry.source_version)
    )
      throw new Error("文件来源已变化，请重新选择（FILE_ATTACHMENT_SOURCE_CHANGED）");
    const response = await fetch(expected, {
      credentials: "same-origin",
      cache: "no-store",
      signal: AbortSignal.timeout(20_000),
    });
    if (!response.ok)
      throw new Error("原文件缺失或内容已变化，请重新选择（FILE_ATTACHMENT_SOURCE_UNAVAILABLE）");
    const blob = await response.blob();
    if (blob.size !== opened.entry.size_bytes || blob.size !== entry.size_bytes)
      throw new Error("文件内容已变化，请重新选择（FILE_ATTACHMENT_SOURCE_CHANGED）");
    files.push(new File([blob], entry.name, { type: attachmentType(entry.name) }));
  }
  return files;
}

/** Keep internal file gestures out of Harness's document-wide upload intake. */
export function installCourseFileDrag({
  canAttach,
  onAttach,
  onError,
}: {
  canAttach: () => boolean;
  onAttach: (value: CourseFileDrag) => Promise<void>;
  onError: (error: unknown) => void;
}) {
  let pending = false;
  let highlighted: Element | null = null;
  const reset = () => {
    highlighted?.classList.remove("learning-file-drop-target");
    highlighted = null;
  };
  const internal = (event: DragEvent) => event.dataTransfer?.types.includes(COURSE_FILES_FORMAT);
  const composer = (event: DragEvent) =>
    event.target instanceof Element
      ? event.target.closest('[data-slot="conversation.composer"]')
      : null;
  const over = (event: DragEvent) => {
    if (!internal(event)) return;
    reset();
    if (event.target instanceof Element && event.target.closest(".file-manager")) return;
    event.preventDefault();
    event.stopPropagation();
    const target = composer(event);
    const accepted = !!target && !pending && canAttach();
    if (event.dataTransfer) event.dataTransfer.dropEffect = accepted ? "copy" : "none";
    if (accepted) {
      highlighted = target;
      target.classList.add("learning-file-drop-target");
    }
  };
  const leave = (event: DragEvent) => {
    if (!internal(event)) return;
    if (!(event.relatedTarget instanceof Node) || !highlighted?.contains(event.relatedTarget))
      reset();
  };
  const drop = (event: DragEvent) => {
    if (!internal(event)) return;
    reset();
    if (event.target instanceof Element && event.target.closest(".file-manager")) return;
    event.preventDefault();
    event.stopPropagation();
    if (!composer(event)) return;
    if (pending || !canAttach()) {
      onError(new Error("当前输入框暂时无法添加附件，请稍后重试（FILE_ATTACHMENT_BUSY）"));
      return;
    }
    let value: CourseFileDrag;
    try {
      value = JSON.parse(event.dataTransfer!.getData(COURSE_FILES_FORMAT)) as CourseFileDrag;
    } catch {
      onError(new Error("拖动来源无效，请重新选择文件（FILE_DROP_SOURCE_INVALID）"));
      return;
    }
    pending = true;
    void Promise.resolve()
      .then(() => onAttach(value))
      .catch(onError)
      .finally(() => {
        pending = false;
      });
  };
  document.addEventListener("dragenter", over, true);
  document.addEventListener("dragover", over, true);
  document.addEventListener("dragleave", leave, true);
  document.addEventListener("drop", drop, true);
  window.addEventListener("dragend", reset);
  return () => {
    reset();
    document.removeEventListener("dragenter", over, true);
    document.removeEventListener("dragover", over, true);
    document.removeEventListener("dragleave", leave, true);
    document.removeEventListener("drop", drop, true);
    window.removeEventListener("dragend", reset);
  };
}
