"""Verified private import preparation; no target activation or execution startup."""

import shutil
from pathlib import PureWindowsPath

from learning_app.data_originals import relative_name
from learning_app.data_package import _ID, DataPackage
from learning_app.data_snapshot import _copy_registered, file_digest, stream_digest
from learning_app.errors import AppError
from learning_app.imported_executions import ImportedExecutions
from learning_app.media import _read_locked
from learning_app.storage import Repository


def _under(path, root):
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise AppError("DATA_IMPORT_ID_INVALID", "原件位置关联身份无效")
    return value


def _restore_originals(repository, stage, manifest, final_materials, *, check=lambda: None):
    required = sum(row["bytes"] for rows in manifest["originals"].values() for row in rows)
    if shutil.disk_usage(stage).free < required + 64 * 1024 * 1024:
        raise AppError("DATA_IMPORT_SPACE", "恢复原件所需空间不足，未切换当前数据")
    staged = stage / "materials"
    staged.mkdir()
    with repository.engine.connect() as connection:
        roots = {
            r[0]: PureWindowsPath(r[1])
            for r in connection.exec_driver_sql("SELECT course_id,local_path FROM course_roots")
        }
        versions = [
            dict(r)
            for r in connection.exec_driver_sql(
                "SELECT * FROM source_versions ORDER BY created_at,asset_id,source_version"
            ).mappings()
        ]
        associations = {}
        for course, asset in connection.exec_driver_sql(
            "SELECT course_id,asset_id FROM course_assets"
        ):
            associations.setdefault(asset, []).append(course)
    mapped = {}
    for course, old in sorted(roots.items(), key=lambda item: len(item[1].parts)):
        ancestors = [other for other in mapped if _under(old, roots[other]) is not None]
        if ancestors:
            parent = max(ancestors, key=lambda c: len(roots[c].parts))
            mapped[course] = mapped[parent].joinpath(*old.relative_to(roots[parent]).parts)
        else:
            mapped[course] = final_materials / "courses" / course
    legacy = set(c for courses in associations.values() for c in courses) - set(roots)
    mapped.update({c: final_materials / "courses" / c for c in legacy})

    def staging(path):
        relative = path.relative_to(final_materials)
        relative_name(relative.as_posix())
        return staged.joinpath(*relative.parts)

    def copy(digest, length, final):
        target = staging(final)
        if target.exists():
            if target.stat().st_size == length and file_digest(target, check=check) == digest:
                return
            raise AppError("DATA_IMPORT_ORIGINAL_COLLISION", "原件恢复路径存在内容冲突")
        _copy_registered(stage / "originals/objects" / digest, target, digest, length, check=check)

    for path in mapped.values():
        staging(path).mkdir(parents=True, exist_ok=True)
    for record in manifest["originals"]["course_files"]:
        path = mapped[record["course_id"]].joinpath(*record["relative_path"].split("/"))
        copy(record["sha256"], record["bytes"], path)
    latest = {row["asset_id"]: row["source_version"] for row in versions}
    locations = {}
    legacy_entries = []
    for row in versions:
        asset, version, digest = row["asset_id"], row["source_version"], row["content_hash"]
        old = PureWindowsPath(row["local_path"])
        candidates = [
            c
            for c in associations.get(asset, [])
            if c in roots and _under(old, roots[c]) is not None
        ]
        path = None
        for course in sorted(candidates, key=lambda c: len(roots[c].parts), reverse=True):
            candidate = mapped[course].joinpath(*old.relative_to(roots[course]).parts)
            source = staging(candidate)
            if source.is_file() and file_digest(source, check=check) == digest:
                path = candidate
                break
        if path is None:
            courses = [c for c in associations.get(asset, []) if c in mapped]
            course = (
                max(courses, key=lambda c: len(roots.get(c, PureWindowsPath("X:/")).parts))
                if courses
                else None
            )
            if course in legacy and version == latest[asset]:
                name = relative_name(old.name or asset)
                path = mapped[course] / name
                if staging(path).exists():
                    path = mapped[course] / (asset + "-" + name)
                legacy_entries.append(
                    (course, path.relative_to(mapped[course]).as_posix(), asset, version)
                )
            else:
                path = (
                    (mapped[course] if course else final_materials / "unassociated")
                    / ".source-versions"
                    / asset
                    / version
                )
            copy(digest, row["byte_length"], path)
        locations[(asset, version)] = str(path)
    with repository._lock, repository.engine.begin() as connection:
        for course, path in mapped.items():
            if course in roots:
                connection.exec_driver_sql(
                    "UPDATE course_roots SET local_path=?,state='active' WHERE course_id=?",
                    (str(path), course),
                )
            else:
                connection.exec_driver_sql(
                    "INSERT INTO course_roots(course_id,local_path,state,created_at) SELECT course_id,?,'active',created_at FROM courses WHERE course_id=?",
                    (str(path), course),
                )
        for (asset, version), path in locations.items():
            connection.exec_driver_sql(
                "UPDATE source_versions SET local_path=? WHERE asset_id=? AND source_version=?",
                (path, asset, version),
            )
        for course, relative, asset, version in legacy_entries:
            connection.exec_driver_sql(
                "INSERT INTO course_file_entries(course_id,relative_path,asset_id,source_version) VALUES(?,?,?,?)",
                (course, relative, asset, version),
            )
        repository._hook("data_import_locations_before_commit")
    return {course: str(path) for course, path in mapped.items()}


