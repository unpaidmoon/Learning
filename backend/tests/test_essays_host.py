"""Actual pinned Host and fixed original Node parser, synthetic versioned drafts only."""

import asyncio

import pytest
from test_native_business_host import runtime
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.errors import AppError
from learning_app.service import GenerationService
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.essay_models import EssayAssessmentInput, EssaySuggestionInput
from learning_app.workbench.essays import EssayStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import (
    ArtifactBody,
    ArtifactCreate,
    ArtifactRestore,
    ArtifactUpdate,
    BlockChange,
    EssayDraftBlock,
    EssayDraftContent,
    EssayFeedbackBlock,
    EssayFeedbackContent,
    TaskAction,
)
from learning_app.workbench.task_controls import TaskRecovery
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def draft(repo, course):
    return (
        ArtifactStore(repo)
        .create(
            course,
            ArtifactCreate(
                operation_id="essay",
                kind="essay",
                title="Actual essay",
                body=ArtifactBody(
                    blocks=[
                        EssayDraftBlock(
                            block_id="draft",
                            markdown="前🙂 bad 后 bad 末",
                            attribution="user_authored",
                            essay=EssayDraftContent(
                                topic="Keep the original actual words, including Unicode."
                            ),
                        )
                    ]
                ),
            ),
        )
        .artifact
    )


def request(repo, course, artifact, *, number=0, operation="review"):
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id=operation,
            kind="essay_review",
            instruction="给原稿参考批改，保留实际稿不自动改写。",
            target={"mode": "create", "title": "Essay reference"},
            settings=settings(),
            essay_review={
                "artifact_id": artifact.artifact_id,
                "revision_id": artifact.head_revision_id,
                "block_id": "draft",
                "expected_round_number": number,
            },
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id)
    TaskPlanner(repo).plan(course, task.task_id)
    row = EssayStore(repo).list(course, artifact.artifact_id).items[0]
    return task, row


def feedback(
    row,
    snapshot_id,
    *,
    total=70,
    annotated='前🙂 bad 后 <replace old="bad" new="good" reason="grammar"/> 末',
    raw=None,
):
    return ArtifactBody(
        blocks=[
            EssayFeedbackBlock(
                block_id="feedback",
                markdown=raw
                or f'<score total="{total}" max="100"></score>\nSynthetic reference reasoning.',
                attribution="source_paraphrase",
                source_snapshot_ids=[snapshot_id],
                feedback=EssayFeedbackContent(
                    round_id=row.round_id,
                    draft_revision_id=row.draft_revision_id,
                    draft_content_hash=row.draft_content_hash,
                    annotated_text=annotated,
                ),
            )
        ]
    )


