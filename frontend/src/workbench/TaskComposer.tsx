import { useEffect, useState } from "react";
import { api, ApiError, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { finishWorkbenchDraft, saveWorkbenchDraft } from "./drafts";
import { localDraft, operationId } from "./model";
import type { ComposerDraft, Intent, Task } from "./model";
import { TaskSourcePicker } from "./TaskSourcePicker";
import { TaskCourseSelect } from "./TaskCourseSelect";

const defaults: Record<ComposerDraft["kind"], { title: string; instruction: string }> = {
  aligned_translation: {
    title: "英文段落对照",
    instruction: "把明确指定的文字逐段翻译为中文，保留原文和段落对应，不补选区前后文。",
  },
  note: {
    title: "学习笔记",
    instruction: "依据指定内容整理可独立阅读的笔记，保留来源并区分资料和补充。",
  },
  handout: {
    title: "学习讲义",
    instruction: "完整整理指定范围，说明重点及其联系，区分资料内容与补充解释。",
  },
  deep_solution: {
    title: "深入解答",
    instruction: "依据指定条件深入求解，给出完整推导，并核对适用条件与另一种思路。",
  },
  question_set: {
    title: "练习题集",
    instruction: "依据指定资料编写练习，提供参考答案与解析，保留来源和条件。",
  },
  card_set: {
    title: "复习卡片",
    instruction: "按指定资料制作可独立回忆的复习卡片，正反面分开，保留来源。",
  },
  learning_path: {
    title: "目标学习路线",
    instruction:
      "依据指定资料和学习目标，建议可自由调整和跳过的学习路线，每个知识点配回忆问题与讲解。",
  },
  visualization: {
    title: "概念互动图",
    instruction: "依据指定资料，用合适的图形、参数互动或动画解释概念，说明变量、范围与适用条件。",
  },
  book: {
    title: "交互学习书",
    instruction: "将指定材料组织成连贯的交互学习书，保留来源、例子与练习，区分资料和AI补充。",
  },
  research: {
    title: "资料对照报告",
    instruction: "比较指定资料的共同点、差异、适用条件与局限，逐项保留依据，明确尚缺的证据。",
  },
};

export function TaskComposer({
  courseId,
  sessionId,
  file,
  media,
  onClose,
  onCreated,
}: {
  courseId: string;
  sessionId: string;
  file?: Schema<"CourseFileOpenResponse">;
  media?: Schema<"MediaAssetResponse">;
  onClose: () => void;
  onCreated: (task: Task) => void;
}) {
  const key = courseId + ".compose";
  const [saved] = useState(() => localDraft<ComposerDraft>(key));
  const [sourceFile] = useState(saved.value?.file ?? file);
  const [sourceMedia] = useState(saved.value?.media ?? media);
  file = sourceFile;
  media = sourceMedia;
  const document = file && ["pdf", "text"].includes(file.viewer);
  const [scope, setScope] = useState(
    saved.value?.scope ?? (media ? "transcript" : document ? "document" : "course"),
  );
  const [kind, setKind] = useState<ComposerDraft["kind"]>(saved.value?.kind ?? "handout");
  const [title, setTitle] = useState(saved.value?.title ?? defaults[kind].title);
  const [instruction, setInstruction] = useState(
    saved.value?.instruction ?? defaults[kind].instruction,
  );
  const [sourcesOnly, setSourcesOnly] = useState(saved.value?.sourcesOnly ?? false);
  const [outlineText, setOutlineText] = useState(saved.value?.outlineText ?? "");
  const [includeGenerated, setIncludeGenerated] = useState(saved.value?.includeGenerated ?? false);
  const [sourceCourseId, setSourceCourseId] = useState(saved.value?.sourceCourseId ?? courseId);
  const [pickedSources, setPickedSources] = useState(saved.value?.pickedSources ?? []);
  const [researchTopics, setResearchTopics] = useState(saved.value?.researchTopics ?? "");
  const [researchUrls, setResearchUrls] = useState(saved.value?.researchUrls ?? "");
  const [allowExternal, setAllowExternal] = useState(saved.value?.allowExternal ?? false);
  const [pending, setPending] = useState<string | null>(saved.value?.pending ?? null);
  const [busy, setBusy] = useState(false);
  const [sourcePicking, setSourcePicking] = useState(false);
  const [notice, setNotice] = useState(saved.error);
  const persist = (operation: string | null) => {
    if (saved.error)
      throw new ApiError("WORKBENCH_DRAFT_INVALID", "原本地记录保留，未发起新的操作");
    saveWorkbenchDraft(key, {
      file,
      media,
      scope,
      kind,
      title,
      instruction,
      sourcesOnly,
      outlineText,
      includeGenerated,
      sourceCourseId,
      pickedSources,
      researchTopics,
      researchUrls,
      allowExternal,
      pending: operation,
    });
  };
  useEffect(() => {
    if (saved.error) return;
    try {
      saveWorkbenchDraft(key, {
        file: sourceFile,
        media: sourceMedia,
        scope,
        kind,
        title,
        instruction,
        sourcesOnly,
        outlineText,
        includeGenerated,
        sourceCourseId,
        pickedSources,
        researchTopics,
        researchUrls,
        allowExternal,
        pending,
      });
    } catch (reason) {
      queueMicrotask(() => setNotice(errorMessage(reason)));
    }
  }, [
    key,
    sourceFile,
    sourceMedia,
    scope,
    kind,
    title,
    instruction,
    sourcesOnly,
    outlineText,
    includeGenerated,
    sourceCourseId,
    pickedSources,
    researchTopics,
    researchUrls,
    allowExternal,
    pending,
    saved.error,
  ]);
  const submit = async () => {
    if (busy || pending) return;
    if (
      kind === "aligned_translation" &&
      ((scope !== "document" && scope !== "custom") ||
        (scope === "custom" &&
          pickedSources.some(
            (item) => !["selection", "document", "artifact"].includes(item.declaration.kind),
          )))
    ) {
      setNotice("对照翻译请明确选择文档文字或已有内容，不使用整课、音视频或外部范围。");
      return;
    }
    const operation = operationId();
    const payload: Intent = {
      operation_id: operation,
      kind,
      instruction,
      coverage_mode: "complete",
      sources:
        scope === "external"
          ? []
          : scope === "custom"
            ? pickedSources.map((item) => item.declaration)
            : scope === "document" && file
              ? [
                  {
                    kind: "document",
                    ...(file.course_id !== courseId ? { source_course_id: file.course_id } : {}),
                    asset_id: file.asset_id,
                    source_version: file.source_version,
                    first_page: 0,
                    text_only: kind === "aligned_translation",
                  },
                ]
              : scope === "transcript" && media
                ? [
                    {
                      kind: "transcript",
                      asset_id: media.asset_id,
                      source_version: media.source_version,
                      start_ms: 0,
                    },
                  ]
                : [
                    {
                      kind: "course",
                      include_generated: kind === "book" && includeGenerated,
                      ...(sourceCourseId !== courseId ? { source_course_id: sourceCourseId } : {}),
                    },
                  ],
      target: { mode: "create", title },
      sources_only: kind === "aligned_translation" || sourcesOnly,
      allow_external: kind === "research" && allowExternal && !sourcesOnly,
      ...(kind === "research"
        ? {
            research: {
              topics: researchTopics
                .split(/\r?\n/)
                .map((line) => line.trim())
                .filter(Boolean),
              urls:
                allowExternal && !sourcesOnly
                  ? researchUrls
                      .split(/\r?\n/)
                      .map((line) => line.trim())
                      .filter(Boolean)
                  : [],
            },
          }
        : {}),
      outline_supplied: kind === "book" && !!outlineText.trim(),
      ...(kind === "book" && outlineText.trim()
        ? {
            outline: outlineText
              .split(/\r?\n/)
              .map((line) => line.trim())
              .filter(Boolean)
              .map((title, i) => ({ chapter_id: `chapter_${i + 1}`, title, objective: "" })),
          }
        : {}),
    };
    if (kind === "aligned_translation")
      payload.sources = (payload.sources ?? []).map((source) =>
        source.kind === "document" ? { ...source, text_only: true } : source,
      );
    try {
      persist(operation);
    } catch (reason) {
      setNotice(errorMessage(reason));
      return;
    }
    setBusy(true);
    setPending(operation);
    setNotice("");
    try {
      const task = await api.write<Task>(
        `/desktop/sessions/${encodeURIComponent(sessionId)}/learning-task-intents`,
        payload,
      );
      if (task.course_id !== courseId) throw new Error("TASK_COURSE_MISMATCH");
      finishWorkbenchDraft(key);
      setPending(null);
      onCreated(task);
    } catch (reason) {
      if (
        reason instanceof ApiError &&
        reason.status &&
        reason.status >= 400 &&
        reason.status < 500
      ) {
        persist(null);
        setPending(null);
      }
      setNotice(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  const reconcile = async () => {
    if (!pending || busy) return;
    setBusy(true);
    try {
      const receipt = await api.read<Schema<"LearningOperationView">>(
        `/courses/${courseId}/learning/operations/${pending}`,
      );
      if (receipt.result && "task_id" in receipt.result) {
        finishWorkbenchDraft(key);
        setPending(null);
        onCreated(receipt.result);
      }
    } catch {
      setNotice("尚未取得原操作回执，草稿保留，请继续核对。不会自动重新提交。");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="workbench-composer" aria-label="发起学习任务">
      <label>
        成果类型
        <select
          disabled={busy || !!pending}
          value={kind}
          onChange={(event) => {
            const next = event.target.value as typeof kind;
            if (title === defaults[kind].title) setTitle(defaults[next].title);
            if (instruction === defaults[kind].instruction)
              setInstruction(defaults[next].instruction);
            setKind(next);
            if (next === "aligned_translation" && !["document", "custom"].includes(scope))
              setScope(document ? "document" : "custom");
          }}
        >
          <option value="note">学习笔记</option>
          <option value="aligned_translation">段落／全文文字对照</option>
          <option value="handout">讲义整理</option>
          <option value="deep_solution">深入解答</option>
          <option value="question_set">题集与练习</option>
          <option value="card_set">复习卡片 · 默认入队</option>
          <option value="learning_path">按目标建议学习路线</option>
          <option value="visualization">图形、参数互动与动画</option>
          <option value="book">交互学习书</option>
          <option value="research">多资料比较与专题研究</option>
        </select>
      </label>
      <label>
        资料范围
        <select
          disabled={busy || !!pending}
          value={scope}
          onChange={(event) => setScope(event.target.value)}
        >
          {document && <option value="document">当前整份资料 · {file?.entry.name}</option>}
          {media && (
            <option value="transcript" disabled={kind === "aligned_translation"}>
              当前音视频的完整字幕 · {media.display_name}
            </option>
          )}
          <option value="course" disabled={kind === "aligned_translation"}>
            {sourceCourseId === courseId
              ? "本课程全部原始资料及字幕"
              : "所选课程全部原始资料及字幕"}
          </option>
          <option value="custom">选择若干资料、字幕或已有成果</option>
          {kind === "research" && allowExternal && !sourcesOnly && (
            <option value="external">仅外部公开资料</option>
          )}
        </select>
      </label>
      {scope === "course" && (
        <TaskCourseSelect
          ownerCourseId={courseId}
          value={sourceCourseId}
          disabled={busy || !!pending}
          onChange={(id) => setSourceCourseId(id)}
        />
      )}
      {scope === "custom" && (
        <TaskSourcePicker
          key={courseId}
          courseId={courseId}
          value={pickedSources}
          disabled={busy || !!pending}
          onChange={setPickedSources}
          onBusyChange={setSourcePicking}
        />
      )}
      <label>
        标题
        <input
          disabled={busy || !!pending}
          value={title}
          maxLength={200}
          onChange={(event) => setTitle(event.target.value)}
        />
      </label>
      {kind === "book" && (
        <>
          <label>
            已有目录（可选，每行一章）
            <textarea
              disabled={busy || !!pending}
              value={outlineText}
              onChange={(e) => setOutlineText(e.target.value)}
              placeholder="留空时先拟目录，确认一次后连续生成"
            />
          </label>
          {scope === "course" && (
            <label>
              <input
                type="checkbox"
                disabled={busy || !!pending}
                checked={includeGenerated}
                onChange={(e) => setIncludeGenerated(e.target.checked)}
              />
              加入所选课程已保存的学习成果，保留其实际身份
            </label>
          )}
        </>
      )}
      <label>
        具体要求
        <textarea
          disabled={busy || !!pending}
          value={instruction}
          onChange={(event) => setInstruction(event.target.value)}
        />
      </label>
      {kind === "research" && (
        <>
          <label>
            研究子主题（可选，每行一项）
            <textarea
              disabled={busy || !!pending}
              value={researchTopics}
              onChange={(e) => setResearchTopics(e.target.value)}
              placeholder="留空时按具体要求直接研究，不增加规划步骤"
            />
          </label>
          <label>
            <input
              type="checkbox"
              disabled={busy || !!pending || sourcesOnly}
              checked={allowExternal && !sourcesOnly}
              onChange={(e) => setAllowExternal(e.target.checked)}
            />
            允许为本次研究检索和读取外部公开文字资料
          </label>
          {allowExternal && !sourcesOnly && (
            <label>
              指定外部网页（可选，每行一个网址）
              <textarea
                disabled={busy || !!pending}
                value={researchUrls}
                onChange={(e) => setResearchUrls(e.target.value)}
                placeholder="填入时仅逐一读取这些网址；留空时允许按研究主题检索"
              />
            </label>
          )}
        </>
      )}
      <label>
        <input
          type="checkbox"
          disabled={busy || !!pending || kind === "aligned_translation"}
          checked={sourcesOnly || kind === "aligned_translation"}
          onChange={(event) => {
            setSourcesOnly(event.target.checked);
            if (event.target.checked) setAllowExternal(false);
          }}
        />
        只依据指定资料
      </label>
      <p>沿用原生输入框当前选择的模型与思考强度。范围将在提交时冻结，资料缺口会暂停并保留草稿。</p>
      {kind === "aligned_translation" && (
        <p>
          逐段对照只处理指定文字；整份文档只取可读文字，不向模型发送页面图像。无法取得文字时保留缺口。
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      {pending ? (
        <Button disabled={busy} onClick={() => void reconcile()}>
          核对原操作
        </Button>
      ) : (
        <Button
          disabled={
            busy ||
            sourcePicking ||
            !title.trim() ||
            !instruction.trim() ||
            (scope === "custom" && !pickedSources.length) ||
            (scope === "external" && (kind !== "research" || !allowExternal || sourcesOnly))
          }
          onClick={() => void submit()}
        >
          开始生成并保存
        </Button>
      )}
      <Button variant="ghost" disabled={busy || !!pending} onClick={onClose}>
        关闭
      </Button>
    </section>
  );
}
