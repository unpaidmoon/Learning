// Existing playback behavior, compiled against the Harness React instance.
import { useEffect, useImperativeHandle, useLayoutEffect, useRef } from "react";
import type { ComponentProps, Ref } from "react";
import { api } from "../../frontend/src/api/client";
import { VideoWorkspace as ExistingVideoWorkspace } from "../../frontend/src/media/VideoWorkspace";
import type { VideoWorkspaceHandle } from "../../frontend/src/media/VideoWorkspace";

export const initialize = () => api.connect();

export async function interactionPriority(active: boolean, leaseId = "desktop-capture") {
  await api.write("/video-previews/priority", {
    lease_id: leaseId, active, kind: "interaction", ttl_ms: 3000,
  });
}

type Props = Omit<ComponentProps<typeof ExistingVideoWorkspace>, "ref"> & {
  workspaceHandleRef?: Ref<VideoWorkspaceHandle>;
};

/** The ref adapter is explicit because the pinned Harness shell uses React 18. */
export function VideoWorkspace({ workspaceHandleRef, ...props }: Props) {
  const internal = useRef<VideoWorkspaceHandle>(null);
  const lease = useRef(`playback-${crypto.randomUUID()}`);
  useImperativeHandle(workspaceHandleRef, () => ({
    openAsset: (...args) => internal.current?.openAsset(...args) ?? Promise.resolve(),
    openSource: (...args) => internal.current?.openSource(...args) ?? Promise.resolve(),
    resumeFullscreen: () => internal.current?.resumeFullscreen() ?? Promise.resolve(),
    toggleFullscreen: () => internal.current?.toggleFullscreen() ?? Promise.resolve(),
    savePosition: () => internal.current?.savePosition() ?? Promise.resolve(),
    pauseForCapture: () => internal.current?.pauseForCapture(),
    getElement: () => internal.current?.getElement() ?? null,
    closeAsset: () => internal.current?.closeAsset() ?? Promise.resolve(),
  }), []);
  useLayoutEffect(() => () => {
    // Layout teardown precedes ref detachment: retain the actual media element
    // long enough to pause and queue its last accepted position.
    const handle = internal.current;
    handle?.getElement()?.pause();
    void handle?.savePosition().catch(() => {});
  }, []);
  useEffect(() => {
    const renew = () => {
      const video = internal.current?.getElement();
      void api.write("/video-previews/priority", {
        lease_id: lease.current, active: !!video && !video.paused && props.active !== false,
        kind: "playback", ttl_ms: 3000,
      }).catch(() => {});
    };
    renew();
    const timer = window.setInterval(renew, 2000);
    return () => {
      window.clearInterval(timer);
      void api.write("/video-previews/priority", {
        lease_id: lease.current, active: false, kind: "playback", ttl_ms: 3000,
      }, { keepalive: true }).catch(() => {});
    };
  }, [props.active]);
  // The source build changes only the reserved React-19 `ref` prop spelling to
  // workspaceHandleRef. Playback logic and its public imperative methods stay shared.
  return <ExistingVideoWorkspace {...props} {...{ workspaceHandleRef: internal }} />;
}
