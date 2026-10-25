"""Explicit whole-source origins keep task/results owned by the original course."""

import wave
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
from test_cross_course_sources import courses
from test_workbench_reading import generate
from workbench_support import note, settings

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.storage import repository as storage
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.corpus import CourseCorpus
from learning_app.workbench.models import (
    DocumentGapRef,
    TaskAction,
    TaskCreate,
    TaskGapDecision,
    TaskUnitInput,
)
from learning_app.workbench.pdf_worker import PARSER_VERSION
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.source_grants import declare_snapshot, snapshot_grants
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_controls import TaskRecovery
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import SourceGapDecision, TaskAdmission
from learning_app.workbench.tasks import TaskStore


def test_normalized_local_origin_still_rejects_overlapping_document_ranges(tmp_path):
    repo, owner, _ = courses(tmp_path)
    request = declaration(repo, owner, "definition.md")
    default_origin = {k: v for k, v in request.items() if k != "source_course_id"}
    with pytest.raises(AppError) as rejected:
        admit(repo, owner, [{**default_origin, "last_page": 0}, request])
    assert rejected.value.code == "SOURCE_RANGE_OVERLAP"
    assert TaskStore(repo).list(owner) == []
    repo.close()


def test_legacy_foreign_gap_retry_grants_only_the_new_same_page_snapshot(tmp_path):
    repo, owner, origin = courses(tmp_path)
    generate(tmp_path / "second-course/chapter.pdf", page_count=3)
    request = declaration(repo, origin, "chapter.pdf")
    source = repo.resolve_source(request["asset_id"], request["source_version"])
    gap = SourceStore(repo)._save_verified(
        origin,
        DocumentGapRef(
            asset_id=request["asset_id"],
            source_version=request["source_version"],
            source_digest=source["content_hash"],
            page_index=1,
            error_code="PDF_PROCESSING_TIMEOUT",
            parser_version=PARSER_VERSION,
        ),
        "",
        "original",
    )
    with repo.engine.begin() as conn:
        declare_snapshot(conn, owner, gap.snapshot_id, authority={"kind": "user_selection"})
    task = TaskStore(repo).create(
        owner,
        TaskCreate(
            operation_id="legacy_foreign_gap",
            kind="handout",
            instruction="仅重试已指定页",
            settings=settings(),
            coverage_mode="selection",
            units=[
                TaskUnitInput(
                    unit_id="original_page_unit",
                    label="B第二页",
                    source_snapshot_id=gap.snapshot_id,
                )
            ],
        ),
    )
    assert task.state == "needs_sources"
    choice = TaskGapDecision(
        operation_id="explicit_legacy_retry",
        expected_revision=task.revision,
        decision="retry",
        unit_ids=["original_page_unit"],
    )
    current = TaskStore(repo).decide_gap(owner, task.task_id, choice)
    refreshed = SourceStore(repo).snapshot(owner, current.units[0].source_snapshot_id)
    assert (
        current.units[0].unit_id == "original_page_unit"
        and refreshed.snapshot_id != gap.snapshot_id
    )
    assert refreshed.course_id == origin and refreshed.reference.page_index == 1
    assert (
        "page 2" in refreshed.text and SourceStore(repo).snapshot(owner, gap.snapshot_id).text == ""
    )
    assert TaskStore(repo).decide_gap(owner, task.task_id, choice) == current
    repo.close()


def test_changed_foreign_original_is_a_gap_without_grant_or_new_version(tmp_path):
    repo, owner, origin = courses(tmp_path)
    task = admit(repo, owner, [declaration(repo, origin, "definition.md")])
    (tmp_path / "second-course/definition.md").write_text("REPLACEMENT_MUST_NOT_BE_READ", "utf-8")
    TaskCoverage(repo).refresh(owner, task.task_id)
    source = TaskCoverage(repo).get(owner, task.task_id).sources[0]
    assert source.state == "blocked" and source.error_code == "SOURCE_VERSION_UNAVAILABLE"
    with repo.engine.connect() as conn:
        assert conn.execute(select(snapshot_grants.c.snapshot_id)).all() == []
    assert not TaskCoverage(repo).get(owner, task.task_id).target.materialized
    repo.close()


