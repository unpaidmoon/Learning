"""Import history isolation before startup, direct calls and native binding reuse."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_native_business import call, prepare_chat, stage_args, task_scope
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.desktop import DesktopGateway
from learning_app.errors import AppError
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.boundary import MessagesBoundary
from learning_app.execution.native_tools import (
    NativeCallRegistry,
    NativeToolTransport,
    ToolRejected,
)
from learning_app.imported_executions import ImportedExecutions
from learning_app.service import GenerationService
from learning_app.workbench import schema as s
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def seed(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    submitted, binding = prepare_chat(repo, course, source, "old_unknown")
    port = NativeBusiness(repo)
    port.ensure_chat_scope(binding.generation_id, binding)
    repo.mark_unknown(binding.generation_id, "original_network_unknown")
    repo.create_chat(course, "pending_chat")
    pending = repo.publish_submission("pending_chat", "old pending", "old_pending")
    task, run, task_binding, _, _ = task_scope(
        repo, course, source, operation="old_prepared", dispatch=False
    )
    queued = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="old_queued",
            kind="handout",
            instruction="old queued",
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            sources_only=True,
            target={"mode": "create", "title": "old queued"},
            settings=settings(),
        ),
    )
    return repo, course, source, submitted, binding, pending, task, run, task_binding, queued


class NoDispatch:
    mode = "fixture"
    question_answering_ready = True

    def __init__(self, repo, tmp_path):
        self.restored = []
        self.started = []
        self.native_tools = SimpleNamespace(
            port=NativeBusiness(repo), registry=NativeCallRegistry(tmp_path / "native.jsonl")
        )
        self.host = SimpleNamespace(workspace=tmp_path)
        self.peer = SimpleNamespace(rpc=self.rpc)
        self.sessions = []

    async def open(self):
        pass

    def restore_execution_policy(self, *args, **kwargs):
        self.restored.append(args)

    async def prepare_task(self, *args):
        self.started.append(args)
        raise AssertionError("OLD_TASK_DISPATCHED")

    async def start(self, *args, **kwargs):
        self.started.append(args)
        raise AssertionError("OLD_GENERATION_DISPATCHED")

    async def rpc(self, method, args):
        self.sessions.append((method, args))
        return {"sessionId": args["sessionId"]}


@pytest.mark.asyncio
async def test_imported_pending_prepared_queued_unknown_never_restore_or_dispatch(tmp_path):
    repo, course, _, submitted, binding, pending, task, run, task_binding, queued = seed(tmp_path)
    with repo.engine.connect() as connection:
        scope_before = list(connection.execute(select(s.native_scopes)).mappings())
    fence = ImportedExecutions.install(repo, "package_fixture", "a" * 64)
    assert fence.contains("generation", pending["generation_id"]) and fence.contains(
        "task", queued.task_id
    )
    assert repo.current_binding("native_chat")["active_generation_id"] == binding.generation_id
    assert repo.current_binding("native_chat")["state"] == "retired"
    runtime = NoDispatch(repo, tmp_path)
    service = GenerationService(repo, AppConfig(mode="test", data_dir=repo.root), execution=runtime)
    executor = TaskExecutor(repo, service)
    await service.open()
    await executor.open()
    await asyncio.wait_for(executor._queue.join(), 3)
    assert not service.tasks and not runtime.restored and not runtime.started
    assert TaskStore(repo).get(course, queued.task_id).units == []
    for operation in [
        service._dispatch(binding.generation_id),
        service.reconcile(binding.generation_id),
        service.cancel(binding.generation_id),
        executor.execute_unit(course, task.task_id, task.units[0].unit_id),
        executor.reconcile_run(course, task.task_id, run.run_id),
    ]:
        with pytest.raises(AppError) as error:
            await operation
        assert error.value.code == "IMPORTED_EXECUTION_READ_ONLY"
    with pytest.raises(AppError):
        executor.schedule(course, queued.task_id)
    port = runtime.native_tools.port
    with pytest.raises(AppError):
        port.restore_scope(binding.generation_id, binding)
    args = stage_args(repo, run, SimpleNamespace(snapshot_id="unused"))
    with pytest.raises(ToolRejected) as rejected:
        port.invoke(call(task_binding, "stage_result", args), args)
    assert rejected.value.code == "IMPORTED_EXECUTION_READ_ONLY"
    with repo.engine.connect() as connection:
        assert list(connection.execute(select(s.native_scopes)).mappings()) == scope_before
    assert repo.get_generation(binding.generation_id)["state"] == "unknown"
    assert repo.get_generation(pending["generation_id"])["state"] == "pending"
    await executor.close()
    await service.close()
    repo.close()


@pytest.mark.asyncio
async def test_terminal_imported_chat_gets_fresh_sid_and_unknown_keeps_active_history(tmp_path):
    repo, course, _, _, binding, _, _, _, _, _ = seed(tmp_path)
    repo.create_chat(course, "terminal_chat")
    old = repo.bind_execution("terminal_chat", "old_terminal_sid")
    ImportedExecutions.install(repo, "package_fixture", "b" * 64)
    execution = NoDispatch(repo, tmp_path)
    service = GenerationService(
        repo, AppConfig(mode="test", data_dir=repo.root, desktop_enabled=True), execution=execution
    )
    service.execution_ready = True
    gateway = DesktopGateway(
        SimpleNamespace(state=SimpleNamespace(service=service, config=service.config, ready=True))
    )
    history = await gateway.open_chat("native_chat")
    assert history["read_only"] and history["has_unresolved_history"] and len(execution.sessions) == 0
    value = await gateway.open_chat("terminal_chat")
    assert value["session_id"] != "old_terminal_sid" and value["epoch"] == old["epoch"] + 1
    assert len(execution.sessions) == 1
    with pytest.raises(AppError) as error:
        await gateway.open_chat("native_chat", history_view=False)
    assert error.value.code == "IMPORTED_EXECUTION_READ_ONLY"
    assert (
        len(execution.sessions) == 1
        and repo.current_binding("native_chat")["active_generation_id"] == binding.generation_id
    )
    with pytest.raises(AppError):
        gateway.mapping("old_terminal_sid")
    await gateway.close()
    repo.close()


@pytest.mark.parametrize(
    "point", ["data_import_fence_before_commit", "data_import_fence_after_commit"]
)
def test_fence_commit_fault_is_fail_closed_and_same_package_reconciles_once(tmp_path, point):
    repo, _, _, _, binding, _, _, _, _, _ = seed(tmp_path)

    def fault(name, detail):
        if name == point:
            raise RuntimeError("owned import interruption")

    repo.fault_hook = fault
    with pytest.raises(RuntimeError):
        ImportedExecutions.install(repo, "package_fixture", "c" * 64)
    with pytest.raises(AppError) as error:
        ImportedExecutions(repo)
    assert error.value.code == "IMPORTED_EXECUTION_NOT_READY"
    repo.fault_hook = None
    completed = ImportedExecutions.install(repo, "package_fixture", "c" * 64)
    again = ImportedExecutions.install(repo, "package_fixture", "c" * 64)
    assert again.import_id == completed.import_id
    with repo.engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM data_import_events").scalar_one() == 1
        )
    assert repo.get_generation(binding.generation_id)["state"] == "unknown"
    repo.close()


def test_missing_or_changed_marker_never_recreates_lease_authority(tmp_path):
    repo, _, _, _, _, _, _, _, _, _ = seed(tmp_path)
    ImportedExecutions.install(repo, "package_fixture", "d" * 64)
    path = repo.root / "data-import.json"
    original = path.read_text("utf-8")
    path.unlink()
    with pytest.raises(AppError):
        ImportedExecutions(repo)
    value = json.loads(original)
    value["fence_sha256"] = "0" * 64
    path.write_text(json.dumps(value), "utf-8")
    with pytest.raises(AppError):
        ImportedExecutions(repo)
    repo.close()


@pytest.mark.host
@pytest.mark.asyncio
async def test_actual_cold_harness_does_not_replay_imported_owners_and_new_question_runs(tmp_path):
    repo, course, _, _, _, _, _, _, _, _ = seed(tmp_path)
    ImportedExecutions.install(repo, "package_actual_host", "e" * 64)
    registry = NativeCallRegistry(tmp_path / "fresh-native.jsonl")
    boundary = MessagesBoundary(tmp_path / "fresh-boundary", native_registry=registry)
    boundary.set_scenario({"text_chunks": ["新的明确问题正常完成，旧导入请求未派发。"]})
    adapter = ExecutionAdapter(
        Path(__file__).resolve().parents[2],
        tmp_path / "fresh-host",
        boundary=boundary,
        native_tools=NativeToolTransport(registry, NativeBusiness(repo)),
    )
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        executor = TaskExecutor(repo, service)
        try:
            await service.open()
            await executor.open()
            await asyncio.wait_for(executor._queue.join(), 10)
            assert boundary.records == [] and not service.tasks
            repo.create_chat(course, "new_after_import")
            submitted = await service.submit(
                {
                    "chat_id": "new_after_import",
                    "submission_id": "explicit_new_question",
                    "text": "这是导入后用户明确提出的新问题。",
                    "model": "deepseek-flash",
                    "thinking": "none",
                }
            )
            await asyncio.wait_for(service.tasks[submitted["generation_id"]], 40)
            assert repo.get_generation(submitted["generation_id"])["state"] == "completed"
            assert (
                len(boundary.records) == 1
                and boundary.records[0]["generation_id"] == submitted["generation_id"]
            )
            assert boundary.actual_outbound_attempts == 0
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
