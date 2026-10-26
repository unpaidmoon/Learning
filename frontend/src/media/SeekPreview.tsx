import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { createPreviewDecoder } from "./previewDecoder";
import "./SeekPreview.css";

type Frame = { index: number; time_ms: number; url: string };
type Manifest = { status: string; frames: Frame[]; strategy?: "thumbnails" | "media" };
type PreviewCache = {
  frames: Frame[];
  loaded: Map<string, Frame>;
  loading: Set<string>;
  listeners: Set<() => void>;
  strategy?: "thumbnails" | "media";
  decoderFailed?: boolean;
  failed: Set<string>;
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
      failed: new Set(),
      strategy: "thumbnails",
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
  if (cache.loaded.has(frame.url) || cache.loading.has(frame.url) || cache.failed.has(frame.url))
    return;
  cache.loading.add(frame.url);
  const image = new Image();
  image.onload = () => {
    cache.loading.delete(frame.url);
    cache.loaded.set(frame.url, frame);
    while (cache.loaded.size > 96) cache.loaded.delete(cache.loaded.keys().next().value!);
    publish(cache);
  };
  image.onerror = () => {
    cache.loading.delete(frame.url);
    cache.failed.add(frame.url);
  };
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
      cache.strategy = manifest.strategy || "thumbnails";
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
      if (cache.desiredTime !== undefined) {
        const requested = nearest(cache.frames, cache.desiredTime);
        if (requested) preload(cache, requested);
      }
      // Three coarse images warm the cache without competing with a hovered
      // target or the primary video's first Range requests.
      for (const index of [0, Math.floor(cache.frames.length / 2), cache.frames.length - 1]) {
        if (cache.frames[index]) preload(cache, cache.frames[index]);
      }
      publish(cache);
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

/** Versioned UI thumbnails; neither the cache nor the decoder submits attachments. */
export function SeekPreview({ src, time, visible, left, sourceKey }: PreviewProps) {
  const source = sourceKey ? localSource(src) : undefined;
  const sourceHref = source?.href;
  const cache = cacheFor(`${sourceKey}:${sourceHref || ""}`);
  const [, redraw] = useState(0);
  const [leaseId] = useState(() => `preview-${crypto.randomUUID()}`);
  const decoder = useRef<ReturnType<typeof createPreviewDecoder> | null>(null);
  const latestTime = useRef(time);
  useEffect(() => {
    latestTime.current = time;
  }, [time]);
  useEffect(() => {
    if (!sourceHref) return;
    const url = new URL(sourceHref);
    const changed = () => redraw((value) => value + 1);
    cache.listeners.add(changed);
    poll(cache, url);
    return () => {
      cache.listeners.delete(changed);
      if (!cache.listeners.size) window.clearTimeout(cache.timer);
    };
  }, [sourceHref, cache]);
  useEffect(() => {
    if (!visible) return;
    requestFrame(cache, time);
  }, [time, visible, cache]);
  useEffect(() => {
    if (!visible || !sourceHref || cache.strategy === "media") return;
    const url = new URL(sourceHref);
    const renew = () =>
      void api
        .write("/video-previews/priority", {
          lease_id: leaseId,
          active: true,
          kind: "preview",
          asset_id: url.pathname.split("/")[4],
          source_version: url.searchParams.get("source_version"),
          time_ms: Math.round(Math.max(0, latestTime.current) * 1000),
          ttl_ms: 3000,
        })
        .catch(() => {});
    const timer = window.setInterval(renew, 2000);
    return () => {
      window.clearInterval(timer);
      void api
        .write(
          "/video-previews/priority",
          { lease_id: leaseId, active: false, kind: "preview", ttl_ms: 3000 },
          { keepalive: true },
        )
        .catch(() => {});
    };
  }, [visible, sourceHref, leaseId, cache, cache.strategy]);
  useEffect(() => {
    if (!visible || !sourceHref || cache.strategy !== "media" || cache.decoderFailed) return;
    let instance: ReturnType<typeof createPreviewDecoder> | undefined;
    const publishFrame = (value: { time_ms: number; url: string }) => {
      const key = `media-${Math.round(value.time_ms / 2000)}`;
      cache.loaded.delete(key);
      cache.loaded.set(key, { ...value, index: -1 });
      while (cache.loaded.size > 48) cache.loaded.delete(cache.loaded.keys().next().value!);
      publish(cache);
    };
    decoder.current = {
      seek(targetTime) {
        const target = Math.floor(targetTime / 2) * 2000;
        const cached = nearest(cache.loaded.values(), target / 1000);
        if (cache.decoderFailed || (cached && Math.abs(cached.time_ms - target) < 1000)) return;
        instance ??= createPreviewDecoder(sourceHref, publishFrame, () => {
          cache.decoderFailed = true;
        });
        instance.seek(targetTime);
      },
      dispose() {
        instance?.dispose();
      },
    };
    return () => {
      instance?.dispose();
      decoder.current = null;
    };
  }, [visible, sourceHref, cache, cache.strategy]);
  useEffect(() => {
    if (!visible) return;
    const timer = window.setTimeout(() => {
      decoder.current?.seek(time);
      if (sourceHref && cache.strategy !== "media") {
        const url = new URL(sourceHref);
        void api
          .write("/video-previews/priority", {
            lease_id: leaseId,
            active: true,
            kind: "preview",
            ttl_ms: 3000,
            asset_id: url.pathname.split("/")[4],
            source_version: url.searchParams.get("source_version"),
            time_ms: Math.round(Math.max(0, time) * 1000),
          })
          .catch(() => {});
      }
    }, 80);
    return () => window.clearTimeout(timer);
  }, [time, visible, sourceHref, cache, cache.strategy, leaseId]);
  const candidate = nearest(cache.loaded.values(), Number.isFinite(time) ? time : 0);
  const frame =
    cache.strategy === "media" && candidate && Math.abs(candidate.time_ms - time * 1000) >= 2000
      ? undefined
      : candidate;
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
