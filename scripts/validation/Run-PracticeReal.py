"""Actual provider + fixed native Host, synthetic questions/cards and reference grading."""

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
from learning_app.workbench.models import ModelSettings, QuestionBlock
from learning_app.workbench.practice import PracticeStore
from learning_app.workbench.practice_models import (
    AnswerInput,
    ReviewControl,
    ReviewRatingInput,
    ReviewSessionInput,
    ReviewUndo,
    UserVerdict,
)
from learning_app.workbench.reviews import ReviewStore
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.scheduler import CalculationHelper
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def freeze():
    files = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app/workbench").glob("*.py")),
        *sorted((PROJECT_ROOT / "backend/src/learning_app/execution").glob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        PROJECT_ROOT / "backend/src/learning_app/service.py",
        PROJECT_ROOT / "harness/runtime.lock.json",
    ]
    for name in ("fsrs", "question"):
        files.append(PROJECT_ROOT / f"resources/workbench/{name}-runtime.lock.json")
        files.append(CalculationHelper(name).verified())
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in files
    }


async def run(args):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,100}", args.run_id):
        raise ValueError("invalid owned run identity")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "layer": "real_provider_fixed_native_harness",
        "input": "self-generated text only; no user material or video",
        "status": "not_run",
        "source_hashes_before": freeze(),
        "cases": [],
        "actual_user_learning_experience": "not_run",
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
        "A rectangle has width 3 cm and length 4 cm. Its area is 12 square centimetres. Verify the given dimensions before calculating. This is synthetic validation material.",
        "utf-8",
    )
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    repo = Repository(config.data_dir)
    files = CourseFileStore(repo)
    course = files.bind(None, str(material), "合成练习真实模型验证")["course_id"]
    opened = files.open(course, "rectangle.txt")
    source = {
        "kind": "document",
        "asset_id": opened["asset_id"],
        "source_version": opened["source_version"],
    }
    settings = ModelSettings(
        model=args.model,
        thinking=args.thinking,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout,
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

        async def complete(value):
            task = TaskCoverage(repo).admit(course, value)
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(
                executor._queue.join(), timeout=args.timeout * 3 + 30
            )
            current = TaskStore(repo).get(course, task.task_id)
            runs = [
                TaskRunStore(repo).get(course, task.task_id, unit.run_id)
                for unit in current.units
                if unit.run_id
            ]
            record["cases"].append(
                {
                    "kind": current.kind,
                    "task_id": current.task_id,
                    "state": current.state,
                    "units": [
                        {"state": unit.state, "error_code": unit.error_code}
                        for unit in current.units
                    ],
                    "runs": [run.model_dump(mode="json") for run in runs],
                }
            )
            if current.state != "completed":
                raise RuntimeError(
                    "REAL_TASK_NOT_COMPLETE:" + current.kind + ":" + current.state
                )
            target = TaskCoverage(repo).get(course, task.task_id).target
            return ArtifactStore(repo).get(course, target.artifact_id)

        paper = await complete(
            TaskAdmission(
                operation_id="questions",
                kind="question_set",
                instruction="只生成一道关于此矩形面积的单选题，恰好A/B/C/D四个互异选项，恰好一个正确。reference_answer只填正确选项字母，answer_authority必须ai_reference。保留来源，不给用户作答。",
                coverage_mode="complete",
                sources=[source],
                target={"mode": "create", "title": "合成矩形练习"},
                settings=settings,
                sources_only=True,
            )
        )
        question = next(
            block
            for block in paper.revision.body.blocks
            if isinstance(block, QuestionBlock)
        )
        keys = {option.option_id for option in question.question.options}
        reference = question.question.reference_answer.strip()
        if reference not in keys or len(keys) != 4:
            raise RuntimeError("REAL_QUESTION_REFERENCE_NOT_CANONICAL")
        wrong_key = sorted(keys - {reference})[0]
        practice, reviews = PracticeStore(repo), ReviewStore(repo)
        actual = practice.submit(
            course,
            paper.artifact_id,
            AnswerInput(
                operation_id="synthetic_actual_answer",
                question_revision_id=paper.head_revision_id,
                block_id=question.block_id,
                text=wrong_key,
            ),
        )
        if actual.effective_assessment.verdict != "pending":
            raise RuntimeError("UNCONFIRMED_ANSWER_MUST_BE_PENDING")
        record["actual_answer_before_reference"] = actual.model_dump(mode="json")
        await complete(
            TaskAdmission(
                operation_id="reference_review",
                kind="answer_review",
                instruction="核对实际题意和实际作答，给参考判分及具体理由；AI参考答案本身不证明正确，无法可靠判断则pending。",
                target={"mode": "create", "title": "合成作答参考评价"},
                settings=settings,
                answer_review={
                    "answer_id": actual.answer_id,
                    "expected_revision": actual.assessment_revision,
                },
            )
        )
        assessed = practice.get(course, actual.answer_id)
        if (
            assessed.text != wrong_key
            or assessed.effective_assessment.author != "ai_reference"
            or assessed.effective_assessment.verdict != "wrong"
        ):
            raise RuntimeError("REAL_REFERENCE_RESULT_UNEXPECTED")
        record["actual_answer_after_reference"] = assessed.model_dump(mode="json")
        # Query the future queue through the same formal product table projection.
        from learning_app.workbench import practice_schema as p
        from sqlalchemy import select

        with repo.engine.connect() as conn:
            wrong_id = conn.execute(
                select(p.review_items.c.review_id).where(
                    p.review_items.c.kind == "wrong_question"
                )
            ).scalar_one()
        wrong = reviews.get(course, wrong_id)
        reviews.control(
            course,
            wrong.review_id,
            ReviewControl(
                operation_id="pause_wrong",
                expected_revision=wrong.schedule_revision,
                action="pause",
            ),
        )
        practice.override(
            course,
            actual.answer_id,
            UserVerdict(
                operation_id="synthetic_user_correction",
                expected_revision=assessed.assessment_revision,
                verdict="correct",
                reason="合成验收：仅验证用户改判历史，不用此更正声明学科事实。",
            ),
        )
        corrected = reviews.get(course, wrong_id)
        if corrected.eligible or corrected.queue_state != "paused":
            raise RuntimeError("USER_CORRECTION_MUST_PRESERVE_PAUSE")
        record["judgment_history"] = practice.assessments(
            course, actual.answer_id
        ).model_dump(mode="json")
        deck = await complete(
            TaskAdmission(
                operation_id="cards",
                kind="card_set",
                instruction="根据矩形材料明确创建两张简短中文复习卡：一张回忆面积计算，一张回忆计算前检查尺寸。每卡只有一个要点，保留实际来源。",
                coverage_mode="complete",
                sources=[source],
                target={"mode": "create", "title": "合成矩形复习卡"},
                settings=settings,
                sources_only=True,
            )
        )
        today = reviews.today()
        item = next(
            item
            for item in today.items
            if item.artifact_id == deck.artifact_id and item.kind == "card"
        )
        session = reviews.open_session(
            course,
            item.review_id,
            ReviewSessionInput(
                operation_id="open_recall", expected_revision=item.schedule_revision
            ),
        )
        reviews.reveal(
            course,
            item.review_id,
            session.session_id,
            ReviewSessionInput(
                operation_id="user_reveal", expected_revision=item.schedule_revision
            ),
        )
        rating = reviews.rate(
            course,
            item.review_id,
            ReviewRatingInput(
                operation_id="synthetic_user_rating",
                expected_revision=item.schedule_revision,
                session_id=session.session_id,
                rating=3,
            ),
        )
        if rating.item.scheduler_state["reps"] != 1:
            raise RuntimeError("ACTUAL_FSRS_DID_NOT_APPLY")
        record["rating"] = rating.model_dump(mode="json")
        record["undo"] = reviews.undo(
            course,
            item.review_id,
            ReviewUndo(
                operation_id="undo_rating",
                expected_revision=rating.item.schedule_revision,
                event_id=rating.event_id,
            ),
        ).model_dump(mode="json")
        record["today"] = reviews.today().model_dump(mode="json")
        record["status"] = "passed"
    except BaseException as error:
        record["status"] = "failed"
        record["failure"] = f"{type(error).__name__}: {error}"
    finally:
        if executor:
            await executor.close()
        await service.close()
        record["cleanup"] = service.cleanup
        from learning_app.workbench import schema as learning
        from sqlalchemy import select

        with repo.engine.connect() as conn:
            run_rows = conn.execute(
                select(learning.task_runs.c.run_id, learning.task_runs.c.task_id)
            ).all()
        final_runs = [
            TaskRunStore(repo).get(course, task_id, run_id).model_dump(mode="json")
            for run_id, task_id in run_rows
        ]
        record["runs_final"] = final_runs
        record["actual_outbound_attempts"] = sum(
            run["actual_outbound_attempts"] for run in final_runs
        )
        record["raw_usage"] = [entry for run in final_runs for entry in run["usage"]]
        record["original_unchanged"] = (
            hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
        )
        if any(run["state"] == "unknown" for run in final_runs):
            record["status"] = "unknown"
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
