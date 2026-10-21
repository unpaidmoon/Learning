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
  playing,
  elapsed,
  duration,
  volume,
  muted,
  rate,
  onFullscreen,
  onSeek,
  onTogglePlay,
  onMute,
  onVolume,
  onRate,
}: {
  playing: boolean;
  elapsed: number;
  duration: number;
  volume: number;
  muted: boolean;
  rate: number;
  onFullscreen: () => Promise<void>;
  onSeek: (seconds: number) => void;
  onTogglePlay: () => void;
  onMute: () => void;
  onVolume: (value: number) => void;
  onRate: (value: number) => void;
}) {
  return (
    <div className="player-toolbar" data-player-controls="true" aria-label="视频播放控制">
      <input
        className="player-progress"
        aria-label="视频播放进度"
        type="range"
        min={0}
        max={duration || 1}
        step={0.1}
        value={Math.min(elapsed, duration || 1)}
        onChange={(event) => onSeek(Number(event.target.value))}
      />
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
