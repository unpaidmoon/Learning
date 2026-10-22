"""Real synthetic files, course identities and version/input boundaries."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.course_files import (
    CourseDeleteInput,
    CourseDetachInput,
    CourseDirectoryInput,
    CourseFileStore,
    CourseImportInput,
    CourseMoveInput,
    CourseRenameFolderInput,
    CourseRenameInput,
)
from learning_app.errors import AppError
from learning_app.storage import Repository


@pytest.fixture
def files(tmp_path):
    repository = Repository(tmp_path / "data")
    root = tmp_path / "数学课程"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    store = CourseFileStore(repository)
    course = store.bind(None, str(root))
    yield repository, store, course["course_id"], root, outside
    repository.close()


def test_folder_binding_names_and_preserves_legacy(files):
    repository, store, _, root, outside = files
    original = outside / "旧资料.txt"
    original.write_text("旧资料不搬运", encoding="utf-8")
    course = repository.create_course("旧课")
    source = repository.register_source(course["course_id"], original)
    other = root.parent / "新根目录"
    other.mkdir()
    bound = store.bind(course["course_id"], str(other))
    assert bound["title"] == "旧课"
    assert bound["root_status"] == "ready"
    assert bound["legacy_asset_count"] == 1
    assert original.read_text("utf-8") == "旧资料不搬运"
    assert not list(other.iterdir())
    assert (
        repository.resolve_source(source["asset_id"], source["source_version"])["status"]
        == "available"
    )


def test_all_files_list_without_reading_contents(files, monkeypatch):
    _, store, course, root, _ = files
    (root / "第10课.txt").write_text("10")
    (root / "第2课.txt").write_text("2")
    (root / "未支持.docx").write_bytes(b"synthetic office placeholder")
    (root / "子目录").mkdir()

    def no_content(*_args, **_kwargs):
        raise AssertionError("listing must not hash/read materials")

    monkeypatch.setattr("learning_app.course_files._digest", no_content)
    listed = store.list(course)
    assert [entry["name"] for entry in listed["entries"]] == [
        "子目录",
        "未支持.docx",
        "第2课.txt",
        "第10课.txt",
    ]
    assert all(entry["asset_id"] is None for entry in listed["entries"])


@pytest.mark.parametrize("mode", ["move", "copy"])
def test_import_default_move_copy_and_idempotency(files, mode):
    repository, store, course, root, outside = files
    original = outside / "题目.txt"
    original.write_text("用户主动选择的合成内容", encoding="utf-8")
    if mode == "move":
        with pytest.raises(ValidationError):
            CourseImportInput(paths=[str(original)], mode=mode, operation_id="import_once")
        assert original.exists() and not (root / original.name).exists()
        return
    request = CourseImportInput(paths=[str(original)], mode=mode, operation_id="import_once")
    result = store.import_files(course, request)
    assert result["state"] == "completed"
    assert original.exists() == (mode == "copy")
    assert (root / "题目.txt").read_text("utf-8") == "用户主动选择的合成内容"
    assert store.import_files(course, request) == result
    entry = result["entries"][0]
    assert (
        repository.resolve_source(entry["asset_id"], entry["source_version"])["status"]
        == "available"
    )
    with pytest.raises(AppError, match="同一操作身份"):
        store.import_files(
            course, request.model_copy(update={"destination": "different_directory"})
        )


@pytest.mark.parametrize("choice", ["ask", "keep_both", "skip", "replace"])
def test_import_conflicts_are_explicit_and_versions_preserved(files, choice):
    repository, store, course, root, outside = files
    target = root / "同名.txt"
    target.write_text("原来的内容", encoding="utf-8")
    old = store.open(course, "同名.txt")
    source = outside / "同名.txt"
    source.write_text("明确选择的新内容", encoding="utf-8")
    if choice != "keep_both":
        with pytest.raises(ValidationError):
            CourseImportInput(paths=[str(source)], conflict=choice, operation_id="conflict_choice")
        assert target.read_text("utf-8") == "原来的内容" and source.exists()
        return
    result = store.import_files(
        course,
        CourseImportInput(paths=[str(source)], conflict=choice, operation_id="conflict_choice"),
    )
    if choice == "ask":
        assert result["state"] == "conflict"
        assert result["conflicts"] == ["同名.txt"]
        assert source.exists()
        assert target.read_text("utf-8") == "原来的内容"
    elif choice == "skip":
        assert result["entries"] == [] and source.exists()
        assert target.read_text("utf-8") == "原来的内容"
    elif choice == "keep_both":
        assert (root / "同名 (2).txt").read_text("utf-8") == "明确选择的新内容"
        assert target.read_text("utf-8") == "原来的内容"
    else:
        assert target.read_text("utf-8") == "明确选择的新内容"
        assert (
            repository.resolve_source(old["asset_id"], old["source_version"])["status"]
            == "version_mismatch"
        )
        current = store.open(course, "同名.txt")
        assert current["asset_id"] == old["asset_id"]
        assert current["source_version"] != old["source_version"]


def test_move_previously_registered_external_preserves_source(files):
    repository, store, course, root, outside = files
    original = outside / "已有来源.txt"
    original.write_text("来源仍是同一份内容", encoding="utf-8")
    source = repository.register_source(course, original)
    imported = store.import_files(
        course, CourseImportInput(paths=[str(original)], operation_id="move_legacy")
    )["entries"][0]
    assert imported["asset_id"] != source["asset_id"]
    assert original.exists()
    resolved = repository.resolve_source(source["asset_id"], source["source_version"])
    assert resolved["status"] == "available"
    assert Path(resolved["local_path"]) == original


def test_rename_move_folder_and_course_preserve_identity(files):
    repository, store, course, root, _ = files
    (root / "题目.txt").write_text("合成题文", encoding="utf-8")
    opened = store.open(course, "题目.txt")
    rename = CourseRenameInput(path="题目.txt", name="更名.txt", operation_id="rename_once")
    renamed = store.rename(course, rename)
    assert store.rename(course, rename) == renamed
    store.directory(course, CourseDirectoryInput(name="章节", operation_id="mkdir"))
    move = CourseMoveInput(path="更名.txt", destination="章节", operation_id="move_once")
    moved = store.move(course, move)
    assert store.move(course, move) == moved
    assert moved["entry"]["asset_id"] == opened["asset_id"]
    assert moved["entry"]["source_version"] == opened["source_version"]
    renamed_course = store.rename_course(
        course, CourseRenameFolderInput(name="数学重命名", operation_id="rename_course")
    )
    new_root = Path(renamed_course["root"]["root_path"])
    assert new_root == root and root.exists()
    assert renamed_course["root"]["title"] == "数学重命名"
    source = repository.resolve_source(opened["asset_id"], opened["source_version"])
    assert source["status"] == "available"
    assert Path(source["local_path"]) == new_root / "章节" / "更名.txt"


def test_directory_rename_also_preserves_not_yet_opened_legacy_sources(files):
    repository, store, course, root, _ = files
    chapter = root / "旧章节"
    chapter.mkdir()
    original = chapter / "已有资料.txt"
    original.write_text("旧资产无需先打开再改名", encoding="utf-8")
    legacy = repository.register_source(course, original)
    store.rename(
        course, CourseRenameInput(path="旧章节", name="新章节", operation_id="rename_legacy_folder")
    )
    preserved = repository.resolve_source(legacy["asset_id"], legacy["source_version"])
    assert preserved["status"] == "available"
    assert Path(preserved["local_path"]) == root / "新章节" / original.name


def test_detach_keeps_chats_files_and_reattach_identity(files):
    repository, store, course, root, _ = files
    (root / "保留.txt").write_text("保留")
    chat = repository.create_chat(course)
    detached = store.detach(course, CourseDetachInput(confirm=True, operation_id="detach"))
    assert detached["root"]["root_status"] == "detached"
    assert all(item["course_id"] != course for item in repository.list_courses())
    assert repository.get_chat(chat["chat_id"])["chat_id"] == chat["chat_id"]
    assert (root / "保留.txt").is_file()
    assert store.bind(None, str(root))["course_id"] == course


@pytest.mark.skipif(os.name != "nt", reason="actual Windows Recycle Bin")
def test_delete_requires_confirmation_keeps_source_and_is_idempotent(files):
    repository, store, course, root, _ = files
    (root / "删除.txt").write_text("明确删除合成文件")
    old = store.open(course, "删除.txt")
    request = CourseDeleteInput(path="删除.txt", confirm=True, operation_id="delete_once")
    result = store.delete(course, request)
    assert store.delete(course, request) == result
    assert repository.resolve_source(old["asset_id"], old["source_version"])["status"] == "missing"
    assert not (root / "删除.txt").exists()
    directory = root / "非空"
    directory.mkdir()
    (directory / "保留.txt").write_text("保留")
    result = store.delete(course, CourseDeleteInput(path="非空", confirm=True))
    assert result["recycled"] is True and not directory.exists()


@pytest.mark.parametrize(
    "path", ["../outside", "a/../../outside", "C:/outside", "/outside", "\\\\server\\share"]
)
def test_escape_paths_rejected(files, path):
    _, store, course, _, _ = files
    with pytest.raises(AppError) as error:
        store.list(course, path)
    assert error.value.code == "FILE_PATH_ESCAPE"


def test_http_content_exact_version_range_and_capability(tmp_path):
    root = tmp_path / "合成课程"
    root.mkdir()
    (root / "题文.txt").write_bytes(b"synthetic source bytes")
    (root / "not-active.html").write_text("<script>never rendered inline</script>")
    config = AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run")
    with TestClient(create_app(config), base_url="http://127.0.0.1:8765") as client:
        assert client.post("/api/v1/courses/folder", json={"path": str(root)}).status_code == 403
        csrf = client.get("/api/v1/session").json()["csrf_token"]
        client.headers["X-Learning-CSRF"] = csrf
        course = client.post("/api/v1/courses/folder", json={"path": str(root)}).json()
        opened = client.post(
            f"/api/v1/courses/{course['course_id']}/files/open", json={"path": "题文.txt"}
        ).json()
        response = client.get(opened["content_url"], headers={"Range": "bytes=2-8"})
        assert response.status_code == 206 and response.content == b"nthetic"
        assert response.headers["content-type"].startswith("text/plain")
        assert "sandbox" in response.headers["content-security-policy"]
        assert client.head(opened["content_url"]).headers["content-length"] == "22"
        assert client.get(opened["content_url"], headers={"Range": "bytes=999-"}).status_code == 416
        unsupported = client.post(
            f"/api/v1/courses/{course['course_id']}/files/open", json={"path": "not-active.html"}
        ).json()
        assert unsupported["viewer"] == "external" and unsupported["content_url"] is None
        (root / "题文.txt").write_bytes(b"new bytes")
        rejected = client.get(opened["content_url"])
        assert rejected.status_code == 409 and rejected.json()["code"] == "SOURCE_VERSION_MISMATCH"
