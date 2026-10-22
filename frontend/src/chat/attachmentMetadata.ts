import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { api, errorMessage } from "../api/client";
import type { Schema } from "../api/client";

type Metadata = Schema<"AttachmentMetadataResponse">;
const metadataCache = new Map<string, Metadata>();
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

export function rememberAttachment(metadata: Metadata) {
  metadataCache.set(metadata.attachment_id, metadata);
  listeners.forEach((listener) => listener());
}

export function useAttachmentMetadata(id: string) {
  const snapshot = useCallback(() => metadataCache.get(id), [id]);
  const value = useSyncExternalStore(subscribe, snapshot);
  const [error, setError] = useState("");
  const [readAttempt, setReadAttempt] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    if (value) return;
    void api
      .read<Metadata>(`/attachments/${id}/metadata`, abort.signal)
      .then((metadata) => {
        if (abort.signal.aborted) return;
        rememberAttachment(metadata);
      })
      .catch((reason) => {
        if (!abort.signal.aborted) setError(errorMessage(reason));
      });
    return () => abort.abort();
  }, [id, value, readAttempt]);
  const reload = () => {
    setError("");
    setReadAttempt((current) => current + 1);
  };
  return { value, error, reload };
}
