"""Explicit scope revisions keep completed evidence and retire invalid research work."""

import asyncio
import json

import pytest
from sqlalchemy import select, update
from test_task_coverage import finish_work, refresh
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.service import GenerationService
from learning_app.workbench import schema as s
from learning_app.workbench.artifact_scope import ArtifactScopeStore
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import ArtifactUpdate, BlockChange
from learning_app.workbench.offline import OfflineExporter
from learning_app.workbench.research import ResearchStore
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_controls import TaskRecovery
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import SourceGapDecision, TaskAdmission
from learning_app.workbench.tasks import TaskStore


def research(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    (root / "lesson.txt").write_text("甲" * 30_000, "utf-8")
    (root / "second.txt").write_text("乙资料仍可读取，保留单位约束。", "utf-8")
    files = CourseFileStore(repo)
    opened = [files.open(course, name) for name in ("lesson.txt", "second.txt")]
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="research",
            kind="research",
            instruction="比较给定资料，保留来源。",
            coverage_mode="complete",
            sources_only=True,
            settings=settings(),
            target={"mode": "create", "title": "对照"},
            sources=[
                {
                    "kind": "document",
                    "asset_id": item["asset_id"],
                    "source_version": item["source_version"],
                }
                for item in opened
            ],
        ),
    )
    task = refresh(repo, course, task.task_id)
    assert len(task.units) == 2
    return repo, course, root, task


@pytest.mark.asyncio
async def test_changed_member_of_a_mixed_batch_does_not_block_other_source_or_reuse_excluded_notes(
    tmp_path,
):
    repo, course, root, task = research(tmp_path)
    first_run, receipt = finish_work(repo, course, task, task.units[0], text="旧范围的甲资料笔记")
    (root / "lesson.txt").write_text("known changed original", "utf-8")
    await TaskExecutor(repo, None).execute_unit(course, task.task_id, task.units[1].unit_id)
    coverage = TaskCoverage(repo).get(course, task.task_id)
    first = next(source for source in coverage.sources if source.label == "lesson.txt")
    second = next(source for source in coverage.sources if source.label == "second.txt")
    assert first.state == "blocked" and second.state == "ready"
    before = TaskStore(repo).get(course, task.task_id)
    decided = TaskRecovery(repo).source_gap(
        course,
        task.task_id,
        SourceGapDecision(
            operation_id="exclude_changed",
            expected_revision=before.revision,
            decision="exclude",
            source_ids=[first.source_id],
        ),
    )
    assert all(unit.excluded_at_scope_revision == 1 for unit in decided.units)
    assert decided.units[0].run_id == first_run.run_id
    assert TaskRunStore(repo).get(course, task.task_id, first_run.run_id).state == "completed"
    current = refresh(repo, course, task.task_id)
    active = [unit for unit in current.units if unit.excluded_at_scope_revision is None]
    assert len(active) == 1
    spec, inputs = TaskPlanner(repo).inputs(course, task.task_id, active[0].unit_id)
    assert {item["label"] for item, _ in inputs} == {"second.txt · 第1页"}
    assert spec["research"]["phase"] == "evidence"
    _, result = finish_work(repo, course, current, active[0], text="当前范围仅乙资料")
    assert result.artifact.artifact_id == receipt.artifact.artifact_id
    assert TaskStore(repo).reconcile(course, task.task_id).state == "completed"
    scope = ArtifactScopeStore(repo).get(
        course, result.artifact.artifact_id, result.artifact.head_revision_id
    )
    old_block = receipt.artifact.revision.body.blocks[0].block_id
    assert old_block in scope.block_notices and "较早范围" in scope.block_notices[old_block]
    current_blocks = [
        block
        for block in result.artifact.revision.body.blocks
        if block.markdown == "当前范围仅乙资料"
    ]
    assert len(current_blocks) == 1 and current_blocks[0].block_id not in scope.block_notices
    document, _ = OfflineExporter(repo).snapshot(
        course, result.artifact.artifact_id, result.artifact.head_revision_id
    )
    assert len(document["block_notices"]) == 1 and document["notices"]
    assert (
        next(block for block in document["blocks"] if block["markdown"] == "旧范围的甲资料笔记")[
            "block_id"
        ]
        in document["block_notices"]
    )
    assert any(
        block.markdown == "旧范围的甲资料笔记"
        for block in ArtifactStore(repo)
        .history(course, receipt.artifact.artifact_id)[0]
        .body.blocks
    )
    assert repo.counts()["outbound_attempts"] == 0
    repo.close()


