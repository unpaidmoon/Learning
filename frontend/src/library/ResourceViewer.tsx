import { useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { Icon } from "../components/ui/Icon";

/** Every URL here is a version-bound local business resource, never answer HTML. */
export function ResourceViewer({ file }: { file: Schema<"CourseFileOpenResponse"> }) {
  const [view, setView] = useState({
    file,
    text: "",
    error: "",
    loading: file.viewer === "text",
    busy: false,
  });
  const { text, error, loading, busy } =
    view.file === file
      ? view
      : { text: "", error: "", loading: file.viewer === "text", busy: false };
  useEffect(() => {
    const controller = new AbortController();
    // Reopening the same business version is a new read, so failed state and
    // response bodies from its preceding attempt cannot survive into this one.
    void Promise.resolve()
      .then(async () => {
        if (controller.signal.aborted) return;
        setView({ file, text: "", error: "", loading: file.viewer === "text", busy: false });
        if (file.viewer !== "text" || !file.content_url) return;
        const response = await fetch(file.content_url, {
          credentials: "same-origin",
          cache: "no-store",
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        if (!response.ok)
          throw new Error("原文件缺失或内容已变化，请重新打开当前版本（SOURCE_UNAVAILABLE）");
        const value = await response.text();
        if (controller.signal.aborted) return;
        if (value.length > 2_000_000)
          throw new Error("文本超过当前阅读上限，请使用系统程序打开（TEXT_TOO_LARGE）");
        setView({ file, text: value, error: "", loading: false, busy: false });
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setView({
            file,
            text: "",
            error: reason instanceof Error ? reason.message : errorMessage(reason),
            loading: false,
            busy: false,
          });
      });
    return () => controller.abort();
  }, [file]);
  const openExternal = async () => {
    setView((current) => (current.file === file ? { ...current, busy: true } : current));
    try {
      await api.write(`/courses/${file.course_id}/files/external`, { path: file.entry.path });
    } catch (reason) {
      setView((current) =>
        current.file === file ? { ...current, error: errorMessage(reason) } : current,
      );
    } finally {
      setView((current) => (current.file === file ? { ...current, busy: false } : current));
    }
  };
  return (
    <div className={`resource-viewer resource-viewer-${file.viewer}`}>
      {error && (
        <p role="alert" className="inline-error">
          {error}
        </p>
      )}
      {file.viewer === "image" && file.content_url ? (
        <img
          className="document-image"
          src={file.content_url}
          alt={file.entry.name}
          key={`${file.asset_id}:${file.source_version}:${file.content_url}`}
          onLoad={() =>
            setView((current) => (current.file === file ? { ...current, error: "" } : current))
          }
          onError={() =>
            setView((current) =>
              current.file === file
                ? { ...current, error: "图片无法读取，请核对原文件与版本（SOURCE_UNAVAILABLE）" }
                : current,
            )
          }
        />
      ) : file.viewer === "text" && !error ? (
        loading ? (
          <p className="muted" role="status">
            正在读取资料…
          </p>
        ) : (
          <pre className="document-text" aria-label="资料正文">
            {text}
          </pre>
        )
      ) : (
        <div className="unsupported-document">
          <Icon name="file" size={34} />
          <h2>{file.entry.name}</h2>
          <p>
            {file.viewer === "pdf"
              ? "PDF 阅读将在后续阶段接入；当前可使用系统默认程序打开。"
              : "当前格式使用系统默认程序打开。"}
          </p>
          <Button variant="outline" disabled={busy} onClick={() => void openExternal()}>
            {busy ? "正在打开…" : "使用系统默认程序打开"}
          </Button>
          <small>外部程序的内容不属于本软件截图范围。</small>
        </div>
      )}
    </div>
  );
}