@pytest.mark.host
async def test_two_actual_drafts_rounds_self_override_late_result_and_versioned_acceptance(
    tmp_path,
):
    repo, course, _, source = learning_course(tmp_path)
    original, essays = draft(repo, course), EssayStore(repo)
    task1, round1 = request(repo, course, original)
    essays.assess(
        course,
        round1.round_id,
        EssayAssessmentInput(
            operation_id="self",
            expected_revision=0,
            origin="user_self",
            total=85,
            reason="My independent self assessment.",
        ),
    )
    changed = (
        ArtifactStore(repo)
        .update(
            course,
            original.artifact_id,
            ArtifactUpdate(
                operation_id="actual_revision_2",
                expected_revision_id=original.head_revision_id,
                changes=[
                    BlockChange(
                        block_id="draft",
                        replacement=original.revision.body.blocks[0].model_copy(
                            update={"markdown": "前🙂 good 后 bad 末"}
                        ),
                    )
                ],
            ),
        )
        .artifact
    )
    snapshot1 = TaskCoverage(repo).get(course, task1.task_id).leaves[0].snapshot_ids[0]
    adapter, port, registry, boundary = runtime(tmp_path, repo, source, bare=True)
    boundary.set_scenario({"text_chunks": [feedback(round1, snapshot1).model_dump_json()]})
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), timeout=30)
            assert TaskStore(repo).get(course, task1.task_id).state == "completed"
            first = essays.get(course, round1.round_id)
            assert first.draft.markdown == original.revision.body.blocks[0].markdown
            assert (
                first.result["status"] == "reference_ready"
                and first.result["annotations"][0]["start"] == 9
            )
            assert first.result["annotations"][0]["end"] == 12
            assert [item.origin for item in first.assessments] == ["user_self", "ai_reference"]
            assert [item.total for item in first.assessments] == [85, 70]
            assert first.user_assessment_id != first.reference_assessment_id
            assert (
                ArtifactStore(repo).get(course, original.artifact_id).head_revision_id
                == changed.head_revision_id
            )
            overridden = essays.assess(
                course,
                first.round_id,
                EssayAssessmentInput(
                    operation_id="override",
                    expected_revision=2,
                    origin="user_override",
                    total=90,
                    reason="Preserve the reference 70 and my correction 90.",
                ),
            )
            assert [item.total for item in overridden.assessments] == [85, 70, 90]
            task2, round2 = request(
                repo, course, changed, number=1, operation="review_second_draft"
            )
            snapshot2 = TaskCoverage(repo).get(course, task2.task_id).leaves[0].snapshot_ids[0]
            boundary.set_scenario(
                {
                    "text_chunks": [
                        feedback(
                            round2,
                            snapshot2,
                            total=80,
                            annotated='前🙂 good 后 <replace old="bad" new="good" reason="grammar"/> 末',
                        ).model_dump_json()
                    ]
                }
            )
            executor.schedule(course, task2.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=30)
            assert TaskStore(repo).get(course, task2.task_id).state == "completed"
            second = essays.get(course, round2.round_id)
            assert (
                second.draft.markdown == "前🙂 good 后 bad 末" and second.assessments[0].total == 80
            )
            assert essays.get(course, first.round_id).assessments[1].total == 70
            conflicted = essays.apply_suggestions(
                course,
                first.round_id,
                EssaySuggestionInput(
                    operation_id="old_proposal",
                    expected_revision_id=original.head_revision_id,
                    annotation_indices=[0],
                ),
            )
            assert conflicted.outcome == "conflict" and conflicted.conflict
            assert conflicted.artifact.head_revision_id == changed.head_revision_id
            accepted = essays.apply_suggestions(
                course,
                second.round_id,
                EssaySuggestionInput(
                    operation_id="accept_second",
                    expected_revision_id=changed.head_revision_id,
                    annotation_indices=[0],
                ),
            )
            assert accepted.outcome == "applied" and accepted.artifact.content_role == "mixed"
            assert accepted.artifact.revision.body.blocks[0].markdown == "前🙂 good 后 good 末"
            assert accepted.artifact.revision.body.blocks[0].source_snapshot_ids
            replay = essays.apply_suggestions(
                course,
                second.round_id,
                EssaySuggestionInput(
                    operation_id="accept_second",
                    expected_revision_id=changed.head_revision_id,
                    annotation_indices=[0],
                ),
            )
            assert replay == accepted
            restored = ArtifactStore(repo).restore(
                course,
                original.artifact_id,
                ArtifactRestore(
                    operation_id="undo_acceptance",
                    expected_revision_id=accepted.artifact.head_revision_id,
                    restore_revision_id=changed.head_revision_id,
                ),
            )
            assert (
                restored.artifact.revision.body.blocks[0].markdown
                == changed.revision.body.blocks[0].markdown
            )
            assert original.head_revision_id in {
                item.revision_id
                for item in ArtifactStore(repo).history(course, original.artifact_id)
            }
            assert len(boundary.records) == 2 and repo.counts()["outbound_attempts"] == 0
        finally:
            await executor.close()
            await service.close()


