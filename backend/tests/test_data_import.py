"""Whole package preparation keeps history, fences execution and relocates exact bytes."""

import hashlib

import pytest
from workbench_support import learning_course

from learning_app.data_import import prepare_import
from learning_app.data_package import DataPackage
from learning_app.errors import AppError
from learning_app.imported_executions import ImportedExecutions
from learning_app.storage import Repository


@pytest.mark.parametrize("with_originals", [False, True])
def test_prepared_import_never_opens_old_local_paths_or_runs_old_pending_request(
    tmp_path, with_originals
):
    repo, course, root, selected = learning_course(tmp_path)
    repo.create_chat(course, "saved")
    pending = repo.publish_submission("saved", "old pending request", "old_pending")
    binding = repo.bind_execution("saved", "old_sid")
    (root / "unopened.txt").write_bytes(b"not opened original")
    package = tmp_path / "export.zip"
    stream, _ = DataPackage(repo).build(include_originals=with_originals)
    with stream, package.open("wb") as out:
        out.write(stream.read())
    source_counts = repo.counts()
    final = tmp_path / "new-data"
    materials = tmp_path / "new-materials"
    stage = tmp_path / "private-stage"
    receipt = prepare_import(
        package, stage, final, final_materials=materials if with_originals else None
    )
    assert receipt["old_execution_read_only"] and not final.exists() and not materials.exists()
    imported = Repository(stage / "data")
    fence = ImportedExecutions(imported)
    assert fence.contains("generation", pending["generation_id"])
    assert imported.get_generation(pending["generation_id"])["state"] == "pending"
    assert imported.get_binding(binding["binding_id"])["state"] == "retired"
    with imported.engine.connect() as connection:
        locator = connection.exec_driver_sql("SELECT local_path FROM source_versions").scalar_one()
        root_path, root_state = connection.exec_driver_sql(
            "SELECT local_path,state FROM course_roots"
        ).one()
    if with_originals:
        assert root_state == "active" and locator.startswith(str(materials))
        actual = stage / "materials" / locator[len(str(materials)) + 1 :]
        assert hashlib.sha256(actual.read_bytes()).hexdigest() == selected.reference.source_digest
        assert (
            stage / "materials/courses" / course / "unopened.txt"
        ).read_bytes() == b"not opened original"
    else:
        assert root_state == "needs_source" and locator.startswith(str(final / "unlocated-sources"))
        assert (
            imported.resolve_source(selected.reference.asset_id, selected.reference.source_version)[
                "status"
            ]
            != "available"
        )
    assert root_path != str(root)
    assert repo.counts() == source_counts
    assert (root / "unopened.txt").read_bytes() == b"not opened original"
    imported.close()
    repo.close()


def test_target_with_existing_data_is_rejected_before_any_import_preparation(tmp_path):
    repo, _, _, _ = learning_course(tmp_path)
    package = tmp_path / "export.zip"
    stream, _ = DataPackage(repo).build()
    with stream, package.open("wb") as out:
        out.write(stream.read())
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("keep", "utf-8")
    with pytest.raises(AppError) as error:
        prepare_import(package, tmp_path / "stage", target)
    assert error.value.code == "DATA_IMPORT_TARGET_EXISTS" and not (tmp_path / "stage").exists()
    assert (target / "keep.txt").read_text("utf-8") == "keep"
    repo.close()
