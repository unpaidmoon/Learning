"""Same saved mixed-material handout -> one-confirmation interactive book."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import func, select

from learning_app.config import PROJECT_ROOT, AppConfig
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
        PROJECT_ROOT / "scripts/validation/Run-MaterialBookReal.py",
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
        "input": "exact saved AI handout from owned mixed-PDF and foreign video subtitles; no new source ingestion",
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
    prior = PROJECT_ROOT / ".runtime/validation/material-chain-real-20261024-r1"
    review = json.loads(
        (
            PROJECT_ROOT
            / ".runtime/validation/material-chain-review-20261024-r1/summary.json"
        ).read_text("utf-8")
    )
    if review["status"] != "passed":
        raise RuntimeError("SAVED_HANDOUT_REVIEW_REQUIRED")
    handout = review["handout"]
    original_data = {
        p.relative_to(prior / "data").as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in (prior / "data").rglob("*")
        if p.is_file()
    }
    shutil.copytree(prior / "data", config.data_dir)
    original_hashes = json.loads(
        (
            PROJECT_ROOT
            / ".runtime/validation/material-chain-prepare-20261024-r1/summary.json"
        ).read_text("utf-8")
    )["originals"]
    repo = Repository(config.data_dir)
    course = handout["course_id"]
    if (
        ArtifactStore(repo).get(course, handout["artifact_id"]).head_revision_id
        != handout["head_revision_id"]
    ):
        raise RuntimeError("HANDOUT_VERSION_CHANGED")
    record["handout_input"] = handout
    record["old_actual_outbound_attempts"] = 2
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
                instruction="将这份确切修订的讲义组织成恰好两章中文交互书：第一章矩形面积公式与3乘4等于12，第二章长度和面积单位与5乘2等于10。先拟目录，确认一次后连续生成；每章至少一道独立question练习，reference_answer为实际正确内容，answer_authority只标ai_reference。讲义为AI归纳，保留该身份和来源，不冒充原教材，不产生用户作答或掌握。",
                sources=[
                    {
                        "kind": "artifact",
                        "artifact_id": handout["artifact_id"],
                        "revision_id": handout["head_revision_id"],
                    }
                ],
                target={"mode": "create", "title": "矩形面积与单位交互书"},
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
        if TaskCoverage(repo).get(course, task.task_id).target.materialized or sum(
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
            len(ArtifactStore(repo).list(course)) != 2
            or view.book_status != "ready"
            or not all(ch.pages and ch.state == "ready" for ch in view.chapters)
        ):
            raise RuntimeError("REAL_BOOK_LAYOUT_NOT_COMPLETE")
        questions = [
            b for b in artifact.revision.body.blocks if isinstance(b, QuestionBlock)
        ]
        if len(questions) < 2:
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
            hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest
            for name, digest in original_hashes.items()
        )
        record["original_owned_data_unchanged"] = original_data == {
            p.relative_to(prior / "data").as_posix(): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in (prior / "data").rglob("*")
            if p.is_file()
        }
        record["actual_outbound_attempts_new"] = (
            record["actual_outbound_attempts"] - record["old_actual_outbound_attempts"]
        )
        record["source_hashes_after"] = freeze()
        record["source_changed_during_run"] = (
            record["source_hashes_before"] != record["source_hashes_after"]
        )
        if (
            not record["originals_unchanged"]
            or record["source_changed_during_run"]
            or not record["original_owned_data_unchanged"]
        ):
            record["status"] = "failed"
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        repo.close()
    print(
        json.dumps(
            {
                "status": record["status"],
                "actual_outbound_attempts_new": record.get(
                    "actual_outbound_attempts_new"
                ),
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
    parser.add_argument("--max-output-tokens", type=int, default=16384)
    parser.add_argument("--timeout", type=float, default=300)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
