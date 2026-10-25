"""Isolated task/Harness real-call evidence, using only this synthetic text fixture."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re

from dotenv import dotenv_values

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.manifests import ManifestStore
from learning_app.workbench.models import (
    DocumentRangeInput,
    ManifestCreate,
    ModelSettings,
    TaskCreate,
    TaskUnitInput,
)
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.tasks import TaskStore


def source_manifest() -> dict[str, str]:
    paths = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app/workbench").glob("*.py")),
        *sorted((PROJECT_ROOT / "backend/src/learning_app/execution").glob("*.py")),
        PROJECT_ROOT / "backend/src/learning_app/service.py",
        PROJECT_ROOT / "harness/plugins/learning-guard.mjs",
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "backend/uv.lock",
    ]
    return {
        str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


async def run(args) -> int:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,100}", args.run_id):
        raise ValueError("invalid run identity")
    root = PROJECT_ROOT / ".runtime/validation" / args.run_id
    root.mkdir(parents=True, exist_ok=False)
    record = {
        "run_id": args.run_id,
        "layer": "real_model_fixed_harness",
        "input": "synthetic_text_only",
        "model": args.model,
        "thinking": args.thinking,
        "max_output_tokens": args.max_output_tokens,
        "timeout_seconds": args.timeout,
        "source_manifest_before": source_manifest(),
        "cases": [],
        "result": "not_run",
        "actual_outbound_attempts": None,
        "user_learning_experience": "not_run",
    }
    config = AppConfig(
        mode="test",
        harness_enabled=True,
        credential_route="api_key",
        model=args.model,
        thinking=args.thinking,
        max_output_tokens=args.max_output_tokens,
        model_timeout_seconds=args.timeout,
        data_dir=root / "data",
        runtime_dir=root / "app",
    )
    if not dotenv_values(config.secret_file).get("DEEPSEEK_API_KEY"):
        record["missing_prerequisite"] = "PRIVATE_CREDENTIAL_NOT_CONFIGURED"
        (root / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        print(json.dumps({"result": "not_run", "evidence": str(root / "summary.json")}))
        return 2
    materials = root / "synthetic-course"
    materials.mkdir()
    sentence = "A triangle with three equal sides is an equilateral triangle."
    (materials / "lesson.txt").write_text(
        sentence + "\nUNSELECTED_TAIL_SENTINEL", "utf-8"
    )
    repo = Repository(config.data_dir)
    course_id = CourseFileStore(repo).bind(None, str(materials), "合成验证课程")[
        "course_id"
    ]
    opened = CourseFileStore(repo).open(course_id, "lesson.txt")
    selection = SourceStore(repo).capture_text(
        course_id, opened["asset_id"], opened["source_version"], 0, len(sentence)
    )
    service = GenerationService(repo, config)
    executor = None
    success = False
    try:
        await service.open()
        if not service.question_answering_ready:
            record["missing_prerequisite"] = (
                service.execution_error or "MODEL_ROUTE_NOT_READY"
            )
            return 2
        executor = TaskExecutor(repo, service)
        await executor.open()
        settings = ModelSettings(
            model=args.model,
            thinking=args.thinking,
            max_output_tokens=args.max_output_tokens,
            timeout_seconds=args.timeout,
        )
        tasks = TaskStore(repo)
        translation = tasks.create(
            course_id,
            TaskCreate(
                operation_id="real_translation",
                kind="translation",
                instruction="准确翻译为简体中文，只返回译文。",
                settings=settings,
                units=[
                    TaskUnitInput(
                        unit_id="selection",
                        label="合成选句",
                        source_snapshot_id=selection.snapshot_id,
                    )
                ],
            ),
        )
        executor.schedule(course_id, translation.task_id)
        await asyncio.wait_for(executor._queue.join(), timeout=args.timeout + 30)
        translation = tasks.get(course_id, translation.task_id)
        record["cases"].append(
            case_record(
                repo,
                service,
                course_id,
                translation,
                forbidden="UNSELECTED_TAIL_SENTINEL",
            )
        )
        # The handout gets its own explicitly frozen full source, including its tail.
        manifest = ManifestStore(repo).create(
            course_id,
            ManifestCreate(
                documents=[
                    DocumentRangeInput(
                        asset_id=opened["asset_id"],
                        source_version=opened["source_version"],
                    )
                ]
            ),
        )
        handout = tasks.create(
            course_id,
            TaskCreate(
                operation_id="real_handout",
                kind="handout",
                instruction="完整整理指定正文为简短中文讲义，保留所有实际内容，末行标记按原样保留。",
                settings=settings,
                sources_only=True,
                coverage_mode="complete",
                scope_manifest_id=manifest.manifest_id,
            ),
        )
        executor.schedule(course_id, handout.task_id)
        await asyncio.wait_for(executor._queue.join(), timeout=args.timeout + 30)
        handout = tasks.get(course_id, handout.task_id)
        record["cases"].append(case_record(repo, service, course_id, handout))
        success = all(case["result"] == "passed" for case in record["cases"])
        record["result"] = "passed" if success else "failed"
        record["meaning"] = (
            "Technical source/parameter/stream/save/accounting checks only; no user learning or general accuracy claim."
        )
    except (TimeoutError, OSError, ValueError) as exc:
        record["result"] = "failed"
        record["error_type"] = type(exc).__name__
    finally:
        if executor:
            await executor.close()
        if service.execution:
            record["actual_outbound_attempts"] = (
                service.execution.boundary.actual_outbound_attempts
            )
            record["transport_request_count"] = len(service.execution.boundary.records)
        await service.close()
        record["cleanup"] = service.cleanup
        record["source_manifest_after"] = source_manifest()
        record["source_changed_during_run"] = (
            record["source_manifest_before"] != record["source_manifest_after"]
        )
        if record["source_changed_during_run"]:
            record["result"] = "failed"
            success = False
        (root / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        repo.close()
        print(
            json.dumps(
                {
                    "result": record["result"],
                    "actual_outbound_attempts": record["actual_outbound_attempts"],
                    "evidence": str(root / "summary.json"),
                },
                ensure_ascii=False,
            )
        )
    return 0 if success else 1


def case_record(repo, service, course_id, task, *, forbidden=None):
    unit = task.units[0]
    run = (
        TaskRunStore(repo).get(course_id, task.task_id, unit.run_id)
        if unit.run_id
        else None
    )
    requests = [
        item
        for item in service.execution.boundary.records
        if run and item.get("generation_id") == run.run_id
    ]
    policy_correct = bool(requests)
    scope_correct = True
    for item in requests:
        path = service.execution.boundary.private / f"{item['request_id']}.request.json"
        value = json.loads(path.read_text("utf-8"))
        policy_correct &= (
            value.get("model") == task.settings.model
            and value.get("max_tokens") == task.settings.max_output_tokens
        )
        policy_correct &= value.get("thinking", {}).get("type") == (
            "disabled" if task.settings.thinking == "none" else "enabled"
        ) and (
            task.settings.thinking == "none"
            or value.get("output_config", {}).get("effort") == task.settings.thinking
        )
        scope_correct &= not item["images"] and not value.get("tools")
        if forbidden:
            scope_correct &= forbidden not in json.dumps(value, ensure_ascii=False)
    product = (
        ArtifactStore(repo).get(course_id, unit.result_artifact_id)
        if unit.result_artifact_id
        else None
    )
    passed = (
        task.state == "completed"
        and product is not None
        and policy_correct
        and scope_correct
    )
    return {
        "kind": task.kind,
        "task_id": task.task_id,
        "run_id": run.run_id if run else None,
        "state": task.state,
        "result": "passed" if passed else "failed",
        "policy_checked": policy_correct,
        "scope_checked": scope_correct,
        "actual_attempts": run.actual_outbound_attempts if run else None,
        "usage_status": run.usage_status if run else "unknown",
        "usage": [item.model_dump(mode="json") for item in run.usage] if run else [],
        "totals": run.totals.model_dump() if run else None,
        "output_preview": run.body[:500] if run else None,
        "error_code": unit.error_code,
        "saved_role": product.content_role if product else None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument(
        "--thinking", choices=["none", "low", "high", "max"], default="high"
    )
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=240)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
