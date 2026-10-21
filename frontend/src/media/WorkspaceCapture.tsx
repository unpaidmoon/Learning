import { useEffect, useRef, useState } from "react";
import type { PointerEvent } from "react";
import { createPortal } from "react-dom";
import { CaptureError, exportCrop } from "./capture";
import type { CaptureResult, CaptureSource, CropRect } from "./capture";
import { captureWorkspace } from "./workspaceSnapshot";
import type { WorkspaceSnapshot } from "./workspaceSnapshot";
import "./WorkspaceCapture.css";

type Point = { x: number; y: number };
const rectangle = (start: Point, end: Point): CropRect => ({
  x: Math.min(start.x, end.x),
  y: Math.min(start.y, end.y),
  width: Math.abs(start.x - end.x),
  height: Math.abs(start.y - end.y),
});

export function WorkspaceCapture({
  root,
  onConfirm,
  onCancel,
}: {
  root: HTMLElement | null;
  sourceMetadata?: CaptureSource["source"];
  onConfirm: (result: CaptureResult) => void | Promise<void>;
  onCancel: () => void;
}) {
  const [snapshot, setSnapshot] = useState<WorkspaceSnapshot | null>(null);
  const [selection, setSelection] = useState<CropRect | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const drag = useRef<Point | null>(null);
  const confirming = useRef(false);
  const callbacks = useRef({ onConfirm, onCancel });
  useEffect(() => {
    callbacks.current = { onConfirm, onCancel };
  }, [onConfirm, onCancel]);

  useEffect(() => {
    let disposed = false;
    void Promise.resolve()
      .then(() => {
        if (disposed) return null;
        setSnapshot(null);
        setError("");
        if (!root)
          throw new CaptureError(
            "CAPTURE_AREA_UNAVAILABLE",
            "当前学习界面不可见，请返回软件后截图",
          );
        return captureWorkspace(root);
      })
      .then((value) => {
        if (!disposed && value) setSnapshot(value);
      })
      .catch((reason: unknown) => {
        if (!disposed)
          setError(
            reason instanceof CaptureError
              ? `${reason.message}（${reason.code}）`
              : "当前界面截图失败，请取消后重试（CAPTURE_RENDER_FAILED）",
          );
      });
    return () => {
      disposed = true;
    };
  }, [root, attempt]);

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      if (!confirming.current) callbacks.current.onCancel();
    };
    document.addEventListener("keydown", escape, true);
    return () => document.removeEventListener("keydown", escape, true);
  }, []);

  const point = (event: PointerEvent<HTMLDivElement>): Point => {
    const bounds = event.currentTarget.getBoundingClientRect();
    return {
      x: Math.max(0, Math.min(bounds.width, event.clientX - bounds.left)),
      y: Math.max(0, Math.min(bounds.height, event.clientY - bounds.top)),
    };
  };

  const finish = async (event: PointerEvent<HTMLDivElement>) => {
    const start = drag.current;
    drag.current = null;
    if (!start || !snapshot || confirming.current) return;
    const selected = rectangle(start, point(event));
    setSelection(selected);
    if (selected.width < 1 || selected.height < 1) {
      setError("请按住鼠标拖出截图范围（CROP_EMPTY）");
      return;
    }
    confirming.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await exportCrop(snapshot.source, {
        x: (selected.x * snapshot.source.width) / snapshot.bounds.width,
        y: (selected.y * snapshot.source.height) / snapshot.bounds.height,
        width: (selected.width * snapshot.source.width) / snapshot.bounds.width,
        height: (selected.height * snapshot.source.height) / snapshot.bounds.height,
      });
      await callbacks.current.onConfirm(result);
    } catch (reason) {
      setError(
        reason instanceof CaptureError
          ? `${reason.message}（${reason.code}）`
          : "选区未加入草稿，请重新框选或取消（CAPTURE_SAVE_FAILED）",
      );
    } finally {
      confirming.current = false;
      setBusy(false);
    }
  };

  return createPortal(
    <div
      className="workspace-capture"
      role="dialog"
      aria-modal="true"
      aria-label="框选软件内截图"
      data-workspace-capture-ignore
      data-workspace-capture-overlay="true"
      onWheel={(event) => {
        event.preventDefault();
        event.stopPropagation();
      }}
      onContextMenu={(event) => event.preventDefault()}
    >
      {snapshot && (
        <div
          className="workspace-capture-surface"
          aria-label="截图选区画布"
          style={{
            left: snapshot.bounds.left,
            top: snapshot.bounds.top,
            width: snapshot.bounds.width,
            height: snapshot.bounds.height,
          }}
          onPointerDown={(event) => {
            if (event.button !== 0 || confirming.current) return;
            event.preventDefault();
            event.currentTarget.setPointerCapture?.(event.pointerId);
            drag.current = point(event);
            setSelection(null);
            setError("");
          }}
          onPointerMove={(event) => {
            if (drag.current && !confirming.current)
              setSelection(rectangle(drag.current, point(event)));
          }}
          onPointerUp={(event) => void finish(event)}
          onPointerCancel={() => {
            drag.current = null;
            setSelection(null);
          }}
        >
          <img
            className="workspace-capture-frozen"
            src={snapshot.source.dataUrl}
            alt="本次截图时可见的软件内容，仅保留在本机"
            draggable={false}
          />
          <div className="workspace-capture-shade" />
          {selection && (
            <div
              className="workspace-capture-selection"
              aria-label="已选择范围"
              style={{
                left: selection.x,
                top: selection.y,
                width: selection.width,
                height: selection.height,
                backgroundImage: `url("${snapshot.source.dataUrl}")`,
                backgroundSize: `${snapshot.bounds.width}px ${snapshot.bounds.height}px`,
                backgroundPosition: `${-selection.x}px ${-selection.y}px`,
              }}
            >
              <span>
                {Math.floor(selection.width)} × {Math.floor(selection.height)}
              </span>
            </div>
          )}
        </div>
      )}
      <div className="workspace-capture-instructions">
        <span role="status">
          {busy
            ? "正在将选区加入草稿…"
            : snapshot
              ? "按住鼠标拖动，松开加入草稿 · Esc 取消"
              : error
                ? "截图尚未完成"
                : "正在冻结当前软件画面…"}
        </span>
        {error && !snapshot && (
          <button type="button" disabled={busy} onClick={() => setAttempt((value) => value + 1)}>
            重新截图
          </button>
        )}
        <button type="button" disabled={busy} onClick={() => callbacks.current.onCancel()}>
          取消截图
        </button>
      </div>
      {error && (
        <p className="workspace-capture-error" role="alert">
          {error}
        </p>
      )}
    </div>,
    document.fullscreenElement || document.body,
  );
}
