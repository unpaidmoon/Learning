import assert from "node:assert/strict";
import {readFile,writeFile,mkdir} from "node:fs/promises";
import {createHash} from "node:crypto";
import {createRequire} from "node:module";
import {fileURLToPath} from "node:url";
import path from "node:path";

const root=path.dirname(fileURLToPath(import.meta.url));
const repository=path.resolve(root,"../../../..");
const require=createRequire(path.join(repository,"frontend/package.json"));
const typescript=require("typescript");
assert.equal(typescript.version,"5.9.3");
const modules=[
  ["DTQuiz",path.join(root,"upstream/web/lib/quiz-question-type.ts")],
  ["DTNavigation",path.join(root,"upstream/web/lib/book-reader-navigation.ts")],
  ["DTVisualization",path.join(repository,"third_party/deeptutor/web/visualizationParsers.ts")],
];
const records=[];
let bundle='"use strict";\n// DeepTutor selected original pure modules; Apache-2.0. See licenses/UPSTREAM.txt.\n';
for(const [name,sourcePath] of modules) {
  const source=await readFile(sourcePath,"utf8");
  const result=typescript.transpileModule(source,{compilerOptions:{target:typescript.ScriptTarget.ES2020,module:typescript.ModuleKind.CommonJS}});
  assert(!result.outputText.includes("require("),`Unexpected runtime import in ${name}`);
  bundle+=`\n{const exports={};\n${result.outputText}\nwindow.${name}=Object.freeze(exports);}\n`;
  records.push({global:name,source:path.relative(repository,sourcePath).replaceAll("\\","/"),source_sha256:createHash("sha256").update(source).digest("hex"),transpiled_sha256:createHash("sha256").update(result.outputText).digest("hex")});
}
await mkdir(path.join(root,"package/runtime"),{recursive:true});
await writeFile(path.join(root,"package/runtime/upstream.js"),bundle,"utf8");
await mkdir(path.join(root,"evidence"),{recursive:true});
await writeFile(path.join(root,"evidence/browser-module-build.json"),JSON.stringify({typescript:typescript.version,modules:records,bundle_sha256:createHash("sha256").update(bundle).digest("hex")},null,2)+"\n","utf8");
process.stdout.write(JSON.stringify({typescript:typescript.version,pure_modules:records.length,bundle_sha256:createHash("sha256").update(bundle).digest("hex")})+"\n");
