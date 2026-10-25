import { useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { roles } from "./model";

export function EvidencePanel({
  courseId,
  sessionId,
  onOpenSource,
  onClose,
}: {
  courseId: string;
  sessionId: string;
  onClose: () => void;
  onOpenSource?: (source: Schema<"SourceSnapshot">) => Promise<void>;
}) {
  const [items, setItems] = useState<Schema<"EvidenceCard">[]>([]);
  const [error, setError] = useState("");
  const [after, setAfter] = useState("");
  const [cursor, setCursor] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const read = async () => {
      try {
        const page = await api.read<Schema<"ChatEvidence">>(
          `/desktop/sessions/${encodeURIComponent(sessionId)}/learning-evidence?after=${encodeURIComponent(after)}`,
        );
        if (!live || page.course_id !== courseId) return;
        setItems(page.items);
        setCursor(page.next_cursor ?? null);
      } catch (reason) {
        if (live) setError(errorMessage(reason));
      }
    };
    void read();
    const timer = setInterval(() => void read(), 3000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [courseId, sessionId, after]);
  return (
    <section className="workbench-editor" aria-label="问答资料来源">
      <div className="workbench-toolbar">
        <h3>问答资料来源</h3>
        <Button onClick={onClose}>关闭</Button>
      </div>
      <p>最近20次问答中提交或由工具提供的资料。检索候选也会保留在这里。</p>
      {error && <p role="alert">{error}</p>}
      {items.map((item) => (
        <article key={`${item.generation_id}:${item.source.snapshot_id}`}>
          <strong>{item.label}</strong> · {roles[item.source.content_role]}
          <p>{item.question}</p>
          <blockquote>{item.source.text}</blockquote>
          <details>
            <summary>来源位置与版本</summary>
            <pre>{JSON.stringify(item.source.reference, null, 2)}</pre>
          </details>
          {onOpenSource && (
            <Button
              onClick={() =>
                void onOpenSource(item.source).catch((reason) => setError(errorMessage(reason)))
              }
            >
              打开原文
            </Button>
          )}
        </article>
      ))}
      {!items.length && <p>当前对话还没有已记录的资料来源。</p>}
      {cursor && <Button onClick={() => setAfter(cursor)}>下一批来源</Button>}
      {after && <Button onClick={() => setAfter("")}>返回首批资料</Button>}
    </section>
  );
}
