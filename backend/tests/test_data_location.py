"""Empty-target publication, process exclusion and crash-window reconciliation."""

import json
from pathlib import Path

import pytest
from workbench_support import learning_course

from learning_app.data_location import ProfileLocation, inventory
from learning_app.data_operations import DataOperations
from learning_app.errors import AppError
from learning_app.imported_executions import ImportedExecutions
from learning_app.storage import Repository


def test_closed_move_preserves_all_registered_bytes_and_external_originals(tmp_path):
    repo, course, materials, _ = learning_course(tmp_path)
    repo.create_chat(course, "saved")
    repo.publish_submission("saved", "pending remains pending", "pending")
    (repo.root / "execution").mkdir()
    (repo.root / "execution/native-calls.jsonl").write_bytes(b'{"saved":"journal"}\n')
    location = ProfileLocation(repo.root)
    destination = tmp_path / "中文 另一个磁盘目录"
    destination.mkdir()
    plan = location.plan("move_one", "move", destination)
    location.ready(plan)
    location.schedule("move_one")
    repo.close()
    before = inventory(repo.root)
    with location.lease():
        result = location.apply_closed()
    assert result["state"] == "completed" and not location.pending.exists()
    assert inventory(location.current()) == before == inventory(repo.root)
    assert (materials / "lesson.txt").is_file()
    relocated = Repository(location.current())
    assert relocated.get_chat("saved")["messages"][0]["text"] == "pending remains pending"
    assert not ImportedExecutions(relocated).contains("chat", "saved")
    relocated.close()


@pytest.mark.parametrize("fault", ["before_publish", "after_publish", "after_pointer"])
def test_switch_failure_retains_old_data_and_reconciles_original_published_cut(tmp_path, fault):
    repo, _, _, _ = learning_course(tmp_path)
    location = ProfileLocation(repo.root)
    destination = tmp_path / "target"
    destination.mkdir()
    plan = location.plan("move_fault", "move", destination)
    location.ready(plan)
    location.schedule("move_fault")
    repo.close()
    before = inventory(repo.root)
    location.fault_hook = (
        lambda name: (_ for _ in ()).throw(RuntimeError("owned failure")) if name == fault else None
    )
    with location.lease(), pytest.raises(RuntimeError):
        location.apply_closed()
    assert inventory(repo.root) == before
    location.fault_hook = None
    if fault == "before_publish":
        assert location.current() == repo.root
        with pytest.raises(AppError, match="原数据切换未完成"):
            location.apply_closed()
        location.cancel_schedule("move_fault")
        assert not location.pending.exists()
    else:
        with location.lease():
            complete = location.apply_closed()
        assert complete["state"] == "completed"
        assert inventory(location.current()) == before


def test_profile_excludes_second_writer_and_existing_target_never_overwritten(tmp_path):
    repo, _, _, _ = learning_course(tmp_path)
    location = ProfileLocation(repo.root)
    with location.lease(), pytest.raises(AppError) as error:
        with ProfileLocation(repo.root).lease():
            pytest.fail("second profile writer admitted")
    assert error.value.code == "DATA_PROFILE_IN_USE"
    existing = tmp_path / "学习数据-conflict"
    existing.mkdir()
    (existing / "keep.txt").write_bytes(b"keep")
    with pytest.raises(AppError) as error:
        location.plan("conflict", "move", tmp_path)
    assert error.value.code == "DATA_IMPORT_TARGET_EXISTS"
    assert (existing / "keep.txt").read_bytes() == b"keep"
    repo.close()


def test_prepared_operation_cannot_switch_a_different_current_profile(tmp_path):
    repo, _, _, _ = learning_course(tmp_path)
    location = ProfileLocation(repo.root)
    destination = tmp_path / "target"
    destination.mkdir()
    location.ready(location.plan("first_move", "move", destination))
    location.ready(location.plan("older_plan", "move", destination))
    location.schedule("first_move")
    repo.close()
    with location.lease():
        location.apply_closed()
    current = location.current()
    with pytest.raises(AppError) as error:
        location.schedule("older_plan")
    assert error.value.code == "DATA_LOCATION_SOURCE_CHANGED"
    assert location.current() == current and not location.pending.exists()


def test_import_stage_changed_after_review_never_becomes_active(tmp_path):
    repo, _, _, _ = learning_course(tmp_path)
    location = ProfileLocation(repo.root)
    operations = DataOperations(repo, location.anchor)
    operations.prepare_export("export_for_stage")
    operations.perform_export("export_for_stage")
    archive, _ = operations.download("export_for_stage")
    destination = tmp_path / "destination"
    destination.mkdir()
    operations.prepare(
        "import_review", {"kind": "import", "parent": str(destination), "package": str(archive)}
    )
    assert operations.perform_transfer("import_review", location)["state"] == "ready"
    switch = json.loads(location.transfer("import_review").read_text("utf-8"))
    (Path(switch["stage"]) / "offline/reader/reader.js").write_bytes(b"changed after review")
    location.schedule("import_review")
    repo.close()
    with location.lease(), pytest.raises(AppError) as error:
        location.apply_closed()
    assert error.value.code == "DATA_IMPORT_STAGE_CHANGED"
    assert location.current() == repo.root and not Path(switch["target"]).exists()


@pytest.mark.parametrize("originals", [False, True])
def test_export_stage_activate_import_keeps_pending_read_only_and_whole_material_tree(
    tmp_path, originals
):
    source = tmp_path / "source"
    source.mkdir()
    repo, course, materials, selected = learning_course(source)
    repo.create_chat(course, "saved")
    pending = repo.publish_submission("saved", "never rerun", "old")
    (materials / "unopened.txt").write_bytes(b"whole original course")
    destination = tmp_path / "destination"
    destination.mkdir()
    initial = tmp_path / "initial"
    initial.mkdir()
    target_repo = Repository(initial)
    location = ProfileLocation(initial)
    operations = DataOperations(repo, tmp_path / "source-profile")
    operations.prepare_export("all", include_originals=originals)
    assert operations.perform_export("all")["state"] == "completed"
    archive, _ = operations.download("all")
    importing = DataOperations(target_repo, location.anchor)
    importing.prepare(
        "restore", {"kind": "import", "parent": str(destination), "package": str(archive)}
    )
    receipt = importing.perform_transfer("restore", location)
    assert receipt["state"] == "ready" and location.current() == initial
    location.schedule("restore")
    target_repo.close()
    with location.lease():
        location.apply_closed()
    restored = Repository(location.current())
    assert ImportedExecutions(restored).contains("generation", pending["generation_id"])
    assert restored.get_generation(pending["generation_id"])["state"] == "pending"
    if originals:
        assert (
            location.current().parent / "materials/courses" / course / "unopened.txt"
        ).read_bytes() == b"whole original course"
        assert (
            restored.resolve_source(selected.reference.asset_id, selected.reference.source_version)[
                "status"
            ]
            == "available"
        )
    else:
        assert (
            restored.resolve_source(selected.reference.asset_id, selected.reference.source_version)[
                "status"
            ]
            != "available"
        )
    assert importing.reconcile("restore")["state"] == "completed"
    assert (
        json.loads((location.current() / "data-import.json").read_text("utf-8"))["state"]
        == "installed"
    )
    restored.close()
    repo.close()
