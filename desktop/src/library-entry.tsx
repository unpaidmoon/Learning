// Reuse the application's existing file operations and immutable resource reads.
// The desktop build substitutes only Button and the shared React runtime.
import { useEffect, useRef, useState } from "react";
import type { ComponentProps, ReactNode } from "react";
import { api, errorMessage } from "../../frontend/src/api/client";
import type { Schema } from "../../frontend/src/api/client";
import { Button } from "../../frontend/src/components/ui/button";
import { ResourceViewer as ExistingResourceViewer } from "../../frontend/src/library/ResourceViewer";
import { MarkdownAnswer } from "../../frontend/src/chat/MarkdownAnswer";
import type { QuoteSelection } from "../../frontend/src/chat/selection";
import { FileManager as ExistingFileManager } from "../../frontend/src/library/FileManager";
import { Icon } from "../../frontend/src/components/ui/Icon";
import type { ReadingActionHandler, SourceJump } from "../../frontend/src/reading/actions";

export { CourseExplorer } from "../../frontend/src/library/CourseExplorer";
export { AccountPanel } from "../../frontend/src/account/AccountPanel";
export { DataPanel } from "../../frontend/src/data/DataPanel";
export { RuntimeSettingsPanel } from "../../frontend/src/data/RuntimeSettingsPanel";
export { StartupSettingsControl } from "../../frontend/src/data/StartupSettingsControl";
export { useDraft, saveAllDrafts, getDraftController } from "../../frontend/src/chat/draft";
export { saveCapture, loadCapture } from "../../frontend/src/chat/captureCache";
export { MarkdownAnswer };
export { Icon } from "../../frontend/src/components/ui/Icon";
export { WorkspaceCapture } from "../../frontend/src/media/WorkspaceCapture";
export { CaptureEditor } from "../../frontend/src/media/CaptureEditor";
export { WorkbenchPanel } from "../../frontend/src/workbench/WorkbenchPanel";
export { BookReader } from "../../frontend/src/workbench/BookReader";
export { translatePdfSelection } from "../../frontend/src/reading/translationMemo";
export { captureWorkspace } from "../../frontend/src/media/workspaceSnapshot";
export * from "../../frontend/src/media/capture";
export { quoteFromRange, createSelectionRendering } from "../../frontend/src/chat/selection";

export const initialize = () => api.connect();

type Entry = Schema<"CourseFileEntry">;

function FileTile({ entry, courseId }: { entry: Entry; courseId: string }) {
  const [image, setImage] = useState("");
  const root = useRef<HTMLSpanElement>(null);
  const video = entry.kind === "file" && entry.viewer === "video";
  const extension = entry.name.split(".").at(-1)?.toUpperCase() || "文件";
  const word = /^(?:DOCX?|ODT|RTF)$/.test(extension);
  const kind = entry.kind === "directory" ? "folder" : video ? "video" : word ? "word" : entry.viewer;
  useEffect(() => {
    setImage("");
    if (!video || !root.current) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    let visible = false;
    const read = async () => {
      if (!visible || controller.signal.aborted) return;
      try {
        const endpoint = `/api/v1/courses/${encodeURIComponent(courseId)}/files/preview?path=${encodeURIComponent(entry.path)}`;
        const response = await fetch(endpoint, { credentials: "same-origin", signal: controller.signal });
        if (!response.ok) return;
        const value = await response.json() as Schema<"CourseCoverResponse">;
        if (value.status === "ready" && value.url) {
          const url = new URL(value.url, location.origin);
          if (url.origin === location.origin && url.pathname === `/api/v1/courses/${encodeURIComponent(courseId)}/files/cover.jpg`) {
            if (!controller.signal.aborted) setImage(url.href);
          }
        } else if (++attempts < 20 && !controller.signal.aborted) timer = setTimeout(() => { void read(); }, 2000);
      } catch { /* Keep the file type visible when the derived cover is unavailable. */ }
    };
    const observer = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
      if (visible) { clearTimeout(timer); void read(); }
    }, { rootMargin: "100px" });
    observer.observe(root.current);
    return () => { controller.abort(); clearTimeout(timer); observer.disconnect(); };
  }, [video, courseId, entry.path, entry.size_bytes, entry.modified_at]);
  return <span ref={root} className={`learning-file-tile learning-file-kind-${kind}`} data-file-type={kind}>
    {image ? <img src={image} alt="" loading="lazy" onError={() => setImage("")} /> : <Icon name={entry.kind === "directory" ? "folder" : video ? "video" : entry.viewer === "pdf" ? "file-pdf" : entry.viewer === "text" ? "file-text" : "file"} size={38} />}
    {(word || entry.viewer === "pdf") && <span className="learning-file-type" aria-hidden="true">{word ? "Word" : "PDF"}</span>}
  </span>;
}

