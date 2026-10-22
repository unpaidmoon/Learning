import { useCallback, useEffect, useRef, useState } from "react";
import "./SeekPreview.css";

function formatTime(seconds: number) {
  const value = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const rest = String(value % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
}

function localSource(value: string) {
  if (!value) return undefined;
  try {
    const url = new URL(value, window.location.href);
    if (
      url.origin !== window.location.origin ||
      !/^https?:$/.test(url.protocol) ||
      !/^\/api\/v1\/videos\/[^/]+\/content$/.test(url.pathname) ||
      !url.searchParams.get("source_version") ||
      url.username ||
      url.password
    )
      return undefined;
    return url.href;
  } catch {
    return undefined;
  }
}

type PreviewProps = {
  src: string;
  time: number;
  visible: boolean;
  /** Pixel position relative to the progress track; its owner clamps the edges. */
  left: number;
  sourceKey: string;
};

/** A local UI decoder, isolated from playback, capture drafts and model inputs. */
export function SeekPreview({ src, time, visible, left, sourceKey }: PreviewProps) {
  const source = localSource(src);
  if (!visible || !source || !sourceKey) return null;
  return (
    <PreviewDecoder
      key={`${sourceKey}:${source}`}
      src={source}
      time={Number.isFinite(time) ? Math.max(0, time) : 0}
      left={Number.isFinite(left) ? left : 0}
    />
  );
}

function PreviewDecoder({ src, time, left }: { src: string; time: number; left: number }) {
  const decoder = useRef<HTMLVideoElement>(null);
  const desired = useRef(time);
  const inFlight = useRef<number | null>(null);
  const metadata = useRef(false);
  const disposed = useRef(false);
  const failed = useRef(false);
  const [frameTime, setFrameTime] = useState<number | null>(null);
  const [error, setError] = useState(false);

  const pump = useCallback(() => {
    const video = decoder.current;
    if (
      !video ||
      disposed.current ||
      failed.current ||
      !metadata.current ||
      inFlight.current !== null
    )
      return;
    const maximum = Number.isFinite(video.duration) ? Math.max(0, video.duration - 0.001) : 0;
    const target = Math.min(maximum, Math.max(0, desired.current));
    if (video.readyState >= 2 && Math.abs(video.currentTime - target) < 0.04) {
      setFrameTime(video.currentTime);
      return;
    }
    inFlight.current = target;
    try {
      video.currentTime = target;
    } catch {
      inFlight.current = null;
      failed.current = true;
      setError(true);
    }
  }, []);

  useEffect(() => {
    const video = decoder.current;
    disposed.current = false;
    // StrictMode replays setup after cleanup on the same committed DOM element.
    if (video && video.getAttribute("src") !== src) video.setAttribute("src", src);
    return () => {
      disposed.current = true;
      metadata.current = false;
      inFlight.current = null;
      if (video) {
        video.pause();
        video.removeAttribute("src");
        video.load();
      }
    };
  }, [src]);

  useEffect(() => {
    let cancelled = false;
    // React commits the requested label immediately; decoder work never blocks pointer state.
    void Promise.resolve().then(() => {
      if (cancelled || disposed.current) return;
      desired.current = time;
      pump();
    });
    return () => {
      cancelled = true;
    };
  }, [time, pump]);

  const ready = frameTime !== null && Math.abs(frameTime - time) < 0.06 && !error;
  return (
    <div
      className="seek-preview"
      role="img"
      aria-label="视频进度预览"
      aria-busy={!ready && !error}
      style={{ left }}
      data-preview-time={time}
      data-preview-frame-time={frameTime ?? ""}
      data-preview-local-only="true"
    >
      <div className="seek-preview-frame">
        <video
          ref={decoder}
          data-preview-video="true"
          src={src}
          aria-hidden="true"
          tabIndex={-1}
          muted
          playsInline
          disablePictureInPicture
          preload="metadata"
          className={ready ? "seek-preview-video ready" : "seek-preview-video"}
          onLoadedMetadata={() => {
            if (disposed.current) return;
            metadata.current = true;
            pump();
          }}
          onLoadedData={() => {
            const video = decoder.current;
            if (
              video &&
              !disposed.current &&
              !video.seeking &&
              inFlight.current !== null &&
              Math.abs(video.currentTime - inFlight.current) < 0.06
            ) {
              inFlight.current = null;
              setFrameTime(video.currentTime);
            }
            pump();
          }}
          onSeeked={() => {
            const video = decoder.current;
            if (!video || disposed.current || video.seeking) return;
            inFlight.current = null;
            if (video.readyState >= 2) setFrameTime(video.currentTime);
            pump();
          }}
          onError={() => {
            if (disposed.current) return;
            inFlight.current = null;
            failed.current = true;
            setError(true);
          }}
        />
        {!ready && (
          <span className="seek-preview-placeholder">{error ? "预览不可用" : "正在预览…"}</span>
        )}
      </div>
      <span className="seek-preview-time">{formatTime(time)}</span>
    </div>
  );
}
