"""Actual pinned Host: local comparison, topic checkpoints and bounded synthesis."""

import asyncio
import json
from copy import deepcopy

import pytest
from sqlalchemy import select
from test_native_business_host import runtime
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.service import GenerationService
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import ArtifactBody
from learning_app.workbench.research import ResearchStore
from learning_app.workbench.research_synthesis import ResearchSynthesis
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


@pytest.mark.host
@pytest.mark.parametrize(
    "long,topics", [(False, []), (True, []), (True, ["共同条件", "差异与局限"])]
)
async def test_local_research_keeps_one_indexed_report_and_requires_cross_batch_synthesis(
    tmp_path, long, topics
):
    repo, course, root, source = learning_course(tmp_path)
    first_path = root / "lesson.txt"
    if long:
        first_path.write_text("甲资料：条件成立时按同一公式计算。\n" * 1800, "utf-8")
    first = CourseFileStore(repo).open(course, "lesson.txt")
    (root / "second.txt").write_text("乙资料：同一公式只在量纲一致时适用，不能省略单位。", "utf-8")
    second = CourseFileStore(repo).open(course, "second.txt")
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="explicit_research",
            kind="research",
            instruction="比较两份资料的共同条件、差异与局限。",
            coverage_mode="complete",
            sources=[
                {
                    "kind": "document",
                    "asset_id": f["asset_id"],
                    "source_version": f["source_version"],
                }
                for f in (first, second)
            ],
            target={"mode": "create", "title": "资料对照报告"},
            settings=settings(),
            sources_only=True,
            research={"topics": topics},
        ),
    )
    adapter, _, registry, boundary = runtime(tmp_path, repo, source, bare=True)
    original = boundary._fixture
    phases = []

    def fixture(handler, request, scenario, row, observer):
        run = TaskRunStore(repo).internal(row["generation_id"])
        context = run["input_contract"]["research"]
        phases.append(context["phase"])
        assert not {t["name"] for t in request.get("tools", [])} - {
            "skill",
            "read_source_handle",
            "stage_result",
        }
        if context["phase"] == "synthesis":
            assert not run["input_contract"]["inputs"] and run["native_allowed_tools"] == [
                "skill",
                "read_source_handle",
                "stage_result",
            ]
            assert context["notes"] and all(
                n["content_role"] == "ai_generated" for n in context["notes"]
            )
            assert "不是原教材" in run["prompt"]
            ids = context["citation_sources"]
            text = "综合：共同公式须核对条件；不同资料对单位的约束应并列保留。"
        else:
            ids = [i["snapshot_id"] for i in run["input_contract"]["inputs"]]
            text = "本批对照：保留资料给出的条件、公式与单位。"
        value = {
            "blocks": [
                {
                    "block_id": "comparison",
                    "kind": "markdown",
                    "markdown": text,
                    "attribution": "source_paraphrase",
                    "source_snapshot_ids": ids,
                }
            ]
        }
        return original(
            handler,
            request,
            {"text_chunks": [json.dumps(value, ensure_ascii=False)]},
            row,
            observer,
        )

    boundary._fixture = fixture
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), 50)
            done = TaskStore(repo).get(course, task.task_id)
            assert done.state == "completed", done.model_dump()
            assert len(ArtifactStore(repo).list(course)) == 1
            artifact = ArtifactStore(repo).list(course)[0]
            assert (
                artifact.kind == "report"
                and artifact.indexed
                and artifact.content_role == "ai_generated"
            )
            assert all(b.kind == "markdown" for b in artifact.revision.body.blocks)
            n_topics = len(topics) or 1
            assert phases.count("evidence") == (2 if long else 1) * n_topics
            assert phases.count("synthesis") == (1 if long else 0) * n_topics
            assert len(boundary.records) == len(phases) and boundary.actual_outbound_attempts == 0
            progress = ResearchStore(repo).get(course, task.task_id)
            assert (
                progress.reconciliation_required == [] and progress.source_decision_required == []
            )
            assert progress.statistics["completed"] == len(phases)
            assert "total_tool_calls" not in progress.statistics
            assert all(c.evidence_count > 0 for c in progress.checkpoints)
            if long:
                synth = next(c for c in progress.checkpoints if c.phase == "synthesis")
                evidence = ResearchStore(repo).evidence(course, task.task_id, synth.unit_id)
                assert synth.evidence_from_prior_runs == len(evidence) and len(evidence) >= 3
                assert all(
                    not e.from_current_run and e.read_in_run_id != synth.run_id for e in evidence
                )
                private = TaskRunStore(repo).internal(synth.run_id)
                context = private["input_contract"]["research"]
                assert context["notes_version"] == 2
                details = [d for note in context["notes"] for d in note["citation_details"]]
                assert len({d["source_group_id"] for d in details}) == 2
                assert {d["source_label"] for d in details} == {"lesson.txt", "second.txt"}
                assert context["coverage_fact"]["authorized_scope_complete"]
                assert private["input_contract"]["image_hashes"] == []
                with repo.engine.connect() as conn:
                    assert (
                        conn.execute(
                            select(s.native_source_handles).where(
                                s.native_source_handles.c.run_id == synth.run_id
                            )
                        ).first()
                        is None
                    )
                    task_row = TaskStore._row(conn, course, task.task_id)
                    context = deepcopy(private["input_contract"]["research"])
                    context["notes"][0]["body"]["blocks"][0]["markdown"] = "篡改中间笔记"
                    with pytest.raises(AppError, match="不能替换"):
                        ResearchSynthesis.validate(conn, task_row, context)
                    bad = deepcopy(private["input_contract"]["research"])
                    bad["citation_sources"].append(source.snapshot_id)
                    with pytest.raises(AppError, match="引用超出"):
                        ResearchSynthesis.validate(conn, task_row, bad)
                    body = ArtifactBody(
                        blocks=[
                            {
                                "block_id": "miss",
                                "markdown": "漏掉后续材料",
                                "attribution": "source_paraphrase",
                                "source_snapshot_ids": private["input_contract"]["research"][
                                    "citation_groups"
                                ][0],
                            }
                        ]
                    )
                    with pytest.raises(AppError, match="遗漏"):
                        ResearchSynthesis.validate_output(conn, task_row, private, body)
            assert all(run in registry.released for run in registry.scopes)
            TaskStore(repo).recover()
            assert not TaskPlanner(repo).plan(course, task.task_id)
            assert TaskStore(repo).get(course, task.task_id).state == "completed"
            coverage = TaskCoverage(repo).get(course, task.task_id)
            assert len(coverage.sources) == 2 and all(
                s.processed_leaf_count == 1 for s in coverage.sources
            )
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
