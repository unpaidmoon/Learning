import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import type { SubtitleActionHandler } from "../reading/actions";
import { SubtitleSession } from "./SubtitleSession";
import type { SubtitleState } from "./SubtitleSession";
import "./subtitles.css";

const time = (ms: number) =>
  `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`;
type PendingEdit = {
  segment: Schema<"SubtitleSegment">;
  head: string | null;
  text: string;
  operation?: Schema<"TranscriptEdit">;
  recovered?: boolean;
};

function SubtitleEditor({
  courseId,
  track,
  segment,
  onSaved,
  onClose,
}: {
  courseId: string;
  track: Schema<"TranscriptView">;
  segment: Schema<"SubtitleSegment">;
  onSaved: () => void;
  onClose: () => void;
}) {
  const key = `learning.subtitle.edit.${track.track_id}.${segment.segment_id}`;
  const [edit, setEdit] = useState<PendingEdit>(() => {
    try {
      const saved = localStorage.getItem(key);
      if (saved) {
        const value = JSON.parse(saved) as Partial<PendingEdit>;
        if (
          value?.segment?.segment_id === segment.segment_id &&
          typeof value.text === "string" &&
          (typeof value.head === "string" || value.head === null)
        )
          return value as PendingEdit;
        return { segment, head: track.edit_head_id, text: saved, recovered: true };
      }
    } catch {
      const saved = localStorage.getItem(key);
      if (saved) return { segment, head: track.edit_head_id, text: saved, recovered: true };
    }
    return { segment, head: track.edit_head_id, text: segment.text };
  });
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState<Schema<"TranscriptEditReceipt"> | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const store = (value: PendingEdit) => {
    localStorage.setItem(key, JSON.stringify(value));
    setEdit(value);
  };
  const accept = (receipt: Schema<"TranscriptEditReceipt">) => {
    onSaved();
    if (receipt.disposition === "conflict") {
      store({ ...edit, operation: undefined });
      setConflict(receipt);
      setNotice("另一份修订已保存；本稿也已保留为冲突版本，当前字幕未被覆盖。");
    } else {
      localStorage.removeItem(key);
      onClose();
    }
  };
  const continueConflict = async () => {
    setBusy(true);
    try {
      const current = await api.read<Schema<"TranscriptView">>(
        `/courses/${courseId}/learning/transcripts/${track.track_id}?limit=1`,
      );
      if (mounted.current) {
        store({ ...edit, head: current.edit_head_id, operation: undefined });
        setConflict(null);
        setNotice("本稿已以当前版本为基准，保存后更新这一条字幕。");
      }
    } catch (error) {
      if (mounted.current) setNotice(errorMessage(error));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  const save = async () => {
    if (busy || edit.operation) return;
    setBusy(true);
    setNotice("");
    const operation: Schema<"TranscriptEdit"> = {
      operation_id: crypto.randomUUID(),
      expected_edit_head_id: edit.head,
      changes: [
        {
          segment_id: edit.segment.segment_id,
          raw_revision_id: edit.segment.raw_revision_id,
          text: edit.text,
          start_ms: edit.segment.start_ms,
          end_ms: edit.segment.end_ms,
        },
      ],
    };
    try {
      // Persist reconciliation identity before the first write.
      store({ ...edit, operation });
      const receipt = await api.write<Schema<"TranscriptEditReceipt">>(
        `/courses/${courseId}/learning/transcripts/${track.track_id}/edit`,
        operation,
      );
      if (mounted.current) accept(receipt);
    } catch (error) {
      if (!mounted.current) return;
      const status = (error as { status?: number })?.status;
      if (status && status >= 400 && status < 500) store({ ...edit, operation: undefined });
      setNotice(errorMessage(error));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  const reconcile = async () => {
    if (!edit.operation || busy) return;
    setBusy(true);
    try {
      const receipt = await api.read<Schema<"TranscriptEditReceipt">>(
        `/courses/${courseId}/learning/transcripts/${track.track_id}/operations/${edit.operation.operation_id}`,
      );
      if (mounted.current) accept(receipt);
    } catch (error) {
      if (mounted.current) setNotice(`保存结果尚未确认，未重新提交。${errorMessage(error)}`);
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  return (
    <div className="subtitle-editor" role="group" aria-label="修改字幕">
      <p>原始转写：{edit.segment.raw_text}</p>
      {edit.recovered && (
        <p role="status">本地草稿格式异常，原始数据保留在编辑框中，请核对后保存。</p>
      )}
      <textarea
        aria-label="字幕修订正文"
        value={edit.text}
        disabled={busy || !!edit.operation}
        onChange={(event) => store({ ...edit, text: event.target.value })}
      />
      {notice && <p role="status">{notice}</p>}
      {conflict ? (
        <Button variant="outline" disabled={busy} onClick={() => void continueConflict()}>
          基于当前字幕继续编辑
        </Button>
      ) : edit.operation ? (
        <Button variant="outline" disabled={busy} onClick={() => void reconcile()}>
          核对字幕保存
        </Button>
      ) : (
        <Button variant="outline" disabled={busy} onClick={() => void save()}>
          保存字幕
        </Button>
      )}
      <Button variant="outline" onClick={onClose}>
        收起编辑
      </Button>
    </div>
  );
}

export function SubtitlePanel({
  courseId,
  asset,
  positionMs,
  active,
  onSeek,
  onAction,
}: {
  courseId: string;
  asset: Schema<"MediaAssetResponse">;
  positionMs: number;
  active: boolean;
  onSeek: (seconds: number) => void;
  onAction?: SubtitleActionHandler;
}) {
  const session = useRef<SubtitleSession | null>(null);
  const visibility = useRef(active),
    initial = useRef(asset.position_ms);
  const [state, setState] = useState<SubtitleState>({ preparation: null, transcript: null });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<{
    segment: Schema<"SubtitleSegment">;
    track: Schema<"TranscriptView">;
  } | null>(null);
  const mounted = useRef(true);
  useLayoutEffect(() => {
    visibility.current = active;
    session.current?.setVisible(active);
  }, [active]);
  useEffect(() => {
    mounted.current = true;
    const controller = new SubtitleSession(
      courseId,
      { ...asset, position_ms: initial.current },
      setState,
      (reason) => setError(reason instanceof Error ? reason.message : errorMessage(reason)),
    );
    session.current = controller;
    controller.setVisible(visibility.current);
    return () => {
      mounted.current = false;
      controller.dispose();
      if (session.current === controller) session.current = null;
    };
  }, [courseId, asset]);
  useEffect(() => {
    session.current?.setPosition(positionMs);
  }, [positionMs]);
  const control = async (action: "pause" | "resume" | "retry") => {
    setBusy(true);
    try {
      await session.current?.control(action);
    } catch (reason) {
      if (mounted.current) setError(errorMessage(reason));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  const quote = async (segment: Schema<"SubtitleSegment">) => {
    if (!onAction || !state.transcript || busy) return;
    setBusy(true);
    setError("");
    try {
      await onAction({
        course_id: courseId,
        track_id: state.transcript.track_id,
        capture: { segment_id: segment.segment_id, revision_id: segment.revision_id },
      });
    } catch (reason) {
      if (mounted.current) setError(errorMessage(reason));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  const batch = state.preparation?.batch;
  const transcript = state.transcript;
  return (
    <section className="subtitle-panel" aria-label="课程字幕">
      <div className="subtitle-heading">
        <strong>字幕</strong>
        {transcript?.units_total != null && (
          <span>
            {transcript.units_ready.length}/{transcript.units_total} 段已保存
          </span>
        )}
        {batch && ["queued", "running"].includes(batch.state) && (
          <Button variant="outline" disabled={busy} onClick={() => void control("pause")}>
            暂停转写
          </Button>
        )}
        {batch && ["paused", "cancelled"].includes(batch.state) && (
          <Button variant="outline" disabled={busy} onClick={() => void control("resume")}>
            恢复转写
          </Button>
        )}
        {batch && ["failed", "needs_source"].includes(batch.state) && (
          <Button variant="outline" disabled={busy} onClick={() => void control("retry")}>
            重试失败片段
          </Button>
        )}
      </div>
      {error && <p role="alert">{error}</p>}
      {!transcript?.segments.length && (
        <p className="muted">
          {transcript?.units_total != null &&
          (transcript.units_ready.includes(Math.floor(positionMs / 30000)) ||
            !transcript.missing_units.length ||
            positionMs < (transcript.audio_start_ms ?? 0) ||
            positionMs >= (transcript.audio_end_ms ?? Infinity))
            ? "该片段没有识别到语音"
            : batch?.state === "paused"
              ? "整份转写已暂停，当前片段可单独准备"
              : "正在准备当前片段的字幕…"}
        </p>
      )}
      <div className="subtitle-lines">
        {transcript?.segments.map((segment) => (
          <div
            key={segment.segment_id}
            className="subtitle-line"
            data-subtitle-segment={segment.segment_id}
            data-current={segment.start_ms <= positionMs && positionMs < segment.end_ms}
          >
            <button
              className="subtitle-time"
              onClick={() => onSeek(segment.start_ms / 1000)}
              aria-label={`重听 ${time(segment.start_ms)}`}
            >
              {time(segment.start_ms)}
            </button>
            <span>{segment.text}</span>
            {segment.origin === "user_note" && <small>已修订</small>}
            <div className="subtitle-actions">
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => setEditing({ segment, track: transcript })}
              >
                修改
              </Button>
              {onAction && (
                <Button
                  variant="outline"
                  disabled={busy || !segment.text.trim()}
                  onClick={() => void quote(segment)}
                >
                  加入 AI
                </Button>
              )}
            </div>
          </div>
        ))}
      </div>
      {editing && (
        <SubtitleEditor
          key={editing.segment.segment_id}
          courseId={courseId}
          track={editing.track}
          segment={editing.segment}
          onSaved={() => session.current?.refresh()}
          onClose={() => setEditing(null)}
        />
      )}
    </section>
  );
}
