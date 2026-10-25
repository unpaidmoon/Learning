"""Native generation uses the narrow existing result publisher for real plot data."""

import asyncio
import json

import pytest
from test_native_business_host import runtime
from test_visualizations import diagram, plot
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.service import GenerationService
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import VisualizationBlock
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


@pytest.mark.host
@pytest.mark.parametrize("malicious", [False, True])
async def test_actual_host_structured_visualization_persistence_and_code_rejection(
    tmp_path, malicious
):
    repo, course, _, source = learning_course(tmp_path)
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="visualization",
            kind="visualization",
            instruction="画一个静态关系图和可以调系数的函数动画。",
            coverage_mode="selection",
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "函数与条件"},
            sources_only=True,
            settings=settings(),
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id)
    TaskPlanner(repo).plan(course, task.task_id)
    data = plot()
    if malicious:
        data["renderer"].update(target="iframe", entry_url="/api/v1/session")
    value = {
        "blocks": [
            {
                "block_id": f"plot_{index}",
                "kind": "visualization",
                "markdown": "静态关系图和函数图都保留各自实际来源。",
                "attribution": "source_paraphrase",
                "source_snapshot_ids": [source.snapshot_id],
                "visualization": envelope,
            }
            for index, envelope in enumerate((diagram(), data))
        ]
    }
    adapter, _, _, boundary = runtime(tmp_path, repo, source, bare=True)
    boundary.set_scenario({"text_chunks": [json.dumps(value, ensure_ascii=False)]})
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=30)
            current = TaskStore(repo).get(course, task.task_id)
            assert len(boundary.records) == 1
            if malicious:
                assert (
                    current.state == "failed"
                    and current.units[0].error_code == "TASK_OUTPUT_STRUCTURE_INVALID"
                )
                assert ArtifactStore(repo).list(course) == []
            else:
                assert current.state == "completed"
                artifact = ArtifactStore(repo).list(course)[0]
                blocks = artifact.revision.body.blocks
                assert artifact.kind == "visualization" and all(
                    isinstance(b, VisualizationBlock) for b in blocks
                )
                assert blocks[1].visualization.payload.data.expression_tree["type"] == "binary"
                executor._finish(course, task.task_id, current.units[0].run_id)
                assert len(ArtifactStore(repo).list(course)) == 1
        finally:
            await executor.close()
            await service.close()
