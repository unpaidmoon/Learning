import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { FormEvent } from "react";
import { api, ApiError, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { Icon } from "../components/ui/Icon";
import "./library.css";

type Entry = Schema<"CourseFileEntry">;
type Listing = Schema<"CourseFileListResponse">;
type OpenFile = Schema<"CourseFileOpenResponse">;
type FileOperation = Schema<"CourseFileOperationResponse">;
type OperationStatus = Schema<"CourseOperationStatusResponse">;
type Action = "rename-course" | "detach" | "root";
type Pending = { operationId: string; action: Action | "relocate" };
const pendingKey = (courseId: string) => `learning.course-operation.${courseId}`;
const legacyPendingKey = (courseId: string) => `learning.course-file-operation.${courseId}`;

function readPending(courseId: string): Pending | null {
  try {
    for (const key of [pendingKey(courseId), legacyPendingKey(courseId)]) {
      const value: unknown = JSON.parse(localStorage.getItem(key) || "null");
      if (!value || typeof value !== "object" || !("operationId" in value) || !("action" in value))
        continue;
      if (
        typeof value.operationId !== "string" ||
        !/^[A-Za-z0-9_-]{1,160}$/.test(value.operationId)
      )
        continue;
      if (["rename-course", "detach", "root", "relocate"].includes(String(value.action)))
        return value as Pending;
    }
  } catch {
    // A broken local cache never authorizes a repeated disk or course operation.
  }
  return null;
}

function FileMark({ kind, viewer }: { kind: Entry["kind"]; viewer: Entry["viewer"] }) {
  return (
    <Icon
      name={kind === "directory" ? "folder" : viewer === "video" ? "video" : "file"}
      size={16}
    />
  );
}

export function CourseExplorer({
  courseId,
  connected,
  onOpen,
  onCourseChanged,
  onDetached,
  onLegacyVideo,
  onDirectorySelect,
  revision = 0,
  rootLabel,
  headerContainer,
  compact = false,
}: {
  courseId: string;
  connected: boolean;
  onOpen: (value: OpenFile) => void;
  onCourseChanged: () => void;
  onDetached: () => void;
  onLegacyVideo?: (asset: Schema<"MediaAssetResponse">) => void;
  onDirectorySelect?: (path: string) => void;
  revision?: number;
  rootLabel?: string;
  headerContainer?: HTMLElement | null;
  compact?: boolean;
}) {
  const [root, setRoot] = useState<Listing | null>(null);
  const [children, setChildren] = useState<Record<string, Entry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set([""]));
  const [selectedPath, setSelectedPath] = useState("");
  const [legacyVideos, setLegacyVideos] = useState<Schema<"MediaAssetResponse">[]>([]);
  const [error, setError] = useState("");
  const [readError, setReadError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [action, setAction] = useState<Exclude<Action, "root"> | null>(null);
  const [name, setName] = useState("");
  const [pending, setPending] = useState<Pending | null>(() => readPending(courseId));
  const [review, setReview] = useState<OperationStatus | null>(null);
  const epoch = useRef(0);
  const listingVersion = useRef(0);
  const lastRevision = useRef(revision);
  const expandedRef = useRef(expanded);
  useEffect(() => {
    expandedRef.current = expanded;
  }, [expanded]);

  const checkpoint = (next: Pending | null) => {
    try {
      if (next) localStorage.setItem(pendingKey(courseId), JSON.stringify(next));
      else {
        localStorage.removeItem(pendingKey(courseId));
        const legacy = readPending(courseId);
        if (legacy && ["rename-course", "detach", "root", "relocate"].includes(legacy.action))
          localStorage.removeItem(legacyPendingKey(courseId));
      }
    } catch {
      // In-memory pending state still blocks duplicate mutations.
    }
  };
  const readDirectory = useCallback(
    (path: string, signal?: AbortSignal) =>
      api.read<Listing>(`/courses/${courseId}/files?path=${encodeURIComponent(path)}`, signal),
    [courseId],
  );
  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      const at = epoch.current;
      const version = ++listingVersion.current;
      const listing = await readDirectory("", signal);
      if (at !== epoch.current || version !== listingVersion.current) return;
      setReadError("");
      setRoot(listing);
      setChildren({ "": listing.entries });
      if (listing.legacy_asset_count && listing.root_status !== "ready") {
        const videos = await api.read<Schema<"MediaAssetResponse">[]>(
          `/courses/${courseId}/videos`,
          signal,
        );
        if (at === epoch.current && version === listingVersion.current) setLegacyVideos(videos);
      }
      await Promise.all(
        Array.from(expandedRef.current)
          .filter(Boolean)
          .map(async (path) => {
            try {
              const value = await readDirectory(path, signal);
              if (at === epoch.current && version === listingVersion.current)
                setChildren((old) => ({ ...old, [path]: value.entries }));
            } catch {
              if (at === epoch.current && version === listingVersion.current)
                setExpanded((old) => new Set(Array.from(old).filter((item) => item !== path)));
            }
          }),
      );
    },
    [courseId, readDirectory],
  );

  useEffect(() => {
    const abort = new AbortController();
    const at = ++epoch.current;
    void refresh(abort.signal).catch((reason) => {
      if (!abort.signal.aborted && at === epoch.current) setReadError(errorMessage(reason));
    });
    return () => {
      epoch.current += 1;
      listingVersion.current += 1;
      abort.abort();
    };
  }, [refresh]);

  useEffect(() => {
    if (lastRevision.current === revision) return;
    lastRevision.current = revision;
    const abort = new AbortController();
    const at = epoch.current;
    void refresh(abort.signal).catch((reason) => {
      if (!abort.signal.aborted && at === epoch.current) setReadError(errorMessage(reason));
    });
    return () => abort.abort();
  }, [refresh, revision]);

  const finish = async (kind: Pending["action"], at: number) => {
    if (at !== epoch.current) return;
    checkpoint(null);
    setPending(null);
    setAction(null);
    setReview(null);
    setError("");
    if (kind === "detach") {
      onDetached();
      return;
    }
    await refresh();
    if (at === epoch.current) onCourseChanged();
  };
  const mutationFailed = (reason: unknown, next: Pending) => {
    setError(errorMessage(reason));
    if (
      !(reason instanceof ApiError) ||
      reason.status === undefined ||
      reason.status >= 500 ||
      /(?:OPERATION_(?:FAILED|UNKNOWN|PENDING)|RENAME_(?:FAILED|UNKNOWN))$/.test(reason.code)
    ) {
      setPending(next);
      setAction(null);
    } else checkpoint(null);
  };
  const bindRoot = async () => {
    if (busy || pending || !connected) return;
    const at = epoch.current;
    const relocation = Boolean(root?.root_path);
    const next: Pending = {
      operationId: crypto.randomUUID(),
      action: relocation ? "relocate" : "root",
    };
    setBusy(true);
    setError("");
    setNotice("");
    checkpoint(next);
    try {
      const result = await api.write<{ unavailable_versions?: unknown[] } | null>(
        `/courses/${courseId}/root${relocation ? "/relocate" : ""}`,
        relocation ? { operation_id: next.operationId, expected_root: root?.root_path } : {},
        { timeoutMs: 180_000 },
      );
      await finish(next.action, at);
      if (at === epoch.current && result?.unavailable_versions?.length)
        setNotice(
          `已重新定位；${result.unavailable_versions.length} 个旧版本仍不可用，旧引用没有替换为同名新版。`,
        );
    } catch (reason) {
      if (at === epoch.current) mutationFailed(reason, next);
    } finally {
      if (at === epoch.current) setBusy(false);
    }
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!action || busy || pending || (action === "rename-course" && !name.trim())) return;
    const kind = action;
    const at = epoch.current;
    const next: Pending = { operationId: crypto.randomUUID(), action: kind };
    setBusy(true);
    setError("");
    setNotice("");
    checkpoint(next);
    try {
      const result = await api.write<FileOperation>(
        `/courses/${courseId}/${kind}`,
        kind === "rename-course"
          ? { name: name.trim(), operation_id: next.operationId }
          : { confirm: true, operation_id: next.operationId },
        { timeoutMs: 180_000 },
      );
      if (at !== epoch.current) return;
      if (result.state === "completed") await finish(kind, at);
      else {
        setPending(next);
        setAction(null);
      }
    } catch (reason) {
      if (at === epoch.current) mutationFailed(reason, next);
    } finally {
      if (at === epoch.current) setBusy(false);
    }
  };
  const reconcile = async () => {
    if (!pending || busy) return;
    const at = epoch.current;
    setBusy(true);
    setError("");
    try {
      if (pending.action === "root") {
        const listing = await readDirectory("");
        if (at !== epoch.current) return;
        if (listing.root_status === "ready") await finish("root", at);
        else setNotice("尚未确认课程文件夹绑定完成，请核对文件夹选择状态。");
      } else {
        const value = await api.read<OperationStatus>(
          `/courses/${courseId}/files/operations/${pending.operationId}`,
        );
        if (at !== epoch.current) return;
        if (value.state === "completed") await finish(pending.action, at);
        else if (value.state === "failed" || value.state === "reviewed") {
          checkpoint(null);
          setPending(null);
          setReview(null);
          setNotice(
            value.state === "failed"
              ? "已核对操作未完成，未自动重试。"
              : "已记录核对，未重复执行操作。",
          );
          await refresh();
        } else {
          setReview(value);
          setNotice("本次操作尚需核对，未自动重试。");
        }
      }
    } catch (reason) {
      if (at === epoch.current) setError(errorMessage(reason));
    } finally {
      if (at === epoch.current) setBusy(false);
    }
  };
  const confirmReview = async () => {
    if (!pending || !review || busy) return;
    const at = epoch.current;
    setBusy(true);
    setError("");
    try {
      const value = await api.write<OperationStatus>(
        `/courses/${courseId}/files/operations/${pending.operationId}/reconcile`,
        { confirm: true },
      );
      if (at !== epoch.current) return;
      if (value.state === "reviewed") {
        checkpoint(null);
        setPending(null);
        setReview(null);
        await refresh();
        if (at === epoch.current) onCourseChanged();
      } else setReview(value);
    } catch (reason) {
      if (at === epoch.current) setError(errorMessage(reason));
    } finally {
      if (at === epoch.current) setBusy(false);
    }
  };
  const openEntry = async (entry: Entry) => {
    if (!connected || entry.kind === "blocked") return;
    const at = epoch.current;
    setSelectedPath(entry.path);
    if (entry.kind === "directory") {
      onDirectorySelect?.(entry.path);
      if (expanded.has(entry.path))
        setExpanded((old) => new Set(Array.from(old).filter((path) => path !== entry.path)));
      else {
        setExpanded((old) => new Set([...old, entry.path]));
        try {
          const value = await readDirectory(entry.path);
          if (at === epoch.current) setChildren((old) => ({ ...old, [entry.path]: value.entries }));
        } catch (reason) {
          if (at === epoch.current) setError(errorMessage(reason));
        }
      }
      return;
    }
    try {
      const file = await api.write<OpenFile>(`/courses/${courseId}/files/open`, {
        path: entry.path,
      });
      if (at === epoch.current) onOpen(file);
    } catch (reason) {
      if (at === epoch.current) setError(errorMessage(reason));
    }
  };
  const tree = (path: string, depth = 0) =>
    (children[path] || []).map((entry) => (
      <li
        key={entry.path}
        role="treeitem"
        aria-expanded={entry.kind === "directory" ? expanded.has(entry.path) : undefined}
      >
        <div
          className={`file-tree-row ${selectedPath === entry.path ? "selected" : ""}`}
          style={{ paddingLeft: 10 + depth * 14 }}
        >
          <button
            className="file-tree-open"
            data-file-path={entry.path}
            disabled={!connected || entry.kind === "blocked"}
            onClick={() => void openEntry(entry)}
            title={entry.path}
          >
            <span className="tree-chevron" aria-hidden="true">
              {entry.kind === "directory" ? (expanded.has(entry.path) ? "⌄" : "›") : ""}
            </span>
            <FileMark kind={entry.kind} viewer={entry.viewer} />
            <span>{entry.name}</span>
          </button>
        </div>
        {entry.kind === "directory" && expanded.has(entry.path) && (
          <ul role="group">{tree(entry.path, depth + 1)}</ul>
        )}
      </li>
    ));
  const ready = root?.course_id === courseId && root.root_status === "ready";
  const heading = (
    <div className="explorer-heading">
      <span>文件</span>
      <div>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="刷新文件"
          title="刷新文件"
          disabled={!connected || busy}
          onClick={() => {
            setError("");
            void refresh().catch((reason) => setError(errorMessage(reason)));
          }}
        >
          <Icon name="refresh" size={16} />
        </Button>
        <details
          className="explorer-menu sidebar-course-menu"
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget))
              event.currentTarget.removeAttribute("open");
          }}
          onClick={(event) => {
            if ((event.target as Element).closest("button"))
              event.currentTarget.removeAttribute("open");
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              event.currentTarget.removeAttribute("open");
              event.currentTarget.querySelector<HTMLElement>("summary")?.focus();
            }
          }}
        >
          <summary aria-label="课程设置" title="课程设置">
            <Icon name="more" size={17} />
          </summary>
          <div className="file-actions-menu">
            <button disabled={!connected || busy || !!pending} onClick={() => void bindRoot()}>
              重新定位课程原件…
            </button>
            {compact && (
              <>
                <button
                  disabled={!connected || busy || !!pending}
                  onClick={() => {
                    setError("");
                    setName(root?.title || "");
                    setAction("rename-course");
                  }}
                >
                  重命名课程…
                </button>
                <button
                  disabled={!ready}
                  onClick={() => {
                    setSelectedPath("");
                    onDirectorySelect?.("");
                  }}
                >
                  返回课程根目录
                </button>
              </>
            )}
            <button
              disabled={!connected || busy || !!pending}
              onClick={() => {
                setError("");
                setAction("detach");
              }}
            >
              移除课程…
            </button>
          </div>
        </details>
      </div>
    </div>
  );
  return (
    <section className="course-explorer" aria-label="课程文件">
      {headerContainer ? createPortal(heading, headerContainer) : heading}
      {!compact && root?.course_id === courseId && (
        <div className="course-root-heading">
          <button
            className="course-root-row"
            title={root.root_path || ""}
            onClick={() => {
              setSelectedPath("");
              onDirectorySelect?.("");
            }}
          >
            <span aria-hidden="true">⌄</span> {rootLabel || root.title}
          </button>
          <button
            type="button"
            className="course-rename-button"
            aria-label="重命名课程"
            title="重命名课程"
            disabled={!connected || busy || !!pending}
            onClick={() => {
              setError("");
              setName(root.title);
              setAction("rename-course");
            }}
          >
            <svg
              aria-hidden="true"
              viewBox="0 0 24 24"
              width="14"
              height="14"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="m15 4 5 5M4 20l4-1L20 7a2 2 0 0 0-5-5L3 15l-1 5Z" />
            </svg>
          </button>
        </div>
      )}
      {ready ? (
        <>
          <ul role="tree" aria-label="当前课程文件树" className="file-tree">
            {tree("")}
          </ul>
          {children[""]?.length === 0 && <p className="explorer-empty">文件夹为空。</p>}
        </>
      ) : root?.course_id === courseId ? (
        <div className="explorer-empty">
          <p>
            {root?.root_status === "missing"
              ? "课程文件夹暂时不可用，请检查磁盘位置。"
              : "为这门课程选择一个真实文件夹。已有资料保持原位置。"}
          </p>
          <Button
            variant="outline"
            disabled={!connected || busy || !!pending}
            onClick={() => void bindRoot()}
          >
            {root?.root_path ? "重新定位课程原件" : "选择课程文件夹"}
          </Button>
          {legacyVideos.length > 0 && (
            <details className="legacy-videos">
              <summary>原有视频</summary>
              {legacyVideos.map((asset) => (
                <button key={asset.asset_id} onClick={() => onLegacyVideo?.(asset)}>
                  {asset.display_name}
                </button>
              ))}
            </details>
          )}
        </div>
      ) : (
        <p className="explorer-empty">正在读取课程文件…</p>
      )}
      {(error || readError) && (
        <p className="explorer-error" role="alert">
          {error || readError}
        </p>
      )}
      {notice && (
        <p className="explorer-notice" role="status">
          {notice}
        </p>
      )}
      {pending && (
        <div className="file-reconcile">
          <p>结果尚未确认，先核对本次操作。</p>
          <Button variant="outline" disabled={busy} onClick={() => void reconcile()}>
            核对课程操作
          </Button>
          {review && (
            <div>
              <p>请核对课程名称与目录的实际状态。</p>
              <Button variant="outline" disabled={busy} onClick={() => void confirmReview()}>
                已核对，解除操作锁定
              </Button>
            </div>
          )}
        </div>
      )}
      {action && (
        <div className="file-dialog-backdrop">
          <form
            className="file-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="course-action-heading"
            onSubmit={(event) => void submit(event)}
          >
            <h2 id="course-action-heading">
              {action === "rename-course" ? "重命名课程" : "移除课程"}
            </h2>
            {action === "rename-course" ? (
              <>
                <label>
                  课程名称
                  <input
                    aria-label="课程名称"
                    autoFocus
                    required
                    maxLength={200}
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                  />
                </label>
                <p className="muted">只修改软件中的课程名称。</p>
              </>
            ) : (
              <p>从课程列表移除，磁盘资料、聊天和已提交附件会保留。</p>
            )}
            {error && (
              <p className="explorer-error" role="alert">
                {error}
              </p>
            )}
            <div className="file-dialog-buttons">
              <Button type="button" variant="ghost" disabled={busy} onClick={() => setAction(null)}>
                取消
              </Button>
              <Button
                type="submit"
                disabled={busy || !!pending || (action === "rename-course" && !name.trim())}
              >
                {busy ? "正在处理…" : action === "rename-course" ? "保存" : "移除课程"}
              </Button>
            </div>
          </form>
        </div>
      )}
    </section>
  );
}
