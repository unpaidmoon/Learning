"""The changed workspace, copy, source and disabled-preparation boundaries."""

import pytest
from test_desktop import app_at

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.workbench.on_demand_sources import current_selection, search
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.processing_models import PreparationCreate
from learning_app.workbench.requested_learning import literal_request
from learning_app.workspaces import UNCLASSIFIED, explicit_subtitles, set_workspaces


async def test_independent_chat_workspace_membership_keeps_save_owner_and_history(tmp_path):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        gateway, repo = app.state.desktop_gateway, app.state.repository
        independent = await gateway.create_chat(None, None)
        assert independent["course_id"] == UNCLASSIFIED
        assert independent["workspace_ids"] == []
        assert CourseFileStore(repo).folder(UNCLASSIFIED)["root_path"]
        repo.create_course("数学", "math")
        repo.create_course("物理", "physics")
        first = await gateway.create_chat("math", None, independent["session_id"])
        assert first["workspace_ids"] == ["math"]
        set_workspaces(repo, first["chat_id"], ["math", "physics"])
        again = gateway.mapping(first["session_id"])
        assert again["workspace_ids"] == ["math", "physics"]
        assert again["course_id"] == "math"
        repo.publish_submission(first["chat_id"], "继续讨论", "first")
        assert (await gateway.open_chat(first["chat_id"]))["workspace_ids"] == ["math", "physics"]
        with pytest.raises(AppError, match="工作区不可用"):
            set_workspaces(repo, first["chat_id"], ["unknown"])
        assert gateway.mapping(first["session_id"])["workspace_ids"] == ["math", "physics"]
        set_workspaces(repo, first["chat_id"], [])
        assert gateway.mapping(first["session_id"])["workspace_ids"] == []
        assert not app.state.service.execution.starts


async def test_no_automatic_preparation_and_requested_text_read_without_index(tmp_path):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        gateway, repo = app.state.desktop_gateway, app.state.repository
        opened = await gateway.create_chat(None, None)
        files = CourseFileStore(repo)
        root = files._root(UNCLASSIFIED)
        (root / "教材.md").write_text("导数描述函数变化率。", encoding="utf-8")
        file = files.open(UNCLASSIFIED, "教材.md")
        context = {
            "course_id": UNCLASSIFIED,
            "asset_id": file["asset_id"],
            "source_version": file["source_version"],
        }
        assert current_selection(repo, context, "你好") is None
        chosen = current_selection(repo, context, "解释这份资料")
        assert "导数" in chosen.text
        assert search(repo, UNCLASSIFIED, "变化率")[0]["snapshot"].text == chosen.text
        assert app.state.preparations.store.list(UNCLASSIFIED) == []
        assert not hasattr(app.state.preparations, "corpus")
        assert opened["workspace_ids"] == []
        with pytest.raises(AppError) as denied:
            PreparationStore(repo).define_pdf(
                UNCLASSIFIED,
                PreparationCreate(
                    kind="pdf_ocr", asset_id=file["asset_id"], source_version=file["source_version"]
                ),
                app.state.config.project_root,
            )
        assert denied.value.code == "OCR_REMOVED"


def test_cancelled_learning_flows_and_explicit_subtitles():
    for request in ("生成复习卡片", "制作练习题集", "生成学习路线", "生成交互图"):
        assert literal_request(request) is None
    assert literal_request("把这页整理成笔记")["kind"] == "note"
    assert explicit_subtitles("生成这个视频的字幕")
    assert explicit_subtitles("读取字幕解释这段内容")
    assert not explicit_subtitles("这个视频没有字幕，解释这道题")
    assert not explicit_subtitles("不要生成字幕")
    assert not explicit_subtitles("解释一下这个按钮“生成字幕”是什么意思")
    assert explicit_subtitles("请帮我生成当前视频的字幕")
    assert explicit_subtitles("/subtitles 当前视频")


def test_retired_mutations_keep_historical_contracts_but_cannot_execute(tmp_path):
    app = app_at(tmp_path)
    retired = [route for route in app.routes if getattr(route, "deprecated", False)
               and "/learning/" in getattr(route, "path", "")]
    assert retired
    for route in retired:
        with pytest.raises(AppError) as denied:
            route.dependant.call()
        assert denied.value.code == "LEARNING_FEATURE_REMOVED"