def test_retired_synthesis_remains_readable_as_historical_evidence_without_reopening_execution(
    tmp_path,
):
    repo, course, _, task = research(tmp_path)
    for unit in task.units:
        finish_work(repo, course, task, unit)
    TaskPlanner(repo).plan(course, task.task_id)
    synth = TaskStore(repo).get(course, task.task_id).units[-1]
    with repo.engine.connect() as conn:
        spec = conn.execute(
            select(s.task_units.c.work_spec).where(s.task_units.c.unit_id == synth.unit_id)
        ).scalar_one()
    run, result = finish_work(
        repo,
        course,
        task,
        synth,
        source_ids=spec["research"]["citation_sources"],
        text="原范围综合",
    )
    before_evidence = ResearchStore(repo).evidence(course, task.task_id, synth.unit_id)
    gap = TaskCoverage(repo).get(course, task.task_id).sources[0]
    with repo.engine.begin() as conn:
        conn.execute(
            update(s.task_sources)
            .where(s.task_sources.c.source_id == gap.source_id)
            .values(state="blocked", error_code="KNOWN_SCOPE_WINDOW_FIXTURE")
        )
        row = TaskStore._row(conn, course, task.task_id)
        TaskStore._event(conn, row, "known_scope_window_fixture", {}, state="needs_sources")
    current = TaskStore(repo).get(course, task.task_id)
    TaskRecovery(repo).source_gap(
        course,
        task.task_id,
        SourceGapDecision(
            operation_id="exclude_after_publication",
            expected_revision=current.revision,
            decision="exclude",
            source_ids=[gap.source_id],
        ),
    )
    after_evidence = ResearchStore(repo).evidence(course, task.task_id, synth.unit_id)
    assert after_evidence == before_evidence and all(
        not item.from_current_run for item in after_evidence
    )
    assert TaskRunStore(repo).get(course, task.task_id, run.run_id).state == "completed"
    with pytest.raises(AppError):
        TaskRunStore(repo).claim(course, task.task_id, synth.unit_id, "不得重开原执行")
    assert any(
        block.markdown == "原范围综合"
        for block in ArtifactStore(repo)
        .get(course, result.artifact.artifact_id)
        .revision.body.blocks
    )
    assert repo.counts()["outbound_attempts"] == 0
    repo.close()


def test_explicit_retry_replaces_only_the_unstarted_synthesis_and_keeps_original_completed_runs(
    tmp_path,
):
    repo, course, _, task = research(tmp_path)
    runs = [finish_work(repo, course, task, unit)[0].run_id for unit in task.units]
    assert TaskPlanner(repo).plan(course, task.task_id)
    current = TaskStore(repo).get(course, task.task_id)
    old_synth = current.units[-1]
    assert old_synth.run_id is None
    coverage = TaskCoverage(repo).get(course, task.task_id)
    gap = coverage.sources[0]
    # A deterministic fault window after all evidence publications, before
    # synthesis admission. No failed model/HTTP execution is manufactured.
    with repo.engine.begin() as conn:
        conn.execute(
            update(s.task_sources)
            .where(s.task_sources.c.source_id == gap.source_id)
            .values(state="blocked", error_code="KNOWN_SOURCE_GAP_FIXTURE")
        )
        row = TaskStore._row(conn, course, task.task_id)
        TaskStore._event(conn, row, "known_source_gap_fixture", {}, state="needs_sources")
    current = TaskStore(repo).get(course, task.task_id)
    after = TaskRecovery(repo).source_gap(
        course,
        task.task_id,
        SourceGapDecision(
            operation_id="retry_same_source",
            expected_revision=current.revision,
            decision="retry",
            source_ids=[gap.source_id],
        ),
    )
    assert after.units[-1].excluded_at_scope_revision == 1
    current = refresh(repo, course, task.task_id)
    active = [unit for unit in current.units if unit.excluded_at_scope_revision is None]
    assert [unit.run_id for unit in active[:-1]] == runs
    assert active[-1].unit_id != old_synth.unit_id and active[-1].ordinal > old_synth.ordinal
    assert active[-1].run_id is None
    with repo.engine.connect() as conn:
        spec = conn.execute(
            select(s.task_units.c.work_spec).where(s.task_units.c.unit_id == active[-1].unit_id)
        ).scalar_one()
    assert spec["research"]["coverage_fact"]["scope_revision"] == 1
    assert {note["run_id"] for note in spec["research"]["notes"]} == set(runs)
    assert repo.counts()["outbound_attempts"] == 0
    repo.close()


