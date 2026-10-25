"""Whole-data cuts, immutable file closure and denied control/schema surprises."""

import hashlib
import io
import sqlite3
from contextlib import closing

import pytest
from PIL import Image
from sqlalchemy import update
from test_document_regions import document_parent
from workbench_support import learning_course

from learning_app.data_snapshot import BusinessSnapshot
from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.storage import schema as s
from learning_app.storage.repository import SCHEMA_REVISION
from learning_app.workbench.document_regions import DocumentRegions
from learning_app.workbench.pdf_worker import LEGACY_REGION_VERSION
from learning_app.workbench.sources import SourceStore


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (12, 8), "#1245ab").save(stream, "PNG")
    return stream.getvalue()


def test_cut_includes_all_registered_objects_and_keeps_later_writes_outside_cut(tmp_path):
    repo = Repository(tmp_path / "data")
    course = repo.create_course("course")["course_id"]
    chat = repo.create_chat(course, "chat")["chat_id"]
    raw = image_bytes()
    first = repo.stage_attachment(raw, attachment_id="draft_not_submitted")
    repo.stage_attachment(raw, attachment_id="same_bytes_other_identity")
    repo.publish_submission(chat, '用户文本里的 {"authorization":"example"} 原样保留', "pending")
    private = repo.root / "execution/private-secret.env"
    private.parent.mkdir()
    private.write_text("PRIVATE_RUNTIME_SENTINEL", "utf-8")

    def after_cut(name, detail):
        if name == "data_snapshot_after_cut":
            repo.create_chat(course, "later_chat")

    repo.fault_hook = after_cut
    with BusinessSnapshot(repo).capture() as cut:
        assert cut.schema_revision == SCHEMA_REVISION
        assert {x["path"] for x in cut.files} == {
            "business.sqlite3",
            f"attachments/objects/{first['content_hash'][:2]}/{first['content_hash']}",
        }
        assert (
            len(cut.unresolved_executions) == 1
            and cut.unresolved_executions[0]["state"] == "pending"
        )
        with closing(sqlite3.connect(cut.database)) as connection:
            assert connection.execute("SELECT count(*) FROM attachments").fetchone()[0] == 2
            assert connection.execute("SELECT count(*) FROM chats").fetchone()[0] == 1
            assert (
                '"authorization"'
                in connection.execute("SELECT text FROM messages WHERE role='user'").fetchone()[0]
            )
        for row in cut.files:
            path = cut.directory / row["path"]
            assert (
                path.stat().st_size == row["bytes"]
                and hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
            )
        assert not (cut.directory / "execution").exists()
    assert private.read_text("utf-8") == "PRIVATE_RUNTIME_SENTINEL"
    assert not list(repo.staging.iterdir())
    repo.close()


def test_old_document_region_cache_is_copied_without_rendering_or_upgrading(tmp_path, monkeypatch):
    repo, course, root, _ = learning_course(tmp_path)
    _, parent = document_parent(repo, course, root)
    regions = DocumentRegions(repo)
    source = SourceStore(repo).capture_document_region(
        course, regions._candidates(course, parent.snapshot_id, LEGACY_REGION_VERSION)[0]
    )
    cache = SourceStore(repo).image_path(source.reference.image_sha256)
    expected = cache.read_bytes()
    monkeypatch.setattr(
        DocumentRegions, "render", lambda *_args: pytest.fail("export must not render an old crop")
    )
    with BusinessSnapshot(repo).capture() as cut:
        name = f"learning-source-images/{source.reference.image_sha256}.png"
        assert (cut.directory / name).read_bytes() == expected
    assert SourceStore(repo).snapshot(course, source.snapshot_id) == source
    repo.close()


@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_corrupt_registered_file_fails_without_repairing_user_asset(tmp_path, missing):
    repo = Repository(tmp_path / "data")
    saved = repo.stage_attachment(image_bytes())
    path = repo._object_path(saved["content_hash"])
    if missing:
        path.unlink()
    else:
        path.write_bytes(b"owned corruption fixture")
    with pytest.raises(AppError) as error:
        with BusinessSnapshot(repo).capture():
            pytest.fail("incomplete file closure must not be delivered")
    assert error.value.code == ("DATA_ASSET_UNAVAILABLE" if missing else "DATA_ASSET_INTEGRITY")
    assert (not path.exists()) if missing else path.read_bytes() == b"owned corruption fixture"
    assert not list(repo.staging.iterdir())
    repo.close()


@pytest.mark.parametrize("surprise", ["table", "control"])
def test_unknown_schema_or_control_keys_are_rejected_without_silent_redaction(tmp_path, surprise):
    repo = Repository(tmp_path / "data")
    course = repo.create_course("course")["course_id"]
    chat = repo.create_chat(course, "chat")["chat_id"]
    repo.publish_submission(chat, "ordinary text", "pending")
    if surprise == "table":
        with repo.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE unexpected_control(secret TEXT)")
    else:
        with repo.engine.begin() as connection:
            connection.execute(
                update(s.generations).values(
                    adapter_metadata={"authorization": "PRIVATE_BEARER_SENTINEL"}
                )
            )
    with pytest.raises(AppError) as error:
        with BusinessSnapshot(repo).capture():
            pytest.fail("unexpected control state cannot be exported")
    assert error.value.code == (
        "DATA_SCHEMA_UNEXPECTED" if surprise == "table" else "DATA_CONTROL_METADATA_UNSUPPORTED"
    )
    assert "PRIVATE_BEARER_SENTINEL" not in str(error.value)
    assert repo.get_chat(chat)["messages"]
    repo.close()
