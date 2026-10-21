import asyncio

import httpx
import pytest

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


async def test_buffered_token_burst_uses_bounded_commits_without_losing_events(tmp_path):
    """V3 STOP regression: per-token FULL commits hid an already finished native turn."""
    from sqlalchemy import event as sqlalchemy_event

    repo, config = setup_store(tmp_path)
    commits = []

    class TokenBurst(ControlledExecution):
        async def observe(self, binding, after=0):
            sqlalchemy_event.listen(repo.engine, "commit", on_commit)
            for index in range(1000):
                yield NormalizedEvent(
                    f"body-{index}", index + 1, binding.generation_id,
                    binding.epoch, "text_delta", text="字",
                )
            yield NormalizedEvent(
                "terminal", 1001, binding.generation_id, binding.epoch,
                "terminal", status="completed",
            )

    def on_commit(connection):
        commits.append(True)

    execution = TokenBurst()
    service = GenerationService(repo, config, execution=execution)
    await service.open()
    result = await service.submit(question())
    await service.tasks[result["generation_id"]]
    sqlalchemy_event.remove(repo.engine, "commit", on_commit)
    generation = repo.get_generation(result["generation_id"])
    assert generation["state"] == "completed"
    assert generation["body"] == "字" * 1000
    assert generation["event_watermark"] == 1001
    assert len(commits) <= 40, f"per-token durability caused backlog: {len(commits)} commits"
    await service.close()
    repo.close()


async def test_user_selection_is_frozen_in_submission_and_forwarded(tmp_path):
    repo, config = setup_store(tmp_path)

    class SelectedExecution(ControlledExecution):
        async def start(self, binding, *args, **kwargs):
            self.selection = (kwargs["model"], kwargs["thinking"])
            return await super().start(binding, *args, **kwargs)

    execution = SelectedExecution()
    service = GenerationService(repo, config, execution=execution)
    await service.open()
    selected = {**question(), "model": "deepseek-v4-pro", "thinking": "none"}
    result = await service.submit(selected)
    await asyncio.wait_for(execution.entered.wait(), 5)
    assert execution.selection == ("deepseek-v4-pro", "none")
    generation = repo.get_generation(result["generation_id"])
    assert (generation["model"], generation["thinking"]) == ("deepseek-v4-pro", "none")
    await service.close()
    repo.close()


@pytest.mark.parametrize("model,thinking,code", [
    ("unavailable-model", "high", "MODEL_SELECTION_UNAVAILABLE"),
    ("deepseek-flash", "medium", "THINKING_SELECTION_UNAVAILABLE"),
    ("deepseek-flash", "xhigh", "THINKING_SELECTION_UNAVAILABLE"),
])
async def test_unsupported_selection_is_rejected_before_publishing(tmp_path, model, thinking, code):
    from learning_app.errors import AppError

    repo, config = setup_store(tmp_path)
    execution = ControlledExecution()
    service = GenerationService(repo, config, execution=execution)
    await service.open()
    with pytest.raises(AppError) as error:
        await service.submit({**question(), "model": model, "thinking": thinking})
    assert error.value.code == code
    assert repo.get_chat("chat")["messages"] == []
    assert execution.starts == 0
    await service.close()
    repo.close()


