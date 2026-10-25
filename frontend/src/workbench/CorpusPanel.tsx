import { useCallback, useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { useWorkbenchOperation } from "./useWorkbenchOperation";
import { localDraft } from "./model";
import { finishWorkbenchDraft, saveWorkbenchDraft } from "./drafts";
import { ReadCourseScope } from "./ReadCourseScope";

const statusLabels: Record<string, string> = {
  pending: "等待索引",
  indexing: "正在索引",
  ready: "文字已就绪",
  needs_ocr: "等待识别文字",
  needs_source: "需要原版本资料",
  failed: "未完成",
  missing: "来源缺失",
  running: "正在扫描",
  completed: "本轮扫描完成",
};

export function CorpusPanel({
  courseId,
  onClose,
  sessionId,
}: {
  courseId: string;
  onClose: () => void;
  sessionId?: string;
}) {
  const draftKey = `learning:corpus-input:${courseId}`;
  const [saved] = useState(() => localDraft<{ paths: string }>(draftKey));
  const [status, setStatus] = useState<Schema<"CorpusStatus"> | null>(null);
  const [paths, setPaths] = useState(saved.value?.paths ?? "");
  const [error, setError] = useState(saved.error);
  const [editing, setEditing] = useState(!!saved.value);
  const [after, setAfter] = useState("");
  const refresh = useCallback(async () => {
    const result = await api.read<Schema<"CorpusStatus">>(
      `/courses/${courseId}/learning/corpus?after=${encodeURIComponent(after)}`,
    );
    setStatus(result);
    return result;
  }, [courseId, after]);
  const operation = useWorkbenchOperation<Schema<"CorpusPolicyReceipt">>(
    `learning:corpus-policy:${courseId}`,
    courseId,
    "artifact",
    (receipt) => {
      setPaths(receipt.exclusions.join("\n"));
      setEditing(false);
      finishWorkbenchDraft(draftKey);
      void refresh().catch((reason) => setError(errorMessage(reason)));
    },
  );
  useEffect(() => {
    let live = true;
    const read = async () => {
      try {
        const result = await api.read<Schema<"CorpusStatus">>(
          `/courses/${courseId}/learning/corpus?after=${encodeURIComponent(after)}`,
        );
        if (!live) return;
        setStatus(result);
        if (!editing) setPaths(result.exclusions.join("\n"));
      } catch (reason) {
        if (live) setError(errorMessage(reason));
      }
    };
    void read();
    const timer = setInterval(() => void read(), 4000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [courseId, editing, after]);
  return (
    <section className="workbench-editor" aria-label="课程资料索引">
      <div className="workbench-toolbar">
        <h3>课程资料索引</h3>
        <Button onClick={onClose}>关闭</Button>
      </div>
      <ReadCourseScope courseId={courseId} sessionId={sessionId} />
      <p>
        自动整理当前课程内的 PDF 和文本，回答需要课程依据时由 AI 按需查阅。字幕不进入课程资料索引。
      </p>
      {status && (
        <p>
          {statusLabels[status.scan_state] ?? status.scan_state}
          {status.scan_error && ` · ${status.scan_error}`}
        </p>
      )}
      <label>
        排除的文件或文件夹（每行一个相对路径）
        <textarea
          aria-label="索引排除项"
          rows={4}
          value={paths}
          disabled={operation.disabled}
          onChange={(event) => {
            setEditing(true);
            setPaths(event.target.value);
            try {
              saveWorkbenchDraft(draftKey, { paths: event.target.value });
            } catch (reason) {
              setError(errorMessage(reason));
            }
          }}
        />
      </label>
      <div className="workbench-toolbar">
        <Button
          disabled={!status || operation.disabled || !editing}
          onClick={() => {
            if (status)
              void operation.execute(`/courses/${courseId}/learning/corpus/policy`, {
                expected_revision: status.revision,
                exclusions: paths
                  .split(/\r?\n/)
                  .map((path) => path.trim())
                  .filter(Boolean),
              });
          }}
        >
          保存排除项
        </Button>
        {operation.pending && (
          <Button disabled={operation.busy} onClick={() => void operation.reconcile()}>
            核对原保存
          </Button>
        )}
      </div>
      {(error || operation.notice) && <p role="alert">{error || operation.notice}</p>}
      <ul>
        {status?.documents.map((doc) => (
          <li key={doc.document_id}>
            <strong>{doc.relative_path}</strong> · {statusLabels[doc.state] ?? doc.state}
            {doc.total != null && `（${doc.cursor}/${doc.total}）`}
            {doc.error_code && ` · ${doc.error_code}`}
          </li>
        ))}
      </ul>
      {status?.has_more && (
        <Button
          onClick={() => {
            const last = status.documents.at(-1)?.relative_path;
            if (!last) return;
            setAfter(last);
          }}
        >
          下一页资料
        </Button>
      )}
      {after && <Button onClick={() => setAfter("")}>返回首批资料</Button>}
    </section>
  );
}