def test_paused_owner_task_does_not_publish_a_late_foreign_grant(tmp_path, monkeypatch):
    repo, owner, origin = courses(tmp_path)
    task = admit(repo, owner, [declaration(repo, origin, "definition.md")])
    original = SourceStore.capture_text

    def pause_after_local_capture(store, course_id, *args, **kwargs):
        snapshot = original(store, course_id, *args, **kwargs)
        current = TaskStore(repo).get(owner, task.task_id)
        TaskStore(repo).control(
            owner,
            task.task_id,
            "pause",
            TaskAction(
                operation_id="pause_before_foreign_grant",
                expected_revision=current.revision,
            ),
        )
        return snapshot

    monkeypatch.setattr(SourceStore, "capture_text", pause_after_local_capture)
    TaskCoverage(repo).refresh(owner, task.task_id)
    assert TaskStore(repo).get(owner, task.task_id).state == "paused"
    with repo.engine.connect() as conn:
        assert conn.execute(select(snapshot_grants.c.snapshot_id)).all() == []
    assert TaskCoverage(repo).get(owner, task.task_id).leaves[0].state == "pending"
    assert not TaskPlanner(repo).plan(owner, task.task_id)
    repo.close()


def admit(repo, owner, sources, *, operation="foreign_whole_task", kind="handout", **extra):
    return TaskCoverage(repo).admit(
        owner,
        TaskAdmission(
            operation_id=operation,
            kind=kind,
            instruction="完整整理明确资料，保留实际来源。",
            coverage_mode="complete",
            sources=sources,
            sources_only=True,
            settings=settings(),
            target={"mode": "create", "title": "跨课整理"},
            **extra,
        ),
    )


def finish_local(repo, owner, task):
    for _ in range(8):
        TaskCoverage(repo).refresh(owner, task.task_id)
        view = TaskCoverage(repo).get(owner, task.task_id)
        if view.leaves and all(leaf.state in {"ready", "empty"} for leaf in view.leaves):
            return view
    raise AssertionError(view.model_dump())


def declaration(repo, origin, path):
    opened = CourseFileStore(repo).open(origin, path)
    return {
        "kind": "document",
        "source_course_id": origin,
        "asset_id": opened["asset_id"],
        "source_version": opened["source_version"],
        "first_page": 0,
    }


def test_explicit_whole_text_is_origin_b_but_task_and_exact_grants_are_owner_a(tmp_path):
    repo, owner, origin = courses(tmp_path)
    task = admit(repo, owner, [declaration(repo, origin, "definition.md")])
    view = finish_local(repo, owner, task)
    assert task.course_id == owner and len(view.leaves) == 1
    snapshots = [
        SourceStore(repo).snapshot(owner, identity)
        for leaf in view.leaves
        for identity in leaf.snapshot_ids
    ]
    assert snapshots and all(item.course_id == origin for item in snapshots)
    assert all(item.content_role == "original" for item in snapshots)
    assert "SECOND_COURSE_SENTINEL" in snapshots[0].text
    assert view.leaves[0].locator["source_course_id"] == origin
    assert "对照课程" in view.leaves[0].label
    assert TaskPlanner(repo).plan(owner, task.task_id)
    planned = TaskStore(repo).get(owner, task.task_id)
    _, inputs = TaskPlanner(repo).inputs(owner, task.task_id, planned.units[0].unit_id)
    assert inputs and all(item.course_id == origin for _, item in inputs)
    assert ArtifactStore(repo).list(origin) == []
    assert repo.counts()["outbound_attempts"] == 0
    repo.close()


def test_foreign_course_freezes_only_its_admitted_members_and_preserves_generated_roles(tmp_path):
    repo, owner, origin = courses(tmp_path)
    root = tmp_path / "second-course"
    (root / "included.txt").write_text("Included at admission.", "utf-8")
    source = CourseCorpus(repo).search(origin, "共同概念")[0]["snapshot"]
    artifact = ArtifactStore(repo).create(origin, note(snapshot_id=source.snapshot_id)).artifact
    task = admit(
        repo, owner, [{"kind": "course", "source_course_id": origin, "include_generated": True}]
    )
    (root / "late.txt").write_text("LATE_MEMBER_MUST_NOT_ENTER", "utf-8")
    saved_root = repo.root
    repo.close()
    repo = storage.Repository(saved_root)
    view = finish_local(repo, owner, task)
    snapshots = [
        SourceStore(repo).snapshot(owner, identity)
        for leaf in view.leaves
        for identity in leaf.snapshot_ids
    ]
    assert len(snapshots) == 3
    assert all(item.course_id == origin for item in snapshots)
    assert any(
        item.reference.kind == "artifact" and item.content_role == artifact.content_role
        for item in snapshots
    )
    assert "LATE_MEMBER_MUST_NOT_ENTER" not in str(snapshots)
    assert "FIRST_COURSE_SENTINEL" not in str(snapshots)
    assert ArtifactStore(repo).list(owner) == []
    repo.close()


