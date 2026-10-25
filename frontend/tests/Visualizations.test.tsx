import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import fixtures from "../../tests/fixtures/workbench/visualizations.json";
import type { Schema } from "../src/api/client";
import { calculatePlot } from "../src/workbench/visualizationMath";
import { StructuredBlockEditor } from "../src/workbench/StructuredBlockEditor";
import { VisualizationViewer } from "../src/workbench/VisualizationViewer";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

for (const fixture of fixtures)
  test(`formal Python expression tree: ${fixture.name}`, () => {
    const result = calculatePlot({
      frame_id: 7,
      data: fixture.data,
      parameters: fixture.parameters as Record<string, number>,
      probe_x: fixture.data.probe_x,
    });
    expect(result.frame_id).toBe(7);
    expect(result.points).toHaveLength(257);
    for (const key of ["y", "slope"] as const) {
      if (fixture.expected[key] === null) expect(result.probe[key]).toBeNull();
      else expect(result.probe[key]).toBeCloseTo(fixture.expected[key]!, 10);
    }
    if (fixture.name === "reciprocal_gap") expect(result.points[128]).toBeNull();
  });

test("changing a parameter changes both the curve and the reported value", () => {
  const data = fixtures[0].data;
  const one = calculatePlot({ frame_id: 1, data, parameters: { a: 1 }, probe_x: 1 });
  const two = calculatePlot({ frame_id: 2, data, parameters: { a: 2 }, probe_x: 1 });
  expect(two.probe).toEqual({ x: 1, y: 2, slope: 4 });
  expect(one.points[192]?.[1]).toBe(4);
  expect(two.points[192]?.[1]).toBe(8);
});

test("the numeric worker refuses capability-shaped nodes and unbounded workloads", () => {
  const input = { frame_id: 1, data: fixtures[0].data, parameters: { a: 1 }, probe_x: 1 };
  for (const tree of [
    { type: "call", name: "fetch", arguments: ["/api/v1/session"] },
    { type: "number", value: "<svg onload=fetch(1)>" },
    { type: "variable", name: "constructor" },
    { type: "number", value: 1, onclick: "open('/secret')" },
  ])
    expect(() =>
      calculatePlot({ ...input, data: { ...input.data, expression_tree: tree } }),
    ).toThrow("VISUALIZATION_CALCULATION_INVALID");
  expect(() =>
    calculatePlot({ ...input, data: { ...input.data, sample_count: 10000000 } }),
  ).toThrow();
});

const diagram: Extract<
  Schema<"ArtifactView">["revision"]["body"]["blocks"][number],
  { kind: "visualization" }
>["visualization"] = {
  render_type: "diagram",
  renderer: {
    native_renderer: "diagram",
    id: "learning.svg-data",
    version: "1.0.0",
    target: "native",
    entry_url: "",
  },
  schema_version: "deeptutor.visualization/v1",
  payload: {
    format: "learning.svg-data/v1",
    data: {
      kind: "diagram",
      nodes: [
        {
          node_id: "a",
          x: 20,
          y: 50,
          label: "<svg onload=fetch(1)>",
          shape: "circle",
          size: 3,
          color: "blue",
        },
        { node_id: "b", x: 80, y: 50, label: "结果", shape: "box", size: 3, color: "green" },
      ],
      edges: [{ source: "a", target: "b", label: "转换", arrow: true }],
    },
  },
  presentation: {
    title: "闭合的关系数据",
    description: "<iframe src='/secret'>",
    alt_text: "输入转换为结果",
    aspect_ratio: "4:3",
  },
  interaction: { events: [] },
  fallback: {},
};

test("diagram labels are inert text and create no iframe, script or external SVG references", () => {
  const fetch = vi.spyOn(window, "fetch");
  const { container } = render(<VisualizationViewer envelope={diagram} />);
  expect(screen.getByRole("img", { name: "输入转换为结果" })).toBeInTheDocument();
  expect(screen.getByText("<svg onload=fetch(1)>")).toBeInTheDocument();
  expect(container.querySelector("iframe,script,foreignObject,image,use")).toBeNull();
  expect(container.querySelector("[onload],[href],[src]")).toBeNull();
  expect(fetch).not.toHaveBeenCalled();
});

test("editing a formula removes the old compiled tree while retaining its source and renderer", () => {
  const onChange = vi.fn();
  const block = {
    block_id: "plot",
    kind: "visualization" as const,
    markdown: "说明",
    attribution: "source_paraphrase" as const,
    source_snapshot_ids: ["source"],
    visualization: {
      ...diagram,
      render_type: "cartesian" as const,
      renderer: { ...diagram.renderer, native_renderer: "cartesian" as const },
      payload: { ...diagram.payload, data: fixtures[0].data as Schema<"CartesianData"> },
    },
  };
  render(<StructuredBlockEditor block={block} disabled={false} onChange={onChange} />);
  fireEvent.change(screen.getByRole("textbox", { name: "函数表达式" }), {
    target: { value: "a*x^3" },
  });
  const changed = onChange.mock.calls[0][0];
  expect(changed.visualization.payload.data.expression_tree).toBeNull();
  expect(changed.visualization.payload.data.expression).toBe("a*x^3");
  expect(changed.source_snapshot_ids).toEqual(["source"]);
});
