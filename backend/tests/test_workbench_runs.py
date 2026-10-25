"""Task persistence invariants with local normalized events, no Host/provider."""

import pytest
from sqlalchemy import func, select
from workbench_support import learning_course, note, settings

from learning_app.errors import AppError
from learning_app.execution.types import ExecutionBinding, NormalizedEvent
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.models import (
    ArtifactUpdate,
    TaskAction,
    TaskCreate,
    TaskGapDecision,
    TaskUnitInput,
)
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.tasks import TaskStore


@pytest.fixture
def run_fixture(tmp_path):
    repo, course_id, _, snapshot = learning_course(tmp_path)
    task = TaskStore(repo).create(
        course_id,
        TaskCreate(
            operation_id="explicit_task",
            kind="handout",
            instruction="按所选来源整理",
            sources_only=True,
            settings=settings(),
            units=[
                TaskUnitInput(unit_id="unit", label="选区", source_snapshot_id=snapshot.snapshot_id)
            ],
        ),
    )
    runs = TaskRunStore(repo)
    run = runs.claim(course_id, task.task_id, "unit", snapshot.text)
    binding = ExecutionBinding(
        task.task_id,
        "task_request",
        run.run_id,
        1,
        "task_session",
        "task_request",
        native_floor=0,
        native_turn=1,
    )
    runs.persist_binding(run.run_id, binding, "owned_fixture_host")
    assert runs.mark_dispatching(run.run_id)
    yield repo, course_id, snapshot, task, runs, run
    repo.close()


def event(run_id, sequence, kind, **values):
    return NormalizedEvent(f"event_{sequence}", sequence, run_id, 1, kind, **values)


def finish(runs, run):
    runs.ingest(
        run.run_id, event(run.run_id, 1, "text_delta", text="来自来源的完整解释"), source="fixture"
    )
    runs.ingest(
        run.run_id,
        event(run.run_id, 2, "terminal", status="completed", native_seq=10),
        source="fixture",
    )


def test_unknown_recovery_never_creates_chat_or_redispatches(run_fixture):
    repo, course_id, snapshot, task, runs, run = run_fixture
    recovered = TaskStore(repo).recover()[0]
    assert recovered.state == "unknown" and recovered.units[0].state == "unknown"
    assert runs.claim(course_id, task.task_id, "unit", snapshot.text).run_id == run.run_id
    assert not runs.mark_dispatching(run.run_id)
    resumed = TaskStore(repo).control(
        course_id,
        task.task_id,
        "resume",
        TaskAction(
            operation_id="resume",
            expected_revision=recovered.revision,
        ),
    )
    assert resumed.state == "unknown"
    with pytest.raises(AppError, match="缺失或失败"):
        TaskStore(repo).decide_gap(
            course_id,
            task.task_id,
            TaskGapDecision(
                operation_id="retry_unknown",
                expected_revision=resumed.revision,
                decision="retry",
                unit_ids=["unit"],
            ),
        )
    counts = repo.counts()
    assert counts["chats"] == counts["generations"] == counts["outbound_attempts"] == 0


def test_frozen_run_input_and_actual_attempt_ownership_cannot_change(run_fixture):
    _, course_id, _, task, runs, run = run_fixture
    with pytest.raises(AppError) as error:
        runs.claim(course_id, task.task_id, "unit", "changed input")
    assert error.value.code == "TASK_RUN_INPUT_CONFLICT"
    wrong = attempt(run.run_id, model="wrong-model")
    with pytest.raises(AppError) as error:
        runs.record_attempts(run.run_id, [wrong])
    assert error.value.code == "TASK_ATTEMPT_OWNER_CONFLICT"
    assert runs.get(course_id, task.task_id, run.run_id).settings == settings()


def attempt(run_id, **values):
    return {
        "attempt_id": "boundary_request",
        "request_id": "boundary_request",
        "generation_id": run_id,
        "model": "deepseek-flash",
        "thinking": "high",
        "kind": "model",
        "actual_outbound": False,
        "actual_outbound_attempt": None,
        "status": "completed",
        "received_ns": 1,
        "finished_ns": 2,
        "request_sha256": "a" * 64,
        **values,
    }


