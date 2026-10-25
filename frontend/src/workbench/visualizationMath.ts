/** A bounded numeric interpreter. This exact function also runs in the owned worker. */
export function calculatePlot(request: {
  frame_id: number;
  data: {
    expression_tree?: Record<string, unknown> | null;
    x_range: { min: number; max: number };
    sample_count?: number;
  };
  parameters: Record<string, number>;
  probe_x: number;
}) {
  // Keep all implementation inside this function: the worker receives its fixed
  // build-time source, never a generated expression, script, callback or import.
  type Node = Record<string, unknown>;
  type Value = { y: number; dy: number };
  const invalid = () => {
    throw new Error("VISUALIZATION_CALCULATION_INVALID");
  };
  const numeric = (value: unknown): value is number =>
    typeof value === "number" && Number.isFinite(value);
  const variables = request.parameters;
  if (
    !variables ||
    Object.keys(variables).length > 8 ||
    Object.entries(variables).some(
      ([name, value]) =>
        !/^[A-Za-z][A-Za-z0-9_]{0,15}$/.test(name) ||
        name === "x" ||
        !numeric(value) ||
        Math.abs(value) > 10000,
    )
  )
    invalid();
  let count = 0;
  const checkTree = (raw: unknown, depth = 0): void => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw) || ++count > 128 || depth > 24)
      invalid();
    const node = raw as Node;
    let keys: string[];
    if (node.type === "number") {
      keys = ["type", "value"];
      if (!numeric(node.value) || Math.abs(node.value) > 1000000) invalid();
    } else if (node.type === "variable") {
      keys = ["type", "name"];
      if (
        typeof node.name !== "string" ||
        (node.name !== "x" && !Object.hasOwn(variables, node.name))
      )
        invalid();
    } else if (node.type === "binary") {
      keys = ["type", "op", "left", "right"];
      if (!["add", "sub", "mul", "div", "pow"].includes(String(node.op))) invalid();
      checkTree(node.left, depth + 1);
      checkTree(node.right, depth + 1);
    } else if (node.type === "unary") {
      keys = ["type", "op", "operand"];
      if (
        !["positive", "negative", "sin", "cos", "tan", "exp", "log", "sqrt", "abs"].includes(
          String(node.op),
        )
      )
        invalid();
      checkTree(node.operand, depth + 1);
    } else return invalid();
    if (Object.keys(node).some((key) => !keys.includes(key))) invalid();
  };
  const tree = request.data.expression_tree;
  checkTree(tree);
  const range = request.data.x_range;
  const samples = request.data.sample_count ?? 256;
  if (
    !numeric(range.min) ||
    !numeric(range.max) ||
    range.max - range.min < 0.0001 ||
    Math.max(Math.abs(range.min), Math.abs(range.max)) > 10000 ||
    !Number.isInteger(samples) ||
    samples < 32 ||
    samples > 512 ||
    !numeric(request.probe_x) ||
    request.probe_x < range.min ||
    request.probe_x > range.max
  )
    invalid();
  const evaluate = (node: Node, x: number): Value => {
    if (node.type === "number") return { y: node.value as number, dy: 0 };
    if (node.type === "variable")
      return node.name === "x" ? { y: x, dy: 1 } : { y: variables[node.name as string], dy: 0 };
    if (node.type === "unary") {
      const a = evaluate(node.operand as Node, x);
      switch (node.op) {
        case "positive":
          return a;
        case "negative":
          return { y: -a.y, dy: -a.dy };
        case "sin":
          return { y: Math.sin(a.y), dy: Math.cos(a.y) * a.dy };
        case "cos":
          return { y: Math.cos(a.y), dy: -Math.sin(a.y) * a.dy };
        case "tan":
          return Math.abs(Math.cos(a.y)) < 1e-9
            ? { y: NaN, dy: NaN }
            : { y: Math.tan(a.y), dy: a.dy / Math.cos(a.y) ** 2 };
        case "exp": {
          const y = Math.exp(a.y);
          return { y, dy: y * a.dy };
        }
        case "log":
          return { y: a.y > 0 ? Math.log(a.y) : NaN, dy: a.y > 0 ? a.dy / a.y : NaN };
        case "sqrt":
          return { y: Math.sqrt(a.y), dy: a.dy / (2 * Math.sqrt(a.y)) };
        case "abs":
          return { y: Math.abs(a.y), dy: a.y === 0 ? NaN : Math.sign(a.y) * a.dy };
      }
    }
    const a = evaluate(node.left as Node, x),
      b = evaluate(node.right as Node, x);
    switch (node.op) {
      case "add":
        return { y: a.y + b.y, dy: a.dy + b.dy };
      case "sub":
        return { y: a.y - b.y, dy: a.dy - b.dy };
      case "mul":
        return { y: a.y * b.y, dy: a.dy * b.y + a.y * b.dy };
      case "div":
        return { y: a.y / b.y, dy: (a.dy * b.y - a.y * b.dy) / (b.y * b.y) };
      case "pow": {
        const y = Math.pow(a.y, b.y);
        const dy =
          b.dy === 0
            ? b.y === 0
              ? 0
              : b.y * Math.pow(a.y, b.y - 1) * a.dy
            : a.y > 0
              ? y * (b.dy * Math.log(a.y) + (b.y * a.dy) / a.y)
              : NaN;
        return { y, dy };
      }
    }
    return invalid();
  };
  const bounded = (value: number) => Number.isFinite(value) && Math.abs(value) <= 1e12;
  const points: ([number, number] | null)[] = [];
  for (let i = 0; i <= samples; i++) {
    const x = range.min + ((range.max - range.min) * i) / samples;
    const value = evaluate(tree as Node, x);
    points.push(bounded(value.y) ? [x, value.y] : null);
  }
  const result = evaluate(tree as Node, request.probe_x);
  return {
    frame_id: request.frame_id,
    points,
    probe: {
      x: request.probe_x,
      y: bounded(result.y) ? result.y : null,
      slope: bounded(result.dy) ? result.dy : null,
    },
  };
}

export type PlotFrame = ReturnType<typeof calculatePlot>;

export function createPlotWorker() {
  const source =
    "self.onmessage=function(event){try{const result=(" +
    calculatePlot.toString() +
    ")(event.data);self.postMessage({ok:true,result});}catch(error){self.postMessage({ok:false,code:'VISUALIZATION_CALCULATION_INVALID'});}};";
  const url = URL.createObjectURL(new Blob([source], { type: "application/javascript" }));
  try {
    return new Worker(url);
  } finally {
    URL.revokeObjectURL(url);
  }
}
