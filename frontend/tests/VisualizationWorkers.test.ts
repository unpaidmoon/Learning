import { expect, test, vi } from "vitest";
import { acquirePlotWorker, onPlotWorkerAvailable } from "../src/workbench/visualizationWorkers";

vi.mock("../src/workbench/visualizationMath", () => ({
  createPlotWorker: () => ({ terminate: vi.fn() }),
}));

test("visible graphs share four slots and release each worker exactly once", async () => {
  const leases = Array.from({ length: 4 }, () => acquirePlotWorker()!);
  const available = vi.fn();
  const stop = onPlotWorkerAvailable(available);
  try {
    expect(leases.every(Boolean)).toBe(true);
    expect(acquirePlotWorker()).toBeNull();
    leases[0].release();
    leases[0].release();
    await Promise.resolve();
    expect(available).toHaveBeenCalledTimes(1);
    expect(leases[0].worker.terminate).toHaveBeenCalledTimes(1);
    const next = acquirePlotWorker();
    expect(next).not.toBeNull();
    if (next) leases.push(next);
    expect(acquirePlotWorker()).toBeNull();
  } finally {
    stop();
    for (const lease of leases) lease.release();
    await Promise.resolve();
  }
});
