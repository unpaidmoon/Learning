import { useEffect, useImperativeHandle, useRef, useState } from "react";
import type { FormEvent, Ref, RefObject, CSSProperties, ComponentType } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import type { CaptureSource } from "./capture";
import { usePlaybackControls } from "./usePlaybackControls";
import { VideoControls } from "./VideoControls";
import { SubtitlePanel } from "./SubtitlePanel";
import { SplitAudio } from "../integrations/SplitAudio";
import { PlatformMediaActions } from "../integrations/PlatformMediaActions";
import { videoContentUrl } from "../integrations/playbackUrl";
import type { SubtitleActionHandler } from "../reading/actions";
import "./VideoWorkspace.css";

type VideoAsset = Schema<"MediaAssetResponse">;
export type VideoSource = { asset_id: string; source_version: string; time_ms: number };
export type VideoWorkspaceHandle = {
  openAsset: (asset: VideoAsset, timeMs?: number) => Promise<void>;
  openSource: (source: VideoSource) => Promise<void>;
  resumeFullscreen: () => Promise<void>;
  toggleFullscreen: () => Promise<void>;
  savePosition: () => Promise<void>;
  pauseForCapture: () => VideoSource | undefined;
  getElement: () => HTMLVideoElement | null;
  closeAsset: () => Promise<void>;
};

