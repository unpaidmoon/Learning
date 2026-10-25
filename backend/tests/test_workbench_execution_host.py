"""Actual pinned Host, deterministic local Messages, synthetic text only."""

import json
from pathlib import Path

import pytest

from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.types import ExecutionError, ExecutionPolicy

REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.mark.host
async def test_task_context_and_parameters_isolated_in_same_host(tmp_path):
    adapter = ExecutionAdapter(REPOSITORY, tmp_path / "runtime", max_tokens=8192)
    async with adapter:
        adapter.boundary.set_scenario({"text_chunks": ["chat answer"]})
        chat = await adapter.prepare("chat", "chat_submission", "chat_generation")
        await adapter.start(chat, "PRIVATE_CHAT_SENTINEL is only in the chat.")
        chat_events = [event async for event in adapter.observe(chat)]
        assert any(event.status == "completed" for event in chat_events)
        task = await adapter.prepare_task("task_A", "selected_text", "task_run_A", "task_request_A")
        assert task.session_id != chat.session_id
        policy = ExecutionPolicy("deepseek-flash", "low", 512, 60, allowed_image_sha256=())
        adapter.boundary.set_scenario({"text_chunks": ["译文"]})
        await adapter.start(
            task, "Translate SELECTED_ONLY", model="deepseek-flash", thinking="low", policy=policy
        )
        result = [event async for event in adapter.observe(task)]
        assert any(event.status == "completed" for event in result)
        assert "".join(event.text for event in result if event.kind == "text_delta") == "译文"
        row = next(row for row in adapter.boundary.records if row["generation_id"] == "task_run_A")
        request = json.loads(
            (adapter.boundary.private / f"{row['request_id']}.request.json").read_text("utf-8")
        )
        assert request["max_tokens"] == 512
        assert request["output_config"]["effort"] == "low"
        assert "SELECTED_ONLY" in json.dumps(request["messages"])
        assert "PRIVATE_CHAT_SENTINEL" not in json.dumps(request)
        assert "Current working directory" not in json.dumps(request)
        assert tmp_path.name not in json.dumps(request)
        assert request.get("tools", []) == [] and row["images"] == []
        assert adapter.boundary.actual_outbound_attempts == 0
        assert adapter.host.process.poll() is None
        with pytest.raises(ExecutionError, match="IMMUTABLE"):
            await adapter.register_execution_policy(
                task, ExecutionPolicy("deepseek-flash", "high", 1024, 60)
            )
    assert adapter.host.cleanup["remaining_pids"] == []


@pytest.mark.host
async def test_task_active_cancel_preserves_chat_host_and_partial_text(tmp_path):
    adapter = ExecutionAdapter(REPOSITORY, tmp_path / "runtime")
    async with adapter:
        policy = ExecutionPolicy("deepseek-flash", "none", 1024, 60, allowed_image_sha256=())
        adapter.boundary.set_scenario(
            {
                "text_chunks": ["partial", "unreleased"],
                "pause_after_event": "text_delta",
                "gate": "cancel_task",
            }
        )
        task = await adapter.prepare_task("task_B", "unit", "task_run_B", "task_request_B")
        await adapter.start(task, "Only task material", thinking="none", policy=policy)
        events = []
        async for event in adapter.observe(task):
            events.append(event)
            if event.kind == "text_delta":
                assert (await adapter.cancel(task))["accepted"]
        assert "".join(event.text for event in events if event.kind == "text_delta") == "partial", [
            event.to_dict() for event in events
        ]
        assert any(event.status == "cancelled" for event in events)
        assert adapter.host.process.poll() is None
        adapter.boundary.set_scenario({"text_chunks": ["follow-up"]})
        chat = await adapter.prepare("chat_after_task", "follow_request", "follow_generation")
        await adapter.start(chat, "An independent chat still works.")
        follow = [event async for event in adapter.observe(chat)]
        assert any(event.status == "completed" for event in follow)
