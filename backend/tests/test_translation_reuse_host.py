"""Actual Host result, truthful saved-result reuse, and no new request on lost ACK."""

import asyncio
from copy import deepcopy

import pytest
from test_native_business_host import runtime
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.service import GenerationService
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import (
    ArtifactUpdate,
    BlockChange,
    PageRegion,
    TaskCreate,
    TaskUnitInput,
)
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.tasks import TaskStore


@pytest.mark.host
async def test_saved_translation_reuses_original_run_and_receipt_without_usage_clone_or_implicit_replay(
    tmp_path,
):
    repo, course, root, source = learning_course(tmp_path)
    value = TaskCreate(
        operation_id="first_translation",
        kind="translation",
        instruction="翻译成中文，保留公式和术语。",
        settings=settings(),
        units=[
            TaskUnitInput(
                unit_id="selection_0", label="选区 1", source_snapshot_id=source.snapshot_id
            )
        ],
    )
    task = TaskStore(repo).create(course, value)
    adapter, _, _, boundary = runtime(tmp_path, repo, source, bare=True)
    original = boundary._fixture

    def fixture(handler, request, scenario, row, observer):
        return original(handler, request, {"text_chunks": ["只翻译这句所选文字。"]}, row, observer)

    boundary._fixture = fixture
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        memo = service.translation_reuse
        memo.remember_admission(task)
        same = value.model_copy(update={"operation_id": "same_request"})
        pending = memo.reuse(course, same, task.task_id)
        assert pending.task_id == task.task_id and pending.translation_reuse.kind == "in_progress"
        assert pending.translation_reuse.actual_outbound_attempts == 0
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), 30)
            finished = TaskStore(repo).get(course, task.task_id)
            assert finished.state == "completed" and len(boundary.records) == 1
            run_id = finished.units[0].run_id
            run = TaskRunStore(repo).get(course, task.task_id, run_id)
            assert run.usage_status == "known"
            usage_before = deepcopy(run.model_dump(mode="json"))
            entry = memo.entry(course, task.task_id)
            assert entry.status == "eligible" and entry.text == "只翻译这句所选文字。"
            assert entry.source == source and entry.policy_id
            before = repo.counts()
            cached_request = value.model_copy(update={"operation_id": "use_saved_translation"})
            cached = memo.reuse(course, cached_request, task.task_id)
            assert (
                cached.task_id == task.task_id and cached.translation_reuse.kind == "saved_result"
            )
            assert cached.translation_reuse.original_run_id == run_id
            assert cached.translation_reuse.actual_outbound_attempts == 0
            assert (
                TaskRunStore(repo).get(course, task.task_id, run_id).model_dump(mode="json")
                == usage_before
            )
            assert len(ArtifactStore(repo).list(course)) == 1 and len(boundary.records) == 1
            assert repo.counts() == before
            assert memo.reuse(course, cached_request, None) == cached
            moved_boxes = SourceStore(repo)._save_verified(
                course,
                source.reference.model_copy(
                    update={"regions": [PageRegion(x0=0.1, y0=0.1, x1=0.2, y1=0.2)]}
                ),
                source.text,
                source.content_role,
            )
            same_range = value.model_copy(
                update={
                    "operation_id": "same_characters_new_display_boxes",
                    "units": [
                        TaskUnitInput(
                            unit_id="selection_0",
                            label="选区 1",
                            source_snapshot_id=moved_boxes.snapshot_id,
                        )
                    ],
                }
            )
            assert moved_boxes.snapshot_id != source.snapshot_id
            geometry_reuse = memo.reuse(course, same_range, task.task_id)
            assert geometry_reuse.translation_reuse.kind == "saved_result"
            assert (
                geometry_reuse.translation_reuse.requested_source_snapshot_id
                == moved_boxes.snapshot_id
            )
            assert geometry_reuse.units[0].source_snapshot_id == source.snapshot_id

            # The result was committed locally, but its acknowledgement was lost.
            def lose_ack(name, _detail):
                if name == "translation_reuse_after_commit":
                    raise OSError("lost saved-translation acknowledgement")

            repo.fault_hook = lose_ack
            uncertain = value.model_copy(update={"operation_id": "lost_ack"})
            with pytest.raises(OSError):
                memo.reuse(course, uncertain, task.task_id)
            repo.fault_hook = None
            recovered = memo.reuse(course, uncertain, None)
            assert (
                recovered.task_id == task.task_id
                and recovered.translation_reuse.kind == "saved_result"
            )
            assert len(boundary.records) == 1

            for index, fields in enumerate(
                [
                    {"instruction": "改用英文解释，不是同一次翻译"},
                    {"settings": settings().model_copy(update={"thinking": "none"})},
                    {"settings": settings().model_copy(update={"timeout_seconds": 181})},
                    {"settings": settings().model_copy(update={"max_output_tokens": 8193})},
                ]
            ):
                changed = value.model_copy(update={"operation_id": f"changed_{index}", **fields})
                assert memo.reuse(course, changed, task.task_id) is None
            (root / "other.txt").write_text((root / "lesson.txt").read_text("utf-8"), "utf-8")
            other_file = CourseFileStore(repo).open(course, "other.txt")
            other_source = SourceStore(repo).capture_text(
                course, other_file["asset_id"], other_file["source_version"], 0, 27
            )
            other = value.model_copy(
                update={
                    "operation_id": "same_text_other_source",
                    "units": [
                        TaskUnitInput(
                            unit_id="selection_0",
                            label="选区 1",
                            source_snapshot_id=other_source.snapshot_id,
                        )
                    ],
                }
            )
            assert (
                other_source.text == source.text and memo.reuse(course, other, task.task_id) is None
            )

            with repo.engine.begin() as conn:
                current = TaskStore._row(conn, course, task.task_id)
                TaskStore._event(
                    conn, current, "synthetic_reconciliation_required", {}, state="unknown"
                )
            with pytest.raises(AppError) as error:
                memo.reuse(
                    course,
                    value.model_copy(update={"operation_id": "must_reconcile"}),
                    task.task_id,
                )
            assert error.value.code == "TASK_RECONCILIATION_REQUIRED"
            with repo.engine.begin() as conn:
                current = TaskStore._row(conn, course, task.task_id)
                TaskStore._event(
                    conn, current, "synthetic_reconciliation_settled", {}, state="completed"
                )
            artifact = ArtifactStore(repo).list(course)[0]
            block = artifact.revision.body.blocks[0]
            ArtifactStore(repo).update(
                course,
                artifact.artifact_id,
                ArtifactUpdate(
                    operation_id="manual_translation_edit",
                    expected_revision_id=artifact.head_revision_id,
                    changes=[
                        BlockChange(
                            block_id=block.block_id,
                            replacement=block.model_copy(update={"markdown": "用户手改译文"}),
                        )
                    ],
                ),
            )
            assert memo.entry(course, task.task_id).status == "ineligible"
            assert (
                memo.reuse(
                    course,
                    value.model_copy(update={"operation_id": "do_not_reuse_manual"}),
                    task.task_id,
                )
                is None
            )
            assert (
                TaskRunStore(repo).get(course, task.task_id, run_id).model_dump(mode="json")
                == usage_before
            )
            assert boundary.actual_outbound_attempts == 0 and len(boundary.records) == 1
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
