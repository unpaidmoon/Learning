"""Pinned native text-only translation with actual scanner and saved range proofs."""

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from test_aligned_translations import selected, stream
from test_offline_export import package, unpack
from workbench_support import learning_course

from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.boundary import MessagesBoundary
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.service import GenerationService
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import ModelSettings
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore

ROOT = Path(__file__).resolve().parents[2]


class AlignedFixture(MessagesBoundary):
    def __init__(self, directory, registry, repo, bad_final):
        super().__init__(directory, native_registry=registry)
        self.repo, self.bad_final, self.requests = repo, bad_final, []

    def _fixture(self, handler, request, scenario, row, observer):
        run = TaskRunStore(self.repo).internal(row["generation_id"])
        with self.repo.engine.connect() as conn:
            course_id = conn.execute(
                select(s.tasks.c.course_id).where(s.tasks.c.task_id == run["task_id"])
            ).scalar_one()
            ordinal = conn.execute(
                select(s.task_units.c.ordinal).where(s.task_units.c.unit_id == run["unit_id"])
            ).scalar_one()
            text = "".join(
                SourceStore.in_transaction(conn, course_id, item["snapshot_id"]).text
                for item in run["input_contract"]["inputs"]
            )
        self.requests.append(request)
        raw = stream({"src": text, "tgt": "本段对应的合成中文译文。"}, {"done": True})
        if self.bad_final and ordinal == 1:
            raw = stream({"src": text, "tgt": "有效前段仍是草稿。"}) + '\n{"src":"broken'
        return super()._fixture(handler, request, {"text_chunks": [raw]}, row, observer)


@pytest.mark.host
@pytest.mark.parametrize("bad_final", [False, True], ids=["complete", "broken-final"])
async def test_native_multiple_units_one_saved_translation_and_broken_tail_stays_incomplete(
    tmp_path, bad_final
):
    repo, course, root, _ = learning_course(tmp_path)
    text = "Selected English sentence. 🙂\n" * 100
    source = selected(repo, course, root, text)
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="aligned",
            kind="aligned_translation",
            instruction="仅将指定英文逐段译为中文。",
            sources_only=True,
            coverage_mode="complete",
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "英文对照"},
            settings=ModelSettings(
                model="deepseek-flash", thinking="high", max_output_tokens=4096, timeout_seconds=120
            ),
        ),
    )
    registry = NativeCallRegistry(tmp_path / "native-facts.jsonl")
    boundary = AlignedFixture(tmp_path / "boundary", registry, repo, bad_final)
    adapter = ExecutionAdapter(
        ROOT,
        tmp_path / "host",
        boundary=boundary,
        native_tools=NativeToolTransport(registry, NativeBusiness(repo)),
    )
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), 40)
            current = TaskStore(repo).get(course, task.task_id)
            assert len(current.units) == 2
            assert current.state == ("failed" if bad_final else "completed"), current.model_dump()
            assets = ArtifactStore(repo).list(course)
            assert len(assets) == 1 and assets[0].kind == "translation"
            translated = assets[0]
            blocks = translated.revision.body.blocks
            assert all(block.kind == "translation" for block in blocks)
            actual_text = "".join(block.translation.source_text for block in blocks)
            assert (
                (0 < len(actual_text) < len(text) and text.startswith(actual_text))
                if bad_final
                else actual_text == text
            )
            if bad_final:
                assert current.units[-1].error_code == "TASK_TRANSLATION_OUTPUT_INVALID"
                run = TaskRunStore(repo).get(course, task.task_id, current.units[-1].run_id)
                assert run.state == "completed" and run.body.endswith('{"src":"broken')
            else:
                data, _, _ = unpack(package(repo, course, translated))
                local_ids = {source["id"] for source in data["sources"]}
                assert all(
                    part["snapshot_id"] in local_ids
                    for block in data["blocks"]
                    for part in block["translation"]["source_parts"]
                )
                assert source.snapshot_id not in json.dumps(data)
            assert len(boundary.records) == 2 and boundary.actual_outbound_attempts == 0
            assert all(not request.get("tools") for request in boundary.requests)
            assert all(not record.get("images") for record in boundary.records)
            before = len(boundary.records)
            executor.schedule(course, current.task_id)
            await asyncio.wait_for(executor._queue.join(), 5)
            assert len(boundary.records) == before
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
