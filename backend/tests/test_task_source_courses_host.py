"""Fixed SEA consumes B task text/page images and publishes one result under A."""

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from test_cross_course_sources import courses
from test_task_source_courses import admit, declaration, finish_local
from test_workbench_reading import generate

from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.service import GenerationService
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.tasks import TaskStore

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.host
@pytest.mark.parametrize("material", ["text", "pdf_page"])
async def test_foreign_whole_source_is_native_input_b_and_result_owner_a(tmp_path, material):
    repo, owner, origin = courses(tmp_path)
    if material == "pdf_page":
        generate(tmp_path / "second-course/chapter.pdf", page_count=3)
        request = {**declaration(repo, origin, "chapter.pdf"), "first_page": 1, "last_page": 1}
    else:
        request = declaration(repo, origin, "definition.md")
    task = admit(repo, owner, [request])
    view = finish_local(repo, owner, task)
    assert TaskPlanner(repo).plan(owner, task.task_id)
    ids = [identity for leaf in view.leaves for identity in leaf.snapshot_ids]
    async with ExecutionAdapter(ROOT, tmp_path / "host") as adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            adapter.boundary.set_scenario(
                {
                    "text_chunks": [
                        json.dumps(
                            {
                                "blocks": [
                                    {
                                        "block_id": "foreign_read",
                                        "kind": "markdown",
                                        "markdown": "仅据明确B资料的整理。",
                                        "attribution": "source_paraphrase",
                                        "source_snapshot_ids": ids,
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    ]
                }
            )
            executor.schedule(owner, task.task_id)
            await asyncio.wait_for(executor._queue.join(), 40)
            current = TaskStore(repo).get(owner, task.task_id)
            assert current.state == "completed" and len(current.units) == 1
            result = ArtifactStore(repo).list(owner)
            assert len(result) == 1 and result[0].course_id == owner
            assert ArtifactStore(repo).list(origin) == [] and repo.list_chats(origin) == []
            block = result[0].revision.body.blocks[0]
            assert set(block.source_snapshot_ids) == set(ids)
            assert all(
                SourceStore(repo).snapshot(owner, identity).course_id == origin for identity in ids
            )
            assert len(adapter.boundary.records) == 1
            record = adapter.boundary.records[0]
            raw = json.loads(
                (adapter.boundary.private / f"{record['request_id']}.request.json").read_text(
                    "utf-8"
                )
            )
            text = json.dumps(raw["messages"], ensure_ascii=False)
            assert origin in text and all(identity in text for identity in ids)
            assert "FIRST_COURSE_SENTINEL" not in text
            if material == "pdf_page":
                image = next(
                    SourceStore(repo).snapshot(owner, identity)
                    for identity in ids
                    if SourceStore(repo).snapshot(owner, identity).reference.kind
                    == "document_image"
                )
                assert len(record["images"]) == 1
                with repo.engine.connect() as conn:
                    proofs = list(
                        conn.execute(
                            select(s.run_inputs.c.proof).where(
                                s.run_inputs.c.run_id == current.units[0].run_id,
                                s.run_inputs.c.representation == "image",
                            )
                        ).scalars()
                    )
                assert len(proofs) == 1
                proof = proofs[0]["native_image"]
                assert proof["course_id"] == origin
                assert proof["source_sha256"] == image.reference.image_sha256
                assert record["images"][0]["sha256"] == proof["encoded_sha256"]
            else:
                assert "SECOND_COURSE_SENTINEL" in text and record["images"] == []
            run = TaskRunStore(repo).get(owner, task.task_id, current.units[0].run_id)
            assert run.actual_outbound_attempts == 0 and len(run.attempts) == 1
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