def test_foreign_document_requires_explicit_origin_and_cannot_lie_about_it(tmp_path):
    repo, owner, origin = courses(tmp_path)
    request = declaration(repo, origin, "definition.md")
    omitted = {k: v for k, v in request.items() if k != "source_course_id"}
    task = admit(repo, owner, [omitted])
    TaskCoverage(repo).refresh(owner, task.task_id)
    view = TaskCoverage(repo).get(owner, task.task_id)
    assert view.sources[0].state == "blocked"
    assert view.sources[0].error_code == "SOURCE_COURSE_DENIED"
    third = repo.create_course("无关联课程")["course_id"]
    with pytest.raises(AppError, match="明确指定"):
        admit(repo, owner, [{**request, "source_course_id": third}], operation="false_origin")
    with pytest.raises(AppError) as missing:
        admit(
            repo,
            owner,
            [{"kind": "course", "source_course_id": "not_existing"}],
            operation="missing_origin",
        )
    assert missing.value.code == "TASK_SOURCE_COURSE_UNAVAILABLE"
    repo.close()


def test_foreign_pdf_only_declared_page_supplies_text_and_original_page_image(tmp_path):
    repo, owner, origin = courses(tmp_path)
    generate(tmp_path / "second-course/chapter.pdf", page_count=3)
    request = {**declaration(repo, origin, "chapter.pdf"), "first_page": 1, "last_page": 1}
    task = admit(repo, owner, [request])
    view = finish_local(repo, owner, task)
    assert len(view.leaves) == 1 and view.leaves[0].locator["page_index"] == 1
    snapshots = [
        SourceStore(repo).snapshot(owner, identity) for identity in view.leaves[0].snapshot_ids
    ]
    assert {item.reference.kind for item in snapshots} == {"document", "document_image"}
    assert all(item.course_id == origin and item.reference.page_index == 1 for item in snapshots)
    assert TaskPlanner(repo).plan(owner, task.task_id)
    repo.close()


def test_foreign_transcript_preparation_and_retry_use_origin_b_without_queue_widening(tmp_path):
    repo, owner, origin = courses(tmp_path)
    path = tmp_path / "second-course/finite.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 16000)
    opened = CourseFileStore(repo).open(origin, path.name)
    task = admit(
        repo,
        owner,
        [
            {
                "kind": "transcript",
                "source_course_id": origin,
                "asset_id": opened["asset_id"],
                "source_version": opened["source_version"],
            }
        ],
    )
    coverage = TaskCoverage(repo)
    coverage.refresh(owner, task.task_id)
    source = coverage.get(owner, task.task_id).sources[0]
    assert source.preparation_job_id
    with repo.engine.begin() as conn:
        assert conn.execute(select(s.preparations.c.course_id)).scalars().all() == [origin]
        conn.execute(
            update(s.preparations)
            .where(s.preparations.c.job_id == source.preparation_job_id)
            .values(state="failed")
        )
    coverage.refresh(owner, task.task_id)
    source = coverage.get(owner, task.task_id).sources[0]
    assert source.state == "blocked" and source.error_code == "TASK_PREPARATION_FAILED"
    current = TaskStore(repo).get(owner, task.task_id)
    TaskRecovery(repo).source_gap(
        owner,
        task.task_id,
        SourceGapDecision(
            operation_id="retry_exact_foreign_preparation",
            expected_revision=current.revision,
            decision="retry",
            source_ids=[source.source_id],
        ),
    )
    coverage.refresh(owner, task.task_id)
    assert PreparationStore(repo).get(origin, source.preparation_job_id).state == "queued"
    assert repo.counts()["outbound_attempts"] == 0
    repo.close()


def test_unprepared_foreign_task_prevents_unsafe_downgrade_and_default_hash_stays_compatible(
    tmp_path,
):
    repo, owner, origin = courses(tmp_path)
    request = declaration(repo, owner, "definition.md")
    task = admit(repo, owner, [request], operation="same_local_payload")
    without_field = {k: v for k, v in request.items() if k != "source_course_id"}
    assert admit(repo, owner, [without_field], operation="same_local_payload") == task
    foreign = admit(repo, owner, [declaration(repo, origin, "definition.md")])
    assert TaskCoverage(repo).get(owner, foreign.task_id).leaves == []
    config = Config()
    config.set_main_option("script_location", str(Path(storage.__file__).parent / "migrations"))
    with pytest.raises(RuntimeError, match="TASK_SOURCE_COURSE_DOWNGRADE_DENIED"):
        with repo.engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, "0030_cross_course_sources")
    assert repo.runtime_info()["schema_revision"] == storage.SCHEMA_REVISION
    repo.close()
