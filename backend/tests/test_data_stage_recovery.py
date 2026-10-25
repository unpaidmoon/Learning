"""Retained stages, real Windows sharing failures and explicit recovery boundaries."""

import ctypes
import json
import os
from pathlib import Path

import pytest
from workbench_support import learning_course

import learning_app.data_location as locations
from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.data_activation import STAGE_MARKER, require_active_data
from learning_app.data_location import ProfileLocation, inventory
from learning_app.data_operations import DataOperations, atomic_record
from learning_app.data_package import DataPackage
from learning_app.errors import AppError
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.processing import PreparationService


def prepare(tmp_path, kind):
    repo, course, _, _ = learning_course(tmp_path)
    repo.create_chat(course, "saved")
    repo.publish_submission("saved", "never replay the original pending question", "pending")
    location = ProfileLocation(repo.root)
    operations = DataOperations(repo, location.anchor)
    destination = tmp_path / "destination"
    destination.mkdir()
    request = {"kind": kind, "parent": str(destination)}
    if kind == "import":
        operations.prepare_export("archive")
        assert operations.perform_export("archive")["state"] == "completed"
        archive, _ = operations.download("archive")
        request["package"] = str(archive)
    operations.prepare("transfer", request)
    assert operations.perform_transfer("transfer", location)["state"] == "ready"
    location.schedule("transfer")
    plan = json.loads(location.transfer("transfer").read_text("utf-8"))
    repo.close()
    return repo, location, operations, plan


@pytest.mark.parametrize("state", ["extracting", "ready", "failed", "wrong_operation", "missing"])
async def test_every_service_rejects_unactivated_stage_before_recovery_or_execution(
    tmp_path, state
):
    stage = tmp_path / ".learning-stage-unactivated"
    repo = Repository(stage / "data")
    repo.create_course("original", "course")
    repo.create_chat("course", "saved")
    pending = repo.publish_submission("saved", "must remain pending", "pending")
    if state != "missing":
        atomic_record(
            stage / STAGE_MARKER,
            {
                "format": 1,
                "state": "active" if state == "wrong_operation" else state,
                "activation_operation_id": "different",
            },
        )
    if state == "wrong_operation":
        atomic_record(stage / "activation.json", {"operation_id": "expected"})
    before = repo.get_generation(pending["generation_id"])
    calls = []
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run")
    service = GenerationService(repo, config, execution_factory=lambda *args: calls.append(args))
    executors = [service, TaskExecutor(repo, service), PreparationService(repo, PROJECT_ROOT)]
    for executor in executors:
        with pytest.raises(AppError) as error:
            await executor.open()
        assert error.value.code == "DATA_STAGE_NOT_ACTIVE"
    assert calls == [] and not (repo.root / "execution").exists()
    assert repo.get_generation(pending["generation_id"]) == before
    repo.close()


def test_interrupted_extraction_keeps_failed_stage_and_cannot_be_reused_as_ready(
    tmp_path, monkeypatch
):
    repo, _, _, _ = learning_course(tmp_path)
    archive = tmp_path / "whole.zip"
    stream, _ = DataPackage(repo).build()
    with stream, archive.open("wb") as output:
        output.write(stream.read())
    original = archive.read_bytes()
    verified = DataPackage().verify(archive)
    monkeypatch.setattr(DataPackage, "verify", lambda *args, **kwargs: verified)
    stage = tmp_path / ".learning-stage-interrupted"

    def stop():
        raise AppError("DATA_OPERATION_CANCELLED", "owned cancellation during extraction")

    with pytest.raises(AppError) as error:
        DataPackage().stage(archive, stage, check=stop)
    assert error.value.code == "DATA_OPERATION_CANCELLED"
    assert json.loads((stage / STAGE_MARKER).read_text("utf-8"))["state"] == "failed"
    before = inventory(stage)
    with pytest.raises(AppError) as error:
        DataPackage().stage(archive, stage)
    assert error.value.code == "DATA_IMPORT_TARGET_EXISTS"
    with pytest.raises(AppError, match="尚未激活"):
        require_active_data(stage / "data")
    assert inventory(stage) == before and archive.read_bytes() == original
    repo.close()


