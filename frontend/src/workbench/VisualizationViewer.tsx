import { useEffect, useId, useRef, useState } from "react";
import type { Schema } from "../api/client";
import { Button } from "../components/ui/button";
import type { PlotFrame } from "./visualizationMath";
import { acquirePlotWorker, onPlotWorkerAvailable } from "./visualizationWorkers";
import "./visualization.css";

type Envelope = Extract<
  Schema<"ArtifactView">["revision"]["body"]["blocks"][number],
  { kind: "visualization" }
>["visualization"];
type Plot = Schema<"CartesianData">;
const colors = {
  blue: "#2563eb",
  orange: "#c86b13",
  green: "#15803d",
  gray: "#64748b",
  red: "#c24145",
};
const display = (value: number | null) =>
  value === null ? "无可用数值" : Number(value.toPrecision(5)).toString();

export function VisualizationViewer({ envelope }: { envelope: Envelope }) {
  const data = envelope.payload.data;
  return (
    <figure
      className="learning-visualization"
      aria-label={envelope.presentation?.title || "学习图形"}
    >
      {envelope.presentation?.title && <figcaption>{envelope.presentation.title}</figcaption>}
      {envelope.presentation?.description && <p>{envelope.presentation.description}</p>}
      {data.kind === "cartesian" ? (
        <ParameterPlot
          key={JSON.stringify(data)}
          data={data}
          alt={envelope.presentation?.alt_text || "函数与参数图"}
        />
      ) : data.kind === "diagram" ? (
        <Diagram data={data} alt={envelope.presentation?.alt_text || "概念关系示意图"} />
      ) : (
        <p>无法识别此图形的数据格式。</p>
      )}
    </figure>
  );
}