def prepare_import(package, stage, final_data, *, final_materials=None, check=lambda: None):
    """Caller must later publish prepared dirs before changing the active pointer."""
    if final_data.exists():
        raise AppError("DATA_IMPORT_TARGET_EXISTS", "目标已有数据，不合并或覆盖")
    with _read_locked(package) as source:
        digest = stream_digest(source, check=check)
        check()
        verified = DataPackage().stage(package, stage, check=check)
        source.seek(0)
        if stream_digest(source, check=check) != digest:
            raise AppError("DATA_PACKAGE_SOURCE_CHANGED", "导入期间原始数据包已变化，未激活数据")
    if verified.manifest["include_originals"] and (
        final_materials is None or final_materials.exists()
    ):
        raise AppError("DATA_IMPORT_ORIGINAL_TARGET_EXISTS", "原件目标须为尚未存在的新目录")
    repository = Repository(stage / "data")
    try:
        # This installer has no Service, TaskExecutor, native Host or provider.
        # Install first; path restoration cannot grant old execution authority.
        fence = ImportedExecutions.install(repository, verified.manifest["package_id"], digest)
        with repository.engine.connect() as connection:
            for asset, version in connection.exec_driver_sql(
                "SELECT asset_id,source_version FROM source_versions"
            ):
                _identity(asset)
                _identity(version)
            for (course,) in connection.exec_driver_sql("SELECT course_id FROM courses"):
                _identity(course)
        if verified.manifest["include_originals"]:
            locations = _restore_originals(
                repository, stage, verified.manifest, final_materials.resolve(), check=check
            )
        else:
            locations = {}
            with repository._lock, repository.engine.begin() as connection:
                for asset, version in connection.exec_driver_sql(
                    "SELECT asset_id,source_version FROM source_versions"
                ).fetchall():
                    path = final_data / "unlocated-sources" / asset / version
                    connection.exec_driver_sql(
                        "UPDATE source_versions SET local_path=? WHERE asset_id=? AND source_version=?",
                        (str(path), asset, version),
                    )
                for (course,) in connection.exec_driver_sql(
                    "SELECT course_id FROM course_roots"
                ).fetchall():
                    connection.exec_driver_sql(
                        "UPDATE course_roots SET local_path=?,state='needs_source' WHERE course_id=?",
                        (str(final_data / "unlocated-courses" / course), course),
                    )
                repository._hook("data_import_locations_before_commit")
        check()
        result = {
            "format": 1,
            "state": "prepared",
            "package_id": verified.manifest["package_id"],
            "package_sha256": digest,
            "import_id": fence.import_id,
            "schema_revision": repository.runtime_info()["schema_revision"],
            "final_data": str(final_data.resolve()),
            "final_materials": str(final_materials.resolve()) if final_materials else None,
            "course_locations": locations,
            "old_execution_read_only": True,
            "include_originals": verified.manifest["include_originals"],
        }
        from learning_app.data_operations import atomic_record

        atomic_record(stage / "prepared-import.json", result)
        return result
    finally:
        repository.close()
