"""Upgrade an actual P0 database without replacing its messages or attachments."""

import base64
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from learning_app.storage import Repository
from learning_app.storage import repository as storage


def p0_database(root: Path):
    root.mkdir()
    engine = create_engine(f"sqlite:///{(root / 'business.sqlite3').as_posix()}")
    config = Config()
    config.set_main_option("script_location", str(Path(storage.__file__).parent / "migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "0001_business")
    engine.dispose()
    with patch.object(storage, "SCHEMA_REVISION", "0001_business"):
        repository = Repository(root)
    repository.create_course("已有课程", course_id="course_preserved")
    repository.create_chat("course_preserved", chat_id="chat_preserved")
    fixture = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts/history-example.json").read_text("utf-8")
    )["confirmed_attachment"]
    attachment = repository.stage_attachment(
        base64.b64decode(fixture["fixture_base64"]), "image/png", confirmed=True
    )
    repository.publish_submission(
        "chat_preserved", "保留原题与用户更正", "submission_preserved",
        attachment_ids=[attachment["attachment_id"]], corrections=["分母应为2x³"],
    )
    before = repository.get_chat("chat_preserved")
    repository.close()
    return before, attachment


@pytest.mark.parametrize("fail_before_commit", [False, True])
def test_p0_upgrade_preserves_assets_and_recovery(tmp_path, fail_before_commit):
    root = tmp_path / "p0"
    before, attachment = p0_database(root)

    def hook(point, _details):
        if fail_before_commit and point == "migration_before_commit":
            raise RuntimeError("CONTROLLED_MVP_MIGRATION_FAILURE")

    if fail_before_commit:
        with pytest.raises(RuntimeError, match="CONTROLLED_MVP_MIGRATION_FAILURE"):
            Repository(root, fault_hook=hook)
        with sqlite3.connect(root / "business.sqlite3") as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
                "0001_business"
            )
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 2

    repository = Repository(root)
    try:
        assert repository.runtime_info()["schema_revision"] == storage.SCHEMA_REVISION
        assert repository.get_chat("chat_preserved") == before
        assert repository.resolve_attachment(attachment["attachment_id"])["content_hash"] == (
            attachment["content_hash"]
        )
        backups = list((root / "migration-recovery").glob("*.sqlite3"))
        assert backups
        with sqlite3.connect(backups[0]) as connection:
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
                "0001_business"
            )
            assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        repository.close()
