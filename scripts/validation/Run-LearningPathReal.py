"""Real provider route generation with self-created text and labelled fixture actions."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re

from dotenv import dotenv_values
from sqlalchemy import func, select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.workbench import practice_schema as practice
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import ArtifactUpdate, BlockChange, ModelSettings
from learning_app.workbench.path_models import PathAction
from learning_app.workbench.paths import PathStore
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
        PROJECT_ROOT / "third_party/deeptutor/reuse-manifest.json",
        PROJECT_ROOT / "scripts/validation/Run-LearningPathReal.py",
    ]
    return {
        p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in files
    }


async def run(args):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,100}", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "status": "not_run",
        "layer": "actual provider + fixed native Harness + formal task/path stores",
        "input": "self-created rectangle text; no images, videos or user assets",
        "actions": "scripted synthetic user actions, not actual learner experience",
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
    original = material / "rectangle.txt"
    original.write_text(
        "Learning goal: understand the area of a rectangle. The width is 3 cm and the length is 4 cm. "
        "First identify the given dimensions and their units. Area equals length times width: 4 times 3 = 12 square centimetres. "
        "A length is measured in centimetres while an area is measured in square centimetres. "
        "Check the dimensions and units before calculating. Synthetic validation material.",
        "utf-8",
    )
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    repo = Repository(config.data_dir)
    files = CourseFileStore(repo)
    course = files.bind(None, str(material), "自造矩形学习路线验证")["course_id"]
    opened = files.open(course, "rectangle.txt")
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
                operation_id="explicit_path",
                kind="learning_path",
                instruction="只根据指定资料，为理解矩形面积生成恰好两个知识点的中文建议路线：先辨别已知尺寸与单位，再理解面积计算与平方单位。每点有回忆问题和完整讲解；能自由跳过，不包含用户作答、评分或已掌握结论；questions为空数组。",
                coverage_mode="complete",
                sources=[
                    {
                        "kind": "document",
                        "asset_id": opened["asset_id"],
                        "source_version": opened["source_version"],
                    }
                ],
                target={"mode": "create", "title": "矩形面积建议学习路线"},
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
            raise RuntimeError("REAL_PATH_TASK_NOT_COMPLETE:" + current.state)
        target = TaskCoverage(repo).get(course, current.task_id).target
        artifact = ArtifactStore(repo).get(course, target.artifact_id)
        paths = PathStore(repo)
        before = paths.get(course, artifact.artifact_id)
        if len(before.steps) != 2 or any(
            s.visited or s.answers or s.self_assessment for s in before.steps
        ):
            raise RuntimeError("PATH_STRUCTURE_OR_USER_EVIDENCE_INVALID")
        record["generated_path"] = before.model_dump(mode="json")
        schedule_before = [s.review.model_dump(mode="json") for s in before.steps]
        actual = before
        for index, (action, step) in enumerate(
            (
                ("skip", before.steps[0]),
                ("navigate", before.steps[1]),
                ("view_explanation", before.steps[1]),
            )
        ):
            actual = paths.act(
                course,
                artifact.artifact_id,
                PathAction(
                    operation_id=f"scripted_action_{index}",
                    expected_revision=actual.revision,
                    content_revision_id=actual.content_revision_id,
                    block_id=step.block.block_id,
                    action=action,
                ),
            ).path
        if (
            not actual.steps[0].skipped
            or not actual.steps[1].explanation_seen
            or actual.current_block_id != before.steps[1].block.block_id
        ):
            raise RuntimeError("FREE_NAVIGATION_NOT_PERSISTED")
        if [s.review.model_dump(mode="json") for s in actual.steps] != schedule_before:
            raise RuntimeError("BROWSING_CHANGED_REVIEW_SCHEDULE")
        first = before.steps[0].block
        changed = ArtifactStore(repo).update(
            course,
            artifact.artifact_id,
            ArtifactUpdate(
                operation_id="scripted_reorder",
                expected_revision_id=artifact.head_revision_id,
                changes=[
                    BlockChange(
                        block_id=first.block_id,
                        replacement=first.model_copy(
                            update={
                                "step": first.step.model_copy(
                                    update={"suggested_order": 9}
                                ),
                            }
                        ),
                    )
                ],
            ),
        )
        if (
            changed.outcome != "applied"
            or paths.get(course, artifact.artifact_id).steps[0].block.block_id
            == first.block_id
        ):
            raise RuntimeError("PATH_REORDER_NOT_SAVED")
        with repo.engine.connect() as conn:
            if (
                conn.execute(
                    select(func.count()).select_from(practice.answers)
                ).scalar_one()
                != 0
            ):
                raise RuntimeError("PATH_ACTION_CREATED_FAKE_ANSWER")
        record["action_history"] = paths.events(
            course, artifact.artifact_id
        ).model_dump(mode="json")
        record["final_path"] = paths.get(course, artifact.artifact_id).model_dump(
            mode="json"
        )
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
            hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
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