def test_usage_cumulative_settlement_is_not_added_twice_and_missing_attempt_is_unknown(run_fixture):
    _, course_id, _, task, runs, run = run_fixture
    raw = {
        "settled": True,
        "field_presence": {"input_tokens": True, "output_tokens": True},
        "normalized": {"input_tokens": 3, "output_tokens": 5},
        "observations": [{"output_tokens": 5}, {"output_tokens": 5}],
    }
    usage = event(
        run.run_id,
        1,
        "usage",
        raw_usage=raw,
        usage_presence=raw["field_presence"],
        data={"request_id": "boundary_request", "settlement_id": "provider:boundary_request:usage"},
    )
    assert runs.ingest(run.run_id, usage, source="fixture")
    assert runs.get(course_id, task.task_id, run.run_id).usage_status == "unknown"
    runs.record_attempts(run.run_id, [attempt(run.run_id)])
    duplicate = event(
        run.run_id, 2, "usage", raw_usage=raw, usage_presence=raw["field_presence"], data=usage.data
    )
    runs.ingest(run.run_id, duplicate, source="fixture")
    result = runs.get(course_id, task.task_id, run.run_id)
    assert (
        result.usage_status == "known"
        and result.totals.input_tokens == 3
        and result.totals.output_tokens == 5
    )
    assert result.totals.reasoning_tokens is None and result.actual_outbound_attempts == 0
    assert len(result.usage) == 1
    changed = {**raw, "normalized": {"input_tokens": 3, "output_tokens": 6}}
    with pytest.raises(AppError) as error:
        runs.ingest(
            run.run_id,
            event(run.run_id, 3, "usage", raw_usage=changed, data=usage.data),
            source="fixture",
        )
    assert error.value.code == "TASK_USAGE_CONFLICT"


def test_generated_publication_is_single_atomic_product_and_role_is_preserved(run_fixture):
    repo, course_id, snapshot, task, runs, run = run_fixture
    finish(runs, run)
    store = ArtifactStore(repo)
    value = note("model_supplied_id", snapshot_id=snapshot.snapshot_id)
    published = store.publish_run_result(run.run_id, value)
    assert published.artifact.content_role == "ai_generated"
    complete = TaskStore(repo).reconcile(course_id, task.task_id)
    assert complete.state == "completed"
    repeated = value.model_copy(update={"operation_id": "different_model_id"})
    assert store.publish_run_result(run.run_id, repeated) == published
    with repo.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(s.artifacts)) == 1
    with pytest.raises(AppError) as error:
        store.publish_run_result(
            run.run_id, note("third_id", text="new body", snapshot_id=snapshot.snapshot_id)
        )
    assert error.value.code == "LEARNING_OPERATION_CONFLICT"
    selected = SourceStore(repo).capture_artifact(
        course_id,
        published.artifact.artifact_id,
        published.artifact.head_revision_id,
        "paragraph_1",
    )
    assert selected.content_role == "ai_generated"
    mixed = store.update(
        course_id,
        published.artifact.artifact_id,
        ArtifactUpdate(
            operation_id="manual_change",
            expected_revision_id=published.artifact.head_revision_id,
            title="用户改标题",
        ),
    )
    assert mixed.artifact.content_role == "mixed"


def test_model_cannot_claim_user_authorship_or_publish_from_normal_chat(run_fixture):
    repo, _, _, _, runs, run = run_fixture
    finish(runs, run)
    with pytest.raises(AppError) as error:
        ArtifactStore(repo).publish_run_result(run.run_id, note())
    assert error.value.code == "TASK_USER_AUTHOR_DENIED"
    with pytest.raises(AppError) as error:
        ArtifactStore(repo).publish_run_result("ordinary_chat_generation", note())
    assert error.value.code == "TASK_RUN_NOT_FOUND"
    assert not hasattr(TaskStore(repo), "bind_generation")


def test_result_publish_fault_rolls_back_index_receipt_and_unit_reference(run_fixture):
    repo, course_id, snapshot, task, runs, run = run_fixture
    finish(runs, run)

    def failure(point, _detail):
        if point == "learning_run_product_before_commit":
            raise RuntimeError("injected product/coverage boundary")

    repo.fault_hook = failure
    with pytest.raises(RuntimeError):
        ArtifactStore(repo).publish_run_result(run.run_id, note(snapshot_id=snapshot.snapshot_id))
    assert ArtifactStore(repo).list(course_id) == []
    assert TaskStore(repo).get(course_id, task.task_id).units[0].result_artifact_id is None
    repo.fault_hook = None
    result = ArtifactStore(repo).publish_run_result(
        run.run_id, note(snapshot_id=snapshot.snapshot_id)
    )
    assert result.outcome == "applied"
    assert runs.get(course_id, task.task_id, run.run_id).state == "completed"


def test_stale_event_identity_epoch_and_final_late_delta_are_rejected(run_fixture):
    _, _, _, _, runs, run = run_fixture
    first = event(run.run_id, 1, "text_delta", text="partial")
    assert runs.ingest(run.run_id, first, source="fixture")
    assert not runs.ingest(run.run_id, first, source="fixture")
    first.text = "changed"
    with pytest.raises(AppError) as error:
        runs.ingest(run.run_id, first, source="fixture")
    assert error.value.code == "TASK_RUN_EVENT_CONFLICT"
    stale = event(run.run_id, 2, "text_delta", text="stale")
    stale.epoch = 0
    with pytest.raises(AppError):
        runs.ingest(run.run_id, stale, source="fixture")
    runs.ingest(run.run_id, event(run.run_id, 3, "terminal", status="cancelled"), source="fixture")
    with pytest.raises(AppError) as error:
        runs.ingest(run.run_id, event(run.run_id, 4, "text_delta", text="late"), source="fixture")
    assert error.value.code == "TASK_RUN_ALREADY_FINAL"
