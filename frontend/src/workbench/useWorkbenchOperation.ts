import { useEffect, useRef, useState } from "react";
import { api, ApiError, errorMessage } from "../api/client";
import { finishWorkbenchDraft, saveWorkbenchDraft } from "./drafts";
import { localDraft, operationId } from "./model";

/** A lost response keeps its original receipt identity; this hook never retries a POST. */
export function useWorkbenchOperation<T>(
  key: string,
  courseId: string,
  family: "practice" | "task" | "artifact" | "essay" | "path",
  onResolved: (result: T) => void,
) {
  const [saved] = useState(() => localDraft<{ operation: string }>(key));
  const [pending, setPending] = useState<string | null>(saved.value?.operation ?? null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(saved.error);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const resolve = (result: T) => {
    if (!mounted.current) return;
    onResolved(result);
    finishWorkbenchDraft(key);
    setPending(null);
    setNotice("");
  };
  const execute = async (path: string, payload: Record<string, unknown>) => {
    if (inFlight.current || busy || pending || saved.error) return;
    const operation = operationId();
    try {
      saveWorkbenchDraft(key, { operation });
    } catch (reason) {
      setNotice(errorMessage(reason));
      return;
    }
    setPending(operation);
    inFlight.current = true;
    setBusy(true);
    try {
      resolve(await api.write<T>(path, { ...payload, operation_id: operation }));
    } catch (reason) {
      if (
        reason instanceof ApiError &&
        reason.status &&
        reason.status >= 400 &&
        reason.status < 500
      ) {
        try {
          finishWorkbenchDraft(key);
          setPending(null);
        } catch (storageError) {
          setNotice(errorMessage(storageError));
          return;
        }
      }
      setNotice(errorMessage(reason));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };
  const reconcile = async () => {
    if (!pending || busy || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    try {
      const route =
        family === "practice"
          ? "practice-operations"
          : family === "essay"
            ? "essay-operations"
            : family === "path"
              ? "path-operations"
              : "operations";
      const receipt = await api.read<{ result: T }>(
        `/courses/${courseId}/learning/${route}/${pending}`,
      );
      resolve(receipt.result);
    } catch (reason) {
      setNotice(`原操作尚未核实，已保留提交身份。${errorMessage(reason)}`);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };
  return {
    execute,
    reconcile,
    pending,
    busy,
    notice,
    disabled: busy || !!pending || !!saved.error,
  };
}
