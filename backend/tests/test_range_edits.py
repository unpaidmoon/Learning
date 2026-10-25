"""Long edits preserve original CP coordinates, receipts and concurrent drafts."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select
from test_task_coverage import admit, finish_work, refresh
from workbench_support import learning_course, note

from learning_app.errors import AppError
from learning_app.storage import repository as storage
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.models import ArtifactCreate, ArtifactUpdate, TaskAction
from learning_app.workbench.range_edits import RangeEdits
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.tasks import TaskStore


def long_target(repo, course, source, *, paired=False):
    text = "This 🧠 old phrase stays beside a formula $x^2$.\r\n\r\n" * 600
    block = {
        "block_id": "paragraph_1",
        "kind": "markdown",
        "markdown": text,
        "attribution": "user_authored",
        "source_snapshot_ids": [],
    }
    if paired:
        block.update(
            kind="translation",
            source_snapshot_ids=[source.snapshot_id],
            translation={
                "source_text": source.text,
                "source_parts": [
                    {"snapshot_id": source.snapshot_id, "start": 0, "end": len(source.text)}
                ],
            },
        )
    artifact = (
        ArtifactStore(repo)
        .create(
            course,
            ArtifactCreate(
                operation_id="long_target",
                kind="translation" if paired else "note",
                title="长正文",
                body={
                    "blocks": [
                        block,
                        {
                            "block_id": "untouched",
                            "kind": "markdown",
                            "markdown": "其他内容",
                            "attribution": "user_authored",
                            "source_snapshot_ids": [],
                        },
                    ]
                },
            ),
        )
        .artifact
    )
    task = refresh(
        repo,
        course,
        admit(
            repo,
            course,
            [],
            target={
                "mode": "edit",
                "artifact_id": artifact.artifact_id,
                "expected_revision_id": artifact.head_revision_id,
                "block_ids": ["paragraph_1"],
            },
        ).task_id,
    )
    return artifact, task, text


@pytest.mark.parametrize("paired", [False, True], ids=["markdown", "translation"])
def test_disjoint_publications_rebuild_from_base_in_reverse_order_and_keep_other_edits(
    tmp_path, paired
):
    repo, course, _, source = learning_course(tmp_path)
    artifact, task, text = long_target(repo, course, source, paired=paired)
    assert len(task.units) > 3 and task.state == "queued"
    planned = [TaskPlanner(repo).inputs(course, task.task_id, unit.unit_id) for unit in task.units]
    intervals = [spec["edit_range"] for spec, _ in planned]
    assert intervals[0]["start"] == 0 and intervals[-1]["end"] == len(text)
    assert all(a["end"] == b["start"] for a, b in zip(intervals, intervals[1:]))
    assert "".join(values[0][1].text for _, values in planned) == text
    # In a translation, neither the source-language prefix nor its ancestors is input.
    assert all(values[0][1].content_role == "user_note" for _, values in planned)
    if paired:
        assert all(source.text not in values[0][1].text for _, values in planned)
    outputs = {}
    for index, unit in enumerate(reversed(task.units)):
        _, inputs = TaskPlanner(repo).inputs(course, task.task_id, unit.unit_id)
        replacement = inputs[0][1].text.replace("old", "a much longer NEW") + f"[{index}🧠]"
        run, receipt = finish_work(
            repo,
            course,
            task,
            unit,
            block_id="paragraph_1",
            text=replacement,
        )
        assert receipt.outcome == "applied" and receipt.artifact.artifact_id == artifact.artifact_id
        outputs[unit.unit_id] = replacement
        if index == 0:
            current = receipt.artifact
            ArtifactStore(repo).update(
                course,
                artifact.artifact_id,
                ArtifactUpdate(
                    operation_id="unrelated_manual",
                    expected_revision_id=current.head_revision_id,
                    changes=[
                        {
                            "block_id": "untouched",
                            "replacement": {
                                "block_id": "untouched",
                                "kind": "markdown",
                                "markdown": "另一块的手动修改",
                                "attribution": "user_authored",
                                "source_snapshot_ids": [],
                            },
                        }
                    ],
                ),
            )
        # Reconciliation/planning never replaces completed or still-pending work IDs.
        assert not TaskPlanner(repo).plan(course, task.task_id)
        assert TaskRunStore(repo).internal(run.run_id)["input_contract"]["edit_range"]
    result = ArtifactStore(repo).get(course, artifact.artifact_id)
    assert result.revision.body.blocks[0].markdown == "".join(
        outputs[u.unit_id] for u in task.units
    )
    assert result.revision.body.blocks[1].markdown == "另一块的手动修改"
    if paired:
        assert (
            result.revision.body.blocks[0].translation
            == artifact.revision.body.blocks[0].translation
        )
        assert result.revision.body.blocks[0].source_snapshot_ids == [source.snapshot_id]
    assert result.content_role == "mixed" and result.revision.author == "model"
    assert TaskStore(repo).reconcile(course, task.task_id).state == "completed"
    with repo.engine.connect() as conn:
        assert conn.execute(
            select(func.count())
            .select_from(s.operations)
            .where(s.operations.c.kind == "task_range_patch")
        ).scalar_one() == len(task.units)
    assert len(ArtifactStore(repo).list(course)) == 1
    repo.close()


def test_manual_change_inside_target_retains_both_and_no_receipt_for_failed_patch(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    artifact, task, _ = long_target(repo, course, source)
    _, first = finish_work(
        repo, course, task, task.units[0], block_id="paragraph_1", text="首段修改🧠"
    )
    block = first.artifact.revision.body.blocks[0]
    manual = ArtifactStore(repo).update(
        course,
        artifact.artifact_id,
        ArtifactUpdate(
            operation_id="same_block_manual",
            expected_revision_id=first.artifact.head_revision_id,
            changes=[
                {
                    "block_id": "paragraph_1",
                    "replacement": block.model_copy(update={"markdown": "用户的整块修改"}),
                }
            ],
        ),
    )
    _, conflict = finish_work(
        repo, course, task, task.units[1], block_id="paragraph_1", text="第二段模型修改"
    )
    assert conflict.outcome == "conflict"
    assert conflict.artifact.head_revision_id == manual.artifact.head_revision_id
    history = ArtifactStore(repo).history(course, artifact.artifact_id)
    proposal = next(r for r in history if r.revision_id == conflict.conflict.proposed_revision_id)
    assert proposal.body.blocks[0].markdown.startswith("首段修改🧠第二段模型修改")
    assert conflict.artifact.revision.body.blocks[0].markdown == "用户的整块修改"
    assert TaskStore(repo).reconcile(course, task.task_id).state == "failed"
    with repo.engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count())
                .select_from(s.operations)
                .where(s.operations.c.kind == "task_range_patch")
            ).scalar_one()
            == 1
        )
    repo.close()


def test_range_contract_rejects_changed_source_interval_wrong_block_and_changed_translation(
    tmp_path,
):
    repo, course, _, source = learning_course(tmp_path)
    _, task, _ = long_target(repo, course, source, paired=True)
    with pytest.raises(AppError) as rejected:
        finish_work(repo, course, task, task.units[0], block_id="untouched")
    assert rejected.value.code == "TASK_EDIT_RANGE_OUTPUT_DENIED"
    current = TaskStore(repo).get(course, task.task_id)
    unit = current.units[1]
    spec, _ = TaskPlanner(repo).inputs(course, task.task_id, unit.unit_id)
    target = None
    with repo.engine.connect() as conn:
        target = (
            conn.execute(select(s.task_targets).where(s.task_targets.c.task_id == task.task_id))
            .mappings()
            .one()
        )
        bad = {**spec, "edit_range": {**spec["edit_range"], "start": 0}}
        with pytest.raises(AppError) as rejected:
            RangeEdits.validate_work(conn, {"course_id": course}, bad, target)
        assert rejected.value.code == "TASK_EDIT_RANGE_INPUT_DENIED"
    assert ArtifactStore(repo).list(course)[0].revision.author == "user"
    repo.close()


def test_pause_and_unknown_do_not_replan_or_publish_another_copy(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    artifact, task, _ = long_target(repo, course, source)
    runs = TaskRunStore(repo)
    run = runs.claim(course, task.task_id, task.units[0].unit_id, "local unknown fixture")
    runs.mark_unknown(run.run_id, "SYNTHETIC_UNKNOWN")
    current = TaskStore(repo).reconcile(course, task.task_id)
    assert current.state == "unknown" and not TaskPlanner(repo).plan(course, task.task_id)
    paused = TaskStore(repo).control(
        course,
        task.task_id,
        "pause",
        TaskAction(
            operation_id="pause",
            expected_revision=current.revision,
        ),
    )
    assert paused.state == "paused" and not TaskPlanner(repo).plan(course, task.task_id)
    assert ArtifactStore(repo).get(course, artifact.artifact_id) == artifact
    assert TaskRunStore(repo).internal(run.run_id)["state"] == "unknown"
    repo.close()


def test_range_contract_downgrade_is_refused_before_any_publication(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    artifact, _, _ = long_target(repo, course, source)
    config = Config()
    config.set_main_option("script_location", str(Path(storage.__file__).parent / "migrations"))
    with pytest.raises(RuntimeError, match="LEARNING_RANGE_DOWNGRADE_DENIED"):
        with repo.engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, "0026_learning_text_contracts")
    assert ArtifactStore(repo).get(course, artifact.artifact_id) == artifact
    assert repo.runtime_info()["schema_revision"] == storage.SCHEMA_REVISION
    repo.close()


def test_lost_commit_rolls_back_patch_revision_index_and_receipts_then_reconciles_once(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    artifact, task, _ = long_target(repo, course, source)

    def fail_commit(name, detail):
        if name == "learning_run_product_before_commit":
            raise RuntimeError("synthetic product commit failure")

    repo.fault_hook = fail_commit
    with pytest.raises(RuntimeError, match="synthetic product commit failure"):
        finish_work(
            repo, course, task, task.units[0], block_id="paragraph_1", text="恢复后的第一段"
        )
    repo.fault_hook = None
    assert ArtifactStore(repo).get(course, artifact.artifact_id) == artifact
    current = TaskStore(repo).get(course, task.task_id)
    unit = current.units[0]
    assert unit.run_id and unit.result_artifact_id is None
    _, inputs = TaskPlanner(repo).inputs(course, task.task_id, unit.unit_id)
    value = ArtifactCreate(
        operation_id="ignored",
        kind="note",
        title="忽略模型重命名",
        body={
            "blocks": [
                {
                    "block_id": "paragraph_1",
                    "kind": "markdown",
                    "markdown": "恢复后的第一段",
                    "attribution": "source_paraphrase",
                    "source_snapshot_ids": [snapshot.snapshot_id for _, snapshot in inputs],
                }
            ]
        },
    )
    first = ArtifactStore(repo).publish_run_result(unit.run_id, value)
    counts = repo.counts()
    second = ArtifactStore(repo).publish_run_result(unit.run_id, value)
    assert first == second and repo.counts() == counts
    assert first.artifact.revision.body.blocks[0].markdown.startswith("恢复后的第一段")
    with repo.engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count())
                .select_from(s.operations)
                .where(s.operations.c.kind == "task_range_patch")
            ).scalar_one()
            == 1
        )
    repo.close()


def test_quoted_target_is_deduplicated_and_oversized_extra_context_is_never_dropped(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    artifact, _, _ = long_target(repo, course, source)
    quote = SourceStore(repo).capture_artifact(
        course, artifact.artifact_id, artifact.head_revision_id, "paragraph_1"
    )
    target = {
        "mode": "edit",
        "artifact_id": artifact.artifact_id,
        "expected_revision_id": artifact.head_revision_id,
        "block_ids": ["paragraph_1"],
    }
    quoted = refresh(
        repo,
        course,
        admit(
            repo,
            course,
            [{"kind": "selection", "snapshot_id": quote.snapshot_id}],
            operation="quoted_edit",
            target=target,
        ).task_id,
    )
    assert quoted.state == "queued" and len(quoted.units) > 1
    assert len(TaskCoverage(repo).get(course, quoted.task_id).sources) == 1
    extra = (
        ArtifactStore(repo)
        .create(course, note(operation_id="extra_material", text="其他已授权内容。" * 4000))
        .artifact
    )
    extra_source = SourceStore(repo).capture_artifact(
        course, extra.artifact_id, extra.head_revision_id, "paragraph_1"
    )
    blocked = refresh(
        repo,
        course,
        admit(
            repo,
            course,
            [{"kind": "selection", "snapshot_id": extra_source.snapshot_id}],
            operation="large_context",
            target=target,
        ).task_id,
    )
    assert blocked.state == "failed" and not blocked.units
    assert (
        TaskCoverage(repo).get(course, blocked.task_id).target.last_receipt["error_code"]
        == "TASK_EDIT_CONTEXT_LIMIT"
    )
    assert ArtifactStore(repo).get(course, artifact.artifact_id) == artifact
    repo.close()
