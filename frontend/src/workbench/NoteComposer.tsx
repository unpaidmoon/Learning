import { useState } from "react";
import type { Schema } from "../api/client";
import { errorMessage } from "../api/client";
import { Button } from "../components/ui/button";
import { finishWorkbenchDraft, saveWorkbenchDraft } from "./drafts";
import { localDraft } from "./model";
import { useWorkbenchOperation } from "./useWorkbenchOperation";

type Draft = { title: string; text: string };

export function NoteComposer({
  courseId,
  onClose,
  onCreated,
}: {
  courseId: string;
  onClose: () => void;
  onCreated: (artifact: Schema<"ArtifactView">) => void;
}) {
  const key = courseId + ".note.new";
  const [saved] = useState(() => localDraft<Draft>(key));
  const [draft, setDraft] = useState<Draft>(saved.value ?? { title: "我的学习笔记", text: "" });
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
  const change = (value: Draft) => {
    setDraft(value);
    try {
      if (!saved.error) saveWorkbenchDraft(key, value);
    } catch (reason) {
      setNotice(errorMessage(reason));
    }
  };
  return (
    <section className="workbench-composer" aria-label="新建用户笔记">
      <h2>学习笔记</h2>
      <label>
        笔记标题
        <input
          value={draft.title}
          maxLength={200}
          disabled={operation.disabled}
          onChange={(event) => change({ ...draft, title: event.target.value })}
        />
      </label>
      <label>
        笔记正文
        <textarea
          value={draft.text}
          maxLength={160_000}
          disabled={operation.disabled}
          onChange={(event) => change({ ...draft, text: event.target.value })}
        />
      </label>
      <p>保存到当前课程，可编辑和检索。</p>
      <Button
        disabled={operation.disabled || !!saved.error || !draft.title.trim() || !draft.text.trim()}
        onClick={() =>
          void operation.execute(`/courses/${courseId}/learning/artifacts`, {
            kind: "note",
            title: draft.title,
            body: {
              blocks: [
                {
                  block_id: "note_1",
                  kind: "markdown",
                  markdown: draft.text,
                  attribution: "user_authored",
                  source_snapshot_ids: [],
                },
              ],
            },
          })
        }
      >
        保存笔记
      </Button>
      {operation.pending && (
        <Button disabled={operation.busy} onClick={() => void operation.reconcile()}>
          核对原保存
        </Button>
      )}
      <Button variant="ghost" onClick={onClose}>
        收起
      </Button>
      {(operation.notice || notice) && <p role="status">{operation.notice || notice}</p>}
    </section>
  );
}
