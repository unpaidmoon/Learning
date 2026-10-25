"""Actual provider graphs, then the same frontend numeric interpreter on their data."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess

from dotenv import dotenv_values
from sqlalchemy import select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import ModelSettings, VisualizationBlock
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def freeze():
    files = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        *sorted((PROJECT_ROOT / "third_party/deeptutor/src").rglob("*.py")),
        PROJECT_ROOT / "backend/uv.lock",
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "frontend/src/workbench/visualizationMath.ts",
        PROJECT_ROOT / "scripts/validation/Run-VisualizationReal.py",
        PROJECT_ROOT / ".tools/node/node.exe",
    ]
    return {
        p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in files
    }


def verify_actual_data(envelopes):
    # Fixed validation code; generated content is JSON stdin and is never executable.
    code = """
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const {calculatePlot} = await import(pathToFileURL(process.argv[1]).href);
const items = JSON.parse(readFileSync(0,'utf8'));
const results=[];
for(const data of items){
 if(data.kind!=='cartesian')continue;
 const parameters=Object.fromEntries(data.parameters.map(p=>[p.symbol,p.initial]));
 if(!Object.hasOwn(parameters,'a'))throw Error('EXPECTED_PARAMETER_A_MISSING');
 for(const a of [1,2]){
  const frame=calculatePlot({frame_id:a,data,parameters:{...parameters,a},probe_x:1});
  if(Math.abs(frame.probe.y-a)>1e-9||Math.abs(frame.probe.slope-2*a)>1e-9)throw Error('MODEL_FORMULA_ANALYTIC_MISMATCH');
  results.push({expression:data.expression,animated:!!data.animate_parameter,a,probe:frame.probe,points:frame.points.length});
 }
}
if(results.length!==4)throw Error('EXPECTED_TWO_PARAMETER_PLOTS');
console.log(JSON.stringify(results));
"""
    completed = subprocess.run(
        [
            str(PROJECT_ROOT / ".tools/node/node.exe"),
            "--experimental-strip-types",
            "--input-type=module",
            "-e",
            code,
            str(PROJECT_ROOT / "frontend/src/workbench/visualizationMath.ts"),
        ],
        input=json.dumps(envelopes, ensure_ascii=False),
        encoding="utf-8",
        capture_output=True,
        cwd=PROJECT_ROOT,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=True,
    )
    return json.loads(completed.stdout)


async def run(args):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,100}", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "status": "not_run",
        "layer": "real provider + pinned native Harness + actual frontend numeric interpreter",
        "input": "self-created quadratic text; no images, video or user assets",
        "browser_rendering": "separate synthetic browser evidence",
        "user_learning_experience": "not_run",
        "source_hashes_before": freeze(),
    }
    config = AppConfig(
        mode="test",
        harness_enabled=True,
        credential_route="api_key",
        model=args.model,
        thinking=args.thinking,
        max_output_tokens=args.max_output_tokens,
        model_timeout_seconds=args.timeout,
        data_dir=directory / "data",
        runtime_dir=directory / "runtime",
    )
    if not dotenv_values(config.secret_file).get("DEEPSEEK_API_KEY"):
        record["missing_prerequisite"] = "PRIVATE_CREDENTIAL_NOT_CONFIGURED"
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        return 2
    material = directory / "synthetic-course"
    material.mkdir()
    original = material / "quadratic.txt"
    original.write_text(
        "The function is y=a*x^2. First square the input x, then multiply by coefficient a. "
        "For a=1 and x=1, y=1. For a=2 and x=1, y=2. The slope at x=1 is 2*a. "
        "Changing a changes the opening and direction of the parabola. Use a from -2 to 2, x from -3 to 3 and y from -20 to 20. "
        "This is synthetic validation material, not user course data.",
        "utf-8",
    )
    source_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    repo = Repository(config.data_dir)
    files = CourseFileStore(repo)
    course = files.bind(None, str(material), "自造函数可视化真实模型验证")["course_id"]
    opened = files.open(course, "quadratic.txt")
    service, executor = GenerationService(repo, config), None
    try:
        await service.open()
        if not service.question_answering_ready:
            raise RuntimeError(service.execution_error or "MODEL_ROUTE_NOT_READY")
        executor = TaskExecutor(repo, service)
        await executor.open()
        task = TaskCoverage(repo).admit(
            course,
            TaskAdmission(
                operation_id="explicit_visualization",
                kind="visualization",
                coverage_mode="complete",
                instruction="根据本次唯一资料，恰好生成3个独立visualization块：一个diagram静态图说明x→平方→乘a得到y；一个cartesian参数互动图，表达式a*x^2、系数a初值1、animate_parameter=null；一个同样函数的动画图，animate_parameter=a、duration_seconds=4。后两图a范围-2到2、step=0.1、x范围-3到3、y范围-20到20、probe_x=1。中文标题与解释，三块保留实际来源，不生成脚本、URL或任意SVG。",
                sources=[
                    {
                        "kind": "document",
                        "asset_id": opened["asset_id"],
                        "source_version": opened["source_version"],
                    }
                ],
                target={"mode": "create", "title": "二次函数的图、互动与动画"},
                sources_only=True,
                settings=ModelSettings(
                    model=args.model,
                    thinking=args.thinking,
                    max_output_tokens=args.max_output_tokens,
                    timeout_seconds=args.timeout,
                ),
            ),
        )
        executor.schedule(course, task.task_id)
        await asyncio.wait_for(executor._queue.join(), timeout=args.timeout * 3 + 30)
        current = TaskStore(repo).get(course, task.task_id)
        record["task"] = current.model_dump(mode="json")
        if current.state != "completed":
            raise RuntimeError("REAL_VISUALIZATION_TASK_NOT_COMPLETE:" + current.state)
        target = TaskCoverage(repo).get(course, task.task_id).target
        artifact = ArtifactStore(repo).get(course, target.artifact_id)
        blocks = artifact.revision.body.blocks
        if len(blocks) != 3 or any(
            not isinstance(b, VisualizationBlock) for b in blocks
        ):
            raise RuntimeError("MODEL_VISUALIZATION_STRUCTURE_MISMATCH")
        data = [b.visualization.payload.data.model_dump(mode="json") for b in blocks]
        if (
            sum(d["kind"] == "diagram" for d in data) != 1
            or sum(bool(d.get("animate_parameter")) for d in data) != 1
        ):
            raise RuntimeError("MODEL_STATIC_PARAMETER_ANIMATION_MISMATCH")
        record["artifact"] = artifact.model_dump(mode="json")
        record["math_checks"] = verify_actual_data(data)
        record["status"] = "passed"
    except BaseException as error:
        record.update(status="failed", failure=f"{type(error).__name__}: {error}")
    finally:
        if executor:
            await executor.close()
        await service.close()
        record["cleanup"] = service.cleanup
        with repo.engine.connect() as conn:
            rows = conn.execute(
                select(s.task_runs.c.run_id, s.task_runs.c.task_id)
            ).all()
        runs = [
            TaskRunStore(repo).get(course, task_id, run_id).model_dump(mode="json")
            for run_id, task_id in rows
        ]
        record["runs_final"] = runs
        record["actual_outbound_attempts"] = sum(
            r["actual_outbound_attempts"] for r in runs
        )
        record["raw_usage"] = [u for r in runs for u in r["usage"]]
        if any(r["state"] == "unknown" for r in runs):
            record["status"] = "unknown"
        record["original_unchanged"] = (
            hashlib.sha256(original.read_bytes()).hexdigest() == source_hash
        )
        record["source_hashes_after"] = freeze()
        record["source_changed_during_run"] = (
            record["source_hashes_before"] != record["source_hashes_after"]
        )
        if not record["original_unchanged"] or record["source_changed_during_run"]:
            record["status"] = "failed"
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        repo.close()
    print(
        json.dumps(
            {
                "status": record["status"],
                "actual_outbound_attempts": record["actual_outbound_attempts"],
                "evidence": str(directory / "summary.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--timeout", type=float, default=180)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
