import { createPlotWorker } from "./visualizationMath";

// Four visible graphs at most. Off-screen graphs release their owned worker;
// waiting graphs keep their data/state and acquire a slot without model calls.
let active = 0;
const waiting = new Set<() => void>();

export function onPlotWorkerAvailable(listener: () => void) {
  waiting.add(listener);
  return () => {
    waiting.delete(listener);
  };
}

export function acquirePlotWorker() {
  if (active >= 4) return null;
  const worker = createPlotWorker();
  ++active;
  let released = false;
  return {
    worker,
    release() {
      if (released) return;
      released = true;
      worker.terminate();
      --active;
      queueMicrotask(() => {
        for (const notify of [...waiting]) notify();
      });
    },
  };
}
