"""Pinned Windows Host → private transport → actual Repository, synthetic SSE."""

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.boundary import MessagesBoundary
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.service import GenerationService
from learning_app.storage import StorageError
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore

ROOT = Path(__file__).resolve().parents[2]


class BusinessFixture(MessagesBoundary):
    def __init__(self, directory, repo, registry, source, *, task=False):
        super().__init__(directory, native_registry=registry)
        self.repo, self.source, self.task = repo, source, task
        self.steps = {}
        self.requests = []

    def _fixture(self, handler, request, scenario, row, observer):
        run_id = row["generation_id"]
        step = self.steps.get(run_id, 0) + 1
        self.steps[run_id] = step
        self.requests.append(request)
        calls = []
        if step == 1:
            calls = [
                {"id": "native_skill", "name": "skill", "input": {"name": "course-read"}},
                {
                    "id": "read_same_provider_id",
                    "name": "read_source_handle",
                    "input": {
                        "handle_id": self.source.snapshot_id,
                        "source_version": self.source.content_hash,
                    },
                },
            ]
        elif self.task and step == 2:
            run = TaskRunStore(self.repo).internal(run_id)
            target = run["input_contract"]["target"]
            calls = [
                {
                    "id": "stage_course_result",
                    "name": "stage_result",
                    "input": {
                        "task_id": run["task_id"],
                        "object_id": target["reserved_artifact_id"],
                        "expected_version": target["structure_revision"],
                        "stage": "content",
                        "body": json.dumps(
                            {
                                "blocks": [
                                    {
                                        "block_id": "summary",
                                        "markdown": "这份讲义引用实际读取的选区。",
                                        "attribution": "source_paraphrase",
                                        "source_snapshot_ids": [self.source.snapshot_id],
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ]
        fixture = {"text_chunks": [] if calls else ["已完成本次说明。"], "tool_calls": calls}
        return super()._fixture(handler, request, fixture, row, observer)


def runtime(tmp_path, repo, source, *, task=False, bare=False):
    port = NativeBusiness(repo)
    registry = NativeCallRegistry(tmp_path / "native-facts.jsonl")
    transport = NativeToolTransport(registry, port)
    boundary = (
        MessagesBoundary(tmp_path / "boundary", native_registry=registry)
        if bare
        else BusinessFixture(tmp_path / "boundary", repo, registry, source, task=task)
    )
    adapter = ExecutionAdapter(ROOT, tmp_path / "host", boundary=boundary, native_tools=transport)
    return adapter, port, registry, boundary


@pytest.mark.host
async def test_native_tools_read_stage_and_publish_only_through_task_run(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="native_task",
            kind="handout",
            instruction="依据指定选区整理一份讲义",
            coverage_mode="selection",
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "正式任务讲义"},
            settings=settings(),
            sources_only=True,
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id)
    TaskPlanner(repo).plan(course, task.task_id)
    adapter, port, registry, boundary = runtime(tmp_path, repo, source, task=True)
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), 40)
            task = TaskStore(repo).get(course, task.task_id)
            assert task.state == "completed", task.model_dump()
            assert len(task.units) == 1
            run = TaskRunStore(repo).internal(task.units[0].run_id)
            assert run["body"] == "已完成本次说明。"
            asset = ArtifactStore(repo).list(course)
            assert len(asset) == 1 and asset[0].title == "正式任务讲义"
            assert asset[0].revision.body.blocks[0].markdown == "这份讲义引用实际读取的选区。"
            assert asset[0].revision.author == "model"
            assert port.staged_body(run["run_id"]) is not None
            assert len(registry.bindings) == 2  # skill stays native; read + stage reach business.
            assert all(item["state"] == "completed" for item in registry.operations.values())
            assert run["run_id"] in registry.released
            assert boundary.steps[run["run_id"]] == 3
            with repo.engine.connect() as conn:
                scope = port._scope(conn, run["run_id"])
                effects = conn.execute(select(s.native_effects)).mappings().all()
            assert scope["state"] == "released" and len(effects) == 1
            assert boundary.actual_outbound_attempts == 0
            assert "不应补发前后文" not in json.dumps(boundary.requests, ensure_ascii=False)
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()


@pytest.mark.host
async def test_native_qa_continues_same_business_chat_and_reuses_source_handle(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    repo.create_chat(course, "native_chat")
    adapter, port, registry, boundary = runtime(tmp_path, repo, source)
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        runs = []
        try:
            for index in range(2):
                result = await service.submit(
                    {
                        "chat_id": "native_chat",
                        "submission_id": f"input_{index}",
                        "text": "解释这个选区" if index == 0 else "继续解释上次提交的选区",
                        "source_snapshot_ids": [source.snapshot_id] if index == 0 else [],
                        "model": "deepseek-flash",
                        "thinking": "high",
                    }
                )
                await asyncio.wait_for(service.tasks[result["generation_id"]], 40)
                run = repo.get_generation(result["generation_id"])
                assert run["state"] == "completed", run
                assert run["generation_id"] in registry.released
                runs.append(run)
            assert runs[0]["binding_id"] == runs[1]["binding_id"]
            assert [run["epoch"] for run in runs] == [1, 1]
            assert [run["adapter_metadata"]["native_lease_epoch"] for run in runs] == [1, 2]
            assert len({run["adapter_metadata"]["session_id"] for run in runs}) == 1
            assert ArtifactStore(repo).list(course) == []
            assert len(registry.bindings) == 2
            assert len(boundary.records) == 4 and boundary.actual_outbound_attempts == 0
            for request in boundary.requests:
                assert "stage_result" not in {tool["name"] for tool in request.get("tools", [])}
            with repo.engine.connect() as conn:
                assert (
                    conn.execute(
                        select(func.count()).select_from(s.native_source_handles)
                    ).scalar_one()
                    == 2
                )
        finally:
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()


@pytest.mark.host
@pytest.mark.parametrize(
    "fault_point,committed",
    [
        ("native_stage_before_commit", False),
        ("native_stage_after_commit", True),
        ("native_read_after_commit", True),
    ],
)
async def test_native_unknown_effect_is_reconciled_or_keeps_task_unknown(
    tmp_path, fault_point, committed
):
    repo, course, _, source = learning_course(tmp_path)
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="effect_fault",
            kind="handout",
            instruction="整理指定选区",
            coverage_mode="selection",
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "故障窗口成果"},
            settings=settings(),
            sources_only=True,
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id)
    TaskPlanner(repo).plan(course, task.task_id)
    adapter, port, registry, boundary = runtime(tmp_path, repo, source, task=True)
    faults = []

    def fault(point, _detail):
        if point == fault_point:
            faults.append(point)
            raise RuntimeError("synthetic effect receipt loss")

    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        repo.fault_hook = fault
        try:
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), 40)
            current = TaskStore(repo).get(course, task.task_id)
            assert current.state == ("completed" if committed else "unknown"), current.model_dump()
            assert len(ArtifactStore(repo).list(course)) == int(committed)
            assert faults == [fault_point]
            before = len(boundary.records)
            repo.fault_hook = None
            if not committed:
                executor.request_reconciliation(course, task.task_id)
                await asyncio.wait_for(executor._queue.join(), 15)
                current = TaskStore(repo).get(course, task.task_id)
                assert current.state == "unknown"
                assert current.units[0].run_id not in registry.released
                assert len(boundary.records) == before
                assert any(item["state"] == "unknown" for item in registry.operations.values())
            assert boundary.actual_outbound_attempts == 0
        finally:
            repo.fault_hook = None
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()


