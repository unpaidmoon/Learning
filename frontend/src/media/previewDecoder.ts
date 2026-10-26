/** One on-demand UI decoder; only the guarded same-origin media URL reaches it. */
export function createPreviewDecoder(
  src: string,
  publish: (frame: { time_ms: number; url: string }) => void,
  failed: () => void,
) {
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.preload = "metadata";
  let disposed = false;
  let wanted: number | undefined;
  let busy = false;
  let target = -1;
  let completed = -1;
  let timeout: number | undefined;

  const dispose = () => {
    if (disposed) return;
    disposed = true;
    window.clearTimeout(timeout);
    video.onloadedmetadata = video.onloadeddata = video.onseeked = video.onerror = null;
    video.pause();
    video.removeAttribute("src");
    video.load();
  };
  const error = () => {
    dispose();
    failed();
  };
  const clamp = () => Math.max(0, Math.min(wanted ?? 0, Math.max(0, video.duration - 0.05)));
  const capture = () => {
    if (disposed || !busy || video.seeking || video.readyState < 2) return;
    try {
      const canvas = document.createElement("canvas");
      canvas.width = 240;
      canvas.height = 135;
      const context = canvas.getContext("2d");
      if (!context || !video.videoWidth || !video.videoHeight) return;
      const scale = Math.min(240 / video.videoWidth, 135 / video.videoHeight);
      const width = video.videoWidth * scale;
      const height = video.videoHeight * scale;
      context.fillStyle = "black";
      context.fillRect(0, 0, 240, 135);
      context.drawImage(video, (240 - width) / 2, (135 - height) / 2, width, height);
      publish({
        time_ms: Math.round(video.currentTime * 1000),
        url: canvas.toDataURL("image/jpeg", 0.65),
      });
      completed = target;
      busy = false;
      window.clearTimeout(timeout);
      pump();
    } catch {
      error();
    }
  };
  const pump = () => {
    if (
      disposed ||
      wanted === undefined ||
      busy ||
      video.readyState < 1 ||
      !Number.isFinite(video.duration)
    )
      return;
    target = clamp();
    if (Math.abs(completed - target) < 0.01) return;
    busy = true;
    window.clearTimeout(timeout);
    timeout = window.setTimeout(error, 8000);
    if (Math.abs(video.currentTime - target) < 0.01 && video.readyState >= 2) capture();
    else video.currentTime = target;
  };
  video.onloadedmetadata = pump;
  video.onseeked = video.onloadeddata = capture;
  video.onerror = error;
  timeout = window.setTimeout(error, 8000);
  video.src = src;
  video.load();
  return {
    seek(time: number) {
      wanted = Math.max(0, Math.floor(time / 2) * 2);
      pump();
    },
    dispose,
  };
}
