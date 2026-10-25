import { useState } from "react";
import { errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { finishWorkbenchDraft, saveWorkbenchDraft } from "./drafts";
import { localDraft } from "./model";
import { useWorkbenchOperation } from "./useWorkbenchOperation";

type Draft = { title: string; text: string; topic: string; rubric: string; maximum: number };

export function EssayComposer({
  courseId,
  onClose,
  onCreated,
}: {
  courseId: string;
  onClose: () => void;
  onCreated: (artifact: Schema<"ArtifactView">) => void;
}) {
  const key = courseId + ".essay.new";
  const [saved] = useState(() => localDraft<Draft>(key));
  const [draft, setDraft] = useState<Draft>(
    saved.value ?? {
      title: "我的作文",
      text: "",
      topic: "",
      rubric: "按题目要求、内容、结构和语言给学习参考评价。",
      maximum: 100,
    },
  );
  const [notice, setNotice] = useState(saved.error);
  const operation = useWorkbenchOperation<Schema<"ArtifactReceipt">>(
    key + ".create",
    courseId,
    "artifact",
    (receipt) => {
      finishWorkbenchDraft(key);
      onCreated(receipt.artifact);
    },
  );
  const change = (next: Draft) => {
    setDraft(next);
    try {
      if (!saved.error) saveWorkbenchDraft(key, next);
    } catch (error) {
      setNotice(errorMessage(error));
    }
  };
  return (
    <section className="workbench-composer" aria-label="保存作文原稿">
      <h2>作文原稿</h2>
      <label>
        标题
        <input
          maxLength={200}
          disabled={operation.disabled}
          value={draft.title}
          onChange={(event) => change({ ...draft, title: event.target.value })}
        />
      </label>
      <label>
        题目与要求
        <textarea
          disabled={operation.disabled}
          value={draft.topic}
          onChange={(event) => change({ ...draft, topic: event.target.value })}
        />
      </label>
      <label>
        实际作文
        <textarea
          aria-label="实际作文"
          disabled={operation.disabled}
          value={draft.text}
          onChange={(event) => change({ ...draft, text: event.target.value })}
        />
      </label>
      <label>
        评分标准
        <textarea
          disabled={operation.disabled}
          value={draft.rubric}
          onChange={(event) => change({ ...draft, rubric: event.target.value })}
        />
      </label>
      <label>
        参考满分
        <input
          type="number"
          min={1}
          max={10000}
          disabled={operation.disabled}
          value={draft.maximum}
          onChange={(event) => change({ ...draft, maximum: Number(event.target.value) })}
        />
      </label>
      <p>原稿单独保存。批改是AI参考评价，可保留修订稿并比较不同轮次。</p>
      {Array.from(draft.text).length > 50000 && (
        <p role="status">原稿超过50000字，输入仍保留，请先调整提交范围。</p>
      )}
      <Button
        disabled={
          operation.disabled ||
          !!saved.error ||
          !draft.title.trim() ||
          !draft.text.trim() ||
          Array.from(draft.text).length > 50000 ||
          !Number.isFinite(draft.maximum) ||
          draft.maximum <= 0 ||
          draft.maximum > 10000
        }
        onClick={() =>
          void operation.execute(`/courses/${courseId}/learning/artifacts`, {
            kind: "essay",
            title: draft.title,
            body: {
              blocks: [
                {
                  block_id: "draft",
                  kind: "essay_draft",
                  markdown: draft.text,
                  attribution: "user_authored",
                  source_snapshot_ids: [],
                  essay: {
                    topic: draft.topic,
                    rubric: {
                      max_total: draft.maximum,
                      aggregation: "independent",
                      dimensions: [],
                      description: draft.rubric,
                    },
                  },
                },
              ],
            },
          })
        }
      >
        保存原稿
      </Button>
      {operation.pending && (
        <Button disabled={operation.busy} onClick={() => void operation.reconcile()}>
          核对原稿保存
        </Button>
      )}
      <Button variant="ghost" disabled={operation.disabled} onClick={onClose}>
        关闭
      </Button>
      {(notice || operation.notice) && <p role="status">{notice || operation.notice}</p>}
    </section>
  );
}
