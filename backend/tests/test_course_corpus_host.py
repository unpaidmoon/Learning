"""Fixed shipped Harness native skill → concept query → exact handle read."""

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from test_course_corpus import index_all
from workbench_support import learning_course

from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.boundary import MessagesBoundary
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.service import GenerationService
from learning_app.workbench import corpus_schema as c
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.corpus import CourseCorpus
from learning_app.workbench.native_business import NativeBusiness

ROOT = Path(__file__).resolve().parents[2]


class ConceptFixture(MessagesBoundary):
    def __init__(self, directory, repo, registry, *, query=None):
        super().__init__(directory, native_registry=registry)
        self.repo, self.requests = repo, []
        self.query = query or ("search_course_concept", {"concept": "矩形面积"})

    def _fixture(self, handler, request, scenario, row, observer):
        self.requests.append(request)
        step = len(self.requests)
        calls = []
        if step == 1:
            calls = [{"id": "load_course_skill", "name": "skill", "input": {"name": "course-read"}}]
        elif step == 2:
            calls = [
                {
                    "id": "find_concept",
                    "name": self.query[0],
                    "input": self.query[1],
                }
            ]
        elif step == 3:
            with self.repo.engine.connect() as conn:
                receipt = conn.execute(select(c.query_receipts.c.result)).scalar_one()
            hit = receipt["results"][0]
            calls = [
                {
                    "id": "read_found_source",
                    "name": "read_source_handle",
                    "input": {key: hit[key] for key in ("handle_id", "source_version")},
                }
            ]
        return super()._fixture(
            handler,
            request,
            {
                "text_chunks": [] if calls else ["已按当前课程版本解释矩形面积。"],
                "tool_calls": calls,
            },
            row,
            observer,
        )


@pytest.mark.host
async def test_native_skill_retrieves_only_on_demand_and_keeps_original_scope(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    (root / "geometry.md").write_text("矩形面积 = 长 × 宽。COURSE_DEFINITION_SENTINEL", "utf-8")
    (root / "captions.srt").write_text("FORBIDDEN_GLOBAL_SUBTITLE_SENTINEL", "utf-8")
    (root / "video.mp4").write_bytes(b"FORBIDDEN_VIDEO_FRAME_SENTINEL")
    index_all(CourseCorpus(repo), course)
    port = NativeBusiness(repo)
    registry = NativeCallRegistry(tmp_path / "native-facts.jsonl")
    transport = NativeToolTransport(registry, port)
    boundary = ConceptFixture(tmp_path / "boundary", repo, registry)
    adapter = ExecutionAdapter(ROOT, tmp_path / "host", boundary=boundary, native_tools=transport)
    repo.create_chat(course, "corpus_chat")
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        try:
            result = await service.submit(
                {
                    "chat_id": "corpus_chat",
                    "submission_id": "native_concept",
                    "text": "请依据本课定义解释矩形面积",
                    "model": "deepseek-flash",
                    "thinking": "high",
                },
            )
            await asyncio.wait_for(service.tasks[result["generation_id"]], 45)
            run = repo.get_generation(result["generation_id"])
            assert run["state"] == "completed", run
            assert len(boundary.requests) == 4 and boundary.actual_outbound_attempts == 0
            assert "COURSE_DEFINITION_SENTINEL" not in json.dumps(
                boundary.requests[:2], ensure_ascii=False
            )
            assert "COURSE_DEFINITION_SENTINEL" in json.dumps(
                boundary.requests[2:], ensure_ascii=False
            )
            assert "FORBIDDEN_GLOBAL_SUBTITLE_SENTINEL" not in json.dumps(boundary.requests)
            assert "FORBIDDEN_VIDEO_FRAME_SENTINEL" not in json.dumps(boundary.requests)
            assert result["generation_id"] in registry.released
            assert len(registry.bindings) == 2
            assert all(
                operation["state"] == "completed" for operation in registry.operations.values()
            )
            assert ArtifactStore(repo).list(course) == []
            for request in boundary.requests:
                assert "stage_result" not in {tool["name"] for tool in request.get("tools", [])}
        finally:
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
