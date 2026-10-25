"""Exact data-operation receipts, known interruption and no rebuilt export cut."""

import hashlib

import pytest
from workbench_support import learning_course

from learning_app.data_operations import DataOperations
from learning_app.errors import AppError


def test_export_same_operation_keeps_same_package_after_later_business_changes(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    operations = DataOperations(repo, tmp_path / "profile")
    assert operations.prepare_export("export_one")["state"] == "prepared"
    complete = operations.perform_export("export_one")
    assert complete["state"] == "completed"
    path, _ = operations.download("export_one")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    repo.create_chat(course, "later_chat")
    assert operations.prepare_export("export_one") == complete
    assert operations.perform_export("export_one") == complete
    assert operations.download("export_one")[1] == complete
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    with pytest.raises(AppError) as error:
        operations.prepare_export("export_one", include_originals=True)
    assert error.value.code == "DATA_OPERATION_CONFLICT"
    repo.close()


def test_published_package_lost_receipt_reconciles_exact_hash_without_rebuilding(tmp_path):
    repo, _, _, _ = learning_course(tmp_path)
    operations = DataOperations(repo, tmp_path / "profile")
    operations.prepare_export("lost_receipt")

    def fault(name, detail):
        if name == "data_export_after_publish":
            raise RuntimeError("owned lost receipt")

    repo.fault_hook = fault
    failed = operations.perform_export("lost_receipt")
    assert failed["state"] == "unknown"
    package = operations.folder("lost_receipt") / "archive.zip"
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    repo.fault_hook = None
    recovered = operations.reconcile("lost_receipt")
    assert recovered["state"] == "completed" and recovered["result"] == failed["result"]
    assert hashlib.sha256(package.read_bytes()).hexdigest() == digest
    assert operations.perform_export("lost_receipt") == recovered
    repo.close()


def test_prepublication_failure_keeps_source_and_never_automatically_repeats(tmp_path):
    repo, _, root, _ = learning_course(tmp_path)
    operations = DataOperations(repo, tmp_path / "profile")
    operations.prepare_export("failed")

    def fault(name, detail):
        if name == "data_export_after_intent":
            raise RuntimeError("owned before-cut failure")

    repo.fault_hook = fault
    result = operations.perform_export("failed")
    assert result["state"] == "failed"
    repo.fault_hook = None
    assert operations.perform_export("failed") == result and (root / "lesson.txt").is_file()
    assert not (operations.folder("failed") / "archive.zip").exists()
    repo.close()
