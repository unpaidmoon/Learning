"""Synthetic fault windows and paths that must never escape a course root."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from learning_app.course_files import (
    CourseDetachInput,
    CourseFileStore,
    CourseImportInput,
    CourseRenameFolderInput,
)
from learning_app.errors import AppError
from learning_app.storage import Repository


@pytest.fixture
def files(tmp_path):
    repository = Repository(tmp_path / "data")
    root = tmp_path / "合成课程"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    store = CourseFileStore(repository)
    course = store.bind(None, str(root))["course_id"]
    yield repository, store, course, root, outside
    repository.close()


def test_partial_replace_copy_is_unknown_and_blocks_new_mutation(files, monkeypatch):
    _, store, course, root, outside = files
    (root / "same.txt").write_text("original target")
    source = outside / "same.txt"
    source.write_text("source remains intact")

    def partial_copy(original, target):
        Path(target).write_bytes(b"partial target")
        raise OSError("CONTROLLED_PARTIAL_COPY_FAILURE")

    monkeypatch.setattr(store, "_copy_file", partial_copy)
    request = CourseImportInput(paths=[str(source)], operation_id="partial_copy")
    with pytest.raises(AppError):
        store.import_files(course, request)
    status = store.operation(course, "partial_copy")
    assert status["state"] == "needs_review"
    assert status["result"]["planned_paths"][0]["target"] == str(root / "same (2).txt")
    assert (root / "same.txt").read_text() == "original target"
    assert source.read_text() == "source remains intact"
    with pytest.raises(AppError) as duplicate:
        store.import_files(course, request)
    assert duplicate.value.code == "FILE_OPERATION_UNKNOWN"
    with pytest.raises(AppError) as another:
        store.import_files(course, request.model_copy(update={"operation_id": "must_not_stack"}))
    assert another.value.code == "FILE_OPERATION_PENDING"
    assert store.reconcile(course, "partial_copy")["state"] == "reviewed"


def test_failure_before_metadata_compensates_new_move(files):
    repository, store, course, root, outside = files
    source = outside / "rollback.txt"
    source.write_text("safe original")

    def hook(point, _detail):
        if point == "course_file_after_filesystem":
            raise OSError("CONTROLLED_AFTER_MOVE")

    repository.fault_hook = hook
    with pytest.raises(AppError):
        store.import_files(
            course, CourseImportInput(paths=[str(source)], operation_id="rollback_move")
        )
    assert source.read_text() == "safe original"
    assert (root / source.name).exists()
    assert store.operation(course, "rollback_move")["state"] == "needs_review"


def test_metadata_committed_before_lost_receipt_keeps_bytes_and_versions(files):
    repository, store, course, root, outside = files
    source = outside / "receipt.txt"
    source.write_text("already moved and registered")
    old = repository.register_source(course, source)

    def hook(point, _detail):
        if point == "course_file_before_complete":
            raise OSError("CONTROLLED_LOST_COMPLETION")

    repository.fault_hook = hook
    with pytest.raises(AppError):
        store.import_files(
            course, CourseImportInput(paths=[str(source)], operation_id="lost_receipt")
        )
    assert source.exists()
    assert (root / source.name).read_text() == "already moved and registered"
    assert store.operation(course, "lost_receipt")["state"] == "needs_review"
    assert (
        repository.resolve_source(old["asset_id"], old["source_version"])["status"] == "available"
    )
    assert store.list(course)["entries"][0]["asset_id"] != old["asset_id"]
    assert (
        Path(repository.resolve_source(old["asset_id"], old["source_version"])["local_path"])
        == source
    )


def test_course_rename_receipt_loss_preserves_matching_directory_and_db(files):
    repository, store, course, root, _ = files

    def hook(point, _detail):
        if point == "course_file_before_complete":
            raise OSError("CONTROLLED_RENAME_RECEIPT_LOSS")

    repository.fault_hook = hook
    with pytest.raises(AppError):
        store.rename_course(
            course, CourseRenameFolderInput(name="已改名课程", operation_id="rename_receipt")
        )
    folder = store.folder(course)
    assert folder["root_status"] == "ready"
    assert folder["title"] == "已改名课程"
    assert Path(folder["root_path"]) == root and root.exists()
    assert store.operation(course, "rename_receipt")["state"] == "needs_review"


def test_dangling_link_import_target_never_writes_outside(files):
    _, store, course, root, outside = files
    unexpected = outside / "must_not_be_created.txt"
    link = root / "linked.txt"
    try:
        link.symlink_to(unexpected)
    except OSError as exc:
        pytest.skip(
            f"host cannot create synthetic symlink: {exc.winerror if hasattr(exc, 'winerror') else exc.errno}"
        )
    source = outside / "linked.txt"
    source.write_text("explicit source")
    assert not link.exists() and link.is_symlink()
    with pytest.raises(AppError) as rejected:
        store.import_files(
            course,
            CourseImportInput(paths=[str(source)], operation_id="deny_link"),
        )
    assert rejected.value.code == "FILE_LINK_DENIED"
    assert source.exists() and not unexpected.exists()


def test_disk_root_nested_course_and_unbound_detach_are_explicit_errors(files):
    repository, store, _, root, _ = files
    with pytest.raises(AppError) as disk:
        store.bind(None, str(Path(root.anchor)))
    assert disk.value.code == "COURSE_ROOT_INVALID"
    nested = root / "nested"
    nested.mkdir()
    with pytest.raises(AppError) as overlap:
        store.bind(None, str(nested))
    assert overlap.value.code == "COURSE_ROOT_OVERLAP"
    legacy = repository.create_course("旧课程")
    detached = store.detach(
        legacy["course_id"], CourseDetachInput(confirm=True, operation_id="unbound_detach")
    )
    assert detached["root"]["root_status"] == "detached"
    assert all(item["course_id"] != legacy["course_id"] for item in repository.list_courses())


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction boundary")
def test_windows_junction_denies_real_external_directory(files):
    import _winapi

    _, store, course, root, outside = files
    (outside / "not-visible.txt").write_text("must stay outside")
    junction = root / "junction"
    _winapi.CreateJunction(str(outside), str(junction))
    try:
        row = store.list(course)["entries"][0]
        assert row["kind"] == "blocked"
        with pytest.raises(AppError) as denied:
            store.list(course, "junction")
        assert denied.value.code == "FILE_LINK_DENIED"
        assert (outside / "not-visible.txt").read_text() == "must stay outside"
    finally:
        junction.rmdir()
