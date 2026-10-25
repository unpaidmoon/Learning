"""Real provider outline/body phases using only two self-created source texts."""

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
from learning_app.workbench.book_models import BookOutlineConfirm
from learning_app.workbench.books import BookStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import (
    ModelSettings,
    QuestionBlock,
    VisualizationBlock,
)
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
        PROJECT_ROOT / "scripts/validation/Run-BookReal.py",
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
        "layer": "real provider + pinned native Harness + formal book tasks/layouts",
        "input": "two self-created texts only; no images, video or user material",
        "user_learning_experience": "not_run",
        "confirmation": "scripted synthetic outline confirmation, not a real learner approval",
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
    (material / "01-quadratic.txt").write_text(
        "Topic: a quadratic function y=a*x^2. Square x first, then multiply by a. For a=1 and x=1, y=1. "
        "For a=2 and x=1, y=2. The slope at x=1 is 2*a. For the parameter graph use a from -2 to 2, step 0.1, initial 1; "
        "x from -3 to 3 and y from -20 to 20. This self-created text is synthetic validation material.",
        "utf-8",
    )
    (material / "02-rectangle.txt").write_text(
        "Topic: rectangle area and units. Area equals width times length. Width 3 cm and length 4 cm give area 12 square centimetres. "
        "A length uses centimetres; an area uses square centimetres. Check the actual dimensions before applying the formula. "
        "This self-created text is synthetic validation material.",
        "utf-8",
    )
    original_hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in material.iterdir()
    }
    repo = Repository(config.data_dir)
    files = CourseFileStore(repo)
    course = files.bind(None, str(material), "自造两资料交互书真实模型验证")[
        "course_id"
    ]
    documents = [files.open(course, name) for name in original_hashes]
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
                operation_id="explicit_book",
                kind="book",
                coverage_mode="complete",
                instruction="将两份指定资料组织成恰好两章中文交互书：第一章讲二次函数与系数，只分配01-quadratic.txt；第二章讲矩形面积与平方单位，只分配02-rectangle.txt。先拟目录，确认后连续生成。每章正文简洁但完整，至少一道可独立作答的question练习；第一章必须有结构化visualization参数图a*x^2，按资料给定范围。保留来源，题目答案只标ai_reference，不产生用户作答或掌握。",
                sources=[
                    {
                        "kind": "document",
                        "asset_id": d["asset_id"],
                        "source_version": d["source_version"],
                    }
                    for d in documents
                ],
                target={"mode": "create", "title": "函数与面积交互学习书"},
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
        outline = BookStore(repo).outline(course, task.task_id)
        record["outline_before_confirmation"] = outline.model_dump(mode="json")
        if (
            current.state != "awaiting_outline"
            or not outline.proposal
            or len(outline.proposal.chapters) != 2
        ):
            raise RuntimeError("REAL_BOOK_OUTLINE_NOT_READY:" + current.state)
        if ArtifactStore(repo).list(course) or sum(
            v.processed_leaf_count
            for v in TaskCoverage(repo).get(course, task.task_id).sources
        ):
            raise RuntimeError("OUTLINE_PRETENDED_BODY_COVERAGE")
        confirmation = BookOutlineConfirm(
            operation_id="scripted_once",
            expected_revision=current.revision,
            proposal_hash=outline.proposal_hash,
            outline=outline.proposal,
        )
        BookStore(repo).confirm(course, task.task_id, confirmation)
        executor.schedule(course, task.task_id)
        await asyncio.wait_for(executor._queue.join(), timeout=args.timeout * 12 + 60)
        current = TaskStore(repo).get(course, task.task_id)
        record["task"] = current.model_dump(mode="json")
        if current.state != "completed":
            raise RuntimeError("REAL_BOOK_BODY_NOT_COMPLETE:" + current.state)
        target = TaskCoverage(repo).get(course, task.task_id).target
        artifact = ArtifactStore(repo).get(course, target.artifact_id)
        view = BookStore(repo).get(course, artifact.artifact_id)
        record["artifact"] = artifact.model_dump(mode="json")
        record["book"] = view.model_dump(mode="json")
        record["coverage"] = (
            TaskCoverage(repo).get(course, task.task_id).model_dump(mode="json")
        )
        if (
            len(ArtifactStore(repo).list(course)) != 1
            or view.book_status != "ready"
            or not all(ch.pages and ch.state == "ready" for ch in view.chapters)
        ):
            raise RuntimeError("REAL_BOOK_LAYOUT_NOT_COMPLETE")
        questions = [
            b for b in artifact.revision.body.blocks if isinstance(b, QuestionBlock)
        ]
        visuals = [
            b
            for b in artifact.revision.body.blocks
            if isinstance(b, VisualizationBlock)
        ]
        if len(questions) < 2 or not visuals:
            raise RuntimeError("REAL_BOOK_INTERACTIVE_CONTENT_MISSING")
        if BookStore(repo).outline(course, task.task_id).confirmation_count != 1:
            raise RuntimeError("REAL_BOOK_CONFIRMATION_COUNT_INVALID")
        with repo.engine.connect() as conn:
            if (
                conn.execute(
                    select(func.count()).select_from(practice.answers)
                ).scalar_one()
                != 0
            ):
                raise RuntimeError("BOOK_GENERATION_FABRICATED_ANSWER")
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
        record["originals_unchanged"] = all(
            hashlib.sha256((material / name).read_bytes()).hexdigest() == digest
            for name, digest in original_hashes.items()
        )
        record["source_hashes_after"] = freeze()
        record["source_changed_during_run"] = (
            record["source_hashes_before"] != record["source_hashes_after"]
        )
        if not record["originals_unchanged"] or record["source_changed_during_run"]:
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
