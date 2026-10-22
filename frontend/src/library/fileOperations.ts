import { useMemo, useSyncExternalStore } from "react";

export type PendingFileOperation = {
  operationId: string;
  action: string;
  path?: string;
};

const key = (courseId: string) => `learning.course-file-operation.${courseId}`;

/** One course keeps its receipt identity across tab hiding and component remounts. */
class OperationReceipt {
  private value: PendingFileOperation | null;
  private listeners = new Set<() => void>();
  constructor(private courseId: string) {
    try {
      const value = JSON.parse(localStorage.getItem(key(courseId)) || "null");
      this.value =
        value &&
        typeof value.operationId === "string" &&
        /^[A-Za-z0-9_-]{1,160}$/.test(value.operationId) &&
        typeof value.action === "string" &&
        !["rename-course", "detach", "root"].includes(value.action)
          ? value
          : null;
    } catch {
      this.value = null;
    }
  }
  snapshot = () => this.value;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  start(value: PendingFileOperation) {
    if (this.value) throw new Error("请先核对上一项文件操作（FILE_OPERATION_PENDING）");
    this.set(value);
  }
  clear(operationId: string) {
    if (this.value?.operationId === operationId) this.set(null);
  }
  private set(value: PendingFileOperation | null) {
    this.value = value;
    try {
      if (value) localStorage.setItem(key(this.courseId), JSON.stringify(value));
      else localStorage.removeItem(key(this.courseId));
    } catch {
      // The synchronous in-memory guard remains active if browser storage fails.
    }
    this.listeners.forEach((listener) => listener());
  }
}

const receipts = new Map<string, OperationReceipt>();
export function useFileOperation(courseId: string) {
  const receipt = useMemo(() => {
    if (!receipts.has(courseId)) receipts.set(courseId, new OperationReceipt(courseId));
    return receipts.get(courseId)!;
  }, [courseId]);
  const pending = useSyncExternalStore(receipt.subscribe, receipt.snapshot);
  return { pending, receipt };
}