export function VideoWorkspace({
  courseId,
  connected,
  onLibraryChange,
  onOpenChat,
  onImmersiveChange,
  onAssetChange,
  onSubtitleAction,
  workspaceRef,
  active = true,
  minimal = false,
  showSubtitles = false,
  assetId,
  openedAsset,
  RateControl,
  ref,
}: {
  courseId: string;
  connected: boolean;
  onLibraryChange: (assets: VideoAsset[]) => void;
  onCapture?: (source: CaptureSource) => void;
  onOpenChat: () => void;
  onImmersiveChange: (immersive: boolean) => void;
  onAssetChange?: (asset: VideoAsset | null) => void;
  onSubtitleAction?: SubtitleActionHandler;
  workspaceRef?: RefObject<HTMLElement | null>;
  active?: boolean;
  minimal?: boolean;
  showSubtitles?: boolean;
  assetId?: string;
  openedAsset?: VideoAsset;
  RateControl?: ComponentType<{ rate: number; onRate: (rate: number) => void }>;
  ref?: Ref<VideoWorkspaceHandle>;
}) {
  const [assets, setAssets] = useState<VideoAsset[]>([]);
  const [asset, setAsset] = useState<VideoAsset | null>(null);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [playing, setPlaying] = useState(false);
  const [pictureRatio, setPictureRatio] = useState(16 / 9);
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
  const lastKnownPosition = useRef(0);
  const saveFailure = useRef<string | null>(null);
  const mounted = useRef(true);
  const fullscreenExitPending = useRef(false);
  const activeRef = useRef(active);
  const resumeOnActive = useRef(false);
  const pendingPlay = useRef(true);
  const sessions = useRef(new Map<string, { playing: boolean; position: number }>());
  const callbacks = useRef({ onLibraryChange, onImmersiveChange, onAssetChange });

  useEffect(() => {
    callbacks.current = { onLibraryChange, onImmersiveChange, onAssetChange };
  }, [onLibraryChange, onImmersiveChange, onAssetChange]);

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
    lastKnownPosition.current = position;
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
    if (!activeRef.current) return;
    try {
      await video.current?.play();
      setPlayBlocked(false);
    } catch {
      setPlayBlocked(true);
    }
  };

  const openAsset = async (
    selected: VideoAsset,
    timeMs?: number,
    force = false,
    alreadyOpened = false,
  ) => {
    if (
      current.current?.asset_id === selected.asset_id &&
      current.current.source_version === selected.source_version &&
      current.current.playback_session_id === selected.playback_session_id &&
      timeMs === undefined &&
      !force
    )
      return;
    const epoch = ++openEpoch.current;
    const previous = current.current;
    const player = video.current;
    if (previous && player) {
      sessions.current.set(`${previous.asset_id}:${previous.source_version}`, {
        playing: resumeOnActive.current || !player.paused,
        position:
          player.readyState > 0 ? Math.round(player.currentTime * 1000) : lastKnownPosition.current,
      });
      player.pause();
    }
    try {
      await savePosition();
      if (!mounted.current || epoch !== openEpoch.current) return;
      // files/open already validates and opens this exact source. Adopt its
      // response for the first render instead of repeating the same disk scan.
      const opened = alreadyOpened
        ? selected
        : await api.write<VideoAsset>(`/videos/${selected.asset_id}/open`, {
            source_version: selected.source_version,
            ...(force ? { refresh: true } : {}),
          } satisfies Schema<"MediaOpenInput">);
      if (!mounted.current || epoch !== openEpoch.current) return;
      setError("");
      setPlayBlocked(false);
      setPositionStatus("");
      const session = sessions.current.get(`${opened.asset_id}:${opened.source_version}`);
      resumeOnActive.current = false;
      initialPosition.current = timeMs ?? session?.position ?? opened.position_ms;
      lastKnownPosition.current = initialPosition.current;
      pendingPlay.current = timeMs !== undefined || (session?.playing ?? true);
      positionRevision.current = opened.position_revision;
      lastSaved.current = -1;
      current.current = opened;
      callbacks.current.onAssetChange?.(opened);
      if (
        asset?.asset_id === opened.asset_id &&
        asset.source_version === opened.source_version &&
        asset.playback_session_id === opened.playback_session_id
      ) {
        if (force) video.current?.load();
        else if (video.current && video.current.readyState > 0) {
          video.current.currentTime = initialPosition.current / 1_000;
          if (pendingPlay.current) await play();
        }
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

  const enterFullscreen = async () => {
    try {
      const root = workspaceRef?.current || stage.current;
      if (root && document.fullscreenElement !== root) await root.requestFullscreen();
      callbacks.current.onImmersiveChange(true);
    } catch {
      setError("浏览器未允许全屏，请双击视频画面重试（FULLSCREEN_DENIED）");
    }
  };

  const toggleFullscreen = async () => {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await enterFullscreen();
  };

  const resumeFullscreen = async () => {
    await enterFullscreen();
    await play();
  };

  const pauseForCapture = () => {
    const player = video.current;
    const selected = current.current;
    if (!player || !selected) return undefined;
    player.pause();
    saveQuietly();
    return {
      asset_id: selected.asset_id,
      source_version: selected.source_version,
      time_ms: Math.max(0, Math.round(player.currentTime * 1000)),
    };
  };

  const closeAsset = async () => {
    const epoch = ++openEpoch.current;
    video.current?.pause();
    await savePosition();
    if (!mounted.current || epoch !== openEpoch.current) return;
    current.current = null;
    resumeOnActive.current = false;
    pendingPlay.current = false;
    setAsset(null);
    setPlaying(false);
    setPlayBlocked(false);
    setError("");
    callbacks.current.onAssetChange?.(null);
  };

  useImperativeHandle(ref, () => ({
    openAsset,
    openSource,
    resumeFullscreen,
    toggleFullscreen,
    savePosition,
    pauseForCapture,
    closeAsset,
    getElement: () => video.current,
  }));

  const playback = usePlaybackControls({
    video,
    stage,
    active,
    identity: asset ? `${asset.asset_id}:${asset.source_version}` : undefined,
    toggleFullscreen,
  });
  const seekBound = (seconds: number) => {
    if (
      asset &&
      current.current?.asset_id === asset.asset_id &&
      current.current.source_version === asset.source_version
    )
      playback.seekTo(seconds);
  };

  useEffect(() => {
    mounted.current = true;
    let disposed = false;
    void (
      minimal && openedAsset
        ? Promise.resolve([openedAsset])
        : api.read<VideoAsset[]>(`/courses/${courseId}/videos`)
    )
      .then((values) => {
        if (!disposed) {
          setAssets(values);
          callbacks.current.onLibraryChange(values);
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
      // The latest imperative player, rather than the initial empty ref, must stop on course exit.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      video.current?.pause();
      saveQuietly();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", hidden);
      window.removeEventListener("pagehide", pagehide);
    };
    // A course gets a fresh player; entering it never opens a previous resource.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId]);

  useEffect(() => {
    if (!assetId || current.current?.asset_id === assetId) return;
    let disposed = false;
    void Promise.resolve().then(async () => {
      if (disposed) return;
      try {
        const adopted = openedAsset?.asset_id === assetId ? openedAsset : undefined;
        let selected = adopted ?? assets.find((item) => item.asset_id === assetId);
        if (!selected) {
          // Opening a newly imported file may create its media identity after the first listing.
          const values = await api.read<VideoAsset[]>(`/courses/${courseId}/videos`);
          if (disposed) return;
          setAssets(values);
          callbacks.current.onLibraryChange(values);
          selected = values.find((item) => item.asset_id === assetId);
        }
        if (!disposed && selected) await openAsset(selected, undefined, false, !!adopted);
        else if (!disposed) setError("该视频不在当前课程目录中（SOURCE_UNAVAILABLE）");
      } catch (reason) {
        if (!disposed) setError(errorMessage(reason));
      }
    });
    return () => {
      disposed = true;
    };
    // Only the selected source identity may open a resource automatically.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assetId, courseId]);

  useEffect(() => {
    activeRef.current = active;
    const player = video.current;
    if (!player) return;
    if (!active) {
      resumeOnActive.current = !player.paused;
      player.pause();
      saveQuietly();
    } else if (resumeOnActive.current && (!assetId || current.current?.asset_id === assetId)) {
      resumeOnActive.current = false;
      void play();
    }
    // A tab transition restores the actual previous playing/paused state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  useEffect(() => {
    const change = () => callbacks.current.onImmersiveChange(Boolean(document.fullscreenElement));
    const escape = (event: KeyboardEvent) => {
      if (
        event.key !== "Escape" ||
        event.defaultPrevented ||
        document.querySelector("[data-workspace-capture-overlay]")
      )
        return;
      const root = workspaceRef?.current || stage.current;
      if (!root || document.fullscreenElement !== root) return;
      event.preventDefault();
      if (fullscreenExitPending.current) return;
      fullscreenExitPending.current = true;
      // Explicitly leave this workspace even when the host does not dispatch a native exit.
      // Editing focus does not change Escape's fullscreen meaning; capture cancellation wins above.
      void document
        .exitFullscreen()
        .then(() => {
          if (mounted.current)
            setError((value) => (value.includes("FULLSCREEN_EXIT_FAILED") ? "" : value));
        })
        .catch(() => {
          if (mounted.current) setError("退出全屏未完成，请重试（FULLSCREEN_EXIT_FAILED）");
        })
        .finally(() => {
          fullscreenExitPending.current = false;
        });
    };
    document.addEventListener("fullscreenchange", change);
    window.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("fullscreenchange", change);
      window.removeEventListener("keydown", escape);
    };
  }, [workspaceRef]);

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

  return (
    <section
      className={`video-workspace${minimal ? " video-workspace-minimal" : ""}${asset?.media_type.startsWith("audio/") ? " video-workspace-audio" : ""}`}
      aria-label="视频学习区"
      hidden={!active}
    >
      {!minimal && (
        <>
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
        </>
      )}
      {error && (
        <p className="inline-error video-error" role="alert">
          {error}
        </p>
      )}
      <div
        className={`video-stage redesigned-video-stage${playing ? "" : " video-paused"}${asset?.media_type.startsWith("audio/") ? " video-stage-audio" : ""}`}
        ref={stage}
        aria-label="视频画面"
        tabIndex={0}
      >
        <div
          className="video-picture-frame"
          style={{ "--video-ratio": pictureRatio } as CSSProperties}
        >
          {asset ? (
            <video
              ref={video}
              key={`${asset.asset_id}:${asset.source_version}`}
              aria-label={asset.media_type.startsWith("audio/") ? "课程录音" : "课程视频"}
              data-primary-video="true"
              preload="metadata"
              playsInline
              src={videoContentUrl(asset)}
              onLoadedMetadata={() => {
                if (!video.current) return;
                if (video.current.videoWidth && video.current.videoHeight)
                  setPictureRatio(video.current.videoWidth / video.current.videoHeight);
                const target = initialPosition.current / 1_000;
                video.current.currentTime = Number.isFinite(video.current.duration)
                  ? Math.min(target, Math.max(0, video.current.duration - 0.05))
                  : target;
                if (pendingPlay.current) void play();
              }}
              onPause={() => {
                setPlaying(false);
                saveQuietly();
              }}
              onPlay={() => setPlaying(true)}
              onSeeked={saveQuietly}
              onTimeUpdate={() => {
                if (video.current && video.current.readyState > 0)
                  lastKnownPosition.current = Math.round(video.current.currentTime * 1000);
              }}
              onEnded={saveQuietly}
              onError={() =>
                setError(
                  "视频无法播放，请刷新目录检查原件或浏览器格式支持（MEDIA_PLAYBACK_FAILED）",
                )
              }
            />
          ) : (
            <div className="video-empty">
              <span className="video-empty-symbol" aria-hidden="true">
                ▷
              </span>
              <h3>选择视频，接着看</h3>
              <p>从当前课程目录选择视频。打开后恢复播放进度；截图快捷键可框选软件内可见内容。</p>
            </div>
          )}
          {asset?.media_type.startsWith("audio/") && (
            <div className="audio-recording-title">
              <span aria-hidden="true">♫</span>
              <p>{asset.display_name}</p>
            </div>
          )}
          {asset && (
            <VideoControls
              RateControl={RateControl}
              key={`${asset.asset_id}:${asset.source_version}`}
              playing={playing}
              {...playback}
              previewSrc={
                asset.media_type.startsWith("audio/") ? undefined : videoContentUrl(asset)
              }
              sourceKey={`${asset.asset_id}:${asset.source_version}`}
              onFullscreen={toggleFullscreen}
              onSeek={seekBound}
              onTogglePlay={() => {
                if (video.current?.paused) void play();
                else video.current?.pause();
              }}
              onMute={() => {
                if (video.current) video.current.muted = !video.current.muted;
              }}
              onVolume={(value) => {
                if (!video.current) return;
                video.current.volume = value;
                video.current.muted = false;
              }}
              onRate={(value) => {
                if (video.current) video.current.playbackRate = value;
              }}
            />
          )}
        </div>
      </div>
      {asset?.audio_content_url && (
        <SplitAudio
          key={`${asset.asset_id}:${asset.source_version}:audio`}
          video={video}
          src={asset.audio_content_url}
          onError={() => setError("平台音轨无法播放，请重新打开核对（MEDIA_AUDIO_FAILED）")}
        />
      )}
      {asset?.platform === "bilibili" && connected && (
        <PlatformMediaActions
          courseId={courseId}
          asset={asset}
          onReload={() => {
            void openAsset(asset, undefined, true);
          }}
        />
      )}
      {asset && connected && showSubtitles && (
        <SubtitlePanel
          key={`${asset.asset_id}:${asset.source_version}`}
          courseId={courseId}
          asset={asset}
          positionMs={Math.round(playback.elapsed * 1000)}
          active={active}
          onSeek={seekBound}
          onAction={onSubtitleAction}
        />
      )}
      {playBlocked && (
        <p className="playback-notice" role="status">
          浏览器已阻止自动播放，单击视频画面继续。
        </p>
      )}
      {!minimal && (
        <>
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
          <button type="button" className="sr-only" onClick={onOpenChat}>
            打开 AI 对话
          </button>
        </>
      )}
    </section>
  );
}