def test_copy_and_document_publication_preserve_originals_and_local_edits(tmp_path):
    from workbench_support import learning_course, note

    from learning_app.course_files import CourseImportInput
    from learning_app.workbench.artifact_files import save_artifact_file
    from learning_app.workbench.artifacts import ArtifactStore
    from learning_app.workbench.sources import SourceStore

    repo, course, root, _ = learning_course(tmp_path)
    try:
        outside = tmp_path / "外部资料.txt"
        outside.write_text("原件保留", encoding="utf-8")
        files = CourseFileStore(repo)
        first = CourseImportInput(paths=[str(outside)], operation_id="copy-one")
        copied = files.import_files(course, first)
        assert files.import_files(course, first) == copied
        files.import_files(course, CourseImportInput(paths=[str(outside)], operation_id="copy-two"))
        assert outside.read_text(encoding="utf-8") == "原件保留"
        assert len(list(root.glob("外部资料*.txt"))) == 2
        product = ArtifactStore(repo).create(course, note()).artifact
        saved = save_artifact_file(repo, product)
        again = save_artifact_file(repo, product)
        assert saved["asset_id"] == again["asset_id"]
        assert len(list(root.glob("课程笔记-*.md"))) == 1
        text, _ = SourceStore(repo).text(course, saved["asset_id"], saved["source_version"])
        capture = SourceStore(repo).capture_text(
            course, saved["asset_id"], saved["source_version"], 0, len(text)
        )
        assert capture.content_role == "user_note"
        path = root / saved["entry"]["path"]
        path.write_text("用户已经修改", encoding="utf-8")
        with pytest.raises(AppError) as conflict:
            save_artifact_file(repo, product)
        assert conflict.value.code == "GENERATED_FILE_CONFLICT"
        assert path.read_text(encoding="utf-8") == "用户已经修改"
    finally:
        repo.close()


def test_legacy_chat_upgrade_retains_history_and_default_workspace(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from workbench_support import learning_course

    from learning_app.storage import Repository
    from learning_app.workspaces import read_workspaces

    repo, course, _, _ = learning_course(tmp_path)
    repo.create_chat(course, "legacy")
    repo.publish_submission("legacy", "保留历史", "saved-message")
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).parents[1] / "src/learning_app/storage/migrations")
    )
    with repo.engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "0034_data_import_fences")
    repo.close()
    upgraded = Repository(tmp_path / "data")
    try:
        chat = upgraded.get_chat("legacy")
        assert chat["messages"][0]["text"] == "保留历史"
        with upgraded.engine.connect() as conn:
            assert read_workspaces(conn, chat) == [course]
        assert upgraded.runtime_info()["schema_revision"] == "0035_chat_workspaces"
    finally:
        upgraded.close()


async def test_current_document_and_workspace_context_do_not_leave_sent_draft(tmp_path):
    from learning_app.drafts import (
        DraftClearInput,
        DraftInput,
        clear_draft,
        message_selection_ids,
        save_draft,
    )

    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        gateway, repo = app.state.desktop_gateway, app.state.repository
        folder = tmp_path / "阅读资料"
        folder.mkdir()
        (folder / "原文.txt").write_text("当前明确要求读取的原文", encoding="utf-8")
        files = CourseFileStore(repo)
        course = files.bind(None, str(folder))["course_id"]
        file = files.open(course, "原文.txt")
        chat = await gateway.create_chat(None, None)
        set_workspaces(repo, chat["chat_id"], [course])
        draft = save_draft(
            repo, chat["chat_id"], DraftInput(expected_revision=0, text="解释这份资料")
        )
        await gateway.prompt(
            {
                "sessionId": chat["session_id"],
                "requestId": "current-doc",
                "content": [{"type": "text", "text": "解释这份资料"}],
                "current_document": {
                    "course_id": course,
                    "asset_id": file["asset_id"],
                    "source_version": file["source_version"],
                },
            }
        )
        cleared = clear_draft(
            repo,
            chat["chat_id"],
            DraftClearInput(expected_revision=draft["revision"], submission_id="current-doc"),
        )
        assert cleared["text"] == "" and cleared["source_snapshot_ids"] == []
        assert message_selection_ids(repo.get_chat(chat["chat_id"])["messages"][0]["conditions"])
        assert gateway.mapping(chat["session_id"])["workspace_ids"] == [course]
        generation = repo.get_submission("current-doc")["generation_id"]
        await app.state.service.tasks[generation]


