"""Pinned actual Harness -> local provider fixture -> scoped file tool."""

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from test_chat_attachments import arguments, fixture, stored
from test_document_attachments import pdf_bytes

from learning_app import chat_attachments as files
from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.boundary import MessagesBoundary
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.service import GenerationService
from learning_app.workbench import corpus_schema
from learning_app.workbench.native_business import NativeBusiness

ROOT = Path(__file__).resolve().parents[2]


class FileBoundary(MessagesBoundary):
    def __init__(self, directory, registry, args):
        super().__init__(directory, native_registry=registry)
        self.arguments = args
        self.steps = {}
        self.requests = []

    def _fixture(self, handler, request, scenario, row, observer):
        run = row["generation_id"]
        step = self.steps[run] = self.steps.get(run, 0) + 1
        self.requests.append(request)
        calls = [{"id": "read_file", "name": files.TOOL, "input": self.arguments}] if step == 1 else []
        scenario = {"text_chunks": [] if calls else ["已根据实际读取内容回答。"], "tool_calls": calls}
        return super()._fixture(handler, request, scenario, row, observer)


@pytest.mark.host
@pytest.mark.parametrize("pdf_image", [False, True])
async def test_actual_host_reads_file_and_keeps_history_without_resending_original(tmp_path, pdf_image):
    repo = fixture(tmp_path)
    data = pdf_bytes(33) if pdf_image else b"READ-ONLY-FILE-CONTENT-98241"
    metadata = stored(repo, tmp_path, "selected.pdf" if pdf_image else "selected.txt", data)
    args = arguments(metadata, mode="image" if pdf_image else "text", page=33 if pdf_image else None)
    registry = NativeCallRegistry(tmp_path / "native.jsonl")
    port = NativeBusiness(repo)
    boundary = FileBoundary(tmp_path / "boundary", registry, args)
    transport = NativeToolTransport(registry, port)
    adapter = ExecutionAdapter(ROOT, tmp_path / "host", boundary=boundary, native_tools=transport)
    async with adapter:
        service = GenerationService(repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter)
        await service.open()
        runs = []
        try:
            for index in range(2):
                result = await service.submit({
                    "chat_id": "chat", "submission_id": f"file-input-{index}",
                    "text": "读取附件并回答" if not index else "继续根据刚才的附件回答",
                    "attachment_ids": [metadata["attachment_id"]] if not index else [],
                    "model": "deepseek-flash", "thinking": "high",
                })
                await asyncio.wait_for(service.tasks[result["generation_id"]], 40)
                generation = repo.get_generation(result["generation_id"])
                assert generation["state"] == "completed", generation
                assert generation["generation_id"] in registry.released
                runs.append(generation)
            assert runs[0]["binding_id"] == runs[1]["binding_id"]
            with repo.engine.connect() as conn:
                receipts = conn.execute(select(corpus_schema.query_receipts)).mappings().all()
            assert len(receipts) == 2
            assert all(row["result"]["content_read"] for row in receipts)
            if pdf_image:
                assert all(row["result"]["page"] == 33 for row in receipts)
                assert all(row["result"]["image_proof"]["encoded_sha256"] for row in receipts)
                assert any(row.get("images") for row in boundary.records)
                assert "%PDF-" not in json.dumps(boundary.requests)
            else:
                # The first request contains only the file descriptor. Content
                # reaches the provider only in the subsequent real tool result.
                assert data.decode() not in json.dumps(boundary.requests[0])
                assert data.decode() in json.dumps(boundary.requests[1])
            assert boundary.actual_outbound_attempts == 0
        finally:
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
