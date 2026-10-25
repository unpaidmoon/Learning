"""Pinned native encoding of an explicitly selected frozen document crop.

This proves the image preparation primitive, not dynamic tool-result grants.
"""

import asyncio

import pytest
from test_document_regions import document_parent
from test_requested_learning_host import runtime
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.service import GenerationService
from learning_app.workbench.document_regions import DocumentRegions
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


@pytest.mark.host
async def test_selected_document_crop_keeps_typed_raw_normalized_and_actual_encoding_proof(
    tmp_path,
):
    repo, course, root, _ = learning_course(tmp_path)
    _, parent = document_parent(repo, course, root)
    candidate = DocumentRegions(repo).candidates(course, parent.snapshot_id)[0]
    source = SourceStore(repo).capture_document_region(course, candidate)
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="selected_crop_task",
            kind="note",
            instruction="说明本次明确选择的文档局部图。",
            sources_only=True,
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "局部图说明"},
            settings=settings(),
        ),
    )
    adapter, _, _, boundary = runtime(tmp_path / "native", repo)
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), 50)
            current = TaskStore(repo).get(course, task.task_id)
            assert current.state == "completed", current.model_dump()
            run = TaskRunStore(repo).internal(current.units[0].run_id)
            proof = run["request_image_proofs"][0]
            assert proof["kind"] == "document_region"
            assert proof["source_sha256"] == source.reference.image_sha256
            assert proof["parent_text_snapshot_id"] == parent.snapshot_id
            assert proof["parent_text_hash"] == parent.content_hash
            assert proof["region_id"] == candidate.region_id
            assert proof["normalized_ref"]["attachmentId"] == "sha256:" + proof["normalized_sha256"]
            assert boundary.records and all(
                row["images"][0]["sha256"] == proof["encoded_sha256"] for row in boundary.records
            )
            assert boundary.actual_outbound_attempts == 0
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