@pytest.mark.host
async def test_completed_before_release_recovers_from_physical_native_cut_without_prompt(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    repo.create_chat(course, "native_chat")
    config = AppConfig(mode="test", data_dir=repo.root)
    first, port, registry, boundary = runtime(tmp_path, repo, source)
    service = GenerationService(repo, config, execution=first)
    service.owns_execution = True
    await service.open()

    async def unavailable_release(_binding):
        return False  # Explicit fixture: business terminal persisted, control unavailable.

    service.release_native_execution = unavailable_release
    result = await service.submit(
        {
            "chat_id": "native_chat",
            "submission_id": "before_restart",
            "text": "解释选区",
            "source_snapshot_ids": [source.snapshot_id],
            "model": "deepseek-flash",
            "thinking": "high",
        }
    )
    await asyncio.wait_for(service.tasks[result["generation_id"]], 30)
    original = repo.get_generation(result["generation_id"])
    assert original["state"] == "completed" and len(boundary.records) == 2
    assert result["generation_id"] not in registry.released
    await service.close()
    assert first.host.cleanup["remaining_pids"] == []

    second, second_port, recovered_registry, recovered_boundary = runtime(tmp_path, repo, source)
    recovered = GenerationService(repo, config, execution=second)
    recovered.owns_execution = True
    try:
        await recovered.open()
        await asyncio.wait_for(recovered.tasks[result["generation_id"]], 25)
        assert len(recovered_boundary.records) == 2  # No prompt or Messages during recovery.
        assert result["generation_id"] in recovered_registry.released
        assert len(recovered_registry.operations) == 1  # No business tool reinvocation.
        receipt = recovered_registry.release_receipts[result["generation_id"]]
        assert receipt["recovered_from_persisted_cut"] and len(receipt["native_cut_sha256"]) == 64
        assert receipt["native_cursor"] >= receipt["terminal_seq"]
        with repo.engine.connect() as conn:
            assert second_port._scope(conn, result["generation_id"])["state"] == "released"
        follow = await recovered.submit(
            {
                "chat_id": "native_chat",
                "submission_id": "after_restart",
                "text": "继续上次的选区",
                "model": "deepseek-flash",
                "thinking": "high",
            }
        )
        await asyncio.wait_for(recovered.tasks[follow["generation_id"]], 30)
        current = repo.get_generation(follow["generation_id"])
        assert current["state"] == "completed" and current["epoch"] == original["epoch"] == 1
        assert (
            current["adapter_metadata"]["session_id"] == original["adapter_metadata"]["session_id"]
        )
        assert current["adapter_metadata"]["native_lease_epoch"] == 2
        assert (
            len(recovered_boundary.records) == 4
            and recovered_boundary.actual_outbound_attempts == 0
        )
        assert ArtifactStore(repo).list(course) == []
    finally:
        await recovered.close()
    assert second.host.cleanup["remaining_pids"] == []
    repo.close()


@pytest.mark.host
async def test_native_active_cancel_releases_known_owner_and_preserves_raw_usage_unknown(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    repo.create_chat(course, "native_chat")
    port = NativeBusiness(repo)
    registry = NativeCallRegistry(tmp_path / "native-facts.jsonl")
    bridge = NativeToolTransport(registry, port)
    boundary = MessagesBoundary(tmp_path / "boundary", native_registry=registry)
    adapter = ExecutionAdapter(ROOT, tmp_path / "host", native_tools=bridge, boundary=boundary)
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        try:
            boundary.set_scenario(
                {"text_chunks": ["已收到的部分答案"], "gate_before_terminal": "active_cancel"}
            )
            result = await service.submit(
                {
                    "chat_id": "native_chat",
                    "submission_id": "active_cancel",
                    "text": "解释选区",
                    "source_snapshot_ids": [source.snapshot_id],
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )

            async def visible_partial():
                while repo.get_generation(result["generation_id"])["body"] != "已收到的部分答案":
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(visible_partial(), 15)
            assert not service.tasks[result["generation_id"]].done()
            await service.cancel(result["generation_id"])
            await asyncio.wait_for(service.tasks[result["generation_id"]], 15)
            row = repo.get_generation(result["generation_id"])
            assert row["state"] == "cancelled" and row["body"] == "已收到的部分答案"
            assert result["generation_id"] in registry.released
            assert registry.attempts[next(iter(registry.attempts))]["state"] == "closed_cancelled"
            assert boundary.records[0]["status"] == "connection_closed"
            assert service.generation_view(result["generation_id"])["usage"]["status"] == "unknown"
            assert adapter.host.process.poll() is None
            boundary.set_scenario({"text_chunks": ["停止后同一对话继续"]})
            follow = await service.submit(
                {
                    "chat_id": "native_chat",
                    "submission_id": "after_cancel",
                    "text": "继续",
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[follow["generation_id"]], 20)
            assert repo.get_generation(follow["generation_id"])["state"] == "completed"
            assert len(boundary.records) == 2 and boundary.actual_outbound_attempts == 0
            assert ArtifactStore(repo).list(course) == []
        finally:
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()


@pytest.mark.host
async def test_native_tools_keep_exact_document_image_proofs_across_tool_continuations(tmp_path):
    import hashlib
    import runpy

    repo, course, root, _ = learning_course(tmp_path)
    path = root / "synthetic-scan.pdf"
    runpy.run_path(str(ROOT / "tests/fixtures/workbench/generate.py"))["scanned_pdf"](path)
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    opened = CourseFileStore(repo).open(course, path.name)
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="native_pdf",
            kind="handout",
            instruction="整理授权扫描页",
            coverage_mode="complete",
            sources=[
                {
                    "kind": "document",
                    "asset_id": opened["asset_id"],
                    "source_version": opened["source_version"],
                }
            ],
            target={"mode": "create", "title": "扫描页讲义"},
            sources_only=True,
            settings=settings(),
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id)
    TaskPlanner(repo).plan(course, task.task_id)
    leaf = TaskCoverage(repo).get(course, task.task_id).leaves[0]
    source = SourceStore(repo).snapshot(course, leaf.snapshot_ids[0])
    adapter, _, registry, boundary = runtime(tmp_path, repo, source, task=True)
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), 45)
            current = TaskStore(repo).get(course, task.task_id)
            assert current.state == "completed", current.model_dump()
            run = TaskRunStore(repo).internal(current.units[0].run_id)
            proof = run["request_image_proofs"][0]
            assert proof["source_sha256"] != proof["encoded_sha256"]
            assert len(boundary.records) == 3
            assert all(
                [image["sha256"] for image in row["images"]] == [proof["encoded_sha256"]]
                for row in boundary.records
            )
            assert run["run_id"] in registry.released
            assert hashlib.sha256(path.read_bytes()).hexdigest() == original
            assert boundary.actual_outbound_attempts == 0
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()


@pytest.mark.host
async def test_unknown_transport_cold_recovery_never_resends_or_grants_next_owner(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    repo.create_chat(course, "native_chat")
    config = AppConfig(mode="test", data_dir=repo.root)
    first, _, registry, boundary = runtime(tmp_path, repo, source, bare=True)
    boundary.set_scenario({"fault": "drop_response"})
    service = GenerationService(repo, config, execution=first)
    service.owns_execution = True
    await service.open()
    result = await service.submit(
        {
            "chat_id": "native_chat",
            "submission_id": "unknown_original",
            "text": "解释选区",
            "source_snapshot_ids": [source.snapshot_id],
            "model": "deepseek-flash",
            "thinking": "high",
        }
    )
    await asyncio.wait_for(service.tasks[result["generation_id"]], 25)
    assert repo.get_generation(result["generation_id"])["state"] == "unknown"
    assert len(boundary.records) == 1 and result["generation_id"] not in registry.released
    await service.close()
    assert first.host.cleanup["remaining_pids"] == []
    second, _, recovered_registry, recovered_boundary = runtime(tmp_path, repo, source, bare=True)
    recovered = GenerationService(repo, config, execution=second)
    recovered.owns_execution = True
    try:
        await recovered.open()
        await asyncio.wait_for(recovered.tasks[result["generation_id"]], 25)
        assert repo.get_generation(result["generation_id"])["state"] == "unknown"
        assert len(recovered_boundary.records) == 1
        assert result["generation_id"] not in recovered_registry.released
        # Existing active-request guard rejects before creating another owner.
        with pytest.raises(StorageError, match="CHAT_EXECUTION_ACTIVE"):
            await recovered.submit(
                {
                    "chat_id": "native_chat",
                    "submission_id": "after_unknown",
                    "text": "新的明确问题",
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
        assert (
            len(recovered_boundary.records) == 1
            and recovered_boundary.actual_outbound_attempts == 0
        )
        assert ArtifactStore(repo).list(course) == []
    finally:
        await recovered.close()
    assert second.host.cleanup["remaining_pids"] == []
    repo.close()
