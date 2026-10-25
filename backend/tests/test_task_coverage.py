"""Frozen multi-source tasks and model writes using explicit local event fixtures."""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select
from workbench_support import learning_course, note, settings

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.execution.types import ExecutionBinding, NormalizedEvent
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.models import (
    ArtifactBody,
    ArtifactCreate,
    ArtifactUpdate,
    MarkdownBlock,
    TaskAction,
)
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_controls import TaskRecovery
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import SourceGapDecision, TaskAdmission
from learning_app.workbench.tasks import TaskStore

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "task_pdf_fixtures", ROOT / "tests/fixtures/workbench/generate.py"
)
pdf_fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pdf_fixture)


def admit(repo, course_id, sources, *, operation="intent", target=None):
    return TaskCoverage(repo).admit(
        course_id,
        TaskAdmission(
            operation_id=operation,
            kind="handout",
            instruction="完整整理指定资料",
            coverage_mode="complete",
            sources=sources,
            target=target or {"mode": "create", "title": "完整讲义"},
            settings=settings(),
            sources_only=True,
        ),
    )


def refresh(repo, course_id, task_id):
    for _ in range(10):
        pending = TaskCoverage(repo).refresh(course_id, task_id)
        if not pending:
            break
    TaskPlanner(repo).plan(course_id, task_id)
    return TaskStore(repo).reconcile(course_id, task_id)


def finish_work(
    repo,
    course_id,
    task,
    unit,
    *,
    block_id="summary",
    text="来源归纳",
    fixture_attempt=True,
    source_ids=None,
):
    runs = TaskRunStore(repo)
    spec, sources = TaskPlanner(repo).inputs(course_id, task.task_id, unit.unit_id)
    run = runs.claim(
        course_id, task.task_id, unit.unit_id, "合成本地事件 fixture；不是实际模型验收"
    )
    binding = ExecutionBinding(
        task.task_id,
        f"request_{run.run_id}",
        run.run_id,
        1,
        f"sid_{run.run_id}",
        f"request_{run.run_id}",
        native_floor=0,
        native_turn=1,
    )
    runs.persist_binding(run.run_id, binding, "local_event_fixture")
    assert runs.mark_dispatching(run.run_id)
    runs.ingest(
        run.run_id,
        NormalizedEvent(
            f"done_{run.run_id}", 1, run.run_id, 1, "terminal", status="completed", native_seq=10
        ),
        source="fixture",
    )
    if fixture_attempt:
        runs.record_attempts(
            run.run_id,
            [
                dict(
                    attempt_id=f"attempt_{run.run_id}",
                    request_id=f"request_{run.run_id}",
                    generation_id=run.run_id,
                    model=settings().model,
                    thinking=settings().thinking,
                    kind="model",
                    actual_outbound=False,
                    actual_outbound_attempt=None,
                    status="completed",
                    received_ns=1,
                    finished_ns=2,
                    request_sha256="a" * 64,
                )
            ],
            source="fixture",
        )
    result = ArtifactStore(repo).publish_run_result(
        run.run_id,
        ArtifactCreate(
            operation_id="ignored",
            kind=runs.internal(run.run_id)["input_contract"]["target"]["artifact_kind"],
            title="忽略模型重命名",
            body=ArtifactBody(
                blocks=[
                    MarkdownBlock(
                        block_id=block_id,
                        markdown=text,
                        attribution="source_paraphrase",
                        source_snapshot_ids=source_ids
                        if source_ids is not None
                        else [source.snapshot_id for _, source in sources],
                    ),
                ]
            ),
        ),
    )
    return run, result


