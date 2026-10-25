import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type {
  ClipboardEvent,
  DragEvent,
  FormEvent,
  KeyboardEvent,
  MouseEvent,
  ReactNode,
} from "react";
import { api, ApiError, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { Icon } from "../components/ui/Icon";
import { BilibiliImport } from "../integrations/BilibiliImport";
import { BilibiliFileIcon } from "../integrations/BilibiliFileIcon";
import { BilibiliSourceRecovery } from "../integrations/BilibiliSourceRecovery";
import { droppedItems, MAX_UPLOAD_BYTES, type IncomingFile } from "./dropFiles";
import { useFileOperation } from "./fileOperations";
import "./library.css";

type Entry = Schema<"CourseFileEntry">;
type Listing = Schema<"CourseFileListResponse">;
type OpenFile = Schema<"CourseFileOpenResponse">;
type OperationStatus = Schema<"CourseOperationStatusResponse">;
type Result = Schema<"CourseFileOperationResponse"> | Schema<"CourseImportResponse">;
type FileClipboard = { courseId: string; paths: string[]; mode: "copy" | "move" };
type Action = "directory" | "rename" | "move" | "properties";
const CLIPBOARD_FORMAT = "application/x-learning-course-files";
class FileClipboardState {
  private value: FileClipboard | null = null;
  private writing = false;
  private listeners = new Set<() => void>();
  constructor() {
    if (typeof window !== "undefined") window.addEventListener("blur", () => this.set(null));
    if (typeof document !== "undefined") {
      const invalidate = () => {
        if (!this.writing) this.set(null);
      };
      document.addEventListener("copy", invalidate);
      document.addEventListener("cut", invalidate);
    }
  }
  read = () => this.value;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  set(value: FileClipboard | null) {
    this.value = value;
    this.listeners.forEach((listener) => listener());
  }
  beginWrite() {
    this.writing = true;
  }
  endWrite() {
    this.writing = false;
  }
}
const sharedClipboard = new FileClipboardState();

const parentPath = (path: string) => path.split("/").slice(0, -1).join("/");
function displaySize(value: number | null | undefined) {
  if (value == null) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`;
}
function displayTime(value: string | null | undefined) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN");
}
const writableTarget = (target: EventTarget | null) =>
  target instanceof Element && !!target.closest("input,textarea,select,[contenteditable=true]");
function operationUnknown(reason: unknown) {
  return (
    !(reason instanceof ApiError) ||
    reason.status === undefined ||
    reason.status >= 500 ||
    /_(?:FAILED|UNKNOWN|PENDING)$/.test(reason.code)
  );
}
function fileError(reason: unknown) {
  return reason instanceof ApiError
    ? errorMessage(reason)
    : reason instanceof Error
      ? reason.message
      : errorMessage(reason);
}

export function FileManager({
  courseId,
  connected,
  path,
  revision = 0,
  onPathChange,
  onOpen,
  onChanged,
  compact = false,
  renderFileIcon,
}: {
  courseId: string;
  connected: boolean;
  path?: string;
  revision?: number;
  onPathChange?: (path: string) => void;
  onOpen: (file: OpenFile) => void;
  onChanged?: () => void;
  compact?: boolean;
  renderFileIcon?: (entry: Entry) => ReactNode;
}) {
  const [navigation, setNavigation] = useState({ paths: [path || ""], at: 0 });
  const currentPath = path ?? navigation.paths[navigation.at];
  const [listing, setListing] = useState<Listing | null>(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("name");
  const [view, setView] = useState<"icons" | "list">("icons");
  const [selection, setSelection] = useState<string[]>([]);
  const clipboard = useSyncExternalStore(sharedClipboard.subscribe, sharedClipboard.read);
  const [action, setAction] = useState<Action | null>(null);
  const [actionName, setActionName] = useState("");
  const [targetPath, setTargetPath] = useState("");
  const [targetListing, setTargetListing] = useState<Listing | null>(null);
  const [properties, setProperties] = useState<Schema<"CourseFilePropertiesResponse"> | null>(null);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number } | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [progress, setProgress] = useState("");
  const [review, setReview] = useState<OperationStatus | null>(null);
  const root = useRef<HTMLElement>(null);
  const epoch = useRef(0);
  const anchor = useRef<string | null>(null);
  const dragDepth = useRef(0);
  const working = useRef(false);
  const propertyRequest = useRef(0);
  const { pending, receipt } = useFileOperation(courseId);
  const prefix = `/courses/${encodeURIComponent(courseId)}/files`;

  useEffect(() => {
    if (!action) return;
    const owner = root.current;
    const dialog = owner?.querySelector<HTMLElement>('[role="dialog"]');
    if (!dialog) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusable = () =>
      [
        ...dialog.querySelectorAll<HTMLElement>(
          'button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex="0"]',
        ),
      ].filter((element) => element.getClientRects().length);
    focusable()[0]?.focus({ preventScroll: true });
    const key = (event: globalThis.KeyboardEvent) => {
      if (document.querySelector("[data-workspace-capture-overlay]")) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if (!working.current) {
          propertyRequest.current += 1;
          setAction(null);
        }
      } else if (event.key === "Tab") {
        const targets = focusable();
        const next = !dialog.contains(document.activeElement)
          ? targets[0]
          : event.shiftKey && document.activeElement === targets[0]
            ? targets.at(-1)
            : !event.shiftKey && document.activeElement === targets.at(-1)
              ? targets[0]
              : null;
        if (next) {
          event.preventDefault();
          next.focus();
        }
      }
    };
    document.addEventListener("keydown", key, true);
    return () => {
      document.removeEventListener("keydown", key, true);
      if (previous?.isConnected && previous.getClientRects().length)
        previous.focus({ preventScroll: true });
      else if (owner?.isConnected) owner.focus({ preventScroll: true });
    };
  }, [action]);

  useEffect(() => {
    const at = ++epoch.current;
    return () => {
      if (epoch.current === at) epoch.current += 1;
    };
  }, [courseId]);

  useEffect(() => {
    if (path === undefined) return;
    let cancelled = false;
    void Promise.resolve().then(() => {
      if (!cancelled)
        setNavigation((old) =>
          old.paths[old.at] === path
            ? old
            : { paths: [...old.paths.slice(0, old.at + 1), path], at: old.at + 1 },
        );
    });
    return () => {
      cancelled = true;
    };
  }, [path]);

  const readDirectory = useCallback(
    (directory: string, signal?: AbortSignal) =>
      api.read<Listing>(
        `/courses/${encodeURIComponent(courseId)}/files?path=${encodeURIComponent(directory)}`,
        signal,
      ),
    [courseId],
  );
  useEffect(() => {
    const abort = new AbortController();
    const at = epoch.current;
    void Promise.resolve()
      .then(() => {
        if (!abort.signal.aborted) {
          setLoading(true);
          setError("");
          setSelection([]);
          setContextMenu(null);
        }
        return readDirectory(currentPath, abort.signal);
      })
      .then((value) => {
        if (!abort.signal.aborted && at === epoch.current) setListing(value);
      })
      .catch((reason: unknown) => {
        if (!abort.signal.aborted && at === epoch.current) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!abort.signal.aborted && at === epoch.current) setLoading(false);
      });
    return () => abort.abort();
  }, [currentPath, revision, refreshVersion, readDirectory]);

  useEffect(() => {
    if (action !== "move") return;
    const abort = new AbortController();
    void readDirectory(targetPath, abort.signal)
      .then((value) => {
        if (!abort.signal.aborted) setTargetListing(value);
      })
      .catch((reason: unknown) => {
        if (!abort.signal.aborted) setError(errorMessage(reason));
      });
    return () => abort.abort();
  }, [action, targetPath, readDirectory]);

  useEffect(() => {
    if (!contextMenu) return;
    root.current
      ?.querySelector<HTMLButtonElement>(".file-manager-context button:not(:disabled)")
      ?.focus();
    const dismiss = () => setContextMenu(null);
    const key = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") dismiss();
    };
    document.addEventListener("pointerdown", dismiss);
    window.addEventListener("resize", dismiss);
    document.addEventListener("scroll", dismiss, true);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      window.removeEventListener("resize", dismiss);
      document.removeEventListener("scroll", dismiss, true);
      document.removeEventListener("keydown", key);
    };
  }, [contextMenu]);

  const refresh = () => setRefreshVersion((value) => value + 1);
  const ready = connected && listing?.root_status === "ready";
  const disabled = !ready || busy || !!pending;
  const entries = useMemo(() => {
    if (listing?.course_id !== courseId || listing.path !== currentPath) return [];
    const result = listing.entries.filter((entry) =>
      entry.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
    );
    result.sort((left, right) => {
      if (left.kind === "directory" && right.kind !== "directory") return -1;
      if (right.kind === "directory" && left.kind !== "directory") return 1;
      if (sort === "size") return (right.size_bytes ?? -1) - (left.size_bytes ?? -1);
      if (sort === "modified")
        return (right.modified_at || "").localeCompare(left.modified_at || "");
      if (sort === "type") {
        const compared = left.viewer.localeCompare(right.viewer);
        if (compared) return compared;
      }
      return left.name.localeCompare(right.name, "zh-CN", { numeric: true });
    });
    return result;
  }, [listing, courseId, currentPath, query, sort]);
  const selectedEntries = entries.filter((entry) => selection.includes(entry.path));

  const navigate = (next: string, at?: number) => {
    setSelection([]);
    setQuery("");
    setContextMenu(null);
    setNavigation((old) =>
      at !== undefined
        ? { ...old, at }
        : old.paths[old.at] === next
          ? old
          : { paths: [...old.paths.slice(0, old.at + 1), next], at: old.at + 1 },
    );
    onPathChange?.(next);
  };

  const open = async (entry: Entry) => {
    if (entry.kind === "blocked" || !connected || busy) return;
    if (entry.kind === "directory") {
      navigate(entry.path);
      return;
    }
    const at = epoch.current;
    try {
      const value = await api.write<OpenFile>(`${prefix}/open`, { path: entry.path });
      if (at === epoch.current) onOpen(value);
    } catch (reason) {
      if (at === epoch.current) setError(errorMessage(reason));
    }
  };

  const select = (event: MouseEvent, entry: Entry) => {
    setContextMenu(null);
    if (event.shiftKey && anchor.current) {
      const start = entries.findIndex((value) => value.path === anchor.current);
      const end = entries.findIndex((value) => value.path === entry.path);
      if (start >= 0 && end >= 0)
        setSelection(
          entries.slice(Math.min(start, end), Math.max(start, end) + 1).map((v) => v.path),
        );
    } else if (event.ctrlKey || event.metaKey) {
      setSelection((old) =>
        old.includes(entry.path)
          ? old.filter((value) => value !== entry.path)
          : [...old, entry.path],
      );
      anchor.current = entry.path;
    } else {
      setSelection([entry.path]);
      anchor.current = entry.path;
    }
  };

  const execute = async (
    name: string,
    call: (operationId: string) => Promise<Result>,
    operationPath?: string,
  ) => {
    if (receipt.snapshot()) throw new Error("请先核对上一项文件操作（FILE_OPERATION_PENDING）");
    const operationId = crypto.randomUUID();
    receipt.start({ operationId, action: name, path: operationPath });
    try {
      const result = await call(operationId);
      if (result.state !== "completed")
        throw new Error("文件操作结果需要核对（FILE_OPERATION_UNKNOWN）");
      receipt.clear(operationId);
      return result;
    } catch (reason) {
      if (!operationUnknown(reason)) receipt.clear(operationId);
      throw reason;
    }
  };

  const run = async (action: () => Promise<void>) => {
    if (working.current || disabled) return;
    const at = epoch.current;
    working.current = true;
    setBusy(true);
    setError("");
    setContextMenu(null);
    try {
      await action();
      if (at === epoch.current) {
        refresh();
        onChanged?.();
      }
    } catch (reason) {
      if (at === epoch.current) setError(fileError(reason));
    } finally {
      working.current = false;
      if (at === epoch.current) {
        setBusy(false);
        setProgress("");
      }
    }
  };

  const transfer = async (value: FileClipboard, destination = currentPath) => {
    if (value.courseId !== courseId)
      throw new Error(
        "请在当前课程内复制文件；外部文件可直接拖入（FILE_CLIPBOARD_COURSE_MISMATCH）",
      );
    await execute("transfer", (operationId) =>
      api.write(
        `${prefix}/transfer`,
        {
          operation_id: operationId,
          paths: value.paths,
          destination,
          mode: value.mode,
        },
        { timeoutMs: 300_000 },
      ),
    );
    if (value.mode === "move") {
      sharedClipboard.set(null);
    }
  };

  const upload = async (files: IncomingFile[], destination = currentPath) => {
    if (!files.length) throw new Error("没有读取到可复制的文件（FILE_DROP_EMPTY）");
    for (const item of files) {
      if (item.file.size > MAX_UPLOAD_BYTES)
        throw new Error(`${item.file.name} 超过 8 GiB（FILE_UPLOAD_TOO_LARGE）`);
    }
    for (let index = 0; index < files.length; index += 1) {
      const { file, path: relativePath } = files[index];
      setProgress(`正在复制 ${index + 1}/${files.length}：${file.name}`);
      await execute(
        "upload",
        (operationId) => {
          const params = new URLSearchParams({
            operation_id: operationId,
            destination,
            path: relativePath,
          });
          return api.upload(`${prefix}/upload?${params}`, file);
        },
        relativePath,
      );
    }
  };

  const nativePaste = async (allowInternal = true) => {
    try {
      await execute("clipboard", (operationId) =>
        api.write(
          `${prefix}/clipboard`,
          { operation_id: operationId, destination: currentPath },
          {
            timeoutMs: 300_000,
          },
        ),
      );
    } catch (reason) {
      if (
        reason instanceof ApiError &&
        ["CLIPBOARD_FILES_EMPTY", "CLIPBOARD_EMPTY"].includes(reason.code) &&
        allowInternal &&
        sharedClipboard.read()
      ) {
        await transfer(sharedClipboard.read()!);
        return;
      }
      throw reason;
    }
  };

  const copyEmptyDirectories = async (paths: string[], destination: string) => {
    for (const path of paths) {
      let parent = destination;
      for (const name of path.split("/")) {
        const value = await readDirectory(parent);
        const exists = value.entries.find((entry) => entry.name === name);
        if (exists && exists.kind !== "directory")
          throw new Error("目标路径中存在同名文件，请重新选择文件夹（FILE_DIRECTORY_REQUIRED）");
        if (!exists) {
          setProgress(`正在复制文件夹：${path}`);
          await execute("directory", (operationId) =>
            api.write(`${prefix}/directory`, {
              operation_id: operationId,
              path: parent,
              name,
            }),
          );
        }
        parent = parent ? `${parent}/${name}` : name;
      }
    }
  };

  const copySelection = (mode: "copy" | "move") => {
    if (disabled || !selectedEntries.length) return;
    const value: FileClipboard = {
      courseId,
      paths: selectedEntries.map((entry) => entry.path),
      mode,
    };
    sharedClipboard.set(value);
    setContextMenu(null);
    const previous = document.activeElement;
    const area = document.createElement("textarea");
    area.value = selectedEntries.map((entry) => entry.name).join("\n");
    area.setAttribute("aria-hidden", "true");
    area.style.position = "fixed";
    area.style.left = "-10000px";
    root.current?.appendChild(area);
    area.focus();
    area.select();
    const write = (event: globalThis.ClipboardEvent) => {
      if (!event.clipboardData) return;
      event.clipboardData.setData(CLIPBOARD_FORMAT, JSON.stringify(value));
      event.clipboardData.setData("text/plain", area.value);
      event.preventDefault();
    };
    document.addEventListener("copy", write);
    sharedClipboard.beginWrite();
    try {
      document.execCommand("copy");
    } catch {
      // Internal receipt remains usable even where browser clipboard writing is denied.
    } finally {
      sharedClipboard.endWrite();
      document.removeEventListener("copy", write);
      area.remove();
      if (previous instanceof HTMLElement) previous.focus();
    }
  };

  const paste = (event?: ClipboardEvent<HTMLElement>) => {
    if (disabled) return;
    if (event && writableTarget(event.target)) return;
    event?.preventDefault();
    let internal: FileClipboard | null = null;
    try {
      const value = event?.clipboardData.getData(CLIPBOARD_FORMAT);
      const data = value ? JSON.parse(value) : null;
      if (
        data &&
        typeof data.courseId === "string" &&
        Array.isArray(data.paths) &&
        data.paths.every((v: unknown) => typeof v === "string") &&
        ["copy", "move"].includes(data.mode)
      )
        internal = data;
    } catch {
      /* External clipboard data is not trusted as an internal path list. */
    }
    const files = event ? Array.from(event.clipboardData.files || []) : [];
    void run(async () => {
      if (internal) await transfer(internal);
      else if (files.length) await upload(files.map((file) => ({ file, path: file.name })));
      else await nativePaste(!event);
    });
  };

  const show = (next: Action) => {
    if (
      (next === "properties" ? !ready || busy : disabled) ||
      (next !== "directory" && !selectedEntries.length)
    )
      return;
    setAction(next);
    setContextMenu(null);
    setError("");
    setActionName(next === "rename" ? selectedEntries[0].name : "");
    setTargetPath(currentPath);
    setProperties(null);
    const requestId = ++propertyRequest.current;
    if (next === "properties") {
      const at = epoch.current;
      void api
        .write<Schema<"CourseFilePropertiesResponse">>(`${prefix}/properties`, {
          path: selectedEntries[0].path,
        })
        .then((value) => {
          if (at === epoch.current && requestId === propertyRequest.current) setProperties(value);
        })
        .catch((reason) => {
          if (at === epoch.current && requestId === propertyRequest.current)
            setError(errorMessage(reason));
        });
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!action || action === "properties") return;
    const kind = action;
    const selectedPaths = selectedEntries.map((entry) => entry.path);
    void run(async () => {
      if (kind === "move")
        await transfer({ courseId, paths: selectedPaths, mode: "move" }, targetPath);
      else
        await execute(kind, (operationId) =>
          api.write(
            `${prefix}/${kind}`,
            kind === "directory"
              ? { operation_id: operationId, path: currentPath, name: actionName.trim() }
              : { operation_id: operationId, path: selectedPaths[0], name: actionName.trim() },
          ),
        );
      setAction(null);
    });
  };

  const recycle = () => {
    const values = selectedEntries.map((entry) => entry.path);
    if (!values.length) return;
    void run(async () => {
      for (const value of values)
        await execute(
          "delete",
          (operationId) =>
            api.write(
              `${prefix}/delete`,
              {
                operation_id: operationId,
                path: value,
                confirm: true,
              },
              { timeoutMs: 180_000 },
            ),
          value,
        );
      setSelection([]);
    });
  };

  const reconcile = async (confirm = false) => {
    if (!pending || busy) return;
    const at = epoch.current;
    setBusy(true);
    setError("");
    try {
      const endpoint = `${prefix}/operations/${pending.operationId}`;
      const value = confirm
        ? await api.write<OperationStatus>(`${endpoint}/reconcile`, { confirm: true })
        : await api.read<OperationStatus>(endpoint);
      if (at !== epoch.current) return;
      if (["completed", "failed", "reviewed", "conflict"].includes(value.state)) {
        receipt.clear(pending.operationId);
        setReview(null);
        refresh();
        onChanged?.();
        if (value.state === "failed") setError("本次操作未完成，未自动重试。请核对文件状态。");
      } else setReview(value);
    } catch (reason) {
      if (at === epoch.current) setError(errorMessage(reason));
    } finally {
      if (at === epoch.current) setBusy(false);
    }
  };

  const keyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (action) {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        propertyRequest.current += 1;
        setAction(null);
      }
      return;
    }
    if (event.target instanceof Element && event.target.closest('[role="menu"]')) return;
    if (writableTarget(event.target)) return;
    const command = event.ctrlKey || event.metaKey;
    if (command && event.key.toLowerCase() === "a") {
      event.preventDefault();
      setSelection(entries.filter((entry) => entry.kind !== "blocked").map((entry) => entry.path));
    } else if (command && ["c", "x"].includes(event.key.toLowerCase())) {
      event.preventDefault();
      copySelection(event.key.toLowerCase() === "x" ? "move" : "copy");
    } else if (event.key === "Delete") {
      event.preventDefault();
      recycle();
    } else if (event.key === "F2" && selection.length === 1) {
      event.preventDefault();
      show("rename");
    } else if (event.key === "Enter" && selectedEntries.length === 1 && !action) {
      event.preventDefault();
      void open(selectedEntries[0]);
    } else if (
      ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key) &&
      entries.length &&
      event.target instanceof HTMLElement &&
      event.target.closest("[data-file-path]")
    ) {
      event.preventDefault();
      const item = event.target.closest<HTMLElement>("[data-file-path]");
      const current = entries.findIndex((entry) => entry.path === item?.dataset.filePath);
      const container = root.current?.querySelector(".file-manager-content");
      const columns =
        view === "icons" && container
          ? Math.max(1, getComputedStyle(container).gridTemplateColumns.split(" ").length)
          : 1;
      const delta =
        event.key === "ArrowLeft"
          ? -1
          : event.key === "ArrowRight"
            ? 1
            : event.key === "ArrowUp"
              ? -columns
              : columns;
      const next = Math.max(0, Math.min(entries.length - 1, current + delta));
      const entry = entries[next];
      if (event.shiftKey && anchor.current) {
        const start = entries.findIndex((value) => value.path === anchor.current);
        setSelection(
          entries
            .slice(Math.min(Math.max(0, start), next), Math.max(start, next) + 1)
            .map((value) => value.path),
        );
      } else if (!command) {
        setSelection([entry.path]);
        anchor.current = entry.path;
      }
      root.current
        ?.querySelector<HTMLButtonElement>(`[data-file-path="${CSS.escape(entry.path)}"]`)
        ?.focus();
    } else if (event.altKey && event.key === "ArrowUp") {
      event.preventDefault();
      if (currentPath) navigate(parentPath(currentPath));
    } else if (event.key === "Escape") {
      setContextMenu(null);
      setSelection([]);
    }
  };

  const drop = (event: DragEvent<HTMLElement>, destination = currentPath) => {
    event.preventDefault();
    event.stopPropagation();
    dragDepth.current = 0;
    setDragOver(false);
    // This drop is consumed here. Notify document-level drag UI that the
    // gesture ended without bubbling the files into the host chat composer.
    window.dispatchEvent(new Event("dragend"));
    if (disabled) return;
    const data = event.dataTransfer;
    const internal = data.getData(CLIPBOARD_FORMAT);
    void run(async () => {
      if (internal) {
        const value = JSON.parse(internal) as FileClipboard;
        if (value.courseId !== courseId || !Array.isArray(value.paths))
          throw new Error("拖动来源不属于当前课程（FILE_DROP_SOURCE_INVALID）");
        await transfer({ ...value, mode: event.ctrlKey ? "copy" : "move" }, destination);
      } else {
        const incoming = await droppedItems(data);
        if (!incoming.files.length && !incoming.emptyDirectories.length)
          throw new Error("没有读取到可复制的文件（FILE_DROP_EMPTY）");
        if (incoming.files.length) await upload(incoming.files, destination);
        await copyEmptyDirectories(incoming.emptyDirectories, destination);
      }
    });
  };

  const actionButtons = (menu = false) => (
    <>
      <button
        type="button"
        role={menu ? "menuitem" : undefined}
        disabled={disabled}
        onClick={() => show("directory")}
        aria-label="新建文件夹"
      >
        <Icon name="folder" />
        {menu && "新建文件夹"}
      </button>
      <button
        type="button"
        role={menu ? "menuitem" : undefined}
        disabled={disabled || !selection.length}
        onClick={() => copySelection("copy")}
        aria-label="复制"
      >
        <Icon name="copy" />
        {menu && "复制"}
      </button>
      <button
        type="button"
        role={menu ? "menuitem" : undefined}
        disabled={disabled || !selection.length}
        onClick={() => copySelection("move")}
        aria-label="剪切"
      >
        <Icon name="cut" />
        {menu && "剪切"}
      </button>
      <button
        type="button"
        role={menu ? "menuitem" : undefined}
        disabled={disabled}
        onClick={() => paste()}
        aria-label="粘贴"
      >
        <Icon name="paste" />
        {menu && "粘贴"}
      </button>
      <button
        type="button"
        role={menu ? "menuitem" : undefined}
        className="file-manager-secondary-action"
        disabled={disabled || !selection.length}
        onClick={() => show("move")}
        aria-label="移动到"
      >
        <Icon name="folder" />
        {menu && "移动到…"}
      </button>
      <button
        type="button"
        role={menu ? "menuitem" : undefined}
        disabled={disabled || selection.length !== 1}
        onClick={() => show("rename")}
        aria-label="重命名"
      >
        <Icon name="rename" />
        {menu && "重命名"}
      </button>
      <button
        type="button"
        role={menu ? "menuitem" : undefined}
        disabled={disabled || !selection.length}
        onClick={recycle}
        aria-label="移入回收站"
      >
        <Icon name="trash" />
        {menu && "移入回收站"}
      </button>
      <button
        type="button"
        role={menu ? "menuitem" : undefined}
        className="file-manager-secondary-action"
        disabled={!ready || selection.length !== 1}
        onClick={() => show("properties")}
        aria-label="属性"
      >
        <Icon name="info" />
        {menu && "属性"}
      </button>
    </>
  );

  const breadcrumbs = currentPath ? currentPath.split("/") : [];
  const title =
    action === "directory"
      ? "新建文件夹"
      : action === "rename"
        ? "重命名"
        : action === "move"
          ? "移动到"
          : "属性";
  return (
    <section
      className={`file-manager ${compact ? "file-manager-compact" : ""} ${dragOver ? "file-manager-drop-active" : ""}`}
      data-selected-count={selection.length}
      aria-label="文件资源管理器"
      tabIndex={0}
      ref={root}
      onKeyDown={keyDown}
      onPaste={paste}
      onDragEnter={(event) => {
        event.preventDefault();
        event.stopPropagation();
        dragDepth.current += 1;
        if (
          Array.from(event.dataTransfer.types).some(
            (type) => type === "Files" || type === CLIPBOARD_FORMAT,
          )
        )
          setDragOver(true);
      }}
      onDragOver={(event) => {
        event.preventDefault();
        event.stopPropagation();
        event.dataTransfer.dropEffect = event.dataTransfer.types.includes(CLIPBOARD_FORMAT)
          ? "move"
          : "copy";
      }}
      onDragLeave={(event) => {
        event.stopPropagation();
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (!dragDepth.current) setDragOver(false);
      }}
      onDrop={(event) => drop(event)}
    >
      <div className="file-manager-navigation">
        <button
          className="icon-button"
          aria-label="后退"
          title="后退"
          disabled={navigation.at === 0}
          onClick={() => navigate(navigation.paths[navigation.at - 1], navigation.at - 1)}
        >
          <Icon name="back" />
        </button>
        <button
          className="icon-button"
          aria-label="前进"
          title="前进"
          disabled={navigation.at === navigation.paths.length - 1}
          onClick={() => navigate(navigation.paths[navigation.at + 1], navigation.at + 1)}
        >
          <Icon name="forward" />
        </button>
        <button
          className="icon-button"
          aria-label="上一级"
          title="上一级"
          disabled={!currentPath}
          onClick={() => navigate(parentPath(currentPath))}
        >
          <Icon name="up" />
        </button>
        <nav className="file-manager-breadcrumbs" aria-label="文件夹位置">
          <button onClick={() => navigate("")} title={listing?.root_path || ""}>
            <Icon name="folder" size={15} />
            {listing?.title || "课程文件"}
          </button>
          {breadcrumbs.map((part, index) => (
            <span key={breadcrumbs.slice(0, index + 1).join("/")}>
              <span aria-hidden="true">›</span>
              <button onClick={() => navigate(breadcrumbs.slice(0, index + 1).join("/"))}>
                {part}
              </button>
            </span>
          ))}
        </nav>
        <button
          className="icon-button"
          aria-label="刷新文件"
          title="刷新文件"
          disabled={loading}
          onClick={refresh}
        >
          <Icon name="refresh" />
        </button>
        <input
          className="file-manager-search"
          aria-label="搜索当前文件夹"
          placeholder="搜索当前文件夹"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <BilibiliImport
        courseId={courseId}
        destinationPath={path}
        connected={connected}
        onImported={() => {
          onChanged?.();
          refresh();
        }}
      />
      <BilibiliSourceRecovery
        courseId={courseId}
        entry={selectedEntries.length === 1 ? selectedEntries[0] : null}
        disabled={disabled}
      />
      <div className="file-manager-toolbar" role="toolbar" aria-label="文件操作">
        <div className="file-manager-actions">
          {actionButtons()}
          {compact && (
            <details
              className="file-manager-more"
              onKeyDown={(event) => event.stopPropagation()}
              onBlur={(event) => {
                if (!event.currentTarget.contains(event.relatedTarget))
                  event.currentTarget.removeAttribute("open");
              }}
            >
              <summary aria-label="更多文件操作" title="更多文件操作">
                <Icon name="more" />
              </summary>
              <div
                className="file-actions-menu"
                role="menu"
                aria-label="更多文件操作"
                onClick={(event) => {
                  if ((event.target as Element).closest("button"))
                    event.currentTarget.parentElement?.removeAttribute("open");
                }}
                onKeyDown={(event) => {
                  const buttons = [
                    ...event.currentTarget.querySelectorAll<HTMLButtonElement>(
                      "button:not(:disabled)",
                    ),
                  ];
                  const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
                  if (event.key === "Escape") {
                    event.currentTarget.parentElement?.removeAttribute("open");
                    event.currentTarget.parentElement
                      ?.querySelector<HTMLElement>("summary")
                      ?.focus();
                  } else if (
                    ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) &&
                    buttons.length
                  ) {
                    event.preventDefault();
                    const next =
                      event.key === "Home"
                        ? 0
                        : event.key === "End"
                          ? buttons.length - 1
                          : (index + (event.key === "ArrowUp" ? -1 : 1) + buttons.length) %
                            buttons.length;
                    buttons[next]?.focus();
                  }
                }}
              >
                {actionButtons(true)}
              </div>
            </details>
          )}
        </div>
        <div className="file-manager-view">
          <label className="sr-only" htmlFor={`file-sort-${courseId}`}>
            排序方式
          </label>
          <select
            id={`file-sort-${courseId}`}
            aria-label="排序方式"
            value={sort}
            onChange={(event) => setSort(event.target.value)}
          >
            <option value="name">名称</option>
            <option value="type">类型</option>
            <option value="size">大小</option>
            <option value="modified">修改时间</option>
          </select>
          <button
            aria-label="图标视图"
            title="图标视图"
            aria-pressed={view === "icons"}
            onClick={() => setView("icons")}
          >
            <Icon name="grid" />
          </button>
          <button
            aria-label="列表视图"
            title="列表视图"
            aria-pressed={view === "list"}
            onClick={() => setView("list")}
          >
            <Icon name="list" />
          </button>
        </div>
      </div>
      {error && (
        <p role="alert" className="file-manager-error">
          {error}
        </p>
      )}
      {pending && !busy && (
        <div className="file-manager-pending" role="status">
          <span>文件操作结果待核对，已暂停新的文件变更。</span>
          <Button variant="outline" disabled={busy} onClick={() => void reconcile()}>
            核对文件操作
          </Button>
          {review && (
            <>
              <p>请先检查相关文件的实际状态；核对记录不会重新执行复制、移动或删除。</p>
              <Button variant="outline" disabled={busy} onClick={() => void reconcile(true)}>
                已检查磁盘状态
              </Button>
            </>
          )}
        </div>
      )}
      {progress && (
        <p className="file-manager-progress" role="status">
          {progress}
        </p>
      )}
      <div
        className={`file-manager-content ${view === "icons" ? "file-grid" : "file-list"}`}
        role="listbox"
        aria-label="文件列表"
        aria-multiselectable="true"
        aria-busy={loading}
        onClick={(event) => {
          if (event.target === event.currentTarget) {
            setSelection([]);
            root.current?.focus();
          }
        }}
        onContextMenu={(event) => {
          event.preventDefault();
          if (event.target === event.currentTarget) setSelection([]);
          setContextMenu({
            x: Math.min(event.clientX, window.innerWidth - 180),
            y: Math.min(event.clientY, window.innerHeight - 330),
          });
        }}
      >
        {view === "list" && (
          <div className="file-list-heading" aria-hidden="true">
            <span>名称</span>
            <span>修改时间</span>
            <span>类型</span>
            <span>大小</span>
          </div>
        )}
        {loading ? (
          <p className="file-manager-empty" role="status">
            正在读取文件…
          </p>
        ) : !listing || listing.root_status !== "ready" ? (
          <div className="file-manager-empty">
            <Icon name="folder" size={44} />
            <p>
              {listing?.root_status === "unbound"
                ? "请先为当前课程选择文件夹。"
                : "课程文件夹暂不可用。"}
            </p>
          </div>
        ) : !entries.length ? (
          <div className="file-manager-empty">
            <Icon name="folder" size={44} />
            <p>{query ? "当前文件夹没有匹配的文件" : "此文件夹为空"}</p>
          </div>
        ) : (
          entries.map((entry) => (
            <button
              key={entry.path}
              role="option"
              className={`file-manager-entry ${entry.kind === "directory" ? "is-directory" : ""} ${clipboard?.mode === "move" && clipboard.courseId === courseId && clipboard.paths.includes(entry.path) ? "is-cut" : ""}`}
              aria-selected={selection.includes(entry.path)}
              aria-label={entry.name}
              data-file-path={entry.path}
              disabled={!connected || entry.kind === "blocked"}
              title={entry.name}
              draggable={!disabled && entry.kind !== "blocked"}
              onClick={(event) => select(event, entry)}
              onDoubleClick={() => void open(entry)}
              onContextMenu={(event) => {
                if (!selection.includes(entry.path)) setSelection([entry.path]);
                anchor.current = entry.path;
                event.preventDefault();
              }}
              onDragStart={(event) => {
                const paths = selection.includes(entry.path) ? selection : [entry.path];
                const data: FileClipboard = { courseId, paths, mode: "move" };
                event.dataTransfer.setData(CLIPBOARD_FORMAT, JSON.stringify(data));
                event.dataTransfer.effectAllowed = "copyMove";
              }}
              onDragOver={(event) => {
                if (entry.kind === "directory") {
                  event.preventDefault();
                  event.stopPropagation();
                  event.dataTransfer.dropEffect = event.dataTransfer.types.includes(
                    CLIPBOARD_FORMAT,
                  )
                    ? "move"
                    : "copy";
                }
              }}
              onDrop={(event) => {
                if (entry.kind === "directory") drop(event, entry.path);
              }}
            >
              <span className="file-manager-file-icon">
                {entry.platform === "bilibili" && entry.asset_id && entry.source_version ? (
                  <BilibiliFileIcon
                    key={entry.asset_id + entry.source_version}
                    courseId={courseId}
                    assetId={entry.asset_id}
                    sourceVersion={entry.source_version}
                    size={view === "icons" ? 42 : 19}
                    preview={view === "icons"}
                  />
                ) : renderFileIcon ? (
                  renderFileIcon(entry)
                ) : (
                  <Icon
                    name={
                      entry.kind === "directory"
                        ? "folder"
                        : entry.viewer === "video"
                          ? "video"
                          : entry.viewer === "pdf"
                            ? "file-pdf"
                            : entry.viewer === "text"
                              ? "file-text"
                              : "file"
                    }
                    size={view === "icons" ? 42 : 19}
                  />
                )}
              </span>
              <span className="file-manager-file-name">{entry.name}</span>
              {view === "list" && (
                <>
                  <span>{displayTime(entry.modified_at)}</span>
                  <span>
                    {entry.kind === "directory"
                      ? "文件夹"
                      : entry.name.includes(".")
                        ? `${entry.name.split(".").pop()?.toUpperCase()} 文件`
                        : "文件"}
                  </span>
                  <span>{entry.kind === "directory" ? "—" : displaySize(entry.size_bytes)}</span>
                </>
              )}
            </button>
          ))
        )}
      </div>
      <div className="file-manager-status">
        <span>
          {entries.length} 个项目{selection.length ? ` · 已选择 ${selection.length} 项` : ""}
        </span>
        <span>
          {clipboard?.mode === "move" && clipboard.courseId === courseId
            ? `${clipboard.paths.length} 项待移动`
            : ""}
        </span>
      </div>
      {dragOver && (
        <div className="file-manager-drop-hint" aria-hidden="true">
          <Icon name="copy" size={28} />
          <span>复制到当前文件夹</span>
        </div>
      )}
      {contextMenu && (
        <div
          role="menu"
          aria-label="文件右键菜单"
          className="file-manager-context"
          style={{ left: contextMenu.x, top: Math.max(4, contextMenu.y) }}
          onPointerDown={(event) => event.stopPropagation()}
          onKeyDown={(event) => {
            if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            event.stopPropagation();
            const buttons = Array.from(
              event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
            );
            const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
            const next =
              event.key === "Home"
                ? 0
                : event.key === "End"
                  ? buttons.length - 1
                  : (current + (event.key === "ArrowUp" ? -1 : 1) + buttons.length) %
                    buttons.length;
            buttons[next]?.focus();
          }}
        >
          {actionButtons(true)}
        </div>
      )}
      {action && (
        <div className="file-dialog-backdrop">
          <form
            className="file-dialog"
            role="dialog"
            aria-modal="true"
            aria-label={title}
            onSubmit={submit}
          >
            <h2>{title}</h2>
            {action === "properties" ? (
              properties ? (
                <dl className="file-properties">
                  <dt>名称</dt>
                  <dd>{properties.name}</dd>
                  <dt>位置</dt>
                  <dd>
                    {properties.root_path}
                    {properties.path ? `/${parentPath(properties.path)}` : ""}
                  </dd>
                  <dt>类型</dt>
                  <dd>{properties.kind === "directory" ? "文件夹" : "文件"}</dd>
                  <dt>大小</dt>
                  <dd>{displaySize(properties.size_bytes)}</dd>
                  <dt>创建时间</dt>
                  <dd>{displayTime(properties.created_at)}</dd>
                  <dt>修改时间</dt>
                  <dd>{displayTime(properties.modified_at)}</dd>
                </dl>
              ) : (
                <p>正在读取属性…</p>
              )
            ) : action === "move" ? (
              <div className="file-destination-picker">
                <div>
                  <button
                    type="button"
                    disabled={!targetPath}
                    onClick={() => setTargetPath(parentPath(targetPath))}
                  >
                    上一级
                  </button>
                  <span>{targetPath || listing?.title || "课程根目录"}</span>
                </div>
                <ul>
                  {targetListing?.entries
                    .filter(
                      (entry) =>
                        entry.kind === "directory" &&
                        !selection.some(
                          (selected) =>
                            entry.path === selected || entry.path.startsWith(selected + "/"),
                        ),
                    )
                    .map((entry) => (
                      <li key={entry.path}>
                        <button type="button" onClick={() => setTargetPath(entry.path)}>
                          <Icon name="folder" size={17} />
                          {entry.name}
                        </button>
                      </li>
                    ))}
                </ul>
                <p>移动到：{targetPath || "课程根目录"}</p>
              </div>
            ) : (
              <label>
                名称
                <input
                  autoFocus
                  aria-label="名称"
                  value={actionName}
                  maxLength={200}
                  onChange={(event) => setActionName(event.target.value)}
                />
              </label>
            )}
            {error && (
              <p className="explorer-error" role="alert">
                {error}
              </p>
            )}
            <div className="file-dialog-buttons">
              <Button type="button" variant="ghost" onClick={() => setAction(null)}>
                {action === "properties" ? "关闭" : "取消"}
              </Button>
              {action !== "properties" && (
                <Button
                  type="submit"
                  disabled={busy || !!pending || (action !== "move" && !actionName.trim())}
                >
                  确定
                </Button>
              )}
            </div>
          </form>
        </div>
      )}
    </section>
  );
}
