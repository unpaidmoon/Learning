/** Local view intent only; it never resolves, imports, or sends a chat. */
export const INTEGRATIONS_CHANGED = "learning:integrations-changed";

let next = 0;
let request: { courseId: string; revision: number } | null = null;
const listeners = new Set<() => void>();

export function requestBilibiliImport(courseId: string) {
  request = { courseId, revision: ++next };
  listeners.forEach((listener) => listener());
}

export function bilibiliOpenRequest(courseId: string) {
  return request?.courseId === courseId ? request.revision : 0;
}

export function closeBilibiliImport(courseId: string) {
  if (request?.courseId !== courseId) return;
  request = null;
  listeners.forEach((listener) => listener());
}

export function subscribeBilibiliUi(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
