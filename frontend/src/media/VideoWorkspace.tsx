import { useEffect, useImperativeHandle, useRef, useState } from "react";
import type { FormEvent, Ref } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import { captureVideoFrame } from "./capture";
import type { CaptureSource } from "./capture";

type VideoAsset = Schema<"MediaAssetResponse">;
export type VideoSource = { asset_id: string; source_version: string; time_ms: number };
export type VideoWorkspaceHandle = {
  openAsset: (asset: VideoAsset, timeMs?: number) => Promise<void>;
  openSource: (source: VideoSource) => Promise<void>;
  resumeFullscreen: () => Promise<void>;
  savePosition: () => Promise<void>;
};

export function VideoWorkspace({
  courseId,
  connected,
  onLibraryChange,
  onCapture,
  onOpenChat,
  onImmersiveChange,
  ref,
}: {
  courseId: string;
  connected: boolean;
  onLibraryChange: (assets: VideoAsset[]) => void;
  onCapture: (source: CaptureSource) => void;
  onOpenChat: () => void;
  onImmersiveChange: (immersive: boolean) => void;
  ref?: Ref<VideoWorkspaceHandle>;
}) {
  const [assets, setAssets] = useState<VideoAsset[]>([]);
  const [asset, setAsset] = useState<VideoAsset | null>(null);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [playing, setPlaying] = useState(false);
  const [playBlocked, setPlayBlocked] = useState(false);
  const [positionStatus, setPositionStatus] = useState("");
  const video = useRef<HTMLVideoElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const current = useRef<VideoAsset | null>(null);
  const positionRevision = useRef(0);
  const openEpoch = useRef(0);
  const saveQueue = useRef<Promise<void>>(Promise.resolve());
  const initialPosition = useRef(0);
  const lastSaved = useRef(-1);
  const saveFailure = useRef<string | null>(null);
  const openingQuestion = useRef(false);
  const mounted = useRef(true);

  const refresh = async () => {
    const values = await api.read<VideoAsset[]>(`/courses/${courseId}/videos`);
    if (!mounted.current) return;
    setAssets(values);
    onLibraryChange(values);
  };

  // Each write uses the preceding accepted revision, even when pause and seek fire together.
  const savePosition = () => {
    const selected = current.current;
    const player = video.current;
    const completed = async () => {
      await saveQueue.current;
      if (saveFailure.current) throw new Error(saveFailure.current);
    };
    if (!selected || !player || player.readyState === 0) return completed();
    const position = Math.max(0, Math.round(player.currentTime * 1_000));
    if (position === lastSaved.current) return completed();
    lastSaved.current = position;
    saveQueue.current = saveQueue.current.then(async () => {
      try {
        const saved = await api.write<Schema<"MediaPositionResponse">>(
          `/videos/${selected.asset_id}/position`,
          {
            source_version: selected.source_version,
            position_ms: position,
            expected_revision: positionRevision.current,
          } satisfies Schema<"MediaPositionInput">,
          { keepalive: true },
        );
        positionRevision.current = saved.position_revision;
        saveFailure.current = null;
        if (mounted.current) setPositionStatus("播放位置已保存");
      } catch (reason) {
        lastSaved.current = -1;
        saveFailure.current = `播放位置未保存：${errorMessage(reason)}`;
        if (mounted.current) setPositionStatus(saveFailure.current);
      }
    });
    return completed();
  };

  const saveQuietly = () => void savePosition().catch(() => {});

  const play = async () => {
    try {
      await video.current?.play();
      setPlayBlocked(false);
    } catch {
      setPlayBlocked(true);
    }
  };

  const openAsset = async (selected: VideoAsset, timeMs?: number) => {
    const epoch = ++openEpoch.current;
    try {
      await savePosition();
      if (!mounted.current || epoch !== openEpoch.current) return;
      const opened = await api.write<VideoAsset>(`/videos/${selected.asset_id}/open`, {
        source_version: selected.source_version,
      } satisfies Schema<"MediaOpenInput">);
      if (!mounted.current || epoch !== openEpoch.current) return;
      setError("");
      setPlayBlocked(false);
      setPositionStatus("");
      initialPosition.current = timeMs ?? opened.position_ms;
      positionRevision.current = opened.position_revision;
      lastSaved.current = -1;
      current.current = opened;
      if (asset?.asset_id === opened.asset_id && asset.source_version === opened.source_version) {
        if (video.current) video.current.currentTime = initialPosition.current / 1_000;
        await play();
      }
      setAsset(opened);
    } catch (reason) {
      if (mounted.current && epoch === openEpoch.current) {
        video.current?.pause();
        setError(errorMessage(reason));
      }
    }
  };

  const openSource = async (source: VideoSource) => {
    const selected = assets.find((item) => item.asset_id === source.asset_id);
    if (!selected || selected.source_version !== source.source_version) {
      setError("该来源不在当前课程或来源版本已变化，已保留原题图（SOURCE_UNAVAILABLE）");
      return;
    }
    await openAsset(selected, source.time_ms);
  };

  const resumeFullscreen = async () => {
    openingQuestion.current = false;
    try {
      await stage.current?.requestFullscreen();
      onImmersiveChange(true);
    } catch {
      setError("浏览器未允许全屏，请点击视频全屏重试（FULLSCREEN_DENIED）");
    }
    await play();
  };

  useImperativeHandle(ref, () => ({ openAsset, openSource, resumeFullscreen, savePosition }));

  useEffect(() => {
    mounted.current = true;
    let disposed = false;
    void api
      .read<VideoAsset[]>(`/courses/${courseId}/videos`)
      .then((values) => {
        if (!disposed) {
          setAssets(values);
          onLibraryChange(values);
        }
      })
      .catch((reason) => {
        if (!disposed) setError(errorMessage(reason));
      });
    const timer = window.setInterval(saveQuietly, 5_000);
    const hidden = () => {
      if (document.visibilityState === "hidden") saveQuietly();
    };
    document.addEventListener("visibilitychange", hidden);
    const pagehide = saveQuietly;
    window.addEventListener("pagehide", pagehide);
    return () => {
      disposed = true;
      mounted.current = false;
      saveQuietly();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", hidden);
      window.removeEventListener("pagehide", pagehide);
    };
    // A course gets a fresh player; entering it never opens a previous resource.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId]);

  useEffect(() => {
    const change = () =>
      onImmersiveChange(Boolean(document.fullscreenElement) || openingQuestion.current);
    document.addEventListener("fullscreenchange", change);
    return () => document.removeEventListener("fullscreenchange", change);
  }, [onImmersiveChange]);

  const pick = async () => {
    setBusy(true);
    try {
      const value = await api.write<VideoAsset | null>(
        `/courses/${courseId}/videos/pick`,
        {},
        { timeoutMs: 180_000 },
      );
      if (value) {
        await refresh();
        await openAsset(value);
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const register = async (event: FormEvent) => {
    event.preventDefault();
    if (!path.trim()) return;
    setBusy(true);
    try {
      const value = await api.write<VideoAsset>(`/courses/${courseId}/videos`, {
        path: path.trim(),
      });
      setPath("");
      await refresh();
      await openAsset(value);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const relocate = async (selected: VideoAsset) => {
    setBusy(true);
    try {
      const value = await api.write<VideoAsset | null>(
        `/videos/${selected.asset_id}/relocate`,
        { source_version: selected.source_version },
        { timeoutMs: 180_000 },
      );
      if (value) {
        await refresh();
        await openAsset(value);
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const openQuestion = async () => {
    openingQuestion.current = Boolean(document.fullscreenElement);
    if (document.fullscreenElement) await document.exitFullscreen();
    onOpenChat();
  };

  const capture = async () => {
    if (!video.current || !asset) return;
    try {
      const source = captureVideoFrame(video.current, {
        asset_id: asset.asset_id,
        source_version: asset.source_version,
      });
      saveQuietly();
      await openQuestion();
      onCapture(source);
    } catch {
      setError("当前画面尚未就绪，请等待视频缓冲后截图（CAPTURE_NOT_READY）");
    }
  };

  return (
    <section className="video-workspace" aria-label="视频学习区">
      <div className="video-heading">
        <div>
          <span className="eyebrow">当前课程 · 视频</span>
          <h2>{asset?.display_name || "从一节课开始"}</h2>
        </div>
        <Button variant="outline" disabled={!connected || busy} onClick={() => void pick()}>
          选择本地视频
        </Button>
      </div>
      <details className="video-path-entry">
        <summary>用文件路径关联视频</summary>
        <form onSubmit={(event) => void register(event)}>
          <input
            aria-label="视频路径"
            placeholder="粘贴本机视频的完整路径"
            value={path}
            onChange={(event) => setPath(event.target.value)}
            disabled={!connected || busy}
          />
          <Button type="submit" variant="outline" disabled={!path.trim() || !connected || busy}>
            关联视频
          </Button>
        </form>
      </details>
      {error && (
        <p className="inline-error video-error" role="alert">
          {error}
        </p>
      )}
      <div className="video-stage" ref={stage}>
        {asset ? (
          <video
            ref={video}
            key={`${asset.asset_id}:${asset.source_version}`}
            aria-label="课程视频"
            controls
            preload="metadata"
            playsInline
            src={`/api/v1/videos/${asset.asset_id}/content?source_version=${encodeURIComponent(asset.source_version)}`}
            onLoadedMetadata={() => {
              if (!video.current) return;
              const target = initialPosition.current / 1_000;
              video.current.currentTime = Number.isFinite(video.current.duration)
                ? Math.min(target, Math.max(0, video.current.duration - 0.05))
                : target;
              void play();
            }}
            onPause={() => {
              setPlaying(false);
              saveQuietly();
            }}
            onPlay={() => setPlaying(true)}
            onSeeked={saveQuietly}
            onEnded={saveQuietly}
            onError={() =>
              setError("视频无法播放，请刷新目录检查原件或浏览器格式支持（MEDIA_PLAYBACK_FAILED）")
            }
          />
        ) : (
          <div className="video-empty">
            <span className="video-empty-symbol" aria-hidden="true">
              ▷
            </span>
            <h3>选择视频，接着看</h3>
            <p>
              原件保留在原位置。打开后恢复播放进度；只在你主动截图并确认后，选区才会加入提问草稿。
            </p>
          </div>
        )}
        <div className="video-actions">
          <Button
            variant="secondary"
            disabled={!asset}
            onClick={() => (playing ? video.current?.pause() : void play())}
          >
            {playing ? "暂停视频" : "播放视频"}
          </Button>
          <Button disabled={!asset} onClick={() => void capture()}>
            截图提问
          </Button>
          <Button variant="secondary" disabled={!asset} onClick={() => void resumeFullscreen()}>
            视频全屏
          </Button>
          <Button
            className="fullscreen-question"
            variant="secondary"
            onClick={() => void openQuestion()}
          >
            打开问答
          </Button>
        </div>
      </div>
      {playBlocked && (
        <p className="playback-notice" role="status">
          浏览器已阻止自动播放，点击“播放视频”继续。
        </p>
      )}
      <div className="video-footnote">
        <span>{positionStatus || "播放与问答可以同时进行"}</span>
        <span>截图会暂停播放</span>
      </div>
      <div className="resource-list-heading">
        <h3>课程视频</h3>
        <Button
          variant="ghost"
          disabled={!connected || busy}
          onClick={() => void refresh().catch((reason) => setError(errorMessage(reason)))}
        >
          刷新目录
        </Button>
      </div>
      {!assets.length && <p className="muted">还未关联视频，可选择原文件或粘贴路径。</p>}
      <ul className="video-resource-list">
        {assets.map((item) => (
          <li key={item.asset_id}>
            <button
              className={
                asset?.asset_id === item.asset_id ? "resource-link selected" : "resource-link"
              }
              onClick={() => void openAsset(item)}
            >
              <span aria-hidden="true">▷</span>
              <span>{item.display_name}</span>
              <small>
                {item.availability === "available"
                  ? "打开"
                  : item.availability === "missing"
                    ? "原件缺失"
                    : "内容已变化"}
              </small>
            </button>
            {item.availability !== "available" && (
              <Button variant="ghost" disabled={busy} onClick={() => void relocate(item)}>
                重新定位 {item.display_name}
              </Button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
