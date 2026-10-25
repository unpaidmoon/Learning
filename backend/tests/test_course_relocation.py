"""Explicit root association preserves source IDs/versions and exact operation receipts."""

import shutil

import pytest
from workbench_support import learning_course

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.workbench.sources import SourceStore


def test_root_relocation_only_reassociates_matching_bytes_without_moving_originals(tmp_path):
    repo, course, root, source = learning_course(tmp_path)
    new = tmp_path / "new-originals"
    shutil.copytree(root, new)
    before = (root / "lesson.txt").read_bytes()
    store = CourseFileStore(repo)
    result = store.relocate_root(
        course, str(new), expected_root=str(root), operation_id="relocate_once"
    )
    assert result["matched_versions"] == 1 and not result["unavailable_versions"]
    SourceStore(repo).require_current(source)
    opened = store.open(course, "lesson.txt")
    assert (
        opened["asset_id"] == source.reference.asset_id
        and opened["source_version"] == source.reference.source_version
    )
    assert (root / "lesson.txt").read_bytes() == before and (
        new / "lesson.txt"
    ).read_bytes() == before
    assert (
        store.relocate_root(course, str(new), expected_root=str(root), operation_id="relocate_once")
        == result
    )
    repo.close()


def test_same_name_new_content_never_takes_over_the_old_source_version(tmp_path):
    repo, course, root, source = learning_course(tmp_path)
    new = tmp_path / "replacement"
    new.mkdir()
    (new / "lesson.txt").write_bytes(b"different content same filename")
    store = CourseFileStore(repo)
    result = store.relocate_root(course, str(new), expected_root=str(root), operation_id="mismatch")
    assert result["matched_versions"] == 0 and len(result["unavailable_versions"]) == 1
    with pytest.raises(AppError):
        SourceStore(repo).require_current(source)
    # Opening the new file is an explicit new-version action; the old snapshot
    # remains the original text and keeps its old source identity.
    opened = store.open(course, "lesson.txt")
    assert (
        opened["asset_id"] == source.reference.asset_id
        and opened["source_version"] != source.reference.source_version
    )
    assert SourceStore(repo).snapshot(course, source.snapshot_id) == source
    repo.close()


@pytest.mark.parametrize("after_commit", [False, True])
def test_relocation_fault_preserves_either_old_root_or_exact_committed_receipt(
    tmp_path, after_commit
):
    repo, course, root, source = learning_course(tmp_path)
    new = tmp_path / "selected"
    shutil.copytree(root, new)
    point = (
        "course_root_relocation_after_commit"
        if after_commit
        else "course_root_relocation_before_commit"
    )

    def fault(name, detail):
        if name == point:
            raise RuntimeError("owned fault")

    repo.fault_hook = fault
    store = CourseFileStore(repo)
    with pytest.raises(RuntimeError):
        store.relocate_root(course, str(new), expected_root=str(root), operation_id="faulted")
    repo.fault_hook = None
    assert store.folder(course)["root_path"] == str(new if after_commit else root)
    if after_commit:
        result = store.relocate_root(
            course, str(new), expected_root=str(root), operation_id="faulted"
        )
        assert result["state"] == "completed"
    else:
        with pytest.raises(AppError) as error:
            store.relocate_root(course, str(new), expected_root=str(root), operation_id="faulted")
        assert error.value.code == "FILE_OPERATION_UNKNOWN"
    SourceStore(repo).require_current(source)
    repo.close()