def test_bad_initial_pdf_is_durable_unknown_count_and_keeps_good_draft(tmp_path):
    repo, course_id, root, selection = learning_course(tmp_path)
    bad = root / "broken.pdf"
    bad.write_bytes(b"%PDF-1.7\ncorrupt synthetic fixture")
    bad_source = CourseFileStore(repo).open(course_id, "broken.pdf")
    sources = [
        {
            "kind": "document",
            "asset_id": selection.reference.asset_id,
            "source_version": selection.reference.source_version,
        },
        {
            "kind": "document",
            "asset_id": bad_source["asset_id"],
            "source_version": bad_source["source_version"],
        },
    ]
    task = admit(repo, course_id, sources)
    assert task.layout_version == 2 and task.units == []
    assert admit(repo, course_id, sources).task_id == task.task_id
    initial = TaskCoverage(repo).get(course_id, task.task_id)
    assert all(source.expected_leaf_count is None for source in initial.sources)
    assert not initial.target.materialized
    task = refresh(repo, course_id, task.task_id)
    assert len(task.units) == 1
    run, receipt = finish_work(repo, course_id, task, task.units[0])
    current = TaskStore(repo).reconcile(course_id, task.task_id)
    assert current.state == "needs_sources"
    coverage = TaskCoverage(repo).get(course_id, task.task_id)
    broken = next(source for source in coverage.sources if source.state == "blocked")
    assert broken.expected_leaf_count is None and broken.error_code == "PDF_UNREADABLE"
    assert (
        coverage.target.materialized and receipt.artifact.artifact_id == initial.target.artifact_id
    )
    assert len(ArtifactStore(repo).list(course_id)) == 1
    assert receipt.artifact.content_role == "ai_generated"
    # No replay is caused by viewing or planning a blocked complete task.
    refresh(repo, course_id, task.task_id)
    assert TaskStore(repo).get(course_id, task.task_id).units[0].run_id == run.run_id
    partial = TaskRecovery(repo).source_gap(
        course_id,
        task.task_id,
        SourceGapDecision(
            operation_id="accept_partial",
            expected_revision=current.revision,
            decision="accept_partial",
        ),
    )
    assert partial.state == "partial" and partial.coverage_mode == "complete"
    assert bad.read_bytes().startswith(b"%PDF-1.7")
    repo.close()


def test_packed_work_appends_one_result_and_preserves_unrelated_manual_edits(tmp_path):
    repo, course_id, root, _ = learning_course(tmp_path)
    declarations = []
    for i in range(2):
        (root / f"large{i}.txt").write_text((f"来源{i} A🧠B。" * 4000), "utf-8")
        source = CourseFileStore(repo).open(course_id, f"large{i}.txt")
        declarations.append(
            {
                "kind": "document",
                "asset_id": source["asset_id"],
                "source_version": source["source_version"],
            }
        )
    task = refresh(repo, course_id, admit(repo, course_id, declarations).task_id)
    assert len(task.units) > 2  # Exact codepoint slices, not a silent text cap.
    all_ids = set()
    for ordinal, unit in enumerate(task.units):
        run, result = finish_work(
            repo,
            course_id,
            TaskStore(repo).get(course_id, task.task_id),
            unit,
            text=f"完成来源批次{ordinal}",
        )
        all_ids.add(result.artifact.artifact_id)
        if ordinal == 0:
            block = result.artifact.revision.body.blocks[0]
            ArtifactStore(repo).update(
                course_id,
                result.artifact.artifact_id,
                ArtifactUpdate(
                    operation_id="manual_edit",
                    expected_revision_id=result.artifact.head_revision_id,
                    changes=[
                        {
                            "block_id": block.block_id,
                            "replacement": block.model_copy(
                                update={"markdown": "用户修订已完成内容"}
                            ),
                        }
                    ],
                ),
            )
        # Publication is one operation, including index and coverage receipt.
        private = TaskRunStore(repo).internal(run.run_id)
        inputs = private["input_contract"]["inputs"]
        assert all(item["char_end"] - item["char_start"] <= 24_000 for item in inputs)
    final = TaskStore(repo).reconcile(course_id, task.task_id)
    assert final.state == "completed" and len(all_ids) == 1
    result = ArtifactStore(repo).get(course_id, all_ids.pop())
    assert result.content_role == "mixed" and result.revision.author == "model"
    assert any(block.markdown == "用户修订已完成内容" for block in result.revision.body.blocks)
    assert len(result.revision.body.blocks) == len(task.units)
    coverage = TaskCoverage(repo).get(course_id, task.task_id)
    assert sum(source.processed_leaf_count for source in coverage.sources) == 2
    assert len(ArtifactStore(repo).list(course_id)) == 1
    repo.close()


