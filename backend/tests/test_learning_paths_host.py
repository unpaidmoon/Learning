"""Actual pinned Host publishes structured routes; no real provider or user assets."""

import asyncio
import json

import pytest
from test_native_business_host import runtime
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.paths import PathStore
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


@pytest.mark.host
@pytest.mark.parametrize(
    "forged_progress,long_input", [(False, False), (True, False), (False, True)]
)
async def test_actual_native_path_generation_and_user_evidence_guard(
    tmp_path, forged_progress, long_input
):
    repo, course, root, source = learning_course(tmp_path)
    if long_input:
        text = "Only this selected sentence. " * 1800
        (root / "long.txt").write_text(text, "utf-8")
        opened = CourseFileStore(repo).open(course, "long.txt")
        source = SourceStore(repo).capture_text(
            course, opened["asset_id"], opened["source_version"], 0, len(text)
        )
    count = 3 if long_input else 1
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="route",
            kind="learning_path",
            instruction="按明确的学习目标建议两个知识点的顺序，可以自由跳过和查看讲解。",
            coverage_mode="selection",
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "原句阅读路线"},
            settings=settings(),
            sources_only=True,
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id)
    TaskPlanner(repo).plan(course, task.task_id)
    units = TaskStore(repo).get(course, task.task_id).units
    output = {
        "blocks": [
            {
                "block_id": "point",
                "kind": "path_step",
                "markdown": "比较所选的原句。",
                "attribution": "source_paraphrase",
                "source_snapshot_ids": [source.snapshot_id],
                "step": {
                    "title": "识别实际选区",
                    "objective": "能准确说出实际选择的内容。",
                    "suggested_order": 0,
                    "recall_prompt": "所选的句子是什么？",
                    "explanation": "Only this selected sentence.",
                    "questions": [],
                },
            }
        ]
    }
    if forged_progress:
        output["blocks"][0]["step"]["mastered"] = True
    adapter, _, _, boundary = runtime(tmp_path, repo, source, bare=True)
    original_fixture = boundary._fixture

    def scoped_fixture(handler, request, scenario, row, observer):
        run = TaskRunStore(repo).internal(row["generation_id"])
        value = json.loads(json.dumps(output))
        value["blocks"][0]["source_snapshot_ids"] = [
            item["snapshot_id"] for item in run["input_contract"]["inputs"]
        ]
        return original_fixture(
            handler,
            request,
            {"text_chunks": [json.dumps(value, ensure_ascii=False)]},
            row,
            observer,
        )

    boundary._fixture = scoped_fixture
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=45)
            current = TaskStore(repo).get(course, task.task_id)
            assert len(boundary.records) == count
            raw = json.loads(
                (
                    boundary.private / (boundary.records[0]["request_id"] + ".request.json")
                ).read_text("utf-8")
            )
            outbound = json.dumps(raw["messages"], ensure_ascii=False)
            assert (
                "path_step" in outbound
                and source.text[:20] in outbound
                and "不设置闯关条件" in outbound
            )
            if forged_progress:
                assert (
                    current.state == "failed"
                    and current.units[0].error_code == "TASK_OUTPUT_STRUCTURE_INVALID"
                )
                assert ArtifactStore(repo).list(course) == []
            else:
                assert current.state == "completed"
                product = ArtifactStore(repo).list(course)[0]
                view = PathStore(repo).get(course, product.artifact_id)
                assert view.revision == 0 and view.current_block_id is None
                assert (
                    len(view.steps) == count
                    and not view.steps[0].visited
                    and view.steps[0].self_assessment is None
                )
                assert view.steps[0].review.kind == "knowledge"
                assert view.steps[0].review.scheduler_state["reps"] == 0
                assert not view.steps[0].answers
                assert [item.block.step.suggested_order for item in view.steps] == list(
                    range(count)
                )
                run = TaskRunStore(repo).get(course, task.task_id, current.units[0].run_id)
                executor._finish(course, task.task_id, run.run_id)
                assert len(ArtifactStore(repo).list(course)) == 1
                assert run.actual_outbound_attempts == 0
            assert len(units) == len(current.units) == count
        finally:
            await executor.close()
            await service.close()