async def test_confirmed_video_crop_source_is_frozen_without_reading_original(tmp_path):
    import io

    from PIL import Image

    from learning_app.drafts import AttachmentSourceInput, save_attachment_source

    repo, config = setup_store(tmp_path)
    original = tmp_path / "synthetic-source.webm"
    original.write_bytes(b"synthetic source identity; not actual video pixels")
    source = repo.register_source("course", original)
    crop = io.BytesIO()
    Image.new("RGB", (20, 15), "blue").save(crop, format="PNG")
    attachment = repo.stage_attachment(crop.getvalue(), "image/png", confirmed=True)
    provenance = AttachmentSourceInput(
        chat_id="chat", asset_id=source["asset_id"],
        source_version=source["source_version"], time_ms=1200,
        source_width=100, source_height=60,
        crop={"x": 30, "y": 20, "width": 20, "height": 15},
        edit_root_id="original-local-edit",
    )
    save_attachment_source(repo, attachment["attachment_id"], provenance)
    # A confirmed crop remains usable even when its original source is unavailable.
    original.unlink()
    execution = ControlledExecution()
    service = GenerationService(repo, config, execution=execution)
    await service.open()
    result = await service.submit({
        **question(), "attachment_ids": [attachment["attachment_id"]]
    })
    await asyncio.wait_for(execution.entered.wait(), 5)
    user = repo.get_chat("chat")["messages"][0]
    assert user["source_locators"][0]["time_ms"] == 1200
    assert user["source_locators"][0]["crop"] == provenance.crop.model_dump()
    snapshot = repo.get_generation(result["generation_id"])["context_snapshot"]
    assert snapshot["messages"][0]["source_locators"] == user["source_locators"]
    assert snapshot["required_attachment_ids"] == [attachment["attachment_id"]]
    await service.close()
    repo.close()


@pytest.mark.parametrize("old_image", [False, True])
async def test_text_only_model_rejects_current_or_historical_image_without_losing_it(
    tmp_path, old_image
):
    import io

    from PIL import Image

    from learning_app.errors import AppError

    repo, config = setup_store(tmp_path)
    image = io.BytesIO()
    Image.new("RGB", (20, 15), "blue").save(image, format="PNG")
    attachment = repo.stage_attachment(image.getvalue(), "image/png", confirmed=True)
    ids = [attachment["attachment_id"]]
    if old_image:
        prior = repo.publish_submission("chat", "已提交题图", "prior", attachment_ids=ids)
        repo.request_cancel(prior["generation_id"])
    before = repo.get_chat("chat")["messages"]
    service = GenerationService(repo, config, execution=ControlledExecution())
    await service.open()
    with pytest.raises(AppError) as failure:
        await service.submit({
            **question(), "model": "deepseek-v4-pro", "attachment_ids": [] if old_image else ids,
        })
    assert failure.value.code == "MODEL_IMAGE_UNSUPPORTED"
    assert repo.get_chat("chat")["messages"] == before
    assert repo.resolve_attachment(ids[0])["confirmed"]
    assert service.tasks == {}
    await service.close()
    repo.close()


async def test_late_text_only_duplicate_returns_original_receipt_after_later_image(tmp_path):
    import io

    from PIL import Image

    from learning_app.storage import StorageError

    repo, config = setup_store(tmp_path)

    class CompletedExecution(ControlledExecution):
        async def observe(self, binding, after=0):
            yield NormalizedEvent(
                f"terminal:{binding.submission_id}", after + 1, binding.generation_id,
                binding.epoch, "terminal", status="completed",
            )

    execution = CompletedExecution()
    service = GenerationService(repo, config, execution=execution)
    await service.open()
    first_question = {**question(), "model": "deepseek-v4-pro"}
    first = await service.submit(first_question)
    await service.tasks[first["generation_id"]]
    image = io.BytesIO()
    Image.new("RGB", (20, 15), "blue").save(image, format="PNG")
    attachment = repo.stage_attachment(image.getvalue(), "image/png", confirmed=True)
    second = await service.submit({
        **question(), "submission_id": "later-image", "attachment_ids": [attachment["attachment_id"]],
    })
    await service.tasks[second["generation_id"]]
    before = repo.get_chat("chat")["messages"]
    repeated = await service.submit(first_question)
    assert repeated["generation_id"] == first["generation_id"]
    assert repeated["created"] is False and repeated["state"] == "completed"
    with pytest.raises(StorageError) as failure:
        await service.submit({**first_question, "text": "同identity不同内容"})
    assert failure.value.code == "SUBMISSION_CONFLICT"
    assert repo.get_chat("chat")["messages"] == before
    assert execution.starts == 2
    assert set(service.tasks) == {first["generation_id"], second["generation_id"]}
    await service.close()
    repo.close()
