"""Actual pinned native Host reads only frozen question-near subtitle sources."""

import asyncio
import json

import pytest
from test_course_corpus_host import ROOT, ConceptFixture
from test_nearby_subtitles import cached_track
from test_question_context import video_course

from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.service import GenerationService
from learning_app.workbench.native_business import NativeBusiness


@pytest.mark.host
async def test_actual_native_read_nearby_and_handle_continuation_without_images(tmp_path):
    repo, course, _, source, _ = video_course(tmp_path)
    cached_track(
        repo,
        None,
        {
            "asset_id": source["asset_id"],
            "source_version": source["source_version"],
            "source_digest": source["content_hash"],
        },
        course=course,
    )
    registry = NativeCallRegistry(tmp_path / "native-facts.jsonl")
    port = NativeBusiness(repo)
    boundary = ConceptFixture(
        tmp_path / "boundary", repo, registry, query=("read_nearby_subtitles", {"anchor_id": None})
    )
    adapter = ExecutionAdapter(
        ROOT, tmp_path / "host", boundary=boundary, native_tools=NativeToolTransport(registry, port)
    )
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        try:
            submitted = await service.submit(
                {
                    "chat_id": "video_question",
                    "submission_id": "nearby_question",
                    "text": "请按需核对附近字幕",
                    "model": "deepseek-flash",
                    "thinking": "high",
                    "question_video": {
                        "asset_id": source["asset_id"],
                        "source_version": source["source_version"],
                        "time_ms": 75000,
                    },
                }
            )
            await asyncio.wait_for(service.tasks[submitted["generation_id"]], 40)
            generation = repo.get_generation(submitted["generation_id"])
            assert generation["state"] == "completed", generation
            assert len(boundary.requests) == 4 and boundary.actual_outbound_attempts == 0
            requests = json.dumps(boundary.requests, ensure_ascii=False)
            assert "SEGMENT_1_SENTINEL" in requests and "SEGMENT_3_SENTINEL" in requests
            assert "SEGMENT_0_SENTINEL" not in requests and "SEGMENT_4_SENTINEL" not in requests
            assert '"type": "image"' not in requests
            assert submitted["generation_id"] in registry.released
            assert all(op["state"] == "completed" for op in registry.operations.values())
        finally:
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
