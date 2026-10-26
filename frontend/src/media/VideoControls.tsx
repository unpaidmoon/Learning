function formatTime(seconds: number) {
  if (!Number.isFinite(seconds)) return "0:00";
  const value = Math.max(0, Math.floor(seconds));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

function PlayerIcon({ kind }: { kind: "play" | "pause" | "volume" | "muted" | "fullscreen" }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      width="19"
      height="19"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {kind === "play" && <path d="m9 5 11 7-11 7Z" fill="currentColor" stroke="none" />}
      {kind === "pause" && (
        <>
          <path d="M8 5v14M16 5v14" strokeWidth="3.5" />
        </>
      )}
      {(kind === "volume" || kind === "muted") && <path d="M11 5 6 9H3v6h3l5 4Z" />}
      {kind === "volume" && (
        <>
          <path d="M15 8a6 6 0 0 1 0 8M18 5a10 10 0 0 1 0 14" />
        </>
      )}
      {kind === "muted" && <path d="m16 9 5 6m0-6-5 6" />}
      {kind === "fullscreen" && <path d="M4 9V4h5m6 0h5v5m0 6v5h-5m-6 0H4v-5" />}
    </svg>
  );
}

export function VideoControls({
  RateControl,
  playing,
  elapsed,
  duration,
  volume,
  muted,
  rate,
  previewSrc,
  sourceKey,
  onFullscreen,
  onSeek,
  onTogglePlay,
  onMute,
  onVolume,
  onRate,
}: {
  RateControl?: ComponentType<{ rate: number; onRate: (rate: number) => void }>;
  playing: boolean;
  elapsed: number;
  duration: number;
  volume: number;
  muted: boolean;
  rate: number;
  previewSrc?: string;
  sourceKey?: string;
  onFullscreen: () => Promise<void>;
  onSeek: (seconds: number) => void;
  onTogglePlay: () => void;
  onMute: () => void;
  onVolume: (value: number) => void;
  onRate: (value: number) => void;
}) {
  const progress = useRef<HTMLInputElement>(null);
  const track = useRef<HTMLDivElement>(null);
  const drag = useRef<{ pointerId: number; value: number } | null>(null);
  const [scrub, setScrub] = useState<number | null>(null);
  const [preview, setPreview] = useState<{ time: number; left: number } | null>(null);
  const maximum = Number.isFinite(duration) && duration > 0 ? duration : 1;
  const position = Math.min(maximum, Math.max(0, scrub ?? elapsed));

  const clamp = (seconds: number) => Math.min(maximum, Math.max(0, seconds));
  const previewAt = (time: number, left?: number) => {
    const box = track.current?.getBoundingClientRect();
    const width = box?.width || 160;
    const point = left ?? (clamp(time) / maximum) * width;
    setPreview({
      time: clamp(time),
      left: width < 176 ? width / 2 : Math.max(88, Math.min(width - 88, point)),
    });
  };
  const fromPointer = (clientX: number, fallback: number) => {
    const box = progress.current?.getBoundingClientRect();
    if (!box?.width || !Number.isFinite(clientX)) return clamp(fallback);
    // Chromium's range thumb has a fixed footprint; its centre travels on this
    // inset track. Pointer state never waits for media seeking or persistence.
    const inset = Math.min(7, box.width / 2);
    return clamp(((clientX - box.left - inset) / Math.max(1, box.width - inset * 2)) * maximum);
  };
  const move = (event: PointerEvent<HTMLDivElement>) => {
    const time = fromPointer(event.clientX, drag.current?.value ?? elapsed);
    if (drag.current) {
      drag.current.value = time;
      setScrub(time);
    }
    previewAt(time, event.clientX - (track.current?.getBoundingClientRect().left || 0));
  };
  const cancel = () => {
    drag.current = null;
    setScrub(null);
    setPreview(null);
  };

  useEffect(() => {
    const cancelOnBlur = () => {
      drag.current = null;
      setScrub(null);
      setPreview(null);
    };
    window.addEventListener("blur", cancelOnBlur);
    return () => window.removeEventListener("blur", cancelOnBlur);
  }, []);

  return (
    <div className="player-toolbar" data-player-controls="true" aria-label="视频播放控制">
      <div
        className="player-seek-track"
        ref={track}
        onPointerEnter={move}
        onPointerMove={move}
        onPointerLeave={() => {
          if (!drag.current) setPreview(null);
        }}
      >
        {previewSrc && sourceKey && (
          <SeekPreview
            src={previewSrc}
            sourceKey={sourceKey}
            visible={preview !== null}
            time={preview?.time ?? position}
            left={preview?.left ?? 80}
          />
        )}
        <input
          ref={progress}
          className="player-progress"
          aria-label="视频播放进度"
          aria-valuetext={formatTime(position)}
          data-scrubbing={scrub === null ? "false" : "true"}
          type="range"
          min={0}
          max={maximum}
          step={0.1}
          disabled={!Number.isFinite(duration) || duration <= 0}
          value={position}
          onPointerDown={(event) => {
            if (event.button > 0) return;
            const value = fromPointer(event.clientX, Number(event.currentTarget.value));
            drag.current = { pointerId: event.pointerId, value };
            setScrub(value);
            previewAt(value);
            event.currentTarget.setPointerCapture?.(event.pointerId);
          }}
          onChange={(event) => {
            const value = clamp(Number(event.target.value));
            if (drag.current) {
              drag.current.value = value;
              setScrub(value);
              previewAt(value);
            } else onSeek(value);
          }}
          onPointerUp={(event) => {
            const selected = drag.current;
            if (!selected || selected.pointerId !== event.pointerId) return;
            drag.current = null;
            setScrub(null);
            onSeek(selected.value);
            event.currentTarget.releasePointerCapture?.(event.pointerId);
          }}
          onPointerCancel={cancel}
          onLostPointerCapture={() => {
            if (drag.current) cancel();
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape" && drag.current) {
              event.preventDefault();
              cancel();
            }
          }}
        />
      </div>
      <div className="player-toolbar-row">
        <button
          type="button"
          aria-label="播放或暂停"
          title="播放 / 暂停（空格）"
          onClick={onTogglePlay}
        >
          <PlayerIcon kind={playing ? "pause" : "play"} />
        </button>
        <span className="player-time">
          {formatTime(elapsed)} / {formatTime(duration)}
        </span>
        <div className="player-volume" data-player-volume="true" aria-label="音量控制区域">
          <button type="button" aria-label="静音或取消静音" title="静音（M）" onClick={onMute}>
            <PlayerIcon kind={muted || !volume ? "muted" : "volume"} />
          </button>
          <input
            aria-label="视频音量"
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={muted ? 0 : volume}
            onChange={(event) => onVolume(Number(event.target.value))}
          />
        </div>
        {RateControl ? (
          <RateControl rate={rate} onRate={onRate} />
        ) : (
          <select
            aria-label="播放倍速"
            value={rate}
            onChange={(event) => onRate(Number(event.target.value))}
          >
            {[0.5, 0.75, 1, 1.25, 1.5, 1.75, 2].map((value) => (
              <option key={value} value={value}>
                {value}×
              </option>
            ))}
          </select>
        )}
        <button
          type="button"
          aria-label="视频全屏"
          title="全屏 / 退出全屏（双击画面）"
          onClick={() => void onFullscreen()}
        >
          <PlayerIcon kind="fullscreen" />
        </button>
      </div>
    </div>
  );
}
import { useEffect, useRef, useState } from "react";
import type { PointerEvent, ComponentType } from "react";
import { SeekPreview } from "./SeekPreview";
