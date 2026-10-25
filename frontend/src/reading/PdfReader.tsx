import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import type { ReadingActionHandler, SourceJump } from "./actions";
import { ReaderSession } from "./readerSession";
import "./reading.css";

export function PdfReader({
  file,
  onSourceAction,
  sourceJump,
  visible = true,
}: {
  file: Schema<"CourseFileOpenResponse">;
  onSourceAction?: ReadingActionHandler;
  sourceJump?: SourceJump;
  visible?: boolean;
}) {
  const host = useRef<HTMLDivElement>(null);
  const session = useRef<ReaderSession | null>(null);
  const visibility = useRef(visible);
  const [selection, setSelection] = useState<Schema<"PdfSelectionInput"> | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionPosition, setActionPosition] = useState({ left: 12, top: 12 });
  useLayoutEffect(() => {
    visibility.current = visible;
    session.current?.setVisible(visible);
  }, [visible]);
  useEffect(() => {
    if (!host.current) return;
    let live = true;
    const report = (reason: unknown) => {
      if (live) setError(errorMessage(reason));
    };
    const current = new ReaderSession(host.current, file, {
      selection: (value) => {
        if (!live) return;
        setSelection(value);
        const frame = host.current?.querySelector("iframe");
        const range = frame?.contentWindow?.getSelection();
        if (value && range?.rangeCount && frame && host.current) {
          const rect = range.getRangeAt(0).getBoundingClientRect();
          setActionPosition({
            left: Math.max(8, Math.min(rect.left, host.current.clientWidth - 140)),
            top: Math.max(8, Math.min(rect.bottom + 6, host.current.clientHeight - 42)),
          });
        }
      },
      error: report,
      notice: report,
      preparation: () => {},
    });
    session.current = current;
    current.setVisible(visibility.current);
    return () => {
      live = false;
      current.dispose();
      if (session.current === current) session.current = null;
    };
  }, [file]);
  useEffect(() => {
    if (sourceJump) session.current?.adapter.jump(sourceJump);
  }, [sourceJump]);
  const quote = async () => {
    if (!selection || !onSourceAction || busy) return;
    setBusy(true);
    setError("");
    try {
      await onSourceAction({ intent: "quote", operation_id: crypto.randomUUID(), file, selection });
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="pdf-reader" aria-label="PDF阅读器">
      <div ref={host} className="pdf-reader-host" />
      {selection && onSourceAction && (
        <div
          className="pdf-selection-actions"
          style={{
            position: "absolute",
            right: "auto",
            bottom: "auto",
            width: "max-content",
            zIndex: 5,
            ...actionPosition,
          }}
        >
          <Button
            disabled={busy}
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => void quote()}
          >
            添加到对话
          </Button>
        </div>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
