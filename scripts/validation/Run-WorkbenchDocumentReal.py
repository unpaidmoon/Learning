"""Real provider execution over generated PDF fixtures, in an owned runtime."""

import argparse
import asyncio
import hashlib
import json
import re
import runpy

from dotenv import dotenv_values

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import ModelSettings
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def freeze_sources():
    paths = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app/workbench").glob("*.py")),
        *sorted((PROJECT_ROOT / "backend/src/learning_app/execution").glob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        PROJECT_ROOT / "backend/src/learning_app/service.py",
        PROJECT_ROOT / "harness/runtime.lock.json",
    ]
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


async def run(args):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,100}", args.run_id):
        raise ValueError("invalid owned run identity")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "layer": "real_provider_fixed_harness",
        "input": "locally_generated_native_and_scanned_pdf_only",
        "result": "not_run",
        "sources_before": freeze_sources(),
        "cases": [],
        "user_learning_experience": "not_run",
        "require_native_tools": args.require_native_tools,
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
        runtime_dir=directory / "app",
    )
    if not dotenv_values(config.secret_file).get("DEEPSEEK_API_KEY"):
        record["missing_prerequisite"] = "PRIVATE_CREDENTIAL_NOT_CONFIGURED"
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        return 2
    materials = directory / "synthetic-course"
    materials.mkdir()
    fixtures = runpy.run_path(
        str(PROJECT_ROOT / "tests/fixtures/workbench/generate.py")
    )
    fixtures["reading_pdf"](materials / "native.pdf", 1)
    fixtures["scanned_pdf"](materials / "scanned.pdf")
    original_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in materials.iterdir()
    }
    repo = Repository(config.data_dir)
    files = CourseFileStore(repo)
    course_id = files.bind(None, str(materials), "合成PDF真实模型验证")["course_id"]
    sources = []
    for name in ("native.pdf", "scanned.pdf"):
        asset = files.open(course_id, name)
        sources.append(
            {
                "kind": "document",
                "asset_id": asset["asset_id"],
                "source_version": asset["source_version"],
            }
        )
    service, executor = GenerationService(repo, config), None
    try:
        await service.open()
        if not service.question_answering_ready:
            record["missing_prerequisite"] = (
                service.execution_error or "MODEL_ROUTE_NOT_READY"
            )
            return 2
        executor = TaskExecutor(repo, service)
        await executor.open()
        task = TaskCoverage(repo).admit(
            course_id,
            TaskAdmission(
                operation_id="explicit_fixture_documents",
                kind="handout",
                instruction="分别完整整理两份合成PDF的实际内容，用中文讲义，保留扫描页英语原文和数字。不补资料外事实。每个来源单位在相应内容块列出实际snapshot_id。"
                + (
                    "本次明确要求先使用read_source_handle读取已提供文字快照（handle_id用snapshot_id，source_version用snapshot_hash），再使用stage_result暂存完整讲义JSON；不得用普通聊天输出代替暂存。"
                    if args.require_native_tools
                    else ""
                ),
                coverage_mode="complete",
                sources=sources,
                target={"mode": "create", "title": "两份合成PDF讲义"},
                sources_only=True,
                settings=ModelSettings(
                    model=args.model,
                    thinking=args.thinking,
                    max_output_tokens=args.max_output_tokens,
                    timeout_seconds=args.timeout,
                ),
            ),
        )
        executor.schedule(course_id, task.task_id)
        await asyncio.wait_for(executor._queue.join(), timeout=args.timeout + 90)
        task = TaskStore(repo).get(course_id, task.task_id)
        coverage = TaskCoverage(repo).get(course_id, task.task_id)
        runs = [
            TaskRunStore(repo)
            .get(course_id, task.task_id, unit.run_id)
            .model_dump(mode="json")
            for unit in task.units
            if unit.run_id
        ]
        artifacts = ArtifactStore(repo).list(course_id)
        text = "\n".join(
            block.markdown
            for artifact in artifacts
            for block in artifact.revision.body.blocks
        )
        checks = {
            "task_completed": task.state == "completed",
            "one_work": len(task.units) == 1,
            "one_artifact": len(artifacts) == 1,
            "all_leaves_processed": sum(
                source.processed_leaf_count for source in coverage.sources
            )
            == 2,
            "scan_title_retained": "LEARNING WORKBENCH" in text.upper(),
            "scan_digits_retained": "12345" in text,
            "originals_unchanged": original_hashes
            == {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in materials.iterdir()
            },
        }
        native = service.execution.native_tools
        record["native_business_calls"] = list(native.registry.bindings.values())
        if args.require_native_tools:
            names = {call["tool_name"] for call in record["native_business_calls"]}
            checks["native_read_and_stage"] = {
                "read_source_handle",
                "stage_result",
            }.issubset(names)
            checks["native_lease_released"] = all(
                run["run_id"] in native.registry.released for run in runs
            )
        record.update(
            task=task.model_dump(mode="json"),
            coverage=coverage.model_dump(mode="json"),
            runs=runs,
            checks=checks,
            actual_outbound_attempts=sum(
                run["actual_outbound_attempts"] for run in runs
            ),
            result="passed" if all(checks.values()) else "failed",
        )
        record["image_proofs"] = [
            proof
            for unit in task.units
            if unit.run_id
            for proof in TaskRunStore(repo).internal(unit.run_id)[
                "request_image_proofs"
            ]
        ]
        record["boundary_record_summaries"] = [
            {
                key: row.get(key)
                for key in (
                    "request_id",
                    "generation_id",
                    "model",
                    "status",
                    "images",
                    "actual_outbound_attempt",
                )
            }
            for row in service.execution.boundary.records
        ]
        return 0 if record["result"] == "passed" else 1
    except (Exception, asyncio.CancelledError) as error:
        record.update(
            result="unknown", error_code=getattr(error, "code", type(error).__name__)
        )
        return 1
    finally:
        if executor is not None:
            await executor.close()
        await service.close()
        record["cleanup"] = service.cleanup
        record["sources_after"] = freeze_sources()
        record["source_unchanged"] = record["sources_before"] == record["sources_after"]
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        print(
            json.dumps(
                {
                    "result": record["result"],
                    "actual_outbound_attempts": record.get("actual_outbound_attempts"),
                    "evidence": str(directory / "summary.json"),
                },
                ensure_ascii=False,
            )
        )
        repo.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument(
        "--thinking", default="high", choices=["none", "low", "high", "max"]
    )
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--require-native-tools", action="store_true")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