function ParameterPlot({ data, alt }: { data: Plot; alt: string }) {
  const [parameters, setParameters] = useState(() =>
    Object.fromEntries((data.parameters ?? []).map((p) => [p.symbol, p.initial])),
  );
  const [probeX, setProbeX] = useState(data.probe_x ?? 0);
  const [frame, setFrame] = useState<PlotFrame | null>(null);
  const [notice, setNotice] = useState("");
  const [playing, setPlaying] = useState(false);
  const region = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(false);
  const desired = useRef(0);
  const send = useRef<() => void>(() => {});
  const values = useRef({ parameters, probeX });
  useEffect(() => {
    let inViewport = false;
    const sync = () => setActive(inViewport && document.visibilityState !== "hidden");
    if (typeof IntersectionObserver === "undefined") {
      queueMicrotask(() => {
        inViewport = true;
        sync();
      });
      return;
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        inViewport = entry.isIntersecting;
        sync();
      },
      { rootMargin: "120px" },
    );
    if (region.current) observer.observe(region.current);
    document.addEventListener("visibilitychange", sync);
    return () => {
      observer.disconnect();
      document.removeEventListener("visibilitychange", sync);
    };
  }, []);
  useEffect(() => {
    if (!active) return;
    let worker: Worker;
    let lease: ReturnType<typeof acquirePlotWorker> = null;
    let removeWait = () => {};
    let live = true,
      busy = false;
    const dispatch = () => {
      if (busy || !live) return;
      busy = true;
      worker.postMessage({
        frame_id: desired.current,
        data,
        parameters: values.current.parameters,
        probe_x: values.current.probeX,
      });
    };
    const start = () => {
      try {
        if (!live || lease) return;
        const claimed = acquirePlotWorker();
        if (!claimed) {
          removeWait = onPlotWorkerAvailable(start);
          return;
        }
        removeWait();
        lease = claimed;
        worker = claimed.worker;
        worker.onmessage = (event: MessageEvent<{ ok: boolean; result?: PlotFrame }>) => {
          if (!live) return;
          busy = false;
          if (!event.data.ok || !event.data.result) {
            setNotice("VISUALIZATION_CALCULATION_INVALID：图形数据无法计算，请核对表达式。");
            return;
          }
          if (event.data.result.frame_id === desired.current) setFrame(event.data.result);
          else dispatch();
        };
        worker.onerror = () => {
          if (live) {
            setNotice("VISUALIZATION_WORKER_FAILED：图形计算已停止，请重新打开。");
            send.current = () => {};
            lease?.release();
          }
        };
        send.current = () => {
          ++desired.current;
          dispatch();
        };
        send.current();
      } catch {
        queueMicrotask(() => {
          if (live) setNotice("VISUALIZATION_WORKER_UNAVAILABLE：当前窗口不能启动图形计算。");
        });
      }
    };
    start();
    return () => {
      live = false;
      send.current = () => {};
      removeWait();
      lease?.release();
    };
  }, [data, active]);
  useEffect(() => {
    if (!playing || !active || !data.animate_parameter) return;
    const parameter = (data.parameters ?? []).find((p) => p.symbol === data.animate_parameter);
    if (!parameter) return;
    const start = performance.now();
    const span = parameter.max - parameter.min;
    const fraction = Math.max(
      0,
      Math.min(1, (values.current.parameters[parameter.symbol] - parameter.min) / span),
    );
    const phase = Math.acos(1 - 2 * fraction) / (2 * Math.PI);
    let id = 0,
      last = -Infinity;
    const tick = (time: number) => {
      if (time - last >= 1000 / 30) {
        last = time;
        const position =
          0.5 -
          0.5 *
            Math.cos(
              2 * Math.PI * (phase + (time - start) / ((data.duration_seconds ?? 8) * 1000)),
            );
        const next = {
          ...values.current.parameters,
          [parameter.symbol]: parameter.min + span * position,
        };
        values.current = { ...values.current, parameters: next };
        setParameters(next);
        send.current();
      }
      id = requestAnimationFrame(tick);
    };
    id = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(id);
  }, [playing, data, active]);
  const updateParameter = (symbol: string, value: number) => {
    setPlaying(false);
    const next = { ...values.current.parameters, [symbol]: value };
    values.current = { ...values.current, parameters: next };
    setParameters(next);
    send.current();
  };
  const plotX = (x: number) =>
    50 + (550 * (x - data.x_range.min)) / (data.x_range.max - data.x_range.min);
  const plotY = (y: number) =>
    360 - (320 * (y - data.y_range.min)) / (data.y_range.max - data.y_range.min);
  let path = "",
    connected = false;
  for (const point of frame?.points ?? []) {
    if (!point || Math.abs(plotY(point[1])) > 2000) {
      connected = false;
      continue;
    }
    path += `${connected ? "L" : "M"}${plotX(point[0]).toFixed(3)},${plotY(point[1]).toFixed(3)}`;
    connected = true;
  }
  const clip = useId().replaceAll(":", "");
  const probe = frame?.probe;
  return (
    <div ref={region}>
      <p className="visualization-equation">y = {data.expression}</p>
      <svg
        role="img"
        aria-label={alt}
        viewBox="0 0 640 410"
        data-worker-frame={frame?.frame_id ?? 0}
      >
        <defs>
          <clipPath id={clip}>
            <rect x="50" y="40" width="550" height="320" />
          </clipPath>
        </defs>
        <rect
          x="50"
          y="40"
          width="550"
          height="320"
          fill="none"
          stroke="currentColor"
          opacity="0.3"
        />
        {data.x_range.min <= 0 && data.x_range.max >= 0 && (
          <line x1={plotX(0)} x2={plotX(0)} y1="40" y2="360" stroke="currentColor" opacity="0.25" />
        )}
        {data.y_range.min <= 0 && data.y_range.max >= 0 && (
          <line x1="50" x2="600" y1={plotY(0)} y2={plotY(0)} stroke="currentColor" opacity="0.25" />
        )}
        <text x="50" y="388">
          {data.x_range.min}
        </text>
        <text x="590" y="388">
          {data.x_range.max}
        </text>
        <text x="8" y="48">
          {data.y_range.max}
        </text>
        <text x="8" y="360">
          {data.y_range.min}
        </text>
        <text x="610" y="370">
          x
        </text>
        <text x="35" y="25">
          y
        </text>
        <g clipPath={`url(#${clip})`}>
          <path
            d={path}
            fill="none"
            stroke={colors.blue}
            strokeWidth="2.5"
            data-testid="function-curve"
          />
          {probe && probe.y !== null && (
            <circle cx={plotX(probe.x)} cy={plotY(probe.y)} r="5" fill={colors.orange} />
          )}
        </g>
      </svg>
      <div className="visualization-controls">
        {(data.parameters ?? []).map((p) => (
          <label key={p.symbol}>
            {p.label}：{display(parameters[p.symbol])}
            <input
              aria-label={p.label}
              type="range"
              min={p.min}
              max={p.max}
              step={p.step}
              value={parameters[p.symbol]}
              onChange={(e) => updateParameter(p.symbol, Number(e.target.value))}
            />
          </label>
        ))}
        <label>
          观察位置 x：{display(probeX)}
          <input
            aria-label="观察位置 x"
            type="range"
            min={data.x_range.min}
            max={data.x_range.max}
            step={(data.x_range.max - data.x_range.min) / 1000}
            value={probeX}
            onChange={(e) => {
              const value = Number(e.target.value);
              values.current = { ...values.current, probeX: value };
              setProbeX(value);
              send.current();
            }}
          />
        </label>
        {data.animate_parameter && (
          <Button onClick={() => setPlaying((value) => !value)}>
            {playing ? "暂停动画" : "播放动画"}
          </Button>
        )}
        <Button
          variant="ghost"
          onClick={() => {
            setPlaying(false);
            const initial = Object.fromEntries(
              (data.parameters ?? []).map((p) => [p.symbol, p.initial]),
            );
            values.current = { parameters: initial, probeX: data.probe_x ?? 0 };
            setParameters(initial);
            setProbeX(data.probe_x ?? 0);
            send.current();
          }}
        >
          恢复图形初值
        </Button>
      </div>
      {probe && (
        <p aria-live="off">
          f({display(probe.x)}) = {display(probe.y)}；此处斜率 = {display(probe.slope)}
        </p>
      )}
      <small>曲线为所示区间的有界数值采样；无定义或溢出的采样点留空。</small>
      {notice && <p role="status">{notice}</p>}
    </div>
  );
}

