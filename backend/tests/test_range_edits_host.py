"""Fixed native host, local SSE and cold continuation of disjoint text edits."""

import asyncio
import json
from pathlib import Path

import pytest
from test_range_edits import long_target
from workbench_support import learning_course

from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.boundary import MessagesBoundary
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.tasks import TaskStore

ROOT = Path(__file__).resolve().parents[2]


class RangeFixture(MessagesBoundary):
    def __init__(self, path, registry, repo):
        super().__init__(path, native_registry=registry)
        self.repo, self.requests, self.steps = repo, [], {}

    def _fixture(self, handler, request, scenario, row, observer):
        run_id = row["generation_id"]
        self.requests.append((run_id, request))
        step = self.steps.get(run_id, 0) + 1
        self.steps[run_id] = step
        run = TaskRunStore(self.repo).internal(run_id)
        contract = run["input_contract"]
        edit, target = contract["edit_range"], contract["target"]
        actual = SourceStore(self.repo).snapshot(self.course_id, edit["snapshot_id"])
        body = {
            "blocks": [
                {
                    "block_id": edit["block_id"],
                    "kind": "markdown",
                    "markdown": actual.text.replace("old", "NEW AND LONGER"),
                    "attribution": "source_paraphrase",
                    "source_snapshot_ids": [item["snapshot_id"] for item in contract["inputs"]],
                }
            ]
        }
        calls = (
            [
                {
                    "id": "range_candidate",
                    "name": "stage_result",
                    "input": {
                        "task_id": run["task_id"],
                        "object_id": target["reserved_artifact_id"],
                        "expected_version": target["structure_revision"],
                        "stage": "content",
                        "body": json.dumps(body, ensure_ascii=False),
                    },
                }
            ]
            if step == 1
            else []
        )
        return super()._fixture(
            handler,
            request,
            {
                "tool_calls": calls,
                "text_chunks": [] if calls else ["本段已暂存。"],
            },
            row,
            observer,
        )


def runtime(path, repo, course):
    port = NativeBusiness(repo)
    registry = NativeCallRegistry(path / "native-facts.jsonl")
    boundary = RangeFixture(path / "boundary", registry, repo)
    boundary.course_id = course
    adapter = ExecutionAdapter(
        ROOT, path / "host", boundary=boundary, native_tools=NativeToolTransport(registry, port)
    )
    return adapter, registry, boundary


@pytest.mark.host
async def test_native_staged_range_survives_cold_start_without_replaying_published_prefix(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    artifact, task, text = long_target(repo, course, source, paired=True)
    adapter, first_registry, first_boundary = runtime(tmp_path / "first", repo, course)
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        try:
            await asyncio.wait_for(
                executor.execute_unit(course, task.task_id, task.units[0].unit_id), 45
            )
        finally:
            await executor.close()
            await service.close()
    first = TaskStore(repo).get(course, task.task_id)
    assert first.units[0].state == "completed", first.model_dump()
    first_run = first.units[0].run_id
    saved = ArtifactStore(repo).get(course, artifact.artifact_id)
    assert saved.revision.body.blocks[0].markdown.startswith("This 🧠 NEW AND LONGER")
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
    repo = Repository(tmp_path / "data")
    adapter, second_registry, second_boundary = runtime(tmp_path / "second", repo, course)
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), 100)
        finally:
            await executor.close()
            await service.close()
    final = TaskStore(repo).get(course, task.task_id)
    assert final.state == "completed", final.model_dump()
    result = ArtifactStore(repo).get(course, artifact.artifact_id)
    assert result.revision.body.blocks[0].markdown == text.replace("old", "NEW AND LONGER")
    assert (
        result.revision.body.blocks[0].translation == artifact.revision.body.blocks[0].translation
    )
    assert first_run not in {run_id for run_id, _ in second_boundary.requests}
    assert final.units[0].run_id == first_run and len(ArtifactStore(repo).list(course)) == 1
    for run_id, request in [*first_boundary.requests, *second_boundary.requests]:
        assert {tool["name"] for tool in request.get("tools", [])} == {
            "skill",
            "read_source_handle",
            "stage_result",
        }
        assert not [
            part
            for message in request["messages"]
            for part in message["content"]
            if isinstance(part, dict) and part.get("type") == "image"
        ]
        assert source.text not in json.dumps(request, ensure_ascii=False)
    assert len(first_registry.bindings) + len(second_registry.bindings) == len(task.units)
    assert first_boundary.actual_outbound_attempts == second_boundary.actual_outbound_attempts == 0
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
