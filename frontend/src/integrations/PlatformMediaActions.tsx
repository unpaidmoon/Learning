import { useEffect, useState } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";

type Job = Schema<"PreparationView">;
export function PlatformMediaActions({
  courseId,
  asset,
  onReload,
}: {
  courseId: string;
  asset: Schema<"MediaAssetResponse">;
  onReload: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const prefix = `/courses/${courseId}/learning/preparations`;
  useEffect(() => {
    let live = true;
    void api
      .read<Job[]>(prefix)
      .then((rows) => {
        if (live)
          setJob(
            rows.find(
              (value) =>
                value.asset_id === asset.asset_id &&
                value.source_version === asset.source_version &&
                value.kind === "platform_download",
            ) || null,
          );
      })
      .catch((reason) => {
        if (live) setNotice(errorMessage(reason));
      });
    return () => {
      live = false;
    };
  }, [courseId, asset.asset_id, asset.source_version]);
  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.state)) return;
    let live = true;
    const timer = window.setTimeout(() => {
      void api
        .read<Job>(`${prefix}/${job.job_id}`)
        .then((value) => {
          if (live) setJob(value);
        })
        .catch((reason) => {
          if (live) setNotice(errorMessage(reason));
        });
    }, 1000);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [job]);
  const run = async (action: "download" | "pause" | "resume" | "retry") => {
    setBusy(true);
    setNotice("");
    try {
      const result =
        action === "download"
          ? await api.write<Job>(`/courses/${courseId}/integrations/${asset.asset_id}/download`, {
              operation_id: crypto.randomUUID(),
              source_version: asset.source_version,
            })
          : await api.write<Job>(`${prefix}/${job!.job_id}/${action}`, {
              operation_id: crypto.randomUUID(),
              expected_revision: job!.revision,
            });
      setJob(result);
    } catch (reason) {
      setNotice(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };
  const active = !!job && ["queued", "running"].includes(job.state);
  return (
    <div className="integration-media-actions">
      <span className="integration-platform-badge">
        B站来源 · {asset.size_bytes ? "本地副本" : "在线播放"}
      </span>
      {!asset.size_bytes && !job && (
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void run("download")}>
          保存本地
        </Button>
      )}
      {job && (
        <span role="status">
          {
            {
              queued: "等待保存",
              running: "正在保存",
              completed: "已保存本地",
              paused: "保存已暂停",
              failed: "保存未完成",
              cancelled: "保存已取消",
              needs_source: "需要恢复来源",
            }[job.state]
          }
        </span>
      )}
      {active && (
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => void run("pause")}>
          暂停保存
        </Button>
      )}
      {job?.state === "paused" && (
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => void run("resume")}>
          继续保存
        </Button>
      )}
      {job?.state === "failed" && (
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => void run("retry")}>
          重试保存
        </Button>
      )}
      <Button size="sm" variant="ghost" disabled={busy} onClick={onReload}>
        重新打开
      </Button>
      {notice && <p role="alert">{notice}</p>}
    </div>
  );
}
