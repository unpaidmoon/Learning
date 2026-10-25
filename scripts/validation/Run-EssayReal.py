"""Real native provider grading of two synthetic immutable essay drafts."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re

from dotenv import dotenv_values
from sqlalchemy import select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.essay_models import (
    EssayAssessmentInput,
    EssaySuggestionInput,
)
from learning_app.workbench.essay_processing import EssayProcessor
from learning_app.workbench.essays import EssayStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import (
    ArtifactBody,
    ArtifactCreate,
    EssayDraftBlock,
    EssayDraftContent,
    ModelSettings,
)
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def freeze():
    processor = EssayProcessor()
    processor.verified()
    paths = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app/workbench").glob("*.py")),
        *sorted((PROJECT_ROOT / "backend/src/learning_app/execution").glob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        PROJECT_ROOT / "backend/src/learning_app/service.py",
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "resources/workbench/essay-runtime.lock.json",
        processor.executable,
    ]
    paths.extend(path for path in processor.directory.rglob("*") if path.is_file())
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
        "layer": "real_provider_fixed_native_harness",
        "input": "self-written synthetic English essay only",
        "status": "not_run",
        "source_hashes_before": freeze(),
        "cases": [],
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
    (material / "outside.txt").write_text("OUTSIDE_ESSAY_SCOPE_SENTINEL", "utf-8")
    text = "  I has learned English since three year. Last Sunday I go to the library with my friend. We reads an interesting book about nature. Learning English help me understand different cultures. I want improve my writing by practice every day. In the future, I hope I can describes my ideas more clear.  "
    repo = Repository(config.data_dir)
    course = CourseFileStore(repo).bind(None, str(material), "合成作文真实模型验证")[
        "course_id"
    ]
    original = (
        ArtifactStore(repo)
        .create(
            course,
            ArtifactCreate(
                operation_id="actual_draft",
                kind="essay",
                title="合成英语作文",
                body=ArtifactBody(
                    blocks=[
                        EssayDraftBlock(
                            block_id="draft",
                            markdown=text,
                            attribution="user_authored",
                            essay=EssayDraftContent(
                                topic="Explain why you learn English and how you plan to improve. Preserve the actual draft. The default 100-point rubric is a learning reference, not an examination grade."
                            ),
                        )
                    ]
                ),
            ),
        )
        .artifact
    )
    essays, service, executor = EssayStore(repo), GenerationService(repo, config), None
    try:
        await service.open()
        if not service.question_answering_ready:
            record["missing_prerequisite"] = (
                service.execution_error or "MODEL_ROUTE_NOT_READY"
            )
            return 2
        executor = TaskExecutor(repo, service)
        await executor.open()

        async def grade(artifact, number):
            task = TaskCoverage(repo).admit(
                course,
                TaskAdmission(
                    operation_id=f"review_round_{number + 1}",
                    kind="essay_review",
                    instruction="按实际英语稿、题目及100分参考标准完成一次批改。给准确的原文定位和可采纳的替换建议；对全文保留完整原文，不trim。参考分理由明确，不冒充正式考试成绩。",
                    target={
                        "mode": "create",
                        "title": f"合成作文第{number + 1}轮参考批改",
                    },
                    essay_review={
                        "artifact_id": artifact.artifact_id,
                        "revision_id": artifact.head_revision_id,
                        "block_id": "draft",
                        "expected_round_number": number,
                    },
                    settings=ModelSettings(
                        model=args.model,
                        thinking=args.thinking,
                        max_output_tokens=args.max_output_tokens,
                        timeout_seconds=args.timeout,
                    ),
                ),
            )
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(
                executor._queue.join(), timeout=args.timeout * 3 + 30
            )
            current = TaskStore(repo).get(course, task.task_id)
            record["cases"].append(
                {
                    "task_id": current.task_id,
                    "state": current.state,
                    "units": [
                        {"state": unit.state, "error_code": unit.error_code}
                        for unit in current.units
                    ],
                }
            )
            if current.state != "completed":
                raise RuntimeError("ESSAY_REAL_TASK_NOT_COMPLETE:" + current.state)
            row = essays.list(course, artifact.artifact_id).items[0]
            value = essays.get(course, row.round_id)
            record["cases"][-1]["round"] = value.model_dump(mode="json")
            if value.result["status"] != "reference_ready" or not value.result.get(
                "reference_evaluation"
            ):
                raise RuntimeError("ESSAY_REAL_REFERENCE_PENDING")
            for annotation in value.result["annotations"]:
                if (
                    value.draft.markdown[annotation["start"] : annotation["end"]]
                    != annotation["quote"]
                ):
                    raise RuntimeError("ESSAY_REAL_ANCHOR_MISMATCH")
            return value

        first = await grade(original, 0)
        if (
            ArtifactStore(repo).get(course, original.artifact_id).head_revision_id
            != original.head_revision_id
            or first.draft.markdown != text
        ):
            raise RuntimeError("ESSAY_REAL_MODEL_CHANGED_DRAFT")
        candidates = [
            index
            for index, item in enumerate(first.result["annotations"])
            if item["replacement"] != item["quote"]
            and item["kind"] in {"replace", "ins", "del"}
        ]
        if not candidates:
            raise RuntimeError("ESSAY_REAL_NO_ACTIONABLE_SUGGESTION")
        corrected = essays.assess(
            course,
            first.round_id,
            EssayAssessmentInput(
                operation_id="synthetic_user_override",
                expected_revision=first.assessment_revision,
                origin="user_override",
                total=min(100, first.result["reference_evaluation"]["total"] + 5),
                reason="合成验收：验证独立用户更正与保留AI原判，不作正式成绩。",
            ),
        )
        accepted = essays.apply_suggestions(
            course,
            first.round_id,
            EssaySuggestionInput(
                operation_id="explicit_user_acceptance",
                expected_revision_id=original.head_revision_id,
                annotation_indices=[candidates[0]],
            ),
        )
        if accepted.outcome != "applied" or accepted.artifact.content_role != "mixed":
            raise RuntimeError("ESSAY_REAL_ACCEPTANCE_NOT_VERSIONED")
        record["user_corrected_round"] = corrected.model_dump(mode="json")
        record["accepted_revision"] = accepted.artifact.model_dump(mode="json")
        second = await grade(accepted.artifact, 1)
        if (
            first.draft_revision_id == second.draft_revision_id
            or first.draft.markdown == second.draft.markdown
        ):
            raise RuntimeError("ESSAY_REAL_ROUND_NOT_BOUND_TO_NEW_DRAFT")
        if (
            ArtifactStore(repo).get(course, original.artifact_id).head_revision_id
            != accepted.artifact.head_revision_id
        ):
            raise RuntimeError("ESSAY_REAL_SECOND_MODEL_CHANGED_DRAFT")
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
        record["runs"] = runs
        record["actual_outbound_attempts"] = sum(
            run["actual_outbound_attempts"] for run in runs
        )
        record["raw_usage"] = [entry for run in runs for entry in run["usage"]]
        record["source_hashes_after"] = freeze()
        record["source_changed_during_run"] = (
            record["source_hashes_before"] != record["source_hashes_after"]
        )
        record["original_preserved"] = (
            ArtifactStore(repo)
            .history(course, original.artifact_id)[0]
            .body.blocks[0]
            .markdown
            == text
        )
        if any(run["state"] == "unknown" for run in runs):
            record["status"] = "unknown"
        if record["source_changed_during_run"] or not record["original_preserved"]:
            record["status"] = "failed"
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
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
        repo.close()
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--timeout", type=float, default=180)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
