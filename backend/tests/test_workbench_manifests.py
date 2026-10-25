import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from workbench_support import learning_course, note, settings

from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.manifests import ManifestStore
from learning_app.workbench.models import (
    ArtifactRangeInput,
    DocumentRangeInput,
    ManifestCreate,
    TaskCreate,
    TaskUnitInput,
)
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.tasks import TaskStore


def test_complete_scope_enumerates_entire_source_instead_of_selected_quote(tmp_path):
    repo, course_id, _, selection = learning_course(tmp_path)
    try:
        value = ManifestCreate(
            documents=[
                DocumentRangeInput(
                    asset_id=selection.reference.asset_id,
                    source_version=selection.reference.source_version,
                )
            ]
        )
        manifest = ManifestStore(repo).create(course_id, value)
        assert ManifestStore(repo).create(course_id, value) == manifest
        unit = manifest.units[0]
        full = SourceStore(repo).snapshot(course_id, unit.source_snapshot_id)
        assert full.text.startswith(selection.text) and "不应补发前后文" in full.text
        assert full.snapshot_id != selection.snapshot_id
        with pytest.raises(ValidationError):
            TaskCreate(
                operation_id="fake_full",
                kind="handout",
                instruction="完整归纳",
                settings=settings(),
                coverage_mode="complete",
                units=[
                    TaskUnitInput(
                        unit_id="quote",
                        label="one sentence",
                        source_snapshot_id=selection.snapshot_id,
                    )
                ],
            )
        task = TaskStore(repo).create(
            course_id,
            TaskCreate(
                operation_id="full",
                kind="handout",
                instruction="完整归纳指定全文",
                settings=settings(),
                coverage_mode="complete",
                scope_manifest_id=manifest.manifest_id,
            ),
        )
        assert task.coverage_mode == "complete" and task.scope_manifest_id == manifest.manifest_id
        assert task.units[0].source_snapshot_id == full.snapshot_id
        assert task.state == "queued"
    finally:
        repo.close()


def test_scope_role_and_revision_are_preserved_and_cross_course_denied(tmp_path):
    repo, course_id, root, selection = learning_course(tmp_path)
    try:
        artifact = ArtifactStore(repo).create(course_id, note()).artifact
        manifest = ManifestStore(repo).create(
            course_id,
            ManifestCreate(
                artifacts=[
                    ArtifactRangeInput(
                        artifact_id=artifact.artifact_id,
                        revision_id=artifact.head_revision_id,
                    )
                ]
            ),
        )
        assert (
            SourceStore(repo).snapshot(course_id, manifest.units[0].source_snapshot_id).content_role
            == "user_note"
        )
        other = repo.create_course("other")
        with pytest.raises(AppError):
            ManifestStore(repo).get(other["course_id"], manifest.manifest_id)
        source_manifest = ManifestStore(repo).create(
            course_id,
            ManifestCreate(
                documents=[
                    DocumentRangeInput(
                        asset_id=selection.reference.asset_id,
                        source_version=selection.reference.source_version,
                    )
                ]
            ),
        )
        (root / "lesson.txt").write_text("changed", encoding="utf-8")
        task = TaskStore(repo).create(
            course_id,
            TaskCreate(
                operation_id="changed_full_source",
                kind="handout",
                instruction="完整处理",
                settings=settings(),
                coverage_mode="complete",
                scope_manifest_id=source_manifest.manifest_id,
            ),
        )
        assert task.state == "needs_sources" and task.units[0].state == "missing"
        assert task.units[0].source_snapshot_id == source_manifest.units[0].source_snapshot_id
    finally:
        repo.close()


def test_add_manifest_migration_preserves_existing_task_parent_and_recovery_copy(tmp_path):
    repo, course_id, _, selection = learning_course(tmp_path)
    task = TaskStore(repo).create(
        course_id,
        TaskCreate(
            operation_id="existing_task",
            kind="handout",
            instruction="选区整理",
            settings=settings(),
            units=[
                TaskUnitInput(
                    unit_id="unit", label="selected", source_snapshot_id=selection.snapshot_id
                )
            ],
        ),
    )
    from learning_app.storage import repository as repository_module

    config = Config()
    config.set_main_option(
        "script_location",
        str(repository_module.Path(repository_module.__file__).parent / "migrations"),
    )
    with repo.engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "0008_learning_task_runs")
    root = repo.root
    repo.close()
    reopened = Repository(root)
    try:
        restored = TaskStore(reopened).get(course_id, task.task_id)
        assert restored.units[0].source_snapshot_id == selection.snapshot_id
        assert restored.coverage_mode == "selection"
        assert list(
            (root / "migration-recovery").glob(f"before-{repository_module.SCHEMA_REVISION}-*.sqlite3")
        )
    finally:
        reopened.close()
