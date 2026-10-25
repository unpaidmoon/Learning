import type { Schema } from "../api/client";

/** Join adjacent selected glyphs on one original-page line, never whole layers. */
export function coalesceSelectionRegions(regions: Schema<"PageRegion">[], pageAspect: number) {
  const output: Schema<"PageRegion">[] = [];
  for (const region of regions) {
    const previous = output.at(-1);
    const height = previous ? Math.min(previous.y1 - previous.y0, region.y1 - region.y0) : 0;
    const sameLine =
      previous &&
      height > 0 &&
      Math.abs(previous.y0 - region.y0) <= height * 0.2 &&
      Math.abs(previous.y1 - region.y1) <= height * 0.2;
    const gap = previous ? Math.max(region.x0 - previous.x1, previous.x0 - region.x1, 0) : Infinity;
    if (sameLine && gap <= (height / pageAspect) * 2) {
      previous.x0 = Math.min(previous.x0, region.x0);
      previous.y0 = Math.min(previous.y0, region.y0);
      previous.x1 = Math.max(previous.x1, region.x1);
      previous.y1 = Math.max(previous.y1, region.y1);
    } else output.push({ ...region });
  }
  return output;
}
