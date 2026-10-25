"""Reference grading uses the real pinned Host and project task/answer transactions."""

import asyncio
import json

import pytest
from sqlalchemy import select
from test_native_business_host import runtime
from test_practice_reviews import answer, paper, question, review_rows
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.errors import AppError
from learning_app.service import GenerationService
from learning_app.workbench import practice_schema as p
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import (
    ArtifactBody,
    AssessmentBlock,
    AssessmentContent,
)
from learning_app.workbench.practice import PracticeStore
from learning_app.workbench.practice_models import UserVerdict
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def review_task(repo, course, actual, operation="review"):
    return TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id=operation,
            kind="answer_review",
            instruction="按实际题目与实际作答给参考判断和理由，无法可靠判断时保持待判断。",
            target={"mode": "create", "title": "作答参考评价"},
            sources=[],
            answer_review={
                "answer_id": actual.answer_id,
                "expected_revision": actual.assessment_revision,
            },
            settings=settings(),
        ),
    )


@pytest.mark.host
@pytest.mark.parametrize("late_override", [False, True])
async def test_ai_reference_retains_actual_answer_and_late_user_correction(tmp_path, late_override):
    repo, course, _, source = learning_course(tmp_path)
    artifact, practice = (
        paper(repo, course, question(authority="ai_reference", question_type="short_answer")),
        PracticeStore(repo),
    )
    actual = answer(practice, course, artifact, "My actual answer must not be replaced.")
    assert actual.effective_assessment.verdict == "pending"
    task = review_task(repo, course, actual)
    assert review_task(repo, course, actual).task_id == task.task_id
    with pytest.raises(AppError) as error:
        review_task(repo, course, actual, "other_tab_review")
    assert error.value.code == "ASSESSMENT_REVIEW_EXISTS"
    TaskCoverage(repo).refresh(course, task.task_id)
    TaskPlanner(repo).plan(course, task.task_id)
    coverage = TaskCoverage(repo).get(course, task.task_id)
    ids = [leaf.snapshot_ids[0] for leaf in coverage.leaves]
    assert len(ids) == 2
    if late_override:
        practice.override(
            course,
            actual.answer_id,
            UserVerdict(
                operation_id="user_override",
                expected_revision=1,
                verdict="correct",
                reason="Preserve my decision during generation.",
            ),
        )
    body = ArtifactBody(
        blocks=[
            AssessmentBlock(
                block_id="feedback",
                markdown="The answer omits the requested comparison; reference judgment only.",
                attribution="source_paraphrase",
                source_snapshot_ids=ids,
                assessment=AssessmentContent(
                    answer_id=actual.answer_id, expected_revision=1, verdict="wrong", score=0
                ),
            )
        ]
    )
    adapter, port, registry, boundary = runtime(tmp_path, repo, source, bare=True)
    boundary.set_scenario({"text_chunks": [body.model_dump_json()]})
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=30)
            current = TaskStore(repo).get(course, task.task_id)
            assert current.state == "completed"
            result = practice.get(course, actual.answer_id)
            history = practice.assessments(course, actual.answer_id).assessments
            assert (
                result.text == actual.text
                and result.question_revision_id == artifact.head_revision_id
            )
            model = history[-1]
            assert model.author == "ai_reference" and model.run_id == current.units[0].run_id
            assert model.evidence["model_settings"] == settings().model_dump(mode="json")
            if late_override:
                assert result.effective_assessment.author == "user_override"
                assert (
                    result.effective_assessment.verdict == "correct"
                    and result.assessment_revision == 3
                )
                assert model.evidence["disposition"] == "stale" and review_rows(repo) == []
            else:
                assert result.effective_assessment.assessment_id == model.assessment_id
                assert len(review_rows(repo)) == 1 and review_rows(repo)[0]["eligible"]
            run = TaskRunStore(repo).internal(current.units[0].run_id)
            executor._finish(course, task.task_id, run["run_id"])
            assert len(practice.assessments(course, actual.answer_id).assessments) == len(history)
            request = json.loads(
                (
                    boundary.private / (boundary.records[0]["request_id"] + ".request.json")
                ).read_text("utf-8")
            )
            outbound = json.dumps(request["messages"], ensure_ascii=False)
            assert actual.text in outbound and "Which sentence was selected?" in outbound
            assert "不应补发前后文" not in outbound
            assert len(boundary.records) == 1 and repo.counts()["outbound_attempts"] == 0
        finally:
            await executor.close()
            await service.close()


def test_scope_cross_answer_and_non_review_stage_denied(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    artifact = paper(repo, course)
    actual = answer(PracticeStore(repo), course, artifact)
    task = review_task(repo, course, actual)
    TaskCoverage(repo).refresh(course, task.task_id)
    TaskPlanner(repo).plan(course, task.task_id)
    unit = TaskStore(repo).get(course, task.task_id).units[0]
    run = TaskRunStore(repo).claim(course, task.task_id, unit.unit_id, "fixture-only-contract")
    private = TaskRunStore(repo).internal(run.run_id)
    ids = [item["snapshot_id"] for item in private["input_contract"]["inputs"]]
    bad = ArtifactBody(
        blocks=[
            AssessmentBlock(
                block_id="feedback",
                markdown="Wrong target must stay unchanged.",
                attribution="source_paraphrase",
                source_snapshot_ids=ids,
                assessment=AssessmentContent(
                    answer_id="another_answer", expected_revision=1, verdict="wrong"
                ),
            )
        ]
    )
    with repo.engine.connect() as conn, pytest.raises(AppError) as error:
        ArtifactStore.validate_run_body(
            conn,
            {
                "task_id": task.task_id,
                "kind": "answer_review",
                "course_id": course,
                "sources_only": False,
                "coverage_mode": "selection",
            },
            private,
            bad,
        )
    assert error.value.code == "TASK_ASSESSMENT_TARGET_INVALID"
    assert PracticeStore(repo).get(course, actual.answer_id).assessment_revision == 1
    with repo.engine.connect() as conn:
        assert conn.execute(
            select(p.assessments.c.assessment_id).where(
                p.assessments.c.answer_id == actual.answer_id
            )
        ).all()
