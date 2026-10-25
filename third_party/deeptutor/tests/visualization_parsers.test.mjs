import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import test from "node:test";

// Use the project's fixed TypeScript dependency; no second frontend runtime.
const require = createRequire(new URL("../../../frontend/package.json", import.meta.url));
const typescript = require("typescript");
const source = await readFile(new URL("../web/visualizationParsers.ts", import.meta.url), "utf8");
const transpiled = typescript.transpileModule(source, {
  compilerOptions: {
    target: typescript.ScriptTarget.ES2022,
    module: typescript.ModuleKind.ES2022,
  },
}).outputText;
const helpers = await import(`data:text/javascript;base64,${Buffer.from(transpiled).toString("base64")}`);

test("original fence helper preserves JSON and Unicode payloads", () => {
  assert.equal(helpers.stripCodeFence("  ```json\n{\"title\":\"曲线\"}\n```  "), '{"title":"曲线"}');
  assert.equal(helpers.stripCodeFence("  plain  "), "plain");
});

test("original multi-SVG split returns every root without claiming sanitization", () => {
  const first = '<svg id="same"><path d="M0 0"/></svg>';
  const second = '<svg id="same"><text>二</text></svg>';
  assert.deepEqual(helpers.splitSvgBlocks(`${first}\n${second}`), [first, second]);
  assert.deepEqual(helpers.splitSvgBlocks("  text  "), ["text"]);
});

test("adapted chart parser rejects JS object literals and repairs", () => {
  assert.deepEqual(helpers.parseChartConfigStrict('```json\n{"type":"line"}\n```'), { type: "line" });
  for (const malformed of ["{type:'line'}", '{"type":"line",}', '{"value":NaN}']) {
    assert.throws(() => helpers.parseChartConfigStrict(malformed), SyntaxError);
  }
});
