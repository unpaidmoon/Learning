"""Pinned Host book recovery: local commit faults and active page cancellation."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select
from test_native_business_host import runtime
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.service import GenerationService
from learning_app.workbench import book_schema
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.books import BookStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import TaskAction
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_controls import TaskRecovery
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def admit(repo, course, source, *, chapters=1):
    return TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="recovery_book",
            kind="book",
            instruction="按指定目录，完整保留来源并连续生成。",
            coverage_mode="complete",
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "恢复边界合成书"},
            settings=settings(),
            sources_only=True,
            outline=[{"chapter_id": f"part_{i}", "title": f"第{i + 1}章"} for i in range(chapters)],
            outline_supplied=True,
        ),
    )


@asynccontextmanager
async def book_runtime(tmp_path, repo, source, *, pause_second=False):
    adapter, _, registry, boundary = runtime(tmp_path, repo, source, bare=True)
    original = boundary._fixture

    def fixture(handler, request, scenario, row, observer):
        run = TaskRunStore(repo).internal(row["generation_id"])
        contract = run["input_contract"]
        if contract.get("phase") == "outline":
            context = contract["book_context"]
            assert not request.get("tools")
            value = {
                "chapters": [
                    {**c, "source_ids": [s["source_id"] for s in context["sources"]]}
                    for c in context["supplied_outline"]
                ]
            }
        else:
            value = {
                "blocks": [
                    {
                        "block_id": "text",
                        "kind": "markdown",
                        "markdown": "这段已完成内容始终保留。",
                        "attribution": "source_paraphrase",
                        "source_snapshot_ids": [i["snapshot_id"] for i in contract["inputs"]],
                    }
                ]
            }
        scenario = {"text_chunks": [json.dumps(value, ensure_ascii=False)]}
        if pause_second and contract.get("book", {}).get("chapter_id") == "part_1":
            scenario = {
                "text_chunks": ['{"blocks":[', '{"unfinished":true}]}'],
                "pause_after_event": "text_delta",
                "gate": "book_page_active",
            }
        return original(handler, request, scenario, row, observer)

    boundary._fixture = fixture
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            yield executor, adapter, boundary, registry
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []


@pytest.mark.host
@pytest.mark.parametrize("phase", ["outline", "layout"])
async def test_book_commit_failure_reprocesses_original_and_cold_restart_never_regenerates(
    tmp_path, phase
):
    repo, course, _, source = learning_course(tmp_path)
    task = admit(repo, course, source)
    hook = f"learning_book_{phase}_before_commit"
    fired = []

    def fail(point, detail):
        if point == hook:
            fired.append(point)
            raise OSError("synthetic book transaction cut after known model completion")

    repo.fault_hook = fail
    async with book_runtime(tmp_path, repo, source) as (executor, adapter, boundary, registry):
        await asyncio.wait_for(executor._queue.join(), 30)
        failed = TaskStore(repo).get(course, task.task_id)
        assert fired == [hook] and failed.state == "failed"
        unit = next(u for u in failed.units if u.state == "failed")
        assert unit.error_code == "TASK_RESULT_LOCAL_RECOVERY_REQUIRED"
        original = TaskRunStore(repo).internal(unit.run_id)
        assert original["state"] == "completed" and original["body"].strip()
        assert ArtifactStore(repo).list(course) == []
        with repo.engine.connect() as conn:
            assert (
                conn.execute(
                    select(func.count()).select_from(book_schema.book_layouts)
                ).scalar_one()
                == 0
            )
        repo.fault_hook = None
        retried = TaskRecovery(repo).retry_work(
            course,
            task.task_id,
            unit.unit_id,
            TaskAction(operation_id="local_reprocess", expected_revision=failed.revision),
        )
        assert next(u for u in retried.units if u.unit_id == unit.unit_id).run_id == unit.run_id
        executor.schedule(course, task.task_id)
        await asyncio.wait_for(executor._queue.join(), 30)
        done = TaskStore(repo).get(course, task.task_id)
        assert done.state == "completed" and len(boundary.records) == 2
        assert next(u for u in done.units if u.unit_id == unit.unit_id).run_id == unit.run_id
        artifact = ArtifactStore(repo).list(course)[0]
        assert BookStore(repo).get(course, artifact.artifact_id).book_status == "ready"
        assert BookStore(repo).outline(course, task.task_id).confirmation_count == 1
        retained = TaskRunStore(repo).internal(unit.run_id)
        assert retained["body"] == original["body"]
        assert boundary.actual_outbound_attempts == 0
        for scope in registry.scopes.values():
            assert scope["run_id"] in registry.released
    async with book_runtime(tmp_path, repo, source) as (executor, _, boundary, _):
        await asyncio.wait_for(executor._queue.join(), 30)
        assert TaskStore(repo).get(course, task.task_id).state == "completed"
        assert len(boundary.records) == 2 and len(ArtifactStore(repo).list(course)) == 1
    repo.close()


@pytest.mark.host
async def test_active_second_page_stop_keeps_first_and_requires_explicit_retry_after_cold_start(
    tmp_path,
):
    repo, course, _, source = learning_course(tmp_path)
    task = admit(repo, course, source, chapters=2)
    async with book_runtime(tmp_path, repo, source, pause_second=True) as (
        executor,
        adapter,
        boundary,
        _,
    ):
        await asyncio.to_thread(boundary.wait_for_event, "gate_waiting", timeout=20)
        current = TaskStore(repo).get(course, task.task_id)
        active = next(u for u in current.units if u.state == "dispatching")
        first = ArtifactStore(repo).list(course)[0]
        first_page = BookStore(repo).get(course, first.artifact_id).chapters[0].pages[0].layout
        TaskStore(repo).control(
            course,
            task.task_id,
            "pause",
            TaskAction(operation_id="active_pause", expected_revision=current.revision),
        )
        await executor.cancel_task(course, task.task_id)
        await asyncio.wait_for(executor._queue.join(), 25)
        paused = TaskStore(repo).get(course, task.task_id)
        run = TaskRunStore(repo).internal(active.run_id)
        assert paused.state == "paused" and run["state"] == "cancelled"
        assert run["body"] == '{"blocks":['
        assert adapter.host.process.poll() is None and len(boundary.records) == 3
        assert (
            ArtifactStore(repo).get(course, first.artifact_id).head_revision_id
            == first.head_revision_id
        )
        view = BookStore(repo).get(course, first.artifact_id)
        assert len(view.chapters[0].pages) == 1 and not view.chapters[1].pages
    async with book_runtime(tmp_path, repo, source) as (executor, _, boundary, _):
        await asyncio.wait_for(executor._queue.join(), 25)
        paused = TaskStore(repo).get(course, task.task_id)
        assert paused.state == "paused" and len(boundary.records) == 3
        resumed = TaskStore(repo).control(
            course,
            task.task_id,
            "resume",
            TaskAction(operation_id="resume", expected_revision=paused.revision),
        )
        assert resumed.state == "failed"
        retried = TaskRecovery(repo).retry_work(
            course,
            task.task_id,
            active.unit_id,
            TaskAction(operation_id="retry_known_cancel", expected_revision=resumed.revision),
        )
        assert next(u for u in retried.units if u.unit_id == active.unit_id).run_id is None
        executor.schedule(course, task.task_id)
        await asyncio.wait_for(executor._queue.join(), 30)
        done = TaskStore(repo).get(course, task.task_id)
        assert done.state == "completed" and len(boundary.records) == 4
        assert len(ArtifactStore(repo).list(course)) == 1
        view = BookStore(repo).get(course, first.artifact_id)
        assert view.book_status == "ready" and [len(c.pages) for c in view.chapters] == [1, 1]
        assert view.chapters[0].pages[0].layout == first_page
        assert BookStore(repo).outline(course, task.task_id).confirmation_count == 1
        with repo.engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(s.task_runs)).scalar_one() == 4
        assert TaskRunStore(repo).internal(active.run_id)["body"] == '{"blocks":['
    repo.close()
