"""Known tool-stage cancellation survives control failure and a cold native Host."""

import asyncio
import threading
import time

import pytest
from test_course_corpus_host import ROOT, ConceptFixture
from test_question_context import video_course

from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.execution.types import ExecutionError
from learning_app.service import GenerationService
from learning_app.workbench.native_business import NativeBusiness


@pytest.mark.host
@pytest.mark.parametrize(
    "cancel_rpc_delay",
    [
        pytest.param(0, id="normal"),
        pytest.param(0.08, id="delayed_cancel_rpc"),
    ],
)
async def test_persisted_tool_stop_proof_cold_recovers_without_tool_or_model_replay(
    tmp_path, cancel_rpc_delay
):
    repo, _, _, source, _ = video_course(tmp_path)
    entered = threading.Event()

    class WaitingPort(NativeBusiness):
        def invoke(self, call, arguments):
            if call.tool_name == "read_nearby_subtitles":
                entered.set()
                while True:
                    self.authorize(call, arguments)
                    time.sleep(0.01)
            return super().invoke(call, arguments)

    config = AppConfig(mode="test", data_dir=repo.root)

    def runtime(port):
        registry = NativeCallRegistry(tmp_path / "native-facts.jsonl")
        boundary = ConceptFixture(
            tmp_path / "boundary",
            repo,
            registry,
            query=("read_nearby_subtitles", {"anchor_id": None}),
        )
        adapter = ExecutionAdapter(
            ROOT,
            tmp_path / "host",
            boundary=boundary,
            native_tools=NativeToolTransport(registry, port),
        )
        service = GenerationService(repo, config, execution=adapter)
        service.owns_execution = True
        return service, adapter, registry, boundary

    service, first, registry, boundary = runtime(WaitingPort(repo))
    await service.open()

    async def unavailable_control(*_):
        raise ExecutionError("SYNTHETIC_RELEASE_CONTROL_UNAVAILABLE")

    first.host.release_execution = unavailable_control
    original_cancel = first.cancel

    async def delayed_cancel(binding):
        if cancel_rpc_delay:
            await asyncio.sleep(cancel_rpc_delay)
        return await original_cancel(binding)

    first.cancel = delayed_cancel
    try:
        submitted = await service.submit(
            {
                "chat_id": "video_question",
                "submission_id": "cold_tool_stop",
                "text": "核对问题附近字幕",
                "model": "deepseek-flash",
                "thinking": "high",
                "question_video": {
                    "asset_id": source["asset_id"],
                    "source_version": source["source_version"],
                    "time_ms": 75000,
                },
            }
        )
        generation = submitted["generation_id"]
        async with asyncio.timeout(15):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        await service.cancel(generation)
        await asyncio.wait_for(service.tasks[generation], 15)
        async with asyncio.timeout(5):
            while not registry.effects_settled(generation):
                await asyncio.sleep(0.01)
        await service.release_native_execution(service._binding_for(generation))
        assert generation in registry.tool_stops
        assert generation not in registry.released
        assert repo.get_generation(generation)["state"] == "cancelled"
        assert len(boundary.requests) == 2 and boundary.actual_outbound_attempts == 0
        original_proof = registry.tool_stops[generation]
    finally:
        await service.close()
    assert first.host.cleanup["remaining_pids"] == []
    recovered, second, recovered_registry, recovered_boundary = runtime(NativeBusiness(repo))
    try:
        await recovered.open()
        await asyncio.wait_for(recovered.tasks[generation], 25)
        assert generation in recovered_registry.released
        assert recovered_registry.tool_stops[generation] == original_proof
        assert recovered_boundary.requests == [] and len(recovered_boundary.records) == 2
        assert len(recovered_registry.operations) == 1
        assert recovered_registry.release_receipts[generation]["recovered_from_persisted_cut"]
        assert (
            recovered_registry.release_receipts[generation]["native_cursor"]
            >= original_proof["native_cursor"]
        )
    finally:
        await recovered.close()
        repo.close()
    assert second.host.cleanup["remaining_pids"] == []
