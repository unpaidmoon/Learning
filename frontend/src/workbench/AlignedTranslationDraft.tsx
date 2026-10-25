import { useEffect, useMemo, useState } from "react";
import { createNdjsonParser } from "../../../third_party/deepstudent/upstream/src/features/chat/components/translationNdjsonParser";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { TranslationPair } from "./TranslationParagraph";

export function AlignedTranslationDraft({
  courseId,
  taskId,
  runId,
  onClose,
}: {
  courseId: string;
  taskId: string;
  runId: string;
  onClose: () => void;
}) {
  const [run, setRun] = useState<Schema<"TaskRunView"> | null>(null);
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const read = async () => {
      try {
        const current = await api.read<Schema<"TaskRunView">>(
          `/courses/${courseId}/learning/tasks/${taskId}/runs/${runId}`,
        );
        if (!live) return;
        setRun(current);
        setNotice("");
        if (!["completed", "failed", "cancelled", "truncated"].includes(current.state))
          timer = setTimeout(() => void read(), 1500);
      } catch (reason) {
        if (live) setNotice(errorMessage(reason));
      }
    };
    void read();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [courseId, taskId, runId, refresh]);
  const preview = useMemo(() => {
    let raw = (run?.body ?? "").slice(0, 200_000);
    if (/[\uD800-\uDBFF]$/.test(raw)) raw = raw.slice(0, -1);
    return createNdjsonParser().push(raw).segments;
  }, [run?.body]);
  return (
    <aside aria-label="对照译文草稿" className="translation-paragraph">
      <h3>已收到的对照草稿</h3>
      <p>以下片段来自模型原输出，尚未证明完整覆盖。完整结果请通过“阅读成果”打开。</p>
      {preview.map((part, index) => (
        <TranslationPair key={index} sourceText={part.src} translation={part.tgt} unverified />
      ))}
      {run && !preview.length && <p>尚未收到可显示的完整片段。</p>}
      {(run?.body.length ?? 0) > 200_000 && <p>预览已达长度上限，完整原输出仍保存在任务中。</p>}
      {notice && <p role="status">{notice}</p>}
      <Button variant="ghost" onClick={() => setRefresh((old) => old + 1)}>
        重新读取原输出
      </Button>
      <Button variant="ghost" onClick={onClose}>
        收起草稿
      </Button>
    </aside>
  );
}
