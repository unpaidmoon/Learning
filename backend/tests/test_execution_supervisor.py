"""Component recovery is bounded and reconciles original work, never resubmits it."""

import asyncio
from types import SimpleNamespace

import pytest

from learning_app.app import create_app
from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.execution_supervisor import ExecutionSupervisor
from learning_app.workbench.native_business import NativeBusiness


async def until(predicate, timeout=20):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_failed_component_recovery_stops_at_limit_without_any_new_submission():
    calls = []

    class Service:
        owns_execution = True
        execution_ready = True
        execution_error = None
        execution = SimpleNamespace(host=SimpleNamespace(process=SimpleNamespace(poll=lambda: 1)))

        async def open(self):
            calls.append("open")
            self.execution_ready = False
            self.execution_error = "CONTROLLED_UNAVAILABLE"

        async def close(self, **_):
            calls.append("close")

    class Tasks:
        async def suspend_for_recovery(self):
            calls.append("suspend")

        async def resume_after_recovery(self):
            raise AssertionError("unavailable component cannot resume work")

    async def close_gateway():
        calls.append("gateway_close")

    app = SimpleNamespace(
        state=SimpleNamespace(
            service=Service(),
            task_executor=Tasks(),
            desktop_gateway=SimpleNamespace(close=close_gateway),
            submission_gate=asyncio.Lock(),
            exit_prepared=False,
        )
    )
    supervisor = ExecutionSupervisor(app, delays=(0, 0))
    supervisor.start()
    try:
        await until(lambda: supervisor.state == "unavailable")
        assert calls.count("open") == 2 and supervisor.used == 2
        await asyncio.sleep(0.3)
        assert calls.count("open") == 2
    finally:
        await supervisor.close()


@pytest.mark.host
@pytest.mark.asyncio
async def test_actual_host_crash_recovers_without_replaying_active_original_request(tmp_path):
    created = []

    def factory(repo, config):
        adapter = ExecutionAdapter(
            PROJECT_ROOT,
            config.data_dir / "execution",
            mode="fixture",
            thinking="none",
            timeout=60,
            desktop_enabled=True,
            native_tools=NativeToolTransport(
                NativeCallRegistry(config.data_dir / "execution/native-calls.jsonl"),
                NativeBusiness(repo),
            ),
        )
        created.append(adapter)
        return adapter

    config = AppConfig(
        mode="test",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "runtime",
        desktop_enabled=True,
        thinking="none",
        secret_file=tmp_path / "NO_SECRETS",
        model_timeout_seconds=60,
    )
    app = create_app(config, execution_factory=factory)
    async with app.router.lifespan_context(app):
        repo, service = app.state.repository, app.state.service
        repo.create_course("自造恢复验证", "course")
        repo.create_chat("course", "chat")
        supervisor = app.state.execution_supervisor
        supervisor.delays = (0.01, 0.02)
        original = service.execution
        original.boundary.set_scenario(
            {
                "text_chunks": ["saved before owned crash"],
                "gate_before_terminal": True,
                "gate": "owned_crash_gate",
            }
        )
        submitted = await service.submit(
            {
                "chat_id": "chat",
                "submission_id": "original_question",
                "text": "owned active crash fixture",
                "model": "deepseek-flash",
                "thinking": "none",
            }
        )
        generation = submitted["generation_id"]
        await until(lambda: "saved before owned crash" in repo.get_generation(generation)["body"])
        assert len(original.boundary.records) == 1
        old_request_id = original.boundary.records[0]["request_id"]
        # Only this test's live owned Popen handle is terminated.
        original.host.process.kill()
        await until(lambda: supervisor.used == 1 and supervisor.state == "ready", timeout=40)
        assert service.execution is not original and len(created) == 2
        await until(lambda: not any(not task.done() for task in service.tasks.values()), timeout=20)
        after = repo.get_generation(generation)
        assert after["body"].startswith("saved before owned crash")
        assert not after["cancel_requested"]
        assert len(service.execution.boundary.records) == 1
        assert service.execution.boundary.records[0]["request_id"] == old_request_id
        assert service.execution.boundary.actual_outbound_attempts == 0
        repo.create_chat("course", "new_chat")
        follow = await service.submit(
            {
                "chat_id": "new_chat",
                "submission_id": "explicit_new_question",
                "text": "separate explicit new fixture",
                "model": "deepseek-flash",
                "thinking": "none",
            }
        )
        await asyncio.wait_for(service.tasks[follow["generation_id"]], 25)
        assert repo.get_generation(follow["generation_id"])["state"] == "completed"
        assert len(service.execution.boundary.records) == 2