@pytest.mark.host
async def test_malformed_score_stays_pending_preserves_raw_and_does_not_retry_model(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    artifact = draft(repo, course)
    task, row = request(repo, course, artifact)
    snapshot = TaskCoverage(repo).get(course, task.task_id).leaves[0].snapshot_ids[0]
    raw = '<score total="150" max="100"></score> Original invalid score must not be clipped into a result.'
    adapter, _, _, boundary = runtime(tmp_path, repo, source, bare=True)
    boundary.set_scenario({"text_chunks": [feedback(row, snapshot, raw=raw).model_dump_json()]})
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), timeout=30)
            value = EssayStore(repo).get(course, row.round_id)
            assert value.result["status"] == "pending" and value.assessments[0].total is None
            assert (
                value.result["raw_feedback"] == raw
                and "ESSAY_SCORE_RAW_BOUNDS_INVALID" in value.result["diagnostics"]
            )
            assert (
                ArtifactStore(repo).get(course, artifact.artifact_id).head_revision_id
                == artifact.head_revision_id
            )
            assert len(boundary.records) == 1
        finally:
            await executor.close()
            await service.close()


def test_round_occ_scope_and_user_assessment_cas(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    artifact, essays = draft(repo, course), EssayStore(repo)
    task, row = request(repo, course, artifact)
    assert request(repo, course, artifact)[0].task_id == task.task_id
    with pytest.raises(AppError) as error:
        request(repo, course, artifact, operation="duplicate_tab")
    assert error.value.code == "ESSAY_ROUND_VERSION_CONFLICT"
    first = essays.assess(
        course,
        row.round_id,
        EssayAssessmentInput(
            operation_id="self",
            expected_revision=0,
            origin="user_self",
            total=75,
            reason="My self assessment.",
        ),
    )
    assert first.assessments[0].origin == "user_self"
    with pytest.raises(AppError) as error:
        essays.assess(
            course,
            row.round_id,
            EssayAssessmentInput(
                operation_id="stale_self",
                expected_revision=0,
                origin="user_self",
                total=85,
                reason="Other tab.",
            ),
        )
    assert error.value.code == "ESSAY_ASSESSMENT_CONFLICT"
    with pytest.raises(AppError) as error:
        essays.get("other_course", row.round_id)
    assert error.value.code == "ESSAY_ROUND_NOT_FOUND"


@pytest.mark.host
async def test_local_publication_failure_reprocesses_original_output_without_new_model_attempt(
    tmp_path,
):
    repo, course, _, source = learning_course(tmp_path)
    artifact = draft(repo, course)
    task, row = request(repo, course, artifact)
    snapshot = TaskCoverage(repo).get(course, task.task_id).leaves[0].snapshot_ids[0]
    adapter, _, _, boundary = runtime(tmp_path, repo, source, bare=True)
    boundary.set_scenario({"text_chunks": [feedback(row, snapshot).model_dump_json()]})
    fired = []

    def fail(point, detail):
        if point == "learning_essay_feedback_before_commit":
            fired.append(point)
            raise OSError("controlled local commit failure after model completion")

    repo.fault_hook = fail
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), timeout=30)
            failed = TaskStore(repo).get(course, task.task_id)
            assert len(fired) == 1 and failed.state == "failed"
            assert failed.units[0].error_code == "TASK_RESULT_LOCAL_RECOVERY_REQUIRED"
            original_run_id = failed.units[0].run_id
            assert EssayStore(repo).get(course, row.round_id).result is None
            assert len(ArtifactStore(repo).list(course)) == 1 and len(boundary.records) == 1
            repo.fault_hook = None
            retry = TaskRecovery(repo).retry_work(
                course,
                task.task_id,
                failed.units[0].unit_id,
                TaskAction(operation_id="reprocess_original", expected_revision=failed.revision),
            )
            assert retry.units[0].run_id == original_run_id
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=30)
            recovered = TaskStore(repo).get(course, task.task_id)
            assert recovered.state == "completed" and recovered.units[0].run_id == original_run_id
            assert len(boundary.records) == 1
            assert EssayStore(repo).get(course, row.round_id).assessments[0].total == 70
        finally:
            repo.fault_hook = None
            await executor.close()
            await service.close()
