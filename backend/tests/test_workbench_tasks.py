import pytest
from pydantic import ValidationError
from workbench_support import learning_course, settings

from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.workbench.models import (
    OutlineItem,
    TaskAction,
    TaskCreate,
    TaskGapDecision,
    TaskOutline,
    TaskUnitInput,
)
from learning_app.workbench.tasks import TaskStore


@pytest.fixture
def course(tmp_path):
    value = learning_course(tmp_path)
    yield value
    value[0].close()


def request(snapshot, **overrides):
    return TaskCreate(
        **{
            "operation_id": "create_task",
            "kind": "handout",
            "instruction": "整理指定材料",
            "settings": settings(),
            "units": [
                TaskUnitInput(
                    unit_id="document_1",
                    label="指定正文",
                    source_snapshot_id=snapshot.snapshot_id,
                )
            ],
            **overrides,
        }
    )


def test_task_freezes_scope_and_settings_and_create_is_idempotent(course):
    repo, course_id, _, snapshot = course
    store = TaskStore(repo)
    value = request(snapshot)
    task = store.create(course_id, value)
    assert task == store.create(course_id, value)
    assert task.state == "queued" and task.settings == settings()
    assert task.units[0].source_snapshot_id == snapshot.snapshot_id
    assert repo.counts()["outbound_attempts"] == 0


def test_book_once_outline_confirmation_or_supplied_outline(course):
    repo, course_id, _, snapshot = course
    store = TaskStore(repo)
    outline = [OutlineItem(chapter_id="chapter_1", title="第一章")]
    task = store.create(course_id, request(snapshot, kind="book", outline=outline))
    assert task.state == "awaiting_outline" and not task.outline_confirmed
    confirmation = TaskOutline(operation_id="confirm_outline", expected_revision=0, outline=outline)
    confirmed = store.confirm_outline(course_id, task.task_id, confirmation)
    assert confirmed.state == "queued" and confirmed.outline_confirmed
    assert store.confirm_outline(course_id, task.task_id, confirmation) == confirmed
    supplied = store.create(
        course_id,
        request(
            snapshot,
            operation_id="book_with_outline",
            kind="book",
            outline=outline,
            outline_supplied=True,
        ),
    )
    assert supplied.state == "queued" and supplied.outline_confirmed


def test_pause_persists_and_recovery_does_not_unpause_or_send(course):
    repo, course_id, _, snapshot = course
    store = TaskStore(repo)
    task = store.create(course_id, request(snapshot))
    pause = TaskAction(operation_id="pause_task", expected_revision=0)
    paused = store.control(course_id, task.task_id, "pause", pause)
    assert paused.state == "paused"
    assert store.control(course_id, task.task_id, "pause", pause) == paused
    reopened = Repository(repo.root)
    try:
        assert TaskStore(reopened).recover()[0].state == "paused"
        assert reopened.counts()["outbound_attempts"] == 0
    finally:
        reopened.close()
    with pytest.raises(AppError) as error:
        store.control(
            course_id,
            task.task_id,
            "resume",
            TaskAction(
                operation_id="stale_resume",
                expected_revision=0,
            ),
        )
    assert error.value.code == "TASK_REVISION_CONFLICT"


def test_missing_source_pauses_with_snapshot_and_explicit_retry(course):
    repo, course_id, root, snapshot = course
    path = root / "lesson.txt"
    content = path.read_bytes()
    path.unlink()
    store = TaskStore(repo)
    task = store.create(course_id, request(snapshot))
    assert task.state == "needs_sources" and task.units[0].state == "missing"
    assert task.units[0].source_snapshot_id == snapshot.snapshot_id
    retry = TaskGapDecision(
        operation_id="retry_gap", expected_revision=0, decision="retry", unit_ids=["document_1"]
    )
    with pytest.raises(AppError):
        store.decide_gap(course_id, task.task_id, retry)
    path.write_bytes(content)
    queued = store.decide_gap(course_id, task.task_id, retry)
    assert queued.state == "queued" and queued.units[0].state == "pending"
    assert store.decide_gap(course_id, task.task_id, retry) == queued


def test_exclusion_retains_old_scope_and_partial_is_never_complete(course):
    repo, course_id, root, snapshot = course
    (root / "lesson.txt").unlink()
    store = TaskStore(repo)
    task = store.create(course_id, request(snapshot))
    excluded = store.decide_gap(
        course_id,
        task.task_id,
        TaskGapDecision(
            operation_id="exclude_gap",
            expected_revision=0,
            decision="exclude",
            unit_ids=["document_1"],
        ),
    )
    assert excluded.scope_revision == 1 and excluded.units[0].excluded_at_scope_revision == 1
    assert (
        excluded.units[0].state == "missing"
        and store.reconcile(course_id, task.task_id).state != "completed"
    )
    other = store.create(course_id, request(snapshot, operation_id="partial_task"))
    partial = store.decide_gap(
        course_id,
        other.task_id,
        TaskGapDecision(
            operation_id="accept_partial",
            expected_revision=0,
            decision="accept_partial",
            unit_ids=["document_1"],
        ),
    )
    assert partial.state == "partial" and partial.scope_revision == 1


def test_sources_only_and_external_permission_do_not_coalesce():
    with pytest.raises(ValidationError):
        TaskCreate(
            operation_id="external_handout",
            kind="handout",
            instruction="明确任务",
            settings=settings(),
            allow_external=True,
        )
    with pytest.raises(ValidationError):
        TaskCreate(
            operation_id="mixed_scope",
            kind="research",
            instruction="明确任务",
            settings=settings(),
            allow_external=True,
            sources_only=True,
        )
