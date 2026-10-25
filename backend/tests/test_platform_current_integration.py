"""Current workspace migration, explicit preparation and destination contract."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from test_integrations import imported, plan, setup  # noqa: F401
from test_mvp_migration import p0_database

from learning_app.course_files import CourseDirectoryInput, CourseFileStore
from learning_app.integrations.models import ImportSelection
from learning_app.storage import Repository
from learning_app.storage import repository as storage
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.question_context import freeze_subtitle_anchors


def test_import_targets_browsed_folder_without_preparing_subtitles(setup):  # noqa: F811
    repo, host = setup
    files = CourseFileStore(repo)
    files.directory("a", CourseDirectoryInput(operation_id="destination", path="", name="第一章"))
    value = plan(host)
    selection = ImportSelection(operation_id="current-folder-import", plan_id=value.plan_id,
        object_ids=[value.parts[0].object_id], destination_path="第一章")
    receipt = host.import_selected("a", selection)
    assert receipt.items[0].path.startswith("第一章/")
    assert host.import_selected("a", selection) == receipt
    files.open("a", receipt.items[0].path)
    assert PreparationStore(repo).list("a") == []
    assert not any(call[0] == "subtitle" for call in host.provider.calls)
    job = host.queue("a", receipt.items[0].asset_id, receipt.items[0].source_version, "platform_subtitles")
    assert job.parameters["user_requested"] is True
    lease = PreparationStore(repo).claim()
    assert lease and lease["kind"] == "platform_subtitles"


def test_0035_database_upgrades_platform_tables_and_preserves_user_rows(tmp_path):
    root = tmp_path / "data"
    before, attachment = p0_database(root)
    config = Config()
    config.set_main_option("script_location", str(Path(storage.__file__).parent / "migrations"))
    engine = create_engine(f"sqlite:///{(root / 'business.sqlite3').as_posix()}")
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "0035_chat_workspaces")
    engine.dispose()
    repo = Repository(root)
    try:
        assert repo.get_chat("chat_preserved") == before
        assert repo.resolve_attachment(attachment["attachment_id"])["content_hash"] == attachment["content_hash"]
        with repo.engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0036_platform_sources"
            assert conn.execute(text("SELECT count(*) FROM integration_modules")).scalar_one() == 0
    finally:
        repo.close()


def test_selected_platform_video_can_anchor_an_independent_foreign_course_chat(setup):  # noqa: F811
    repo, host = setup
    item = imported(host)
    message = {"message_id": "confirmed-current-video", "attachment_ids": [],
        "source_locators": [{"origin": "video_question", "asset_id": item.asset_id,
            "source_version": item.source_version, "source_course_id": "a", "time_ms": 0}]}
    with repo.engine.connect() as conn:
        anchors = freeze_subtitle_anchors(conn, "b", {"messages": []}, message)
    assert len(anchors) == 1 and anchors[0]["source_course_id"] == "a"
