"""Capability revision protects saved learning JSON; upgrades do not rewrite it."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from workbench_support import learning_course, note, settings

from learning_app.storage import Repository
from learning_app.storage import repository as storage
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.models import ArtifactCreate
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission


def downgrade(repo):
    config = Config()
    config.set_main_option("script_location", str(Path(storage.__file__).parent / "migrations"))
    with repo.engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0025_research_external_reads")


def test_legacy_assets_can_roundtrip_revision_without_content_or_identity_changes(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    artifact = ArtifactStore(repo).create(course, note(snapshot_id=source.snapshot_id)).artifact
    counts = repo.counts()
    downgrade(repo)
    with repo.engine.connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0025_research_external_reads"
        )
    repo.close()
    restored = Repository(tmp_path / "data")
    assert restored.runtime_info()["schema_revision"] == storage.SCHEMA_REVISION
    assert restored.counts() == counts
    assert ArtifactStore(restored).get(course, artifact.artifact_id) == artifact
    restored.close()


@pytest.mark.parametrize("feature", ["task", "paired_block"])
def test_downgrade_refuses_new_json_or_task_data_without_deleting_it(tmp_path, feature):
    repo, course, _, source = learning_course(tmp_path)
    if feature == "task":
        TaskCoverage(repo).admit(
            course,
            TaskAdmission(
                operation_id="new_note",
                kind="note",
                instruction="生成笔记",
                sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
                target={"mode": "create", "title": "笔记"},
                settings=settings(),
            ),
        )
    else:
        ArtifactStore(repo).create(
            course,
            ArtifactCreate(
                operation_id="paired",
                kind="translation",
                title="对照",
                body={
                    "blocks": [
                        {
                            "block_id": "p",
                            "kind": "translation",
                            "markdown": "手写译文",
                            "attribution": "user_authored",
                            "source_snapshot_ids": [source.snapshot_id],
                            "translation": {
                                "source_text": source.text,
                                "source_parts": [
                                    {
                                        "snapshot_id": source.snapshot_id,
                                        "start": 0,
                                        "end": len(source.text),
                                    }
                                ],
                            },
                        }
                    ]
                },
            ),
        )
    before = repo.counts()
    with pytest.raises(RuntimeError, match="LEARNING_TEXT_DOWNGRADE_DENIED"):
        downgrade(repo)
    with repo.engine.connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == storage.SCHEMA_REVISION
        )
    assert repo.counts() == before
    repo.close()
