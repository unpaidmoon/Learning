import { describe, expect, it } from "vitest";
import { coalesceSelectionRegions } from "../src/reading/selectionRegions";

describe("逐字框收束为连续的已选行", () => {
  it("长段落减少区域数，仍保留逐行边界及输入", () => {
    const input = Array.from({ length: 600 }, (_, i) => ({
      x0: 0.1 + (i % 60) * 0.009,
      x1: 0.107 + (i % 60) * 0.009,
      y0: 0.1 + Math.floor(i / 60) * 0.04,
      y1: 0.12 + Math.floor(i / 60) * 0.04,
    }));
    const frozen = structuredClone(input),
      result = coalesceSelectionRegions(input, 0.75);
    expect(result).toHaveLength(10);
    expect(result[0]).toMatchObject({ x0: 0.1, y0: 0.1, y1: 0.12 });
    expect(input).toEqual(frozen);
  });
  it("相同行上的远列或不同基线不能合成整个页面", () => {
    const input = [
      { x0: 0.1, y0: 0.1, x1: 0.2, y1: 0.12 },
      { x0: 0.6, y0: 0.1, x1: 0.7, y1: 0.12 },
      { x0: 0.1, y0: 0.16, x1: 0.2, y1: 0.18 },
    ];
    expect(coalesceSelectionRegions(input, 0.75)).toEqual(input);
  });
  it("兼容反向字序，矩形只覆盖已选连续区域", () => {
    expect(
      coalesceSelectionRegions(
        [
          { x0: 0.2, y0: 0.1, x1: 0.22, y1: 0.12 },
          { x0: 0.17, y0: 0.1, x1: 0.19, y1: 0.12 },
        ],
        0.75,
      ),
    ).toEqual([{ x0: 0.17, y0: 0.1, x1: 0.22, y1: 0.12 }]);
  });
});
