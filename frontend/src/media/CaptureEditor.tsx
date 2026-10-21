import { useEffect, useRef, useState } from "react";
import Cropper from "cropperjs";
import "cropperjs/dist/cropper.css";
import { Button } from "../components/ui/button";
import { CaptureError, exportCrop, normalizeCrop } from "./capture";
import type { CaptureResult, CaptureSource, CropRect } from "./capture";
import "./CaptureEditor.css";

export function CaptureEditor({
  source,
  onConfirm,
  onCancel,
}: {
  source: CaptureSource;
  onConfirm: (result: CaptureResult) => void | Promise<void>;
  onCancel: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const cropperRef = useRef<Cropper | null>(null);
  const [selection, setSelection] = useState<CropRect>(
    source.initialCrop || { x: 0, y: 0, width: source.width, height: source.height },
  );
  const [mode, setMode] = useState<"crop" | "move">("crop");
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    const image = imageRef.current;
    if (!image) return;
    const instance = new Cropper(image, {
      viewMode: 1,
      dragMode: "crop",
      autoCropArea: 1,
      responsive: true,
      restore: true,
      checkOrientation: false,
      rotatable: false,
      scalable: false,
      toggleDragModeOnDblclick: false,
      background: true,
      ready() {
        instance.setData(
          source.initialCrop || { x: 0, y: 0, width: source.width, height: source.height },
        );
        setReady(true);
      },
      crop(event) {
        try {
          setSelection(normalizeCrop(event.detail, source.width, source.height));
        } catch {
          // A subpixel box during dragging is not a valid export; confirm checks it again.
        }
      },
    });
    cropperRef.current = instance;
    return () => {
      cropperRef.current = null;
      instance.destroy();
      if (dialog?.open) dialog.close();
    };
  }, [source]);

  const updateField = (field: keyof CropRect, value: number) => {
    if (!Number.isFinite(value)) return;
    const next = { ...selection, [field]: Math.round(value) };
    next.x = Math.max(0, Math.min(source.width - 1, next.x));
    next.y = Math.max(0, Math.min(source.height - 1, next.y));
    next.width = Math.max(1, Math.min(source.width - next.x, next.width));
    next.height = Math.max(1, Math.min(source.height - next.y, next.height));
    cropperRef.current?.setData(next);
    setSelection(next);
  };

  const confirm = async () => {
    const instance = cropperRef.current;
    if (!instance || !ready || busy) return;
    setBusy(true);
    setError("");
    try {
      const result = await exportCrop(source, instance.getData());
      await onConfirm(result);
    } catch (reason) {
      setError(
        reason instanceof CaptureError
          ? `${reason.message}（${reason.code}）`
          : "图片尚未加入草稿，请保留选区后重试",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <dialog
      ref={dialogRef}
      className="capture-editor"
      aria-labelledby="capture-editor-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onCancel();
      }}
    >
      <div className="capture-heading">
        <div>
          <h2 id="capture-editor-title">编辑截图</h2>
          <p>框选要提问的范围，确认后直接加入草稿。</p>
        </div>
        <Button type="button" variant="ghost" disabled={busy} onClick={onCancel}>
          取消
        </Button>
      </div>
      <div className="capture-toolbar" aria-label="截图编辑工具">
        <Button
          type="button"
          size="sm"
          variant={mode === "crop" ? "default" : "outline"}
          aria-pressed={mode === "crop"}
          disabled={!ready || busy}
          onClick={() => {
            cropperRef.current?.setDragMode("crop");
            setMode("crop");
          }}
        >
          框选
        </Button>
        <Button
          type="button"
          size="sm"
          variant={mode === "move" ? "default" : "outline"}
          aria-pressed={mode === "move"}
          disabled={!ready || busy}
          onClick={() => {
            cropperRef.current?.setDragMode("move");
            setMode("move");
          }}
        >
          移动画面
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          aria-label="缩小截图"
          disabled={!ready || busy}
          onClick={() => cropperRef.current?.zoom(-0.15)}
        >
          −
        </Button>
        <Button
          type="button"
          size="sm"
          variant="outline"
          aria-label="放大截图"
          disabled={!ready || busy}
          onClick={() => cropperRef.current?.zoom(0.15)}
        >
          ＋
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={!ready || busy}
          onClick={() => {
            cropperRef.current?.reset();
            cropperRef.current?.setData({
              x: 0,
              y: 0,
              width: source.width,
              height: source.height,
            });
          }}
        >
          重新框选
        </Button>
        <small>滚轮缩放 · 拖动边框调整范围</small>
      </div>
      <div className="capture-viewport" aria-label="原图与裁剪选区">
        <img ref={imageRef} src={source.dataUrl} alt="仅供本地编辑的原始截图" />
      </div>
      <div className="capture-coordinates" aria-label="选区原图像素坐标">
        {(
          [
            ["x", "左侧"],
            ["y", "顶部"],
            ["width", "宽度"],
            ["height", "高度"],
          ] as const
        ).map(([field, label]) => (
          <label key={field}>
            {label}
            <input
              aria-label={`选区${label}`}
              type="number"
              min={field === "x" || field === "y" ? 0 : 1}
              step={1}
              value={selection[field]}
              disabled={!ready || busy}
              onChange={(event) => updateField(field, event.target.valueAsNumber)}
            />
          </label>
        ))}
        <small>
          原图 {source.width} × {source.height}
          {source.source ? ` · 视频 ${(source.source.time_ms / 1000).toFixed(1)} 秒` : ""}
        </small>
      </div>
      {error && (
        <p className="capture-error" role="alert">
          {error}
        </p>
      )}
      <div className="capture-footer">
        <p>仅确认选区会作为附件；原图保留在本机，供重新裁剪。</p>
        <Button type="button" disabled={!ready || busy} onClick={() => void confirm()}>
          {busy ? "正在加入…" : "确认选区并加入草稿"}
        </Button>
      </div>
    </dialog>
  );
}