function Diagram({ data, alt }: { data: Schema<"DiagramData">; alt: string }) {
  const arrow = useId().replaceAll(":", "");
  const nodes = new Map(data.nodes.map((n) => [n.node_id, n]));
  const x = (v: number) => 45 + v * 5.5,
    y = (v: number) => 40 + v * 3.2;
  return (
    <svg role="img" aria-label={alt} viewBox="0 0 640 410">
      <defs>
        <marker id={arrow} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
          <path d="M0,0L8,4L0,8" fill="none" stroke="currentColor" />
        </marker>
      </defs>
      {(data.edges ?? []).map((edge, index) => {
        const a = nodes.get(edge.source),
          b = nodes.get(edge.target);
        if (!a || !b) return null;
        const dx = x(b.x) - x(a.x),
          dy = y(b.y) - y(a.y),
          distance = Math.hypot(dx, dy);
        const radius = (n: typeof a) =>
          n.shape === "box" ? (n.size ?? 3) * 5 : n.shape === "circle" ? (n.size ?? 3) * 4 : 5;
        const from = Math.min(0.45, radius(a) / Math.max(1, distance));
        const to = Math.min(0.45, (radius(b) + 3) / Math.max(1, distance));
        return (
          <g key={index}>
            <line
              x1={x(a.x) + dx * from}
              y1={y(a.y) + dy * from}
              x2={x(b.x) - dx * to}
              y2={y(b.y) - dy * to}
              stroke="currentColor"
              markerEnd={edge.arrow ? `url(#${arrow})` : undefined}
            />
            <text x={(x(a.x) + x(b.x)) / 2} y={(y(a.y) + y(b.y)) / 2 - 10} textAnchor="middle">
              {edge.label}
            </text>
          </g>
        );
      })}
      {data.nodes.map((n) => (
        <g key={n.node_id}>
          {n.shape === "box" ? (
            <rect
              x={x(n.x) - (n.size ?? 3) * 4}
              y={y(n.y) - (n.size ?? 3) * 3}
              width={(n.size ?? 3) * 8}
              height={(n.size ?? 3) * 6}
              rx="4"
              fill="var(--visualization-surface, white)"
              stroke={colors[n.color ?? "blue"]}
            />
          ) : (
            <circle
              cx={x(n.x)}
              cy={y(n.y)}
              r={n.shape === "circle" ? (n.size ?? 3) * 4 : 4}
              fill={
                n.shape === "circle"
                  ? "var(--visualization-surface, white)"
                  : colors[n.color ?? "blue"]
              }
              stroke={colors[n.color ?? "blue"]}
            />
          )}
          <text x={x(n.x)} y={y(n.y) + (n.size ?? 3) * 4 + 18} textAnchor="middle">
            {n.label}
          </text>
        </g>
      ))}
    </svg>
  );
}
