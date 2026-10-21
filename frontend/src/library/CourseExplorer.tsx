import { useCallback, useEffect, useRef, useState } from "react";
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
type ImportResult = Schema<"CourseImportResponse">;
type OperationStatus = Schema<"CourseOperationStatusResponse">;
type Action =
  "import" | "directory" | "rename" | "move" | "delete" | "rename-course" | "detach" | "root";
type Conflict = "ask" | "keep_both" | "skip" | "replace";
type Pending = { operationId: string; action: Action };
const pendingKey = (courseId: string) => `learning.course-file-operation.${courseId}`;
function readPending(courseId: string): Pending | null {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(pendingKey(courseId)) || "null");
    if (!value || typeof value !== "object" || !("operationId" in value) || !("action" in value))
      return null;
    if (typeof value.operationId !== "string" || !/^[A-Za-z0-9_-]{1,160}$/.test(value.operationId))
      return null;
    if (
      ![
        "import",
        "directory",
        "rename",
        "move",
        "delete",
        "rename-course",
        "detach",
        "root",
      ].includes(String(value.action))
    )
      return null;
    return value as Pending;
  } catch {
    return null;
  }
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
}: {
  courseId: string;
  connected: boolean;
  onOpen: (value: OpenFile) => void;
  onCourseChanged: () => void;
  onDetached: () => void;
  onLegacyVideo?: (asset: Schema<"MediaAssetResponse">) => void;
}) {
  const [root, setRoot] = useState<Listing | null>(null);
  const [children, setChildren] = useState<Record<string, Entry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set([""]));
  const [selectedDirectory, setSelectedDirectory] = useState("");
  const [selectedFile, setSelectedFile] = useState("");
  const [legacyVideos, setLegacyVideos] = useState<Schema<"MediaAssetResponse">[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [action, setAction] = useState<Action | null>(null);
  const [target, setTarget] = useState<Entry | null>(null);
  const [value, setValue] = useState("");
  const [mode, setMode] = useState<"move" | "copy">("move");
  const [conflict, setConflict] = useState<Conflict>("ask");
  const [conflicts, setConflicts] = useState<string[]>([]);
  const [importPaths, setImportPaths] = useState<string[] | null>(null);
  const [pending, setPending] = useState<Pending | null>(() => readPending(courseId));
  const [review, setReview] = useState<OperationStatus | null>(null);
  const epoch = useRef(0);
  const expandedRef = useRef(expanded);
  useEffect(() => {
    expandedRef.current = expanded;
  }, [expanded]);
  const checkpoint = (next: Pending | null) => {
    try {
      if (next) localStorage.setItem(pendingKey(courseId), JSON.stringify(next));
      else localStorage.removeItem(pendingKey(courseId));
    } catch {
      /* The in-memory unknown-operation guard remains active. */
    }
  };

  const readDirectory = useCallback(
    async (path: string, signal?: AbortSignal) =>
      api.read<Listing>(`/courses/${courseId}/files?path=${encodeURIComponent(path)}`, signal),
    [courseId],
  );

  const refresh = useCallback(async () => {
    const at = epoch.current;
    const listing = await readDirectory("");
    if (at !== epoch.current) return;
    setRoot(listing);
    setChildren({ "": listing.entries });
    await Promise.all(
      Array.from(expandedRef.current)
        .filter(Boolean)
        .map(async (path) => {
          try {
            const result = await readDirectory(path);
            if (at === epoch.current) setChildren((old) => ({ ...old, [path]: result.entries }));
          } catch {
            // A user may just have renamed or removed an expanded directory.
            if (at === epoch.current)
              setExpanded((old) => new Set(Array.from(old).filter((item) => item !== path)));
          }
        }),
    );
  }, [readDirectory]);

  useEffect(() => {
    const controller = new AbortController();
    const at = ++epoch.current;
    void readDirectory("", controller.signal)
      .then((listing) => {
        if (at !== epoch.current) return;
        setRoot(listing);
        setChildren({ "": listing.entries });
        if (listing.legacy_asset_count && listing.root_status !== "ready") {
          void api
            .read<Schema<"MediaAssetResponse">[]>(`/courses/${courseId}/videos`, controller.signal)
            .then((videos) => {
              if (at === epoch.current) setLegacyVideos(videos);
            })
            .catch(() => undefined);
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(errorMessage(reason));
      });
    return () => {
      epoch.current = at + 1;
      controller.abort();
    };
  }, [courseId, readDirectory]);

  const show = (next: Action, entry?: Entry) => {
    if (pending) return;
    setAction(next);
    setTarget(entry || null);
    setError("");
    setNotice("");
    setValue(
      next === "rename" ? entry?.name || "" : next === "rename-course" ? root?.title || "" : "",
    );
    setMode("move");
    setConflict("ask");
    setConflicts([]);
    setImportPaths(null);
  };

  const finish = async (kind: Action, result: FileOperation | ImportResult) => {
    if (result.state === "conflict") {
      checkpoint(null);
      setPending(null);
      if ("conflicts" in result) {
        setConflicts(result.conflicts);
        setImportPaths(result.source_paths);
      }
      setNotice("存在同名文件，请明确选择处理方式后继续。");
      return;
    }
    if (result.state !== "completed") {
      setNotice("文件操作需要核对。请保留原件，核对磁盘状态后再处理。");
      setPending({ operationId: result.operation_id, action: kind });
      checkpoint({ operationId: result.operation_id, action: kind });
      setAction(null);
      return;
    }
    checkpoint(null);
    setPending(null);
    setAction(null);
    setValue("");
    if (kind === "detach") {
      onDetached();
      return;
    }
    setNotice("已完成");
    await refresh();
    if (kind === "rename-course" || kind === "root") onCourseChanged();
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!action || busy || pending) return;
    const kind = action;
    const at = epoch.current;
    const operationId = crypto.randomUUID();
    const prefix = `/courses/${courseId}`;
    const body: Record<string, unknown> = { operation_id: operationId };
    if (kind === "import")
      Object.assign(body, {
        paths:
          importPaths ||
          (value.trim()
            ? value
                .split(/\r?\n/)
                .map((line) => line.trim())
                .filter(Boolean)
            : undefined),
        destination: selectedDirectory,
        mode,
        conflict,
      });
    if (kind === "directory") Object.assign(body, { path: selectedDirectory, name: value.trim() });
    if (kind === "rename") Object.assign(body, { path: target?.path, name: value.trim() });
    if (kind === "move")
      Object.assign(body, { path: target?.path, destination: value.trim(), conflict });
    if (kind === "delete") Object.assign(body, { path: target?.path, confirm: true });
    if (kind === "rename-course") Object.assign(body, { name: value.trim() });
    if (kind === "detach") Object.assign(body, { confirm: true });
    if (kind === "root" && value.trim()) Object.assign(body, { path: value.trim() });
    setBusy(true);
    setError("");
    checkpoint({ operationId, action: kind });
    try {
      if (kind === "root") {
        await api.write(`${prefix}/root`, value.trim() ? { path: value.trim() } : {}, {
          timeoutMs: 180_000,
        });
        if (at !== epoch.current) return;
        checkpoint(null);
        setAction(null);
        await refresh();
        onCourseChanged();
      } else {
        const path =
          kind === "rename-course" || kind === "detach"
            ? `${prefix}/${kind}`
            : `${prefix}/files/${kind}`;
        const result = await api.write<FileOperation | ImportResult>(path, body, {
          timeoutMs: 180_000,
        });
        if (at !== epoch.current) return;
        await finish(kind, result);
      }
    } catch (reason) {
      if (at !== epoch.current) return;
      setError(errorMessage(reason));
      if (
        !(reason instanceof ApiError) ||
        reason.status === undefined ||
        reason.status >= 500 ||
        /(?:OPERATION_(?:FAILED|UNKNOWN|PENDING)|RENAME_(?:FAILED|UNKNOWN))$/.test(reason.code)
      ) {
        setPending({ operationId, action: kind });
        setAction(null);
      } else checkpoint(null);
    } finally {
      setBusy(false);
    }
  };

  const reconcile = async () => {
    if (!pending || busy) return;
    const at = epoch.current;
    setBusy(true);
    setError("");
    try {
      if (pending.action === "root") {
        const result = await readDirectory("");
        if (at !== epoch.current) return;
        if (result.root_status === "ready") {
          checkpoint(null);
          setPending(null);
          setAction(null);
          await refresh();
          onCourseChanged();
        } else setNotice("尚未确认课程文件夹绑定成功，请检查本机文件夹选择状态。");
      } else {
        const result = await api.read<OperationStatus>(
          `/courses/${courseId}/files/operations/${pending.operationId}`,
        );
        if (at !== epoch.current) return;
        if (result.result && (result.state === "completed" || result.state === "conflict")) {
          await finish(pending.action, result.result as FileOperation | ImportResult);
        } else if (result.state === "failed") {
          checkpoint(null);
          setPending(null);
          setAction(null);
          setNotice(
            `已核对本次操作未完成${result.error_code ? `（${result.error_code}）` : ""}，未自动重试。`,
          );
          await refresh();
        } else if (result.state === "reviewed") {
          checkpoint(null);
          setPending(null);
          setReview(null);
          setAction(null);
          setNotice("核对记录已保存，未重复执行文件操作。");
          await refresh();
        } else {
          setReview(result);
          setNotice(
            `本次操作需要核对磁盘状态${result.error_code ? `（${result.error_code}）` : ""}，未自动重试。`,
          );
        }
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const confirmReview = async () => {
    if (!pending || !review || busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.write<OperationStatus>(
        `/courses/${courseId}/files/operations/${pending.operationId}/reconcile`,
        { confirm: true },
      );
      if (result.state === "reviewed") {
        checkpoint(null);
        setPending(null);
        setReview(null);
        setNotice("已记录人工核对。本次操作没有重新执行。");
        await refresh();
        onCourseChanged();
      } else setReview(result);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const openEntry = async (entry: Entry) => {
    if (entry.kind === "blocked" || busy) return;
    const at = epoch.current;
    if (entry.kind === "directory") {
      setSelectedDirectory(entry.path);
      if (expanded.has(entry.path)) {
        setExpanded((old) => new Set(Array.from(old).filter((path) => path !== entry.path)));
      } else {
        setExpanded((old) => new Set([...old, entry.path]));
        try {
          const result = await readDirectory(entry.path);
          if (at === epoch.current)
            setChildren((old) => ({ ...old, [entry.path]: result.entries }));
        } catch (reason) {
          setError(errorMessage(reason));
        }
      }
      return;
    }
    setSelectedFile(entry.path);
    try {
      const result = await api.write<OpenFile>(`/courses/${courseId}/files/open`, {
        path: entry.path,
      });
      if (at === epoch.current) onOpen(result);
    } catch (reason) {
      setError(errorMessage(reason));
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
          className={`file-tree-row ${selectedFile === entry.path || selectedDirectory === entry.path ? "selected" : ""}`}
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
          {entry.kind !== "blocked" && (
            <details className="file-row-menu">
              <summary aria-label={`文件操作：${entry.name}`}>···</summary>
              <div className="file-actions-menu">
                <button disabled={busy || !!pending} onClick={() => show("rename", entry)}>
                  重命名
                </button>
                <button disabled={busy || !!pending} onClick={() => show("move", entry)}>
                  移动到…
                </button>
                <button
                  className="danger"
                  disabled={busy || !!pending}
                  onClick={() => show("delete", entry)}
                >
                  删除磁盘原件…
                </button>
              </div>
            </details>
          )}
        </div>
        {entry.kind === "directory" && expanded.has(entry.path) && (
          <ul role="group">{tree(entry.path, depth + 1)}</ul>
        )}
      </li>
    ));

  const ready = root?.root_status === "ready";
  const plannedPaths = Array.isArray(review?.result?.planned_paths)
    ? review.result.planned_paths.flatMap((path: unknown) => {
        if (typeof path === "string") return [path];
        if (
          path &&
          typeof path === "object" &&
          "source" in path &&
          typeof path.source === "string"
        ) {
          return [
            "target" in path && typeof path.target === "string"
              ? `${path.source} → ${path.target}`
              : path.source,
          ];
        }
        return [];
      })
    : [];
  const headings: Record<Action, string> = {
    import: "导入资料",
    directory: "新建文件夹",
    rename: "重命名",
    move: "移动文件",
    delete: "删除磁盘原件",
    "rename-course": "重命名课程",
    detach: "从列表移除课程",
    root: "选择课程文件夹",
  };

  return (
    <section className="course-explorer" aria-label="课程文件">
      <div className="explorer-heading">
        <span>文件</span>
        <div>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="导入资料"
            title="导入资料"
            disabled={!connected || !ready || busy || !!pending}
            onClick={() => show("import")}
          >
            ＋
          </Button>
          <Button
            variant="ghost"
            size="icon-xs"
            aria-label="新建文件夹"
            title="新建文件夹"
            disabled={!connected || !ready || busy || !!pending}
            onClick={() => show("directory")}
          >
            <span aria-hidden="true">▱</span>
          </Button>
          <details className="explorer-menu">
            <summary aria-label="课程文件设置">···</summary>
            <div className="file-actions-menu">
              <button
                onClick={() => void refresh().catch((reason) => setError(errorMessage(reason)))}
              >
                刷新文件
              </button>
              <button disabled={!ready || busy || !!pending} onClick={() => show("rename-course")}>
                重命名课程…
              </button>
              <button disabled={busy || !!pending} onClick={() => show("detach")}>
                从列表移除课程…
              </button>
            </div>
          </details>
        </div>
      </div>
      {ready ? (
        <>
          <button
            className="course-root-row"
            title={root.root_path || ""}
            onClick={() => {
              setSelectedDirectory("");
              setSelectedFile("");
            }}
          >
            <span aria-hidden="true">⌄</span> {root.title}
          </button>
          <ul role="tree" aria-label="当前课程文件树" className="file-tree">
            {tree("")}
          </ul>
          {children[""]?.length === 0 && (
            <p className="explorer-empty">文件夹为空。导入资料开始学习。</p>
          )}
          {selectedDirectory && (
            <p className="explorer-destination" title={selectedDirectory}>
              操作目录：{selectedDirectory}
            </p>
          )}
        </>
      ) : (
        <div className="explorer-empty">
          <p>
            {root?.root_status === "missing"
              ? "课程文件夹暂时不可用，请检查磁盘位置。"
              : "为这门课程选择一个真实文件夹。已有资料不会自动搬动。"}
          </p>
          <Button variant="outline" disabled={!connected || busy} onClick={() => show("root")}>
            选择课程文件夹
          </Button>
          {legacyVideos.length > 0 && (
            <details className="legacy-videos">
              <summary>原有视频（保持原位置）</summary>
              {legacyVideos.map((asset) => (
                <button key={asset.asset_id} onClick={() => onLegacyVideo?.(asset)}>
                  {asset.display_name}
                </button>
              ))}
            </details>
          )}
        </div>
      )}
      {error && (
        <p className="explorer-error" role="alert">
          {error}
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
            核对文件操作
          </Button>
          {review && (
            <div className="file-review-details">
              <p>请在磁盘上检查本次涉及的文件，确认它们的实际位置与内容。</p>
              {plannedPaths.length > 0 && (
                <details>
                  <summary>查看待核对路径</summary>
                  <ul>
                    {plannedPaths.map((path) => (
                      <li key={path}>{path}</li>
                    ))}
                  </ul>
                </details>
              )}
              <Button variant="outline" disabled={busy} onClick={() => void confirmReview()}>
                已核对磁盘，解除操作锁定
              </Button>
              <p>此操作只记录你的核对，不会再次移动或删除文件。</p>
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
            aria-labelledby="file-action-heading"
            onSubmit={(event) => void submit(event)}
          >
            <h2 id="file-action-heading">{headings[action]}</h2>
            {target && <p className="file-dialog-path">{target.path}</p>}
            {action === "import" && (
              <>
                <label>
                  导入方式
                  <select
                    aria-label="导入方式"
                    value={mode}
                    onChange={(event) => setMode(event.target.value as "move" | "copy")}
                  >
                    <option value="move">移动到课程文件夹（默认）</option>
                    <option value="copy">复制到课程文件夹</option>
                  </select>
                </label>
                <p className="muted">目标：{selectedDirectory || "课程根目录"}</p>
                {!importPaths && (
                  <label>
                    资料路径（可选，每行一个）
                    <textarea
                      aria-label="资料导入路径"
                      value={value}
                      onChange={(event) => setValue(event.target.value)}
                      placeholder="留空将在本机打开文件选择器"
                    />
                  </label>
                )}
              </>
            )}
            {action === "root" && (
              <label>
                课程文件夹路径（可选）
                <input
                  aria-label="课程文件夹路径"
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                  placeholder="留空将在本机打开文件夹选择器"
                />
              </label>
            )}
            {action === "directory" && (
              <label>
                文件夹名称
                <input
                  aria-label="文件夹名称"
                  value={value}
                  required
                  autoFocus
                  onChange={(event) => setValue(event.target.value)}
                />
              </label>
            )}
            {(action === "rename" || action === "rename-course") && (
              <label>
                新名称
                <input
                  aria-label="新名称"
                  value={value}
                  required
                  autoFocus
                  onChange={(event) => setValue(event.target.value)}
                />
              </label>
            )}
            {action === "rename-course" && (
              <p className="muted">课程名与真实根文件夹名一起修改，已有来源身份保留。</p>
            )}
            {action === "move" && (
              <label>
                目标文件夹（课程内相对路径）
                <input
                  aria-label="目标文件夹"
                  value={value}
                  onChange={(event) => setValue(event.target.value)}
                  placeholder="留空表示课程根目录"
                />
              </label>
            )}
            {(action === "import" || action === "move") && (
              <label>
                同名冲突
                <select
                  aria-label="同名冲突"
                  value={conflict}
                  onChange={(event) => setConflict(event.target.value as Conflict)}
                >
                  <option value="ask">遇到冲突时询问</option>
                  <option value="keep_both">保留两份</option>
                  <option value="skip">跳过</option>
                  <option value="replace">明确替换原件</option>
                </select>
              </label>
            )}
            {conflicts.length > 0 && (
              <div className="file-conflicts" role="status">
                <p>以下目标已有同名文件：</p>
                <ul>
                  {conflicts.map((path) => (
                    <li key={path}>{path}</li>
                  ))}
                </ul>
              </div>
            )}
            {action === "delete" && (
              <p>
                这会删除磁盘上的原文件{target?.kind === "directory" ? "夹及其内容" : ""}
                。已提交的聊天与截图仍保留。确认删除？
              </p>
            )}
            {action === "detach" && <p>仅从课程列表移除，磁盘原件、聊天和已提交截图会保留。</p>}
            {error && (
              <p className="explorer-error" role="alert">
                {error}
              </p>
            )}
            {pending && <p className="muted">操作结果未知，请先点击“核对文件操作”。</p>}
            <div className="file-dialog-buttons">
              <Button type="button" variant="ghost" disabled={busy} onClick={() => setAction(null)}>
                关闭
              </Button>
              <Button
                type="submit"
                variant={action === "delete" ? "destructive" : "default"}
                disabled={busy || !!pending || (conflicts.length > 0 && conflict === "ask")}
              >
                {busy
                  ? "正在处理…"
                  : action === "delete"
                    ? "确认删除原件"
                    : action === "detach"
                      ? "确认移除课程"
                      : "确认"}
              </Button>
            </div>
          </form>
        </div>
      )}
    </section>
  );
}
