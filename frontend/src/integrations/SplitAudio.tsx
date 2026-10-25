import { useEffect, useRef } from "react";
import type { RefObject } from "react";
import { bindSplitAudio } from "./audioSynchronization";

export function SplitAudio({
  video,
  src,
  onError,
}: {
  video: RefObject<HTMLVideoElement | null>;
  src: string;
  onError: () => void;
}) {
  const audio = useRef<HTMLAudioElement>(null);
  const callback = useRef(onError);
  useEffect(() => {
    callback.current = onError;
  }, [onError]);
  useEffect(() => {
    if (video.current && audio.current)
      return bindSplitAudio(video.current, audio.current, () => callback.current());
  }, [video, src]);
  return <audio ref={audio} src={src} preload="metadata" />;
}