@pytest.mark.skipif(os.name != "nt", reason="actual Windows directory sharing boundary")
@pytest.mark.parametrize("kind", ["move", "import"])
def test_open_child_blocks_one_native_publish_then_only_explicit_action_retries(
    tmp_path, monkeypatch, kind
):
    repo, location, operations, plan = prepare(tmp_path, kind)
    stage, target = Path(plan["stage"]), Path(plan["target"])
    old_files = inventory(repo.root)
    archive = operations.download("archive")[0] if kind == "import" else None
    archive_bytes = archive.read_bytes() if archive else None
    calls, handles, prepared = [], [], []
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    publish = locations._publish_new_directory

    def observed(source, destination, parent):
        calls.append((source, destination))
        if len(calls) == 1:
            handle = create(str(source / "data/business.sqlite3"), 0x80000000, 7, None, 3, 0, None)
            assert handle != wintypes.HANDLE(-1).value
            handles.append(handle)
            prepared.extend(inventory(source))
        return publish(source, destination, parent)

    monkeypatch.setattr(locations, "_publish_new_directory", observed)
    try:
        with location.lease(), pytest.raises(AppError) as error:
            location.apply_closed()
        assert error.value.code == "DATA_IMPORT_PUBLISH_FAILED"
        assert error.value.__cause__.winerror == 5
        assert len(calls) == 1 and not target.exists() and not location.pending.exists()
        assert inventory(stage) == prepared and inventory(repo.root) == old_files
        assert operations.status("transfer")["state"] == "publish_blocked"
        for _ in range(2):
            assert operations.reconcile("transfer")["state"] == "publish_blocked"
            assert ProfileLocation(repo.root).apply_closed() is None
        assert len(calls) == 1 and inventory(stage) == prepared
        diagnostics = list(stage.parent.glob("publish-diagnostic-*.json"))
        assert (
            diagnostics
            and json.loads(diagnostics[0].read_text("utf-8"))["delete_access_failures"] == []
        )
        with pytest.raises(AppError, match="尚未激活"):
            require_active_data(stage / "data")
    finally:
        for handle in handles:
            close(handle)
    location.schedule("transfer")
    with location.lease():
        assert location.apply_closed()["state"] == "completed"
    assert len(calls) == 2 and not stage.exists() and inventory(repo.root) == old_files
    assert operations.reconcile("transfer")["state"] == "completed"
    require_active_data(location.current())
    assert not (location.current() / "execution").exists()
    if archive:
        assert archive.read_bytes() == archive_bytes


@pytest.mark.parametrize("change", ["source", "marker_missing", "marker_identity", "stage_bytes"])
def test_explicit_retry_rejects_stale_source_or_modified_retained_stage(
    tmp_path, monkeypatch, change
):
    repo, location, _, plan = prepare(tmp_path, "move")
    stage, target = Path(plan["stage"]), Path(plan["target"])
    calls = []

    def blocked(*args):
        calls.append(args)
        raise AppError("DATA_IMPORT_PUBLISH_FAILED", "owned blocked publication")

    monkeypatch.setattr(locations, "_publish_new_directory", blocked)
    with pytest.raises(AppError):
        location.apply_closed()
    if change == "source":
        reopened = Repository(repo.root)
        reopened.create_course("new work after failed move", "after_failure")
        reopened.close()
    elif change == "marker_missing":
        (stage / STAGE_MARKER).unlink()
    elif change == "marker_identity":
        atomic_record(stage / STAGE_MARKER, {"format": 1, "state": "ready", "kind": "foreign"})
    else:
        (stage / "data/new-content.txt").write_text("changed stage", "utf-8")
    before = inventory(repo.root)
    location.schedule("transfer")
    with pytest.raises(AppError):
        location.apply_closed()
    assert len(calls) == 1 and not target.exists()
    assert location.current() == repo.root and inventory(repo.root) == before and stage.is_dir()


def test_target_created_after_review_is_never_overwritten_or_adopted(tmp_path, monkeypatch):
    repo, location, _, plan = prepare(tmp_path, "move")
    target = Path(plan["target"])
    before = inventory(repo.root)

    def race(name):
        if name == "before_publish":
            target.mkdir()
            (target / "keep.txt").write_bytes(b"foreign target")

    location.fault_hook = race
    with pytest.raises(AppError) as error:
        location.apply_closed()
    assert error.value.code == "DATA_IMPORT_TARGET_EXISTS"
    assert json.loads(location.transfer("transfer").read_text("utf-8"))["state"] == "failed"
    assert location.current() == repo.root and inventory(repo.root) == before
    assert (target / "keep.txt").read_bytes() == b"foreign target"
    assert Path(plan["stage"]).is_dir() and not (target / "activation.json").exists()


@pytest.mark.parametrize("fault", ["after_publish", "after_pointer"])
def test_lost_acknowledgement_reconciles_same_cut_without_second_publish(
    tmp_path, monkeypatch, fault
):
    repo, location, _, _ = prepare(tmp_path, "move")
    calls = []
    publish = locations._publish_new_directory

    def observed(*args):
        calls.append(args)
        return publish(*args)

    monkeypatch.setattr(locations, "_publish_new_directory", observed)
    location.fault_hook = (
        lambda name: (_ for _ in ()).throw(RuntimeError("lost acknowledgement"))
        if name == fault
        else None
    )
    with pytest.raises(RuntimeError):
        location.apply_closed()
    location.fault_hook = None
    with location.lease():
        assert location.apply_closed()["state"] == "completed"
    assert len(calls) == 1
    require_active_data(location.current())
    assert inventory(location.current()) == inventory(repo.root)
