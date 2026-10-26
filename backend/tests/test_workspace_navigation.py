"""Workspace preferences and system-root protection using synthetic assets."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.course_files import CourseDetachInput, CourseFileStore, CourseRenameFolderInput
from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.workspace_navigation import read_workspace_pins
from learning_app.workspaces import UNCLASSIFIED, ensure_unclassified


def test_workspace_hover_metadata_uses_registered_path_and_creation_time(tmp_path):
    app = create_app(AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run"))
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.get("/api/v1/session")
        repository = app.state.repository
        old = repository.create_course("尚未绑定")
        root = tmp_path / "课程"
        root.mkdir()
        store = CourseFileStore(repository)
        bound = store.bind(None, str(root))
        rows = {row["course_id"]: row for row in client.get("/api/v1/courses").json()}
        assert rows[old["course_id"]]["created_at"] == old["created_at"]
        assert rows[old["course_id"]]["root_path"] is None
        assert rows[bound["course_id"]]["root_path"] == str(root.resolve())
        store.rename_course(bound["course_id"], CourseRenameFolderInput(name="课程别名", operation_id=uuid.uuid4().hex))
        renamed = next(row for row in client.get("/api/v1/courses").json() if row["course_id"] == bound["course_id"])
        assert renamed == {**rows[bound["course_id"]], "title": "课程别名"}


def test_pins_persist_idempotently_without_changing_courses_or_chats(tmp_path):
    config = AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run")
    app = create_app(config)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        repository = app.state.repository
        system = ensure_unclassified(repository)
        course = repository.create_course("高数")
        chat = repository.create_chat(course["course_id"])
        before = repository.get_chat(chat["chat_id"])
        headers = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        ids = [course["course_id"], system]
        for course_id in ids:
            for _ in range(2):
                response = client.post(f"/api/v1/courses/{course_id}/pin", json={"pinned": True}, headers=headers)
                assert response.status_code == 200, response.text
        assert client.get("/api/v1/courses/navigation").json() == {"pinned_course_ids": ids}
        assert repository.get_chat(chat["chat_id"]) == before
        assert [row["course_id"] for row in repository.list_courses()] == [system, course["course_id"]]
        assert client.post(f"/api/v1/courses/{system}/pin", json={"pinned": False}).status_code == 403
        assert client.post("/api/v1/courses/missing/pin", json={"pinned": True}, headers=headers).status_code == 404
        assert client.post(f"/api/v1/courses/{system}/pin", json={"pinned": "false"}, headers=headers).status_code == 422
    with TestClient(create_app(config), base_url="http://127.0.0.1:8765") as client:
        headers = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        assert client.get("/api/v1/courses/navigation").json() == {"pinned_course_ids": ids}
        assert client.post(f"/api/v1/courses/{ids[0]}/pin", json={"pinned": False}, headers=headers).json() == {"pinned_course_ids": [system]}


def test_unclassified_cannot_be_renamed_or_detached_but_regular_workspace_can(tmp_path):
    repository = Repository(tmp_path / "data")
    try:
        ensure_unclassified(repository)
        store = CourseFileStore(repository)
        root = tmp_path / "普通工作区"
        root.mkdir()
        source = root / "原文.txt"
        source.write_text("保留原文", encoding="utf-8")
        course = store.bind(None, str(root))["course_id"]
        opened = store.open(course, source.name)
        chat = repository.create_chat(course)
        before = repository.get_chat(chat["chat_id"])
        for method, value in [
            (store.rename_course, CourseRenameFolderInput(name="其他", operation_id=uuid.uuid4().hex)),
            (store.detach, CourseDetachInput(confirm=True, operation_id=uuid.uuid4().hex)),
        ]:
            with pytest.raises(AppError) as error:
                method(UNCLASSIFIED, value)
            assert error.value.code == "WORKSPACE_SYSTEM_PROTECTED"
        for identity in (None, UNCLASSIFIED):
            with pytest.raises(AppError) as error:
                store.bind(identity, store.folder(UNCLASSIFIED)["root_path"], "绕过显示名")
            assert error.value.code == "WORKSPACE_SYSTEM_PROTECTED"
        store.rename_course(course, CourseRenameFolderInput(name="显示名", operation_id=uuid.uuid4().hex))
        assert root.is_dir() and store.folder(course)["title"] == "显示名"
        operation = CourseDetachInput(confirm=True, operation_id=uuid.uuid4().hex)
        assert store.detach(course, operation)["state"] == "completed"
        assert store.detach(course, operation)["state"] == "completed"
        assert course not in {row["course_id"] for row in repository.list_courses()}
        assert UNCLASSIFIED in {row["course_id"] for row in repository.list_courses()}
        assert repository.get_chat(chat["chat_id"]) == before
        assert source.read_text("utf-8") == "保留原文"
        assert repository.resolve_source(opened["asset_id"], opened["source_version"])["status"] == "available"
    finally:
        repository.close()


def test_invalid_pin_storage_is_preserved(tmp_path):
    path = tmp_path / "workspaces.json"
    raw = json.dumps({"pinned_course_ids": ["course_a", "course_a"]})
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(AppError) as error:
        read_workspace_pins(path)
    assert error.value.code == "WORKSPACE_PINS_INVALID"
    assert path.read_text("utf-8") == raw
