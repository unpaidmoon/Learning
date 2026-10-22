"""Explicit folder aliases, copy-only intake and recoverable file operations."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.course_files import (
    CourseDeleteInput,
    CourseDetachInput,
    CourseFileStore,
    CourseImportInput,
    CourseRenameFolderInput,
    CourseTransferInput,
)
from learning_app.errors import AppError
from learning_app.storage import Repository


@pytest.fixture
def files(tmp_path):
    repository = Repository(tmp_path / "data")
    root = tmp_path / "真实目录名"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    store = CourseFileStore(repository)
    course = store.bind(None, str(root), "软件课程名")["course_id"]
    yield repository, store, course, root, outside
    repository.close()


def test_course_alias_never_renames_folder_and_missing_root_is_still_editable(files):
    _, store, course, root, _ = files
    assert store.folder(course)["title"] == "软件课程名"
    result = store.rename_course(
        course, CourseRenameFolderInput(name="独立名称", operation_id="alias")
    )
    assert result["root"]["root_path"] == str(root)
    assert result["root"]["title"] == "独立名称"
    assert root.exists() and root.name == "真实目录名"
    root.rename(root.with_name("外部改名"))
    result = store.rename_course(
        course, CourseRenameFolderInput(name="暂缺资料的课程", operation_id="missing")
    )
    assert result["root"]["root_status"] == "missing"
    assert result["root"]["title"] == "暂缺资料的课程"


def test_unbound_course_can_be_renamed_and_detached_without_losing_chat(files):
    repository, store, _, _, _ = files
    course = repository.create_course("旧课程")["course_id"]
    chat = repository.create_chat(course)
    store.rename_course(
        course, CourseRenameFolderInput(name="旧课程新名", operation_id="rename-old")
    )
    result = store.detach(course, CourseDetachInput(confirm=True, operation_id="detach-old"))
    assert result["root"]["root_status"] == "detached"
    assert result["root"]["root_path"] is None
    assert all(row["course_id"] != course for row in repository.list_courses())
    assert repository.get_chat(chat["chat_id"])["course_id"] == course


def test_external_copy_is_automatic_keep_both_and_idempotent(files):
    _, store, course, root, outside = files
    source = outside / "题目.txt"
    source.write_text("outside original", encoding="utf-8")
    (root / source.name).write_text("existing", encoding="utf-8")
    request = CourseImportInput(paths=[str(source)], operation_id="copy-once")
    result = store.import_files(course, request)
    assert result["entries"][0]["name"] == "题目 (2).txt"
    assert result["conflicts"] == []
    assert (root / source.name).read_text("utf-8") == "existing"
    assert source.read_text("utf-8") == "outside original"
    assert store.import_files(course, request) == result
    assert not (root / "题目 (3).txt").exists()
    with pytest.raises(ValidationError):
        CourseImportInput(paths=[str(source)], mode="move")
    with pytest.raises(ValidationError):
        CourseImportInput(paths=[str(source)], conflict="replace")


def test_internal_directory_copy_and_move_preserve_the_moved_source_identity(files):
    repository, store, course, root, _ = files
    chapter = root / "章节"
    chapter.mkdir()
    (chapter / "讲义.txt").write_text("source", encoding="utf-8")
    original = store.open(course, "章节/讲义.txt")
    copied = store.transfer(
        course, CourseTransferInput(paths=["章节", "章节/讲义.txt"], operation_id="copy-tree")
    )
    assert len(copied["entries"]) == 1
    assert copied["entries"][0]["path"] == "章节 (2)"
    assert (root / "章节 (2)/讲义.txt").read_text("utf-8") == "source"
    request = CourseTransferInput(
        paths=["章节/讲义.txt"], destination="", mode="move", operation_id="cut"
    )
    moved = store.transfer(course, request)
    assert store.transfer(course, request) == moved
    assert moved["entries"][0]["asset_id"] == original["asset_id"]
    assert moved["entries"][0]["source_version"] == original["source_version"]
    assert not (chapter / "讲义.txt").exists()
    resolved = repository.resolve_source(original["asset_id"], original["source_version"])
    assert Path(resolved["local_path"]) == root / "讲义.txt"


def test_internal_move_into_self_or_outside_is_refused(files):
    _, store, course, root, _ = files
    (root / "folder").mkdir()
    (root / "folder/child").mkdir()
    for request in [
        CourseTransferInput(paths=["folder"], destination="folder/child", operation_id="self"),
        CourseTransferInput(paths=["../outside"], operation_id="escape"),
    ]:
        with pytest.raises(AppError):
            store.transfer(course, request)


def test_copy_failure_is_reviewed_before_any_other_mutation(files, monkeypatch):
    _, store, course, root, outside = files
    source = outside / "partial.txt"
    source.write_text("original remains")

    def partial(_source, target):
        Path(target).write_bytes(b"partial")
        raise OSError("controlled copy interruption")

    monkeypatch.setattr(store, "_copy_file", partial)
    request = CourseImportInput(paths=[str(source)], operation_id="partial")
    with pytest.raises(AppError):
        store.import_files(course, request)
    assert source.read_text() == "original remains"
    assert store.operation(course, "partial")["state"] == "needs_review"
    with pytest.raises(AppError) as duplicate:
        store.import_files(course, request)
    assert duplicate.value.code == "FILE_OPERATION_UNKNOWN"
    with pytest.raises(AppError) as stacked:
        store.import_files(course, request.model_copy(update={"operation_id": "second"}))
    assert stacked.value.code == "FILE_OPERATION_PENDING"
    assert not (root / "partial (2).txt").exists()


def test_recycle_failure_never_calls_unlink_and_does_not_lose_source(files, monkeypatch):
    _, store, course, root, _ = files
    source = root / "keep.txt"
    source.write_text("must remain")

    def unavailable(_path):
        raise AppError("RECYCLE_UNAVAILABLE", "synthetic unavailable", 503)

    monkeypatch.setattr("learning_app.course_files.recycle_path", unavailable)
    with pytest.raises(AppError) as failed:
        store.delete(
            course, CourseDeleteInput(path=source.name, confirm=True, operation_id="recycle-failed")
        )
    assert failed.value.code == "RECYCLE_UNAVAILABLE"
    assert source.read_text() == "must remain"
    assert store.operation(course, "recycle-failed")["state"] == "failed"


def test_native_pick_has_no_course_side_effect_and_streamed_copy_is_versioned(
    tmp_path, monkeypatch
):
    folder = tmp_path / "picked"
    folder.mkdir()
    monkeypatch.setattr("learning_app.course_files.choose_paths", lambda **_kw: [str(folder)])
    config = AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run")
    with TestClient(create_app(config), base_url="http://127.0.0.1:8765") as client:
        csrf = client.get("/api/v1/session").json()["csrf_token"]
        client.headers["X-Learning-CSRF"] = csrf
        pick = client.post("/api/v1/courses/folder/pick").json()
        assert pick == {"path": str(folder), "name": folder.name}
        assert client.get("/api/v1/courses").json() == []
        course = client.post(
            "/api/v1/courses/folder", json={"path": pick["path"], "title": "独立名"}
        ).json()
        assert course["title"] == "独立名" and folder.name == "picked"
        url = f"/api/v1/courses/{course['course_id']}/files/upload"
        response = client.post(
            url,
            params={"path": "子目录/clip.txt", "operation_id": "upload1"},
            content=b"copied bytes",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["entries"][0]["path"] == "子目录/clip.txt"
        assert (folder / "子目录/clip.txt").read_bytes() == b"copied bytes"
        replay = client.post(
            url,
            params={"path": "子目录/clip.txt", "operation_id": "upload1"},
            content=b"copied bytes",
        )
        assert replay.json() == result
        mismatch = client.post(
            url,
            params={"path": "子目录/clip.txt", "operation_id": "upload1"},
            content=b"edited bytes",
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["code"] == "FILE_OPERATION_CONFLICT"
        escaped = client.post(
            url, params={"path": "../outside.txt", "operation_id": "escape"}, content=b"denied"
        )
        assert escaped.status_code == 422
        assert not (tmp_path / "outside.txt").exists()
        prop = client.post(
            f"/api/v1/courses/{course['course_id']}/files/properties",
            json={"path": "子目录/clip.txt"},
        ).json()
        assert prop["size_bytes"] == len(b"copied bytes")
        assert prop["modified_at"] is not None


def test_stream_publish_partial_failure_stays_unknown_and_does_not_retry(tmp_path, monkeypatch):
    folder = tmp_path / "stream-root"
    folder.mkdir()
    config = AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run")
    with TestClient(create_app(config), base_url="http://127.0.0.1:8765") as client:
        csrf = client.get("/api/v1/session").json()["csrf_token"]
        client.headers["X-Learning-CSRF"] = csrf
        course = client.post("/api/v1/courses/folder", json={"path": str(folder)}).json()[
            "course_id"
        ]
        client.get(f"/api/v1/courses/{course}/files")
        store = client.app.state.course_files

        def partial(_source, target):
            Path(target).write_bytes(b"partial published bytes")
            raise OSError("controlled publish failure")

        monkeypatch.setattr(store, "_copy_file", partial)
        url = f"/api/v1/courses/{course}/files/upload"
        response = client.post(
            url, params={"path": "clip.txt", "operation_id": "broken"}, content=b"whole"
        )
        assert response.status_code == 409
        state = client.get(f"/api/v1/courses/{course}/files/operations/broken").json()
        assert state["state"] == "needs_review"
        repeated = client.post(
            url, params={"path": "clip.txt", "operation_id": "broken"}, content=b"whole"
        )
        assert repeated.status_code == 409
        assert repeated.json()["code"] == "FILE_OPERATION_UNKNOWN"
        assert (folder / "clip.txt").read_bytes() == b"partial published bytes"
        assert not (folder / "clip (2).txt").exists()


def test_long_copy_does_not_own_the_repository_generation_lock(files, monkeypatch):
    import concurrent.futures
    import threading

    repository, store, course, _, outside = files
    source = outside / "slow.txt"
    source.write_text("synthetic slow file copy")
    entered, release = threading.Event(), threading.Event()
    copy = store._copy_file

    def gated(original, target):
        entered.set()
        assert release.wait(3)
        return copy(original, target)

    monkeypatch.setattr(store, "_copy_file", gated)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(
            store.import_files, course, CourseImportInput(paths=[str(source)], operation_id="gated")
        )
        assert entered.wait(1)
        try:
            independent = executor.submit(repository.create_chat, course)
            assert independent.result(timeout=0.5)["course_id"] == course
        finally:
            release.set()
        assert pending.result(timeout=3)["state"] == "completed"
