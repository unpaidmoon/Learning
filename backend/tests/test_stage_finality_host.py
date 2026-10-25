"""Actual SEA: reject an old accepted draft when a later final stage is denied."""

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from test_native_business_host import BusinessFixture
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.boundary import MessagesBoundary
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.service import GenerationService
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.stage_attempts import failures
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore

ROOT = Path(__file__).resolve().parents[2]


class FinalStageFixture(BusinessFixture):
    def __init__(self, *args, repair, **kwargs):
        super().__init__(*args, task=True, **kwargs)
        self.repair = repair

    def _fixture(self, handler, request, scenario, row, observer):
        step = self.steps.get(row["generation_id"], 0) + 1
        if step <= 2:
            return super()._fixture(handler, request, scenario, row, observer)
        self.steps[row["generation_id"]] = step
        self.requests.append(request)
        run = TaskRunStore(self.repo).internal(row["generation_id"])
        target = run["input_contract"]["target"]
        calls = []
        if step == 3 or (step == 4 and self.repair):
            calls = [
                {
                    "id": "later_stage_" + str(step),
                    "name": "stage_result",
                    "input": {
                        "task_id": run["task_id"],
                        "object_id": target["reserved_artifact_id"],
                        "expected_version": 1 if step == 3 else target["structure_revision"],
                        "stage": "content",
                        "body": json.dumps(
                            {
                                "blocks": [
                                    {
                                        "block_id": "summary",
                                        "markdown": "实际最终内容。",
                                        "attribution": "source_paraphrase",
                                        "source_snapshot_ids": [self.source.snapshot_id],
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ]
        return MessagesBoundary._fixture(
            self,
            handler,
            request,
            {"tool_calls": calls, "text_chunks": [] if calls else ["本轮已结束。"]},
            row,
            observer,
        )


@pytest.mark.host
@pytest.mark.parametrize("repair", [False, True])
async def test_native_terminal_does_not_publish_earlier_draft_after_rejected_final_write(
    tmp_path, repair
):
    repo, course, _, source = learning_course(tmp_path)
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="stage_finality",
            kind="handout",
            instruction="完整整理所选资料",
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "实际最终成果"},
            settings=settings(),
            sources_only=True,
        ),
    )
    port, registry = NativeBusiness(repo), NativeCallRegistry(tmp_path / "native.jsonl")
    boundary = FinalStageFixture(tmp_path / "boundary", repo, registry, source, repair=repair)
    adapter = ExecutionAdapter(
        ROOT, tmp_path / "host", boundary=boundary, native_tools=NativeToolTransport(registry, port)
    )
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), 45)
            current = TaskStore(repo).get(course, task.task_id)
            assets = ArtifactStore(repo).list(course)
            if repair:
                assert current.state == "completed" and len(assets) == 1
                assert assets[0].revision.body.blocks[0].markdown == "实际最终内容。"
            else:
                assert current.state == "failed" and assets == []
                assert current.units[0].error_code == "NATIVE_FINAL_STAGE_REJECTED"
            with repo.engine.connect() as conn:
                assert len(conn.execute(select(failures)).all()) == 1
                assert len(conn.execute(select(s.model_stages)).all()) == (2 if repair else 1)
            messages = json.dumps(boundary.requests, ensure_ascii=False)
            assert "NATIVE_STAGE_TARGET_DENIED" in messages and "expected_version" in messages
            assert boundary.actual_outbound_attempts == 0
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