export function FileManager(props: ComponentProps<typeof ExistingFileManager>) {
  return <ExistingFileManager {...props} compact renderFileIcon={entry => <FileTile entry={entry} courseId={props.courseId} />} />;
}

type File = Schema<"CourseFileOpenResponse">;
export type MaterialQuote = QuoteSelection & {
  origin: "material";
  asset_id: string;
  source_version: string;
  title: string;
};

/** A visible, version-bound text read; no model request or surrounding source. */
export function MaterialViewer({ file, onQuote, onSourceAction, sourceJump, visible = true }: {
  file: File;
  onQuote?: (quote: MaterialQuote) => void | Promise<void>;
  onSourceAction?: ReadingActionHandler;
  sourceJump?: SourceJump;
  visible?: boolean;
}) {
  const [read, setRead] = useState({ file, text: "", loading: true, error: "" });
  const [selection, setSelection] = useState<QuoteSelection | null>(null);
  const [adding, setAdding] = useState(false);
  const [quoteError, setQuoteError] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const markdown = /\.(?:md|markdown)$/i.test(file.entry.name);
  useEffect(() => {
    const controller = new AbortController();
    setRead({ file, text: "", loading: true, error: "" });
    setSelection(null);
    setQuoteError("");
    if (file.viewer !== "text" || !file.content_url) {
      setRead({ file, text: "", loading: false, error: "" });
      return () => controller.abort();
    }
    void (async () => {
      try {
        const response = await fetch(file.content_url!, {
          credentials: "same-origin", cache: "no-store", signal: controller.signal,
        });
        if (!response.ok) throw new Error("原文件缺失或内容已变化，请重新打开当前版本（SOURCE_UNAVAILABLE）");
        const text = await response.text();
        if (text.length > 2_000_000) throw new Error("文本超过当前阅读上限，请使用系统程序打开（TEXT_TOO_LARGE）");
        if (!controller.signal.aborted) setRead({ file, text, loading: false, error: "" });
      } catch (reason) {
        if (!controller.signal.aborted) setRead({ file, text: "", loading: false,
          error: reason instanceof Error ? reason.message : errorMessage(reason) });
      }
    })();
    return () => controller.abort();
  }, [file]);
  const quote = (value: QuoteSelection) => onQuote?.({ ...value, origin: "material",
    asset_id: file.asset_id, source_version: file.source_version, title: file.entry.name });
  const inspectPlainSelection = () => {
    const selected = window.getSelection();
    const element = root.current;
    if (markdown || !element || !selected?.rangeCount || selected.isCollapsed) { setSelection(null); return; }
    const range = selected.getRangeAt(0);
    const body = element.querySelector("pre");
    if (!body || !body.contains(range.startContainer) || !body.contains(range.endContainer)) { setSelection(null); return; }
    const prefix = range.cloneRange();
    prefix.selectNodeContents(body); prefix.setEnd(range.startContainer, range.startOffset);
    const start = prefix.toString().length;
    const text = range.toString();
    if (!text.trim() || read.text.slice(start, start + text.length) !== text) { setSelection(null); return; }
    setSelection({ text, markdown: text, source_start: start, source_end: start + text.length });
  };
  let content: ReactNode;
  if (file.viewer !== "text") content = <ExistingResourceViewer file={file} onSourceAction={onSourceAction} sourceJump={sourceJump} visible={visible} />;
  else if (read.file !== file || read.loading) content = <p role="status">正在读取资料…</p>;
  else if (read.error) content = <p role="alert">{read.error}</p>;
  else content = markdown ? <MarkdownAnswer text={read.text} onQuote={quote} />
    : <pre className="document-text" aria-label="资料正文" onMouseUp={inspectPlainSelection} onKeyUp={inspectPlainSelection}>{read.text}</pre>;
  return <div className="learning-legacy-module learning-material-viewer" ref={root}
    data-testid="learning-resource-viewer" data-asset-id={file.asset_id} data-source-version={file.source_version}>
    {content}
    {onQuote && selection && read.file === file && !read.loading && <div className="learning-material-selection">
      <Button variant="outline" disabled={adding} onMouseDown={event => event.preventDefault()} onClick={() => {
        if (adding) return;
        setAdding(true); setQuoteError("");
        void Promise.resolve().then(() => quote(selection)).then(() => {
          setSelection(null); window.getSelection()?.removeAllRanges();
        }).catch((reason: unknown) => setQuoteError(errorMessage(reason))).finally(() => setAdding(false));
      }}>添加到对话</Button>
    </div>}
    {quoteError && <p role="alert">{quoteError}</p>}
  </div>;
}
