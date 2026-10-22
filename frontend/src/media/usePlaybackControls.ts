import { useEffect, useRef, useState } from "react";
import type { RefObject } from "react";

export function isPlaybackInput(target: EventTarget | null) {
  return (
    target instanceof Element &&
    Boolean(
      target.closest(
        'input,textarea,select,button,a,[contenteditable="true"],[role="textbox"],.chat-workspace,[data-player-controls]',
      ),
    )
  );
}

/** Bind only the active video. Input, chat scrolling and screenshot gestures retain their keys. */
export function usePlaybackControls({
  video,
  stage,
  active,
  identity,
  toggleFullscreen,
}: {
  video: RefObject<HTMLVideoElement | null>;
  stage: RefObject<HTMLDivElement | null>;
  active: boolean;
  identity?: string;
  toggleFullscreen: () => Promise<void>;
}) {
  const [elapsed, setElapsed] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [rate, setRate] = useState(1);
  const callbacks = useRef({ toggleFullscreen });
  const clickTimer = useRef<number | undefined>(undefined);
  const seekCommand = useRef<(seconds: number) => void>(() => {});

  useEffect(() => {
    callbacks.current = { toggleFullscreen };
  }, [toggleFullscreen]);

  useEffect(() => {
    const player = video.current;
    const frame = stage.current;
    if (!player || !frame || !active || !identity) return;
    let holdTimer: number | undefined;
    let rightDown = false;
    let previousRate: number | undefined;
    const seek = (delta: number) => {
      player.currentTime = Math.min(
        Number.isFinite(player.duration) ? player.duration : Number.MAX_SAFE_INTEGER,
        Math.max(0, player.currentTime + delta),
      );
    };
    const releaseRight = (seekOnRelease: boolean) => {
      if (!rightDown) return;
      window.clearTimeout(holdTimer);
      rightDown = false;
      if (previousRate !== undefined) {
        player.playbackRate = previousRate;
        previousRate = undefined;
      } else if (seekOnRelease) seek(2);
    };
    const keydown = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        event.isComposing ||
        event.ctrlKey ||
        event.altKey ||
        event.metaKey ||
        isPlaybackInput(event.target) ||
        document.querySelector("dialog[open],[data-workspace-capture-overlay]")
      )
        return;
      if (event.code === "Space" || event.key === " ") {
        event.preventDefault();
        if (!event.repeat) {
          if (player.paused) void player.play().catch(() => {});
          else player.pause();
        }
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        seek(-2);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        if (!rightDown) {
          rightDown = true;
          holdTimer = window.setTimeout(() => {
            previousRate = player.playbackRate;
            player.playbackRate = 2;
          }, 400);
        }
      } else if (event.key.toLowerCase() === "m") {
        event.preventDefault();
        if (!event.repeat) player.muted = !player.muted;
      }
    };
    const keyup = (event: KeyboardEvent) => {
      if (event.key !== "ArrowRight") return;
      const continueShortcut =
        !isPlaybackInput(event.target) &&
        !document.querySelector("dialog[open],[data-workspace-capture-overlay]");
      if (rightDown && continueShortcut) event.preventDefault();
      releaseRight(continueShortcut);
    };
    const blur = () => releaseRight(false);
    const wheel = (event: WheelEvent) => {
      const target = event.target;
      if (!(target instanceof Element) || !event.deltaY) return;
      const volumeTarget = target.closest("[data-player-volume]");
      if (!volumeTarget && !document.fullscreenElement) return;
      event.preventDefault();
      player.volume = Math.max(0, Math.min(1, player.volume + (event.deltaY < 0 ? 0.05 : -0.05)));
      if (player.volume > 0) player.muted = false;
    };
    const time = () => {
      setElapsed(player.currentTime);
      setDuration(Number.isFinite(player.duration) ? player.duration : 0);
    };
    const volumeChange = () => {
      setVolume(player.volume);
      setMuted(player.muted);
    };
    const rateChange = () => setRate(player.playbackRate);
    const click = (event: MouseEvent) => {
      if (isPlaybackInput(event.target)) return;
      window.clearTimeout(clickTimer.current);
      if (event.detail > 1) return;
      frame.focus({ preventScroll: true });
      clickTimer.current = window.setTimeout(() => {
        if (player.paused) void player.play().catch(() => {});
        else player.pause();
      }, 220);
    };
    const doubleClick = (event: MouseEvent) => {
      if (isPlaybackInput(event.target)) return;
      window.clearTimeout(clickTimer.current);
      void callbacks.current.toggleFullscreen();
    };
    seekCommand.current = (seconds: number) => {
      if (!Number.isFinite(seconds)) return;
      const end = Number.isFinite(player.duration) ? player.duration : Number.MAX_SAFE_INTEGER;
      const target = Math.min(end, Math.max(0, seconds));
      player.currentTime = target;
      // Commit the thumb in this same event; delayed timeupdate must not snap it back.
      setElapsed(target);
    };
    window.addEventListener("keydown", keydown);
    window.addEventListener("keyup", keyup);
    window.addEventListener("blur", blur);
    frame.addEventListener("wheel", wheel, { passive: false });
    frame.addEventListener("click", click);
    frame.addEventListener("dblclick", doubleClick);
    player.addEventListener("timeupdate", time);
    player.addEventListener("loadedmetadata", time);
    player.addEventListener("durationchange", time);
    player.addEventListener("volumechange", volumeChange);
    player.addEventListener("ratechange", rateChange);
    return () => {
      seekCommand.current = () => {};
      releaseRight(false);
      window.clearTimeout(clickTimer.current);
      window.removeEventListener("keydown", keydown);
      window.removeEventListener("keyup", keyup);
      window.removeEventListener("blur", blur);
      frame.removeEventListener("wheel", wheel);
      frame.removeEventListener("click", click);
      frame.removeEventListener("dblclick", doubleClick);
      player.removeEventListener("timeupdate", time);
      player.removeEventListener("loadedmetadata", time);
      player.removeEventListener("durationchange", time);
      player.removeEventListener("volumechange", volumeChange);
      player.removeEventListener("ratechange", rateChange);
    };
  }, [active, identity, stage, video]);

  const seekTo = (seconds: number) => seekCommand.current(seconds);

  return { elapsed, duration, volume, muted, rate, seekTo };
}