async def test_independent_chat_confirmed_crop_keeps_actual_source_course(tmp_path):
    from test_desktop import png

    from learning_app.drafts import (
        AttachmentSourceInput,
        save_attachment_source,
        submission_source_locators,
    )

    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        gateway, repo = app.state.desktop_gateway, app.state.repository
        chat = await gateway.create_chat(None, None)
        repo.create_course("视频课程", "video-course")
        media = tmp_path / "metadata-only.mp4"
        media.write_bytes(b"synthetic metadata only; must never decode frames")
        source = repo.register_source("video-course", media)
        attachment = repo.stage_attachment(png(), "image/png", confirmed=True)
        save_attachment_source(
            repo,
            attachment["attachment_id"],
            AttachmentSourceInput(
                chat_id=chat["chat_id"],
                origin="video",
                asset_id=source["asset_id"],
                source_version=source["source_version"],
                source_course_id="video-course",
                time_ms=0,
                source_width=32,
                source_height=24,
                crop={"x": 0, "y": 0, "width": 32, "height": 24},
                edit_root_id="confirmed-only",
            ),
        )
        locators = submission_source_locators(repo, chat["chat_id"], [attachment["attachment_id"]])
        repo.publish_submission(
            chat["chat_id"],
            "解释这张图",
            "crop",
            attachment_ids=[attachment["attachment_id"]],
            source_locators=locators,
        )
        assert locators[0]["source_course_id"] == "video-course"
        assert gateway.mapping(chat["session_id"])["workspace_ids"] == []
        assert not app.state.service.execution.starts


@pytest.mark.parametrize("prompt_text", ["解释这个视频", "读取字幕解释这段内容", "生成当前视频的字幕"])
def test_subtitle_tool_requires_explicit_request_and_only_then_queues(tmp_path, monkeypatch, prompt_text):
    from test_native_business import call, prepare_chat
    from test_nearby_subtitles import cached_track
    from test_question_context import video_course

    from learning_app.execution.native_tools import ToolRejected
    from learning_app.workbench.native_business import NativeBusiness

    repo, course, _, source, selected = video_course(tmp_path)
    repo.create_chat(course, "native_chat")
    try:
        _, binding = prepare_chat(repo, course, selected, "subtitle-intent", text=prompt_text,
            source_locators=[{"origin": "video_question", "asset_id": source["asset_id"],
                              "source_version": source["source_version"], "time_ms": 75000}])
        port = NativeBusiness(repo)
        scope = port.ensure_chat_scope(binding.generation_id, binding)
        args = {"anchor_id": None}
        invocation = call(binding, "read_nearby_subtitles", args)
        if prompt_text.startswith("解释"):
            assert "read_nearby_subtitles" not in scope["allowed_tools"]
            with pytest.raises(ToolRejected):
                port.invoke(invocation, args)
            assert PreparationStore(repo).list(course) == []
        elif prompt_text.startswith("读取"):
            with repo.engine.connect() as conn:
                anchor = port._scope(conn, binding.generation_id)["grants"]["subtitle_anchors"][0]
            cached_track(repo, binding, anchor)
            result = port.invoke(invocation, args)
            assert result["status"] == "matched" and len(result["results"]) == 3
            assert PreparationStore(repo).list(course) == []
        else:
            def define(_self, origin, value, _root, *, explicit=False):
                assert explicit and origin == course and value.scope == "full"
                return {"payload": {"course_id": course, "asset_id": source["asset_id"],
                    "source_version": source["source_version"], "source_digest": source["content_hash"],
                    "kind": "asr", "scope": "full", "parameters": {"user_requested": True,
                    "engine": {"fixture": True}, "track_id": "explicit_subtitle_track", "scope_indexes": [0]}},
                    "priority_unit": None}
            monkeypatch.setattr(PreparationStore, "define_asr", define)
            result = port.invoke(invocation, args)
            assert result["scope"] == "explicit_current_video"
            jobs = PreparationStore(repo).list(course)
            assert len(jobs) == 1 and jobs[0].state == "queued"
            assert port.reconcile_effect(invocation) == result
    finally:
        repo.close()
