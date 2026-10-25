/** Keep optional DASH audio under the existing video controls. */
export function bindSplitAudio(
  video: HTMLVideoElement,
  audio: HTMLAudioElement,
  onError: () => void,
) {
  let waiting = false;
  let disposed = false;
  const sync = () => {
    if (
      Number.isFinite(video.currentTime) &&
      Math.abs(audio.currentTime - video.currentTime) > 0.12
    )
      audio.currentTime = video.currentTime;
    audio.playbackRate = video.playbackRate;
    audio.volume = video.volume;
    audio.muted = video.muted;
  };
  const play = () => {
    sync();
    if (!video.paused && !video.seeking && !waiting)
      void audio.play().catch(() => {
        if (!disposed) {
          video.pause();
          onError();
        }
      });
  };
  const pause = () => {
    audio.pause();
    sync();
  };
  const wait = () => {
    waiting = true;
    audio.pause();
  };
  const resume = () => {
    waiting = false;
    play();
  };
  const handlers: [string, () => void][] = [
    ["play", play],
    ["playing", resume],
    ["pause", pause],
    ["ended", pause],
    ["waiting", wait],
    ["seeking", pause],
    ["seeked", play],
    ["timeupdate", sync],
    ["volumechange", sync],
    ["ratechange", sync],
    ["loadedmetadata", sync],
  ];
  handlers.forEach(([name, callback]) => video.addEventListener(name, callback));
  audio.addEventListener("error", onError);
  sync();
  play();
  return () => {
    disposed = true;
    handlers.forEach(([name, callback]) => video.removeEventListener(name, callback));
    audio.removeEventListener("error", onError);
    audio.pause();
  };
}
