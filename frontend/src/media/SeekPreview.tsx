import { useEffect, useState } from "react";
import { api } from "../api/client";
import "./SeekPreview.css";

type Frame = { index: number; time_ms: number; url: string };
type Manifest = { status: string; frames: Frame[] };
type PreviewCache = {
  frames: Frame[];
  loaded: Map<string, Frame>;
  loading: Set<string>;
  listeners: Set<() => void>;
  fallback?: Frame;
  timer?: number;
  polling: boolean;
  desiredTime?: number;
};
// Keep a bounded decoded working set; the versioned durable cache is backend-owned.
const caches = new Map<string, PreviewCache>();
function cacheFor(key: string) {
  let value = caches.get(key);
  if (!value) {
    value = {
      frames: [],
      loaded: new Map(),
      loading: new Set(),
      listeners: new Set(),
      polling: false,
    };
    caches.set(key, value);
    for (const [old, entry] of caches) {
      if (caches.size <= 6) break;
      if (!entry.listeners.size && old !== key) {
        window.clearTimeout(entry.timer);
        caches.delete(old);
      }
    }
  }
  return value;
}
function publish(cache: PreviewCache) {
  for (const listener of cache.listeners) listener();
}
function formatTime(seconds: number) {
  const value = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const rest = String(value % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
}
function localSource(value: string) {
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
    return url;
  } catch {
    return undefined;
  }
}
function nearest(frames: Iterable<Frame>, time: number) {
  let best: Frame | undefined;
  for (const frame of frames) {
    if (!best || Math.abs(frame.time_ms - time * 1000) < Math.abs(best.time_ms - time * 1000))
      best = frame;
  }
  return best;
}
function preload(cache: PreviewCache, frame: Frame) {
  if (cache.loaded.has(frame.url) || cache.loading.has(frame.url)) return;
  cache.loading.add(frame.url);
  const image = new Image();
  image.onload = () => {
    cache.loading.delete(frame.url);
    cache.loaded.set(frame.url, frame);
    while (cache.loaded.size > 96) cache.loaded.delete(cache.loaded.keys().next().value!);
    publish(cache);
  };
  image.onerror = () => cache.loading.delete(frame.url);
  image.src = frame.url;
}
function requestFrame(cache: PreviewCache, time: number) {
  cache.desiredTime = time;
  const closest = nearest(cache.frames, time);
  if (closest) preload(cache, closest);
}
function poll(cache: PreviewCache, source: URL) {
  if (cache.polling || !cache.listeners.size) return;
  cache.polling = true;
  const endpoint = source.pathname.replace(/\/content$/, "/previews") + source.search;
  void fetch(endpoint, { credentials: "same-origin", cache: "no-store" })
    .then(async (response) => {
      if (!response.ok) return false;
      const manifest: Manifest = await response.json();
      cache.frames = manifest.frames.filter((frame) => {
        try {
          const url = new URL(frame.url, location.href);
          return (
            url.origin === location.origin &&
            !url.username &&
            !url.password &&
            url.pathname.startsWith(source.pathname.replace(/\/content$/, "/previews/")) &&
            url.searchParams.get("source_version") === source.searchParams.get("source_version")
          );
        } catch {
          return false;
        }
      });
      const stride = Math.max(1, Math.ceil(cache.frames.length / 32));
      cache.frames.forEach((frame, index) => {
        if (index % stride === 0 || index === cache.frames.length - 1) preload(cache, frame);
      });
      if (cache.desiredTime !== undefined) {
        const requested = nearest(cache.frames, cache.desiredTime);
        if (requested) preload(cache, requested);
      }
      return manifest.status === "queued" || manifest.status === "building";
    })
    .catch(() => false)
    .then((pending) => {
      cache.polling = false;
      if (pending && cache.listeners.size)
        cache.timer = window.setTimeout(() => poll(cache, source), 500);
    });
}

type PreviewProps = {
  src: string;
  time: number;
  visible: boolean;
  left: number;
  sourceKey: string;
};

/** Cached local-only thumbnails; neither this cache nor its decoder submits attachments. */
export function SeekPreview({ src, time, visible, left, sourceKey }: PreviewProps) {
  const source = sourceKey ? localSource(src) : undefined;
  const sourceHref = source?.href;
  const cache = cacheFor(`${sourceKey}:${sourceHref || ""}`);
  const [, redraw] = useState(0);
  const [leaseId] = useState(() => `preview-${crypto.randomUUID()}`);
  useEffect(() => {
    if (!sourceHref) return;
    const url = new URL(sourceHref);
    const changed = () => redraw((value) => value + 1);
    cache.listeners.add(changed);
    poll(cache, url);
    // Warm one low-resolution browser-local image before any pointer movement.
    // It is never a draft, a server upload or a model input.
    const decoder = document.createElement("video");
    decoder.muted = true;
    decoder.playsInline = true;
    decoder.preload = "auto";
    let disposed = false;
    decoder.onloadeddata = () => {
      if (disposed || cache.fallback || decoder.readyState < 2) return;
      const canvas = document.createElement("canvas");
      canvas.width = 240;
      canvas.height = 135;
      const context = canvas.getContext("2d");
      if (!context) return;
      context.drawImage(decoder, 0, 0, canvas.width, canvas.height);
      cache.fallback = {
        index: -1,
        time_ms: Math.round(decoder.currentTime * 1000),
        url: canvas.toDataURL("image/jpeg", 0.65),
      };
      publish(cache);
      decoder.pause();
      decoder.removeAttribute("src");
      decoder.load();
    };
    if (!cache.fallback && !cache.loaded.size) {
      decoder.src = sourceHref;
      decoder.load();
    }
    return () => {
      disposed = true;
      cache.listeners.delete(changed);
      if (!cache.listeners.size) window.clearTimeout(cache.timer);
      decoder.pause();
      decoder.removeAttribute("src");
      decoder.load();
    };
  }, [sourceHref, cache]);
  useEffect(() => {
    if (!visible) return;
    requestFrame(cache, time);
  }, [time, visible, cache]);
  useEffect(() => {
    if (!visible || !sourceHref) return;
    const renew = () =>
      void api
        .write("/video-previews/priority", {
          lease_id: leaseId,
          active: true,
          kind: "interaction",
          ttl_ms: 3000,
        })
        .catch(() => {});
    renew();
    const timer = window.setInterval(renew, 2000);
    return () => {
      window.clearInterval(timer);
      void api
        .write(
          "/video-previews/priority",
          { lease_id: leaseId, active: false, kind: "interaction", ttl_ms: 3000 },
          { keepalive: true },
        )
        .catch(() => {});
    };
  }, [visible, sourceHref, leaseId]);
  const frame = nearest(cache.loaded.values(), Number.isFinite(time) ? time : 0) || cache.fallback;
  // Never present a time-only/loading box as a frame. Keep a real decoded
  // neighbor visible while a closer cached thumbnail is being loaded.
  if (!visible || !source || !sourceKey || !frame) return null;
  return (
    <div
      className="seek-preview"
      role="img"
      aria-label="视频进度预览"
      style={{ left: Number.isFinite(left) ? left : 0 }}
      data-preview-time={time}
      data-preview-frame-time={frame.time_ms / 1000}
      data-preview-local-only="true"
      data-preview-cached="true"
    >
      <div className="seek-preview-frame">
        <img
          className="seek-preview-image"
          src={frame.url}
          alt=""
          aria-hidden="true"
          data-testid="video-preview-image"
        />
      </div>
      <span className="seek-preview-time">{formatTime(time)}</span>
    </div>
  );
}
