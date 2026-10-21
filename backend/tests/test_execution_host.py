"""Real fixed Host integration; provider remains deterministic and offline."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.types import ApprovedAttachment, ExecutionError

REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.mark.host
async def test_real_host_stream_cancel_follow_and_reconcile(tmp_path):
    adapter = ExecutionAdapter(REPOSITORY, tmp_path / "runtime")
    async with adapter:
        state = await adapter.host.audit_state()
        assert state["tools"] == []
        assert state["networkGuardInstalled"]
        adapter.boundary.set_scenario(
            {"text_chunks": ["first ", "second ", "third"], "delay": 0.03}
        )
        binding = await adapter.prepare("chat-A", "submission-1", "generation-1", 1)
        await adapter.start(binding, "Explain the synthetic condition.")
        events = [event async for event in adapter.observe(binding)]
        assert (
            "".join(event.text for event in events if event.kind == "text_delta")
            == "first second third"
        )
        assert any(event.kind == "terminal" and event.status == "completed" for event in events)
        assert len(adapter.boundary.records) == 1
        assert adapter.boundary.actual_outbound_attempts == 0
        reconciled = await adapter.reconcile(binding, events[-1].seq)
        assert reconciled["status"] == "completed"
        assert any(
            event.kind == "snapshot" and event.text == "first second third"
            for event in reconciled["events"]
        )

        adapter.boundary.set_scenario(
            {
                "text_chunks": ["partial text", " forbidden tail"],
                "pause_after_event": "text_delta",
                "gate": "stop-here",
            }
        )
        stop = await adapter.prepare("chat-A", "submission-2", "generation-2", 1)
        await adapter.start(stop, "Second synthetic condition.")
        received = []
        async for event in adapter.observe(stop):
            received.append(event)
            if event.kind == "text_delta":
                assert (await adapter.cancel(stop))["accepted"]
        assert any(event.kind == "terminal" and event.status == "cancelled" for event in received)
        assert "partial text" == "".join(
            event.text for event in received if event.kind == "text_delta"
        )
        assert adapter.host.process.poll() is None
        recovered = await adapter.reconcile(stop)
        assert recovered["status"] == "cancelled"
        assert any(
            event.kind == "snapshot" and "partial text" in event.text
            for event in recovered["events"]
        )

        adapter.boundary.set_scenario({"text_chunks": ["continuation works"]})
        follow = await adapter.prepare("chat-A", "submission-3", "generation-3", 1)
        await adapter.start(follow, "Continue after the cancelled turn.")
        follow_events = [event async for event in adapter.observe(follow)]
        assert (
            "".join(event.text for event in follow_events if event.kind == "text_delta")
            == "continuation works"
        )
        assert follow.session_id == stop.session_id == binding.session_id
        assert len(adapter.boundary.records) == 3
    assert adapter.host.cleanup["remaining_pids"] == []
    assert adapter.host.cleanup["host_port_closed"]


@pytest.mark.host
async def test_lost_receipt_and_observer_reconnect_never_dispatch_again(tmp_path):
    async with ExecutionAdapter(REPOSITORY, tmp_path / "runtime") as adapter:
        adapter.boundary.set_scenario(
            {"text_chunks": ["one", "two"], "pause_after_event": "text_delta", "gate": "continue"}
        )
        binding = await adapter.prepare("chat-B", "submission-B", "generation-B", 1)
        original = adapter.peer.rpc

        async def lose_receipt(method, request):
            result = await original(method, request)
            if method == "session/prompt":
                raise httpx.ReadError("deterministic accepted-receipt loss")
            return result

        adapter.peer.rpc = lose_receipt
        await adapter.start(binding, "Read this synthetic question.")
        adapter.peer.rpc = original
        assert binding.status == "unknown"
        await asyncio.to_thread(
            adapter.boundary.wait_for_event, "gate_waiting", 10, generation_id=binding.generation_id
        )
        await adapter.peer.disconnect()
        reconciled = await adapter.reconcile(binding)
        assert reconciled["status"] == "active"
        assert reconciled["accepted"]
        adapter.boundary.release_event("continue")
        remaining = [event async for event in adapter.observe(binding)]
        assert any(event.kind == "terminal" and event.status == "completed" for event in remaining)
        assert "".join(event.text for event in remaining if event.kind == "text_delta") == "two"
        assert len(adapter.boundary.records) == 1


def test_detached_compact_stream_extraction():
    from learning_app.execution.adapter import _content

    assert _content(
        [
            {"type": "text-chunks", "texts": ["a", "b"]},
            {"type": "reasoning-chunks", "texts": ["c"]},
            {"type": "usage", "text": "not-answer"},
        ]
    ) == ("ab", "c")


@pytest.mark.host
async def test_confirmed_image_scope_and_required_attachment_fail_closed(tmp_path):
    from PIL import Image

    confirmed = tmp_path / "confirmed.png"
    Image.new("RGB", (240, 120), (42, 105, 192)).save(confirmed)
    expected = hashlib.sha256(confirmed.read_bytes()).hexdigest()
    attachment = ApprovedAttachment("confirmed-1", confirmed, expected)
    async with ExecutionAdapter(REPOSITORY, tmp_path / "runtime") as adapter:
        binding = await adapter.prepare("chat-image", "submission-image", "generation-image", 1)
        adapter.boundary.set_scenario({"text_chunks": ["image accepted"]})
        await adapter.start(binding, "Use only this confirmed image.", [attachment])
        events = [event async for event in adapter.observe(binding)]
        assert any(event.status == "completed" for event in events)
        assert adapter.boundary.records[0]["images"][0]["sha256"] == expected
        missing = await adapter.prepare("chat-image", "submission-missing", "generation-missing", 1)
        confirmed.unlink()
        with pytest.raises(ExecutionError, match="CONFIRMED_ATTACHMENT_MISSING"):
            await adapter.start(missing, "Do not silently omit the image.", [attachment])
        assert len(adapter.boundary.records) == 1
        audit = [
            json.loads(line)
            for line in (adapter.host.run_dir / "private/plugin-audit.jsonl")
            .read_text("utf-8")
            .splitlines()
        ]
        assert all(row.get("tools") == [] for row in audit if row["type"] == "llm-dispatch")


@pytest.mark.host
async def test_concurrent_conversations_keep_provider_and_stream_ownership(tmp_path):
    async with ExecutionAdapter(REPOSITORY, tmp_path / "runtime") as adapter:
        a = await adapter.prepare("course-A-chat", "submission-A", "generation-A")
        b = await adapter.prepare("course-B-chat", "submission-B", "generation-B")
        adapter.boundary.set_scenario({"text_chunks": ["shared deterministic answer"]})
        await asyncio.gather(adapter.start(a, "Question A"), adapter.start(b, "Question B"))

        async def collect(binding):
            return [event async for event in adapter.observe(binding)]

        rows_a, rows_b = await asyncio.gather(collect(a), collect(b))
        assert all(event.generation_id == a.generation_id for event in rows_a)
        assert all(event.generation_id == b.generation_id for event in rows_b)
        assert all(len(adapter.attempts_for(binding.generation_id)) == 1 for binding in (a, b))
        assert a.session_id != b.session_id
        assert {row["generation_id"] for row in adapter.boundary.records} == {
            "generation-A",
            "generation-B",
        }


@pytest.mark.host
async def test_usage_late_settlement_has_distinct_observation_and_stable_settlement(tmp_path):
    async with ExecutionAdapter(REPOSITORY, tmp_path / "runtime") as adapter:
        adapter.boundary.set_scenario(
            {
                "text_chunks": ["known partial body"],
                "gate_before_terminal": "hold-final-usage",
            }
        )
        binding = await adapter.prepare("usage-chat", "usage-submit", "usage-generation")
        await adapter.start(binding, "Question with held final usage.")
        await asyncio.to_thread(
            adapter.boundary.wait_for_event, "gate_waiting", 10, generation_id=binding.generation_id
        )
        before = await adapter._usage(binding)
        assert len(before) == 1 and before[0].raw_usage["settled"] is False
        adapter.boundary.release_event("hold-final-usage")
        events = [event async for event in adapter.observe(binding)]
        after = [event for event in events if event.kind == "usage"]
        assert len(after) == 1 and after[0].raw_usage["settled"] is True
        assert before[0].event_id != after[0].event_id
        assert before[0].data["settlement_id"] == after[0].data["settlement_id"]
        repeated = await adapter._usage(binding)
        assert repeated[0].event_id == after[0].event_id