def test_edit_task_retains_same_block_conflict_with_model_identity(tmp_path):
    repo, course_id, _, source = learning_course(tmp_path)
    product = ArtifactStore(repo).create(course_id, note(snapshot_id=source.snapshot_id)).artifact
    task = refresh(
        repo,
        course_id,
        admit(
            repo,
            course_id,
            [{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={
                "mode": "edit",
                "artifact_id": product.artifact_id,
                "expected_revision_id": product.head_revision_id,
                "block_ids": ["paragraph_1"],
            },
        ).task_id,
    )
    ArtifactStore(repo).update(
        course_id,
        product.artifact_id,
        ArtifactUpdate(
            operation_id="concurrent_manual",
            expected_revision_id=product.head_revision_id,
            changes=[
                {
                    "block_id": "paragraph_1",
                    "replacement": {
                        "block_id": "paragraph_1",
                        "kind": "markdown",
                        "markdown": "用户并发修订",
                        "attribution": "user_authored",
                        "source_snapshot_ids": [],
                    },
                }
            ],
        ),
    )
    run, receipt = finish_work(
        repo, course_id, task, task.units[0], block_id="paragraph_1", text="AI并发修订"
    )
    assert (
        receipt.outcome == "conflict"
        and receipt.artifact.revision.body.blocks[0].markdown == "用户并发修订"
    )
    history = ArtifactStore(repo).history(course_id, product.artifact_id)
    proposal = next(
        item for item in history if item.revision_id == receipt.conflict.proposed_revision_id
    )
    assert (
        proposal.author == "model"
        and proposal.task_id == task.task_id
        and proposal.disposition == "conflict"
    )
    assert proposal.body.blocks[0].markdown == "AI并发修订"
    assert TaskStore(repo).reconcile(course_id, task.task_id).state == "failed"
    repo.close()


def test_native_pdf_text_pages_pack_together_and_scanned_page_stays_an_image(tmp_path):
    repo, course_id, root, _ = learning_course(tmp_path)
    pdf_fixture.reading_pdf(root / "native.pdf", 12, blank_pages=(3,))
    pdf_fixture.scanned_pdf(root / "scan.pdf")
    declarations = []
    for name in ("native.pdf", "scan.pdf"):
        source = CourseFileStore(repo).open(course_id, name)
        declarations.append(
            {
                "kind": "document",
                "asset_id": source["asset_id"],
                "source_version": source["source_version"],
            }
        )
    task = refresh(repo, course_id, admit(repo, course_id, declarations).task_id)
    assert len(task.units) == 2
    coverage = TaskCoverage(repo).get(course_id, task.task_id)
    assert (
        len(coverage.leaves) == 13 and sum(leaf.state == "empty" for leaf in coverage.leaves) == 1
    )
    assert sum("image" in leaf.required_representations for leaf in coverage.leaves) == 12
    planned = [
        TaskPlanner(repo).inputs(course_id, task.task_id, unit.unit_id)[1] for unit in task.units
    ]
    inputs = [item for group in planned for item in group]
    images = [snapshot for _, snapshot in inputs if snapshot.reference.kind == "document_image"]
    assert len(images) == 12 and images[0].text == ""
    # A native page's text cannot be sent without its layout in another work.
    for group in planned:
        by_leaf = {}
        for item, snapshot in group:
            by_leaf.setdefault(item["leaf_id"], set()).add(item["representation"])
        assert all("image" in kinds for kinds in by_leaf.values())
    assert SourceStore(repo).image_path(images[0].reference.image_sha256).is_file()
    assert not any(snapshot.reference.kind == "subtitle" for _, snapshot in inputs)
    with repo.engine.connect() as conn:
        assert not conn.execute(
            select(s.preparations)
        ).first()  # No extra OCR model/local job required for this generation.
    repo.close()


def test_unknown_run_cannot_be_repacked_or_retried_and_input_ledger_needs_dispatch(tmp_path):
    repo, course_id, _, source = learning_course(tmp_path)
    task = refresh(
        repo,
        course_id,
        admit(repo, course_id, [{"kind": "selection", "snapshot_id": source.snapshot_id}]).task_id,
    )
    runs = TaskRunStore(repo)
    run = runs.claim(course_id, task.task_id, task.units[0].unit_id, "frozen prompt")
    runs.mark_unknown(run.run_id, "SYNTHETIC_UNKNOWN")
    state = TaskStore(repo).reconcile(course_id, task.task_id)
    assert state.state == "unknown"
    assert not TaskPlanner(repo).plan(course_id, task.task_id)
    with pytest.raises(AppError) as error:
        TaskRecovery(repo).retry_work(
            course_id,
            task.task_id,
            task.units[0].unit_id,
            TaskAction(operation_id="retry_unknown", expected_revision=state.revision),
        )
    assert error.value.code == "TASK_WORK_RETRY_DENIED"
    with repo.engine.connect() as conn:
        item = (
            conn.execute(select(s.run_inputs).where(s.run_inputs.c.run_id == run.run_id))
            .mappings()
            .one()
        )
        assert item["proof"]["state"] == "prepared"
    assert TaskCoverage(repo).get(course_id, task.task_id).sources[0].processed_leaf_count == 0
    repo.close()


def test_course_membership_is_frozen_before_background_and_changed_member_is_a_gap(tmp_path):
    repo, course_id, root, _ = learning_course(tmp_path)
    task = admit(repo, course_id, [{"kind": "course"}])
    (root / "added-later.txt").write_text("不属于原始范围，后台不能自动加入。", "utf-8")
    (root / "lesson.txt").write_text("原课程成员已改成新版本，不能静默使用。", "utf-8")
    current = refresh(repo, course_id, task.task_id)
    assert current.state == "needs_sources" and not current.units
    coverage = TaskCoverage(repo).get(course_id, task.task_id)
    assert len(coverage.sources) == 2
    member = next(
        source for source in coverage.sources if source.declaration["kind"] == "course_member"
    )
    assert (
        member.declaration["relative_path"] == "lesson.txt"
        and member.error_code == "SOURCE_VERSION_UNAVAILABLE"
    )
    assert member.expected_leaf_count is None
    repo.close()


def test_all_blank_sources_do_not_claim_a_completed_learning_result(tmp_path):
    repo, course_id, root, _ = learning_course(tmp_path)
    (root / "blank.txt").write_text(" \n\t", "utf-8")
    source = CourseFileStore(repo).open(course_id, "blank.txt")
    task = refresh(
        repo,
        course_id,
        admit(
            repo,
            course_id,
            [
                {
                    "kind": "document",
                    "asset_id": source["asset_id"],
                    "source_version": source["source_version"],
                }
            ],
        ).task_id,
    )
    assert task.state == "needs_sources" and not task.units
    coverage = TaskCoverage(repo).get(course_id, task.task_id)
    assert coverage.leaves[0].state == "empty" and not coverage.target.materialized
    assert not ArtifactStore(repo).list(course_id)
    repo.close()


def test_pause_during_source_read_keeps_late_local_result_unpublished(tmp_path, monkeypatch):
    repo, course_id, _, source = learning_course(tmp_path)
    task = admit(repo, course_id, [{"kind": "selection", "snapshot_id": source.snapshot_id}])
    original = SourceStore.require_current
    calls = 0

    def pause_during_leaf(store, snapshot, **options):
        nonlocal calls
        original(store, snapshot, **options)
        calls += 1
        if calls == 2:
            latest = TaskStore(repo).get(course_id, task.task_id)
            TaskStore(repo).control(
                course_id,
                task.task_id,
                "pause",
                TaskAction(operation_id="pause_during_read", expected_revision=latest.revision),
            )

    monkeypatch.setattr(SourceStore, "require_current", pause_during_leaf)
    TaskCoverage(repo).refresh(course_id, task.task_id)
    current = TaskStore(repo).get(course_id, task.task_id)
    assert current.state == "paused" and current.units == []
    assert TaskCoverage(repo).get(course_id, task.task_id).leaves[0].state == "pending"
    assert not TaskPlanner(repo).plan(course_id, task.task_id)
    repo.close()


def test_excluding_every_missing_source_never_creates_knowledge_only_work(tmp_path):
    repo, course_id, root, _ = learning_course(tmp_path)
    (root / "bad.pdf").write_bytes(b"%PDF-1.7\ncorrupt local fixture")
    source = CourseFileStore(repo).open(course_id, "bad.pdf")
    task = refresh(
        repo,
        course_id,
        admit(
            repo,
            course_id,
            [
                {
                    "kind": "document",
                    "asset_id": source["asset_id"],
                    "source_version": source["source_version"],
                }
            ],
        ).task_id,
    )
    gap = TaskCoverage(repo).get(course_id, task.task_id).sources[0]
    task = TaskRecovery(repo).source_gap(
        course_id,
        task.task_id,
        SourceGapDecision(
            operation_id="exclude_all",
            expected_revision=task.revision,
            decision="exclude",
            source_ids=[gap.source_id],
        ),
    )
    assert not TaskPlanner(repo).plan(course_id, task.task_id)
    task = TaskStore(repo).reconcile(course_id, task.task_id)
    assert task.state == "needs_sources" and not task.units
    assert not TaskCoverage(repo).get(course_id, task.task_id).target.materialized
    repo.close()


def test_completed_result_with_missing_attempt_proof_waits_for_original_reconciliation(tmp_path):
    repo, course_id, _, source = learning_course(tmp_path)
    task = refresh(
        repo,
        course_id,
        admit(repo, course_id, [{"kind": "selection", "snapshot_id": source.snapshot_id}]).task_id,
    )
    run, result = finish_work(repo, course_id, task, task.units[0], fixture_attempt=False)
    current = TaskStore(repo).reconcile(course_id, task.task_id)
    assert current.state == "unknown" and current.units[0].state == "completed"
    assert (
        result.artifact.artifact_id
        == TaskCoverage(repo).get(course_id, task.task_id).target.artifact_id
    )
    assert not TaskPlanner(repo).plan(course_id, task.task_id)
    with pytest.raises(AppError) as error:
        TaskRecovery(repo).source_gap(
            course_id,
            task.task_id,
            SourceGapDecision(
                operation_id="partial_before_reconciliation",
                expected_revision=current.revision,
                decision="accept_partial",
            ),
        )
    assert error.value.code == "TASK_RECONCILIATION_REQUIRED"
    paused = TaskStore(repo).control(
        course_id,
        task.task_id,
        "pause",
        TaskAction(operation_id="pause_pending_input_proof", expected_revision=current.revision),
    )
    with pytest.raises(AppError) as error:
        TaskRecovery(repo).source_gap(
            course_id,
            task.task_id,
            SourceGapDecision(
                operation_id="partial_while_paused_unknown",
                expected_revision=paused.revision,
                decision="accept_partial",
            ),
        )
    assert error.value.code == "TASK_RECONCILIATION_REQUIRED"
    assert (
        TaskRunStore(repo)
        .claim(
            course_id, task.task_id, task.units[0].unit_id, "合成本地事件 fixture；不是实际模型验收"
        )
        .run_id
        == run.run_id
    )
    repo.close()


def test_legacy_large_edit_without_range_contract_stays_blocked_and_preserves_base(tmp_path):
    repo, course_id, _, source = learning_course(tmp_path)
    product = (
        ArtifactStore(repo)
        .create(course_id, note(text="完整保留原稿" * 9000, snapshot_id=source.snapshot_id))
        .artifact
    )
    task = TaskCoverage(repo).admit(
        course_id,
        TaskAdmission(
            operation_id="large_edit",
            kind="handout",
            instruction="整理这一完整内容块",
            coverage_mode="complete",
            sources=[],
            target={
                "mode": "edit",
                "artifact_id": product.artifact_id,
                "expected_revision_id": product.head_revision_id,
                "block_ids": ["paragraph_1"],
            },
            settings=settings(),
            sources_only=True,
        ),
    )
    # An already-admitted task retains its old whole-block contract. The new
    # planner must not silently migrate its source scope or regenerate work.
    from sqlalchemy import update

    with repo.engine.begin() as conn:
        target = (
            conn.execute(select(s.task_targets).where(s.task_targets.c.task_id == task.task_id))
            .mappings()
            .one()
        )
        conn.execute(
            update(s.task_targets)
            .where(s.task_targets.c.task_id == task.task_id)
            .values(write_scope={"block_ids": target["write_scope"]["block_ids"]})
        )
    TaskCoverage(repo).refresh(course_id, task.task_id)
    assert not TaskPlanner(repo).plan(course_id, task.task_id)
    current = TaskStore(repo).reconcile(course_id, task.task_id)
    assert current.state == "failed" and not current.units
    target = TaskCoverage(repo).get(course_id, task.task_id).target
    assert (
        target.state == "blocked" and target.last_receipt["error_code"] == "TASK_EDIT_INPUT_LIMIT"
    )
    assert (
        ArtifactStore(repo).get(course_id, product.artifact_id).head_revision_id
        == product.head_revision_id
    )
    with repo.engine.connect() as conn:
        assert not conn.execute(select(s.task_runs)).first()
    repo.close()


def test_dense_geometry_stays_in_source_store_and_material_batches_are_bounded(tmp_path):
    from learning_app.workbench.models import DocumentRef, PageRegion
    from learning_app.workbench.task_materials import material
    from learning_app.workbench.task_planning import MAX_WORK_INPUTS

    repo, course_id, _, selection = learning_course(tmp_path)
    declarations = []
    for index in range(120):
        snapshot = SourceStore(repo)._save_verified(
            course_id,
            DocumentRef(
                asset_id=selection.reference.asset_id,
                source_version=selection.reference.source_version,
                source_digest=selection.reference.source_digest,
                page_index=0,
                start=0,
                end=1,
                parser_version="utf8-text-v1",
                regions=[PageRegion(x0=0, y0=0, x1=0.5 + index / 1000, y1=0.5) for _ in range(256)],
            ),
            "O",
            "original",
        )
        declarations.append({"kind": "selection", "snapshot_id": snapshot.snapshot_id})
    task = TaskCoverage(repo).admit(
        course_id,
        TaskAdmission(
            operation_id="many_small_selections",
            kind="handout",
            instruction="整理全部所选字母",
            coverage_mode="complete",
            sources=declarations,
            target={"mode": "create", "title": "大量小选区"},
            settings=settings(),
            sources_only=True,
        ),
    )
    for _ in range(20):
        TaskCoverage(repo).refresh(course_id, task.task_id, budget=16)
    assert TaskPlanner(repo).plan(course_id, task.task_id)
    current = TaskStore(repo).get(course_id, task.task_id)
    assert len(current.units) == 2
    total_characters = 0
    for unit in current.units:
        spec, inputs = TaskPlanner(repo).inputs(course_id, task.task_id, unit.unit_id)
        assert len(inputs) <= MAX_WORK_INPUTS
        for item, snapshot in inputs:
            total_characters += len(snapshot.text)
            assert len(snapshot.reference.regions) == 256
            assert "regions" not in material(item, snapshot)["reference"]
    assert total_characters == 120
    repo.close()


def test_transcript_wait_and_explicit_local_retry_keep_one_job_and_survive_lost_receipt(tmp_path):
    import wave

    from learning_app.workbench.preparations import PreparationStore

    repo, course_id, root, _ = learning_course(tmp_path)
    with wave.open(str(root / "synthetic.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    source = CourseFileStore(repo).open(course_id, "synthetic.wav")
    digest = repo.resolve_source(source["asset_id"], source["source_version"])["content_hash"]
    preparations = PreparationStore(repo)
    job = preparations.publish_pdf(
        {
            "payload": {
                "course_id": course_id,
                "asset_id": source["asset_id"],
                "source_version": source["source_version"],
                "source_digest": digest,
                "kind": "asr",
                "scope": "full",
                "parameters": {
                    "engine": {"fixture": "status and local control only; no decoding"},
                    "track_id": "fixture_track",
                    "scope_indexes": [0],
                    "requested_unit_index": None,
                },
            },
            "priority_unit": None,
        }
    )
    lease = preparations.claim()
    assert preparations.finish(lease, None, "ASR_SYNTHETIC_LOCAL_FAILURE")
    failed_revision = preparations.get(course_id, job.job_id).revision
    task = refresh(
        repo,
        course_id,
        admit(
            repo,
            course_id,
            [
                {
                    "kind": "transcript",
                    "asset_id": source["asset_id"],
                    "source_version": source["source_version"],
                    "track_id": "fixture_track",
                }
            ],
        ).task_id,
    )
    gap = TaskCoverage(repo).get(course_id, task.task_id).sources[0]
    assert gap.state == "blocked" and gap.error_code == "TASK_PREPARATION_FAILED"
    TaskRecovery(repo).source_gap(
        course_id,
        task.task_id,
        SourceGapDecision(
            operation_id="explicit_asr_retry",
            expected_revision=task.revision,
            decision="retry",
            source_ids=[gap.source_id],
        ),
    )

    # Lost acknowledgement after the local control commit is not a new ASR job.
    def lose_receipt(name, detail):
        if name == "task_preparation_retry_after_control":
            raise OSError("synthetic process loss after durable local retry")

    repo.fault_hook = lose_receipt
    with pytest.raises(OSError):
        TaskCoverage(repo).refresh(course_id, task.task_id)
    retried = preparations.get(course_id, job.job_id)
    assert retried.state == "queued" and retried.revision == failed_revision + 1
    repo.fault_hook = None
    TaskCoverage(repo).refresh(course_id, task.task_id)
    assert preparations.get(course_id, job.job_id).revision == retried.revision
    assert len(preparations.list(course_id)) == 1
    # A valid probe may be ready before any raw transcript unit. Waiting is a
    # normal state and must not open a nested BEGIN IMMEDIATE transaction.
    from sqlalchemy import update

    with repo.engine.begin() as conn:
        conn.execute(
            update(s.transcript_tracks)
            .where(s.transcript_tracks.c.track_id == "fixture_track")
            .values(probe={"audio_start_ms": 0, "audio_end_ms": 1000, "unit_indexes": [0]})
        )
    TaskCoverage(repo).refresh(course_id, task.task_id)
    scope = TaskCoverage(repo).get(course_id, task.task_id)
    assert scope.sources[0].expected_leaf_count == 1 and scope.leaves[0].state == "pending"
    assert not scope.target.materialized
    repo.close()


def test_complete_task_expands_selected_current_only_track_with_same_parameters(
    tmp_path, monkeypatch
):
    import wave

    from sqlalchemy import update

    from learning_app.config import PROJECT_ROOT
    from learning_app.workbench.asr_resources import AsrResources
    from learning_app.workbench.preparations import PreparationStore
    from learning_app.workbench.processing_models import PreparationCreate

    monkeypatch.setattr(AsrResources, "prepared", lambda self: True)
    repo, course_id, root, _ = learning_course(tmp_path)
    with wave.open(str(root / "current-only.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    source = CourseFileStore(repo).open(course_id, "current-only.wav")
    preparations = PreparationStore(repo)
    definition = preparations.define_asr(
        course_id,
        PreparationCreate(
            kind="asr",
            asset_id=source["asset_id"],
            source_version=source["source_version"],
            scope="unit",
            unit_index=1,
            language="en",
        ),
        PROJECT_ROOT,
    )
    current = preparations.publish_pdf(definition)
    track_id = current.parameters["track_id"]
    # Only status/probe fixture: this test does not decode or run a speech model.
    with repo.engine.begin() as conn:
        conn.execute(
            update(s.transcript_tracks)
            .where(s.transcript_tracks.c.track_id == track_id)
            .values(probe={"audio_start_ms": 0, "audio_end_ms": 1000, "unit_indexes": [0]})
        )
        conn.execute(
            update(s.preparations)
            .where(s.preparations.c.job_id == current.job_id)
            .values(state="completed")
        )
        conn.execute(
            update(s.preparation_units)
            .where(s.preparation_units.c.job_id == current.job_id)
            .values(state="completed")
        )
    task = admit(
        repo,
        course_id,
        [
            {
                "kind": "transcript",
                "asset_id": source["asset_id"],
                "source_version": source["source_version"],
                "track_id": track_id,
            }
        ],
    )
    TaskCoverage(repo).refresh(course_id, task.task_id)
    scope = TaskCoverage(repo).get(course_id, task.task_id)
    assert scope.sources[0].state == "preparing" and scope.sources[0].expected_leaf_count == 1
    assert scope.sources[0].preparation_job_id != current.job_id
    full = preparations.get(course_id, scope.sources[0].preparation_job_id)
    assert full.scope == "full" and full.parameters["track_id"] == track_id
    assert full.parameters["engine"] == current.parameters["engine"]
    assert preparations.get(course_id, current.job_id).state == "completed"
    TaskCoverage(repo).refresh(course_id, task.task_id)
    assert len(preparations.list(course_id)) == 2
    repo.close()
