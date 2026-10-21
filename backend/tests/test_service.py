import asyncio

import httpx

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.execution.types import ExecutionBinding, ExecutionError, NormalizedEvent
from learning_app.service import GenerationService
from learning_app.storage import Repository


def setup_store(tmp_path):
    repo = Repository(tmp_path / "data")
    repo.create_course("数学", "course")
    repo.create_chat("course", "chat")
    config = AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run")
    return repo, config


def question():
    return {
        "chat_id": "chat",
        "submission_id": "submission",
        "text": "检查当前步骤",
        "model": "deepseek-flash",
        "thinking": "high",
    }


class ControlledExecution:
    def __init__(self, *, block_prepare=False):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block_prepare = block_prepare
        self.starts = 0

    async def prepare(self, chat, submission, generation, epoch):
        if self.block_prepare:
            self.entered.set()
            await self.release.wait()
        return ExecutionBinding(chat, submission, generation, epoch, "session", submission)

    async def start(self, binding, *args, **kwargs):
        self.starts += 1
        binding.accepted = True
        binding.status = "accepted"
        return binding

    async def observe(self, binding, after=0):
        yield NormalizedEvent(
            "activity", after + 1, binding.generation_id, binding.epoch, "activity"
        )
        self.entered.set()
        await self.release.wait()

    def attempts_for(self, generation_id):
        return []

    async def cancel(self, binding):
        raise ExecutionError("CONNECTION_LOST")

    async def reconcile(self, binding, after=0):
        raise ExecutionError("CONNECTION_LOST")


async def test_shutdown_before_dispatch_preserves_confirmed_unstarted_submission(tmp_path):
    repo, config = setup_store(tmp_path)
    execution = ControlledExecution(block_prepare=True)
    service = GenerationService(repo, config, execution=execution)
    await service.open()
    result = await service.submit(question())
    await asyncio.wait_for(execution.entered.wait(), 5)
    await service.close()
    assert repo.get_generation(result["generation_id"])["state"] == "pending"
    assert repo.recoverable_generations()[0]["generation_id"] == result["generation_id"]
    assert execution.starts == 0
    assert len(repo.get_chat("chat")["messages"]) == 2
    repo.close()


async def test_pending_cancel_fences_scheduled_dispatch(tmp_path):
    repo, config = setup_store(tmp_path)
    execution = ControlledExecution()
    service = GenerationService(repo, config, execution=execution)
    await service.open()
    result = await service.submit(question())
    # No yield to the scheduled dispatcher: cancellation is definitely before claim.
    await service.cancel(result["generation_id"])
    await service.tasks[result["generation_id"]]
    assert execution.starts == 0
    assert repo.get_generation(result["generation_id"])["state"] == "cancelled"
    await service.close()
    repo.close()


async def test_cancel_and_reconciliation_transport_failure_preserve_uncertainty(tmp_path):
    repo, config = setup_store(tmp_path)
    execution = ControlledExecution()
    service = GenerationService(repo, config, execution=execution)
    await service.open()
    result = await service.submit(question())
    await asyncio.wait_for(execution.entered.wait(), 5)
    await service.cancel(result["generation_id"])
    value = repo.get_generation(result["generation_id"])
    assert value["state"] == "unknown"
    assert value["cancel_requested"]
    await service.reconcile(result["generation_id"])
    assert repo.get_generation(result["generation_id"])["state"] == "unknown"
    assert execution.starts == 1
    await service.close()
    repo.close()


def test_public_generation_aggregates_settlements_without_leaking_binding(tmp_path):
    repo, config = setup_store(tmp_path)
    result = repo.publish_submission("chat", "问题", "submission")
    gid = result["generation_id"]
    binding = repo.bind_execution("chat", "session")
    repo.claim_dispatch(
        gid,
        binding["binding_id"],
        binding["epoch"],
        adapter_metadata={"session_id": "private", "request_id": "private"},
    )
    for index, output in enumerate([7, 0]):
        repo.ingest_event(
            gid,
            binding_id=binding["binding_id"],
            epoch=binding["epoch"],
            event_id=f"usage-{index}",
            seq=index,
            kind="usage",
            raw_usage={"input_tokens": 3, "output_tokens": output},
        )
    service = GenerationService(repo, config)
    value = service.generation_view(gid)
    assert "adapter_metadata" not in value
    assert "context_snapshot" not in value
    assert value["usage"]["input_tokens"] == 6
    assert value["usage"]["output_tokens"] == 7
    assert value["usage"]["status"] == "known"
    repo.ingest_event(
        gid,
        binding_id=binding["binding_id"],
        epoch=binding["epoch"],
        event_id="usage-missing",
        seq=3,
        kind="usage",
        raw_usage=None,
    )
    value = service.generation_view(gid)
    assert value["usage"]["input_tokens"] is None
    assert value["usage"]["output_tokens"] is None
    assert value["usage"]["status"] == "unknown"
    assert len(value["usage"]["raw"]["settlements"]) == 3
    repo.close()


async def test_http_cancel_remains_schedulable_during_buffered_stream(tmp_path):
    """An already buffered provider stream must not starve the user's HTTP stop."""
    body_saved = asyncio.Event()

    def on_event(name, details):
        if name == "event_after_commit" and details.get("kind") == "body":
            body_saved.set()

    repo = Repository(tmp_path / "data", fault_hook=on_event)
    repo.create_course("数学", "course")
    repo.create_chat("course", "chat")
    config = AppConfig(mode="test", data_dir=tmp_path / "data")

    class BufferedExecution(ControlledExecution):
        cancelled = False

        async def observe(self, binding, after=0):
            for index in range(300):
                if self.cancelled:
                    break
                yield NormalizedEvent(
                    f"body-{index}",
                    index + 1,
                    binding.generation_id,
                    binding.epoch,
                    "text_delta",
                    text="当前活动正文。",
                )
            yield NormalizedEvent(
                "terminal",
                301,
                binding.generation_id,
                binding.epoch,
                "terminal",
                status="cancelled" if self.cancelled else "completed",
            )

        async def cancel(self, binding):
            self.cancelled = True
            return {"accepted": True}

    execution = BufferedExecution()
    app = create_app(config, repository=repo, execution=execution)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765"
        ) as client:
            token = (await client.get("/api/v1/session")).json()["csrf_token"]
            client.headers["X-Learning-CSRF"] = token

            async def click_stop():
                await body_saved.wait()
                gid = repo.get_submission("submission")["generation_id"]
                response = await client.post(f"/api/v1/generations/{gid}/cancel")
                assert response.status_code == 200

            click = asyncio.create_task(click_stop())
            response = await client.post("/api/v1/submissions", json=question())
            assert response.status_code == 200
            gid = response.json()["generation_id"]
            await click
            await app.state.service.tasks[gid]
            assert execution.cancelled, "HTTP stop was starved until the buffered stream completed"
            assert repo.get_generation(gid)["state"] == "cancelled"