@pytest.mark.host
async def test_actual_host_excludes_changed_source_keeps_manual_old_scope_and_never_resends_the_old_work(
    tmp_path,
):
    from test_native_business_host import runtime

    repo, course, root, task = research(tmp_path)
    adapter, _, registry, boundary = runtime(tmp_path, repo, None, bare=True)
    original = boundary._fixture
    inputs_sent = []

    def fixture(handler, request, scenario, row, observer):
        run = TaskRunStore(repo).internal(row["generation_id"])
        assert run["input_contract"]["research"]["phase"] == "evidence"
        inputs_sent.append([item["snapshot_id"] for item in run["input_contract"]["inputs"]])
        value = {
            "blocks": [
                {
                    "block_id": "summary",
                    "kind": "markdown",
                    "markdown": "甲的原范围笔记" if len(inputs_sent) == 1 else "当前范围仅乙资料",
                    "attribution": "source_paraphrase",
                    "source_snapshot_ids": inputs_sent[-1],
                }
            ]
        }
        result = original(
            handler,
            request,
            {"text_chunks": [json.dumps(value, ensure_ascii=False)]},
            row,
            observer,
        )
        if len(inputs_sent) == 1:
            (root / "lesson.txt").write_text("changed after actual frozen input", "utf-8")
        return result

    boundary._fixture = fixture
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), 35)
            current = TaskStore(repo).get(course, task.task_id)
            assert current.state == "needs_sources" and len(inputs_sent) == 1
            coverage = TaskCoverage(repo).get(course, task.task_id)
            first = next(source for source in coverage.sources if source.label == "lesson.txt")
            second = next(source for source in coverage.sources if source.label == "second.txt")
            assert first.state == "blocked" and second.state == "ready"
            artifact = ArtifactStore(repo).list(course)[0]
            old_run = current.units[0].run_id
            block = artifact.revision.body.blocks[0]
            ArtifactStore(repo).update(
                course,
                artifact.artifact_id,
                ArtifactUpdate(
                    operation_id="keep_manual_old_scope",
                    expected_revision_id=artifact.head_revision_id,
                    changes=[
                        BlockChange(
                            block_id=block.block_id,
                            replacement=block.model_copy(
                                update={"markdown": "用户保留的旧范围备注"}
                            ),
                        )
                    ],
                ),
            )
            TaskRecovery(repo).source_gap(
                course,
                task.task_id,
                SourceGapDecision(
                    operation_id="explicit_exclusion",
                    expected_revision=current.revision,
                    decision="exclude",
                    source_ids=[first.source_id],
                ),
            )
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), 35)
            final = TaskStore(repo).get(course, task.task_id)
            assert final.state == "completed" and len(inputs_sent) == len(boundary.records) == 2
            assert (
                final.units[0].run_id == old_run and final.units[0].excluded_at_scope_revision == 1
            )
            artifact = ArtifactStore(repo).list(course)[0]
            assert artifact.content_role == "mixed"
            assert {block.markdown for block in artifact.revision.body.blocks} == {
                "用户保留的旧范围备注",
                "当前范围仅乙资料",
            }
            scope = ArtifactScopeStore(repo).get(
                course, artifact.artifact_id, artifact.head_revision_id
            )
            assert len(scope.block_notices) == 1 and block.block_id in scope.block_notices
            assert not set(inputs_sent[0]) & set(inputs_sent[1])
            assert ResearchStore(repo).get(course, task.task_id).source_decision_required == []
            assert boundary.actual_outbound_attempts == 0
            assert all(run_id in registry.released for run_id in registry.scopes)
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
