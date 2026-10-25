"""Native QA keeps owner A while explicit foreign text/image sources remain B."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select
from test_course_corpus import index_all
from test_document_image_tools_host import FigureFixture, tool_images
from test_document_regions import document_parent
from workbench_support import learning_course, note

from learning_app.config import AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.service import GenerationService
from learning_app.workbench import document_image_schema as d
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.corpus import CourseCorpus
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.sources import SourceStore

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.host
async def test_explicit_foreign_document_image_and_later_precise_history_keep_original_chat(
    tmp_path,
):
    repo, owner, _, _ = learning_course(tmp_path)
    foreign_root = tmp_path / "foreign-course"
    foreign_root.mkdir()
    foreign = CourseFileStore(repo).bind(None, str(foreign_root), "明确对照课程")["course_id"]
    document_parent(repo, foreign, foreign_root)
    index_all(CourseCorpus(repo), owner)
    index_all(CourseCorpus(repo), foreign)
    repo.create_chat(owner, "figure_chat")
    port = NativeBusiness(repo)
    registry = NativeCallRegistry(tmp_path / "native.jsonl")
    boundary = FigureFixture(tmp_path / "boundary", registry, repo)
    adapter = ExecutionAdapter(
        ROOT, tmp_path / "host", boundary=boundary, native_tools=NativeToolTransport(registry, port)
    )
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        try:
            first = await service.submit(
                {
                    "chat_id": "figure_chat",
                    "submission_id": "explicit_foreign",
                    "text": "根据明确指定课程查local green diagram及局部图。",
                    "read_course_ids": [foreign],
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[first["generation_id"]], 65)
            assert repo.get_generation(first["generation_id"])["state"] == "completed"
            with repo.engine.connect() as conn:
                scope = (
                    conn.execute(
                        select(s.native_scopes).where(
                            s.native_scopes.c.run_id == first["generation_id"]
                        )
                    )
                    .mappings()
                    .one()
                )
                image = (
                    conn.execute(
                        select(d.image_reads).where(
                            d.image_reads.c.run_id == first["generation_id"]
                        )
                    )
                    .mappings()
                    .one()
                )
            assert scope["course_id"] == owner and scope["grants"]["corpus_course_ids"] == [foreign]
            snapshot = SourceStore(repo).snapshot(owner, image["snapshot_id"])
            assert (
                snapshot.course_id == foreign
                and image["result"]["encoding_proof"]["course_id"] == foreign
            )
            assert len(tool_images(boundary.requests[-1][1])) == 1
            asset = (
                ArtifactStore(repo).create(owner, note(snapshot_id=snapshot.snapshot_id)).artifact
            )
            assert asset.course_id == owner
            second = await service.submit(
                {
                    "chat_id": "figure_chat",
                    "submission_id": "history_only",
                    "text": "只依据刚才已经提交的局部图继续说明。",
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[second["generation_id"]], 45)
            assert repo.get_generation(second["generation_id"])["state"] == "completed"
            with repo.engine.connect() as conn:
                later = (
                    conn.execute(
                        select(s.native_scopes).where(
                            s.native_scopes.c.run_id == second["generation_id"]
                        )
                    )
                    .mappings()
                    .one()
                )
            assert later["course_id"] == owner and "corpus_course_ids" not in later["grants"]
            assert [item["effect_id"] for item in later["grants"]["document_image_history"]] == [
                image["effect_id"]
            ]
            assert (
                repo.get_chat("figure_chat")["course_id"] == owner
                and repo.list_chats(foreign) == []
            )
            assert boundary.actual_outbound_attempts == 0
        finally:
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
