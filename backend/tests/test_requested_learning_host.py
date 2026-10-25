"""Pinned native admission/staging and exact crop encodings; local SSE only."""

import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from test_native_business import call
from test_requested_learning import chat_scope
from workbench_support import learning_course

from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.boundary import MessagesBoundary
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.service import GenerationService
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.requested_learning import TOOL
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.tasks import TaskStore

ROOT = Path(__file__).resolve().parents[2]


class RequestedFixture(MessagesBoundary):
    def __init__(self, path, registry, repo):
        super().__init__(path, native_registry=registry)
        self.repo, self.steps, self.requests = repo, {}, []

    def _fixture(self, handler, request, scenario, row, observer):
        run_id = row["generation_id"]
        step = self.steps.get(run_id, 0) + 1
        self.steps[run_id] = step
        self.requests.append((run_id, request))
        with self.repo.engine.connect() as conn:
            task_id = conn.execute(
                select(s.task_runs.c.task_id).where(s.task_runs.c.run_id == run_id)
            ).scalar_one_or_none()
        calls = []
        if task_id is not None and step == 1:
            run = TaskRunStore(self.repo).internal(run_id)
            target = run["input_contract"]["target"]
            body = {
                "blocks": [
                    {
                        "block_id": "summary",
                        "kind": "markdown",
                        "markdown": "这是依据本次冻结来源整理的学习笔记。",
                        "attribution": "source_paraphrase",
                        "source_snapshot_ids": [
                            item["snapshot_id"] for item in run["input_contract"]["inputs"]
                        ],
                    }
                ]
            }
            calls = [
                {
                    "id": "write_candidate",
                    "name": "stage_result",
                    "input": {
                        "task_id": task_id,
                        "object_id": target["reserved_artifact_id"],
                        "expected_version": target["structure_revision"],
                        "stage": "content",
                        "body": json.dumps(body, ensure_ascii=False),
                    },
                }
            ]
        elif (
            task_id is None
            and TOOL in {item["name"] for item in request.get("tools", [])}
            and step <= 2
        ):
            calls = [{"id": f"request_task_{step}", "name": TOOL, "input": {}}]
        return super()._fixture(
            handler,
            request,
            {"text_chunks": [] if calls else ["本次操作已完成。"], "tool_calls": calls},
            row,
            observer,
        )


def runtime(tmp_path, repo):
    port = NativeBusiness(repo)
    registry = NativeCallRegistry(tmp_path / "native-facts.jsonl")
    boundary = RequestedFixture(tmp_path / "boundary", registry, repo)
    adapter = ExecutionAdapter(
        ROOT, tmp_path / "host", boundary=boundary, native_tools=NativeToolTransport(registry, port)
    )
    return adapter, port, registry, boundary


@pytest.mark.host
async def test_native_explicit_request_admits_once_then_background_task_stages_and_publishes(
    tmp_path,
):
    repo, course, _, source = learning_course(tmp_path)
    repo.create_chat(course, "native_requested_chat")
    adapter, _, registry, boundary = runtime(tmp_path, repo)
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            ordinary = await service.submit(
                {
                    "chat_id": "native_requested_chat",
                    "submission_id": "ordinary_qa",
                    "text": "解释这段资料",
                    "source_snapshot_ids": [source.snapshot_id],
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[ordinary["generation_id"]], 40)
            assert TaskStore(repo).list(course) == [] and ArtifactStore(repo).list(course) == []
            result = await service.submit(
                {
                    "chat_id": "native_requested_chat",
                    "submission_id": "explicit_note",
                    "text": "请根据这段资料制作学习笔记",
                    "source_snapshot_ids": [source.snapshot_id],
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[result["generation_id"]], 40)
            await asyncio.wait_for(executor._queue.join(), 40)
            tasks = TaskStore(repo).list(course)
            assert len(tasks) == 1 and tasks[0].state == "completed", [
                item.model_dump() for item in tasks
            ]
            assets = ArtifactStore(repo).list(course)
            assert len(assets) == 1 and assets[0].kind == "note"
            assert (
                assets[0].revision.author == "model"
                and assets[0].revision.content_role == "ai_generated"
            )
            assert assets[0].revision.task_id == tasks[0].task_id
            for run_id, request in boundary.requests:
                tools = {item["name"] for item in request.get("tools", [])}
                if run_id == ordinary["generation_id"]:
                    assert TOOL not in tools and "stage_result" not in tools
                elif run_id == result["generation_id"]:
                    assert TOOL in tools and "stage_result" not in tools
                else:
                    assert "stage_result" in tools and TOOL not in tools
            assert (
                len([value for value in registry.bindings.values() if value["tool_name"] == TOOL])
                == 2
            )
            assert boundary.actual_outbound_attempts == 0
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()


@pytest.mark.host
async def test_submitted_crop_keeps_raw_to_native_encoded_to_attempt_identity(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    data = io.BytesIO()
    Image.new("RGB", (80, 48), "#24507c").save(data, format="JPEG")
    attachment = repo.stage_attachment(data.getvalue(), media_type="image/jpeg")
    binding, fake_port, _ = chat_scope(
        repo,
        course,
        source,
        "请根据这张题图制作学习笔记",
        attachments=[attachment["attachment_id"]],
    )
    task = fake_port.invoke(call(binding, TOOL, {}), {})
    adapter, _, _, boundary = runtime(tmp_path, repo)
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), 40)
            current = TaskStore(repo).get(course, task["task_id"])
            assert current.state == "completed", current.model_dump()
            with repo.engine.connect() as conn:
                inputs = list(
                    conn.execute(
                        select(s.run_inputs).where(
                            s.run_inputs.c.run_id == current.units[0].run_id,
                            s.run_inputs.c.representation == "image",
                        )
                    ).mappings()
                )
            assert len(inputs) == 1
            proof = inputs[0]["proof"]["native_image"]
            assert (
                proof["kind"] == "submitted_image"
                and proof["attachment_id"] == attachment["attachment_id"]
            )
            assert proof["source_sha256"] == attachment["content_hash"]
            assert proof["media_type"] == "image/jpeg"
            encoded = []

            def inspect(value):
                if isinstance(value, dict):
                    if value.get("type") == "image" and isinstance(value.get("source"), dict):
                        raw = value["source"].get("data")
                        if isinstance(raw, str):
                            encoded.append(hashlib.sha256(base64.b64decode(raw)).hexdigest())
                    for child in value.values():
                        inspect(child)
                elif isinstance(value, list):
                    for child in value:
                        inspect(child)

            for _, request in boundary.requests:
                inspect(request)
            assert encoded and set(encoded) == {proof["encoded_sha256"]}
            assert boundary.actual_outbound_attempts == 0
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
