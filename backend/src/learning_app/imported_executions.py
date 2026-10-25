"""Imported execution history never becomes a fresh local dispatch authority."""

import hashlib
import json
import os
from contextlib import closing
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Column, ForeignKey, MetaData, String, Table

from learning_app.data_snapshot import _readonly
from learning_app.errors import AppError

metadata = MetaData()

imports = Table(
    "data_import_events",
    metadata,
    Column("import_id", String, primary_key=True),
    Column("package_id", String, nullable=False),
    Column("package_sha256", String, nullable=False),
    Column("created_at", String, nullable=False),
)
owners = Table(
    "data_imported_owners",
    metadata,
    Column("kind", String, primary_key=True),
    Column("owner_id", String, primary_key=True),
    Column("import_id", ForeignKey(imports.c.import_id), nullable=False),
    Column("facts", JSON, nullable=False),
)


def write_marker(root, value):
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "data-import.json"
    temporary = root / (".import-" + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)


def _rows_hash(rows):
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _cached(repository, result):
    previous = getattr(repository, "_imported_executions", None)
    if previous is not None:
        previous.keys = result.keys
        previous.import_id = result.import_id
        previous.ready = True
        result = previous
    repository._imported_executions = result
    return result


def _finish_marker(repository, identity, package_id, package_sha256):
    with closing(_readonly(repository.db_path)) as connection:
        rows = [
            dict(r)
            for r in connection.execute(
                "SELECT kind,owner_id,import_id,facts FROM data_imported_owners ORDER BY kind,owner_id"
            )
        ]
    write_marker(
        repository.root,
        {
            "format": 1,
            "state": "installed",
            "import_id": identity,
            "package_id": package_id,
            "package_sha256": package_sha256,
            "fence_sha256": _rows_hash(rows),
        },
    )


class ImportedExecutions:
    def __init__(self, repository):
        self.repository = repository
        self.keys = frozenset()
        self.import_id = None
        self.ready = True
        marker = repository.root / "data-import.json"
        with closing(_readonly(repository.db_path)) as connection:
            present = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='data_import_events'"
            ).fetchone()
            records = (
                list(connection.execute("SELECT * FROM data_import_events")) if present else []
            )
            if not records and not marker.exists():
                return
            try:
                value = json.loads(marker.read_text("utf-8"))
                if value.get("format") != 1 or value.get("state") != "installed":
                    raise ValueError("pending")
                rows = [
                    dict(r)
                    for r in connection.execute(
                        "SELECT kind,owner_id,import_id,facts FROM data_imported_owners ORDER BY kind,owner_id"
                    )
                ]
                if value["fence_sha256"] != _rows_hash(rows) or not any(
                    r["import_id"] == value["import_id"] for r in records
                ):
                    raise ValueError("mismatch")
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise AppError(
                    "IMPORTED_EXECUTION_NOT_READY", "导入执行隔离尚未完成，未启动旧请求", 503
                ) from error
        self.keys = frozenset((r["kind"], r["owner_id"]) for r in rows)
        self.import_id = value["import_id"]

    def contains(self, kind, identity):
        if not self.ready:
            raise AppError(
                "IMPORTED_EXECUTION_NOT_READY", "导入执行隔离尚未完成，未启动旧请求", 503
            )
        return (kind, identity) in self.keys

    def require_fresh(self, kind, identity):
        if self.contains(kind, identity):
            raise AppError(
                "IMPORTED_EXECUTION_READ_ONLY",
                "原执行仅保留历史，不在本机重新派发；可新建请求继续学习",
            )

    @classmethod
    def install(cls, repository, package_id, package_sha256):
        """Called only on a verified private stage before any service is opened."""
        identity = "import_" + uuid4().hex
        marker = repository.root / "data-import.json"
        if marker.exists():
            try:
                value = json.loads(marker.read_text("utf-8"))
            except (ValueError, OSError) as error:
                raise AppError("IMPORTED_EXECUTION_NOT_READY", "导入隔离记录无法核对") from error
            if (
                value.get("format") != 1
                or value.get("package_id") != package_id
                or value.get("package_sha256") != package_sha256
            ):
                raise AppError(
                    "DATA_IMPORT_OPERATION_CONFLICT", "暂存数据已用于另一次导入，未覆盖原记录"
                )
            identity = value["import_id"]
            if value.get("state") == "installed":
                return _cached(repository, cls(repository))
            with closing(_readonly(repository.db_path)) as connection:
                existing = connection.execute(
                    "SELECT 1 FROM data_import_events WHERE import_id=?", (identity,)
                ).fetchone()
            if existing:
                _finish_marker(repository, identity, package_id, package_sha256)
                return _cached(repository, cls(repository))
        previous = getattr(repository, "_imported_executions", None)
        if previous is not None:
            previous.ready = False
        write_marker(
            repository.root,
            {
                "format": 1,
                "state": "installing",
                "import_id": identity,
                "package_id": package_id,
                "package_sha256": package_sha256,
            },
        )
        with repository._lock, repository.engine.begin() as connection:
            connection.execute(
                imports.insert().values(
                    import_id=identity,
                    package_id=package_id,
                    package_sha256=package_sha256,
                    created_at=datetime.now(UTC).isoformat(),
                )
            )
            selected = {}

            def add(kind, owner, facts=None):
                if owner is not None:
                    selected.setdefault((kind, owner), facts or {})

            for row in connection.exec_driver_sql("SELECT generation_id,state FROM generations"):
                add("generation", row[0], {"state": row[1]})
            for row in connection.exec_driver_sql("SELECT task_id,state FROM learning_tasks"):
                add("task", row[0], {"state": row[1]})
            for row in connection.exec_driver_sql("SELECT run_id,state FROM learning_task_runs"):
                add("run", row[0], {"state": row[1]})
            for row in connection.exec_driver_sql(
                "SELECT run_id,session_id FROM learning_native_scopes"
            ):
                add("run", row[0])
                add("session", row[1])
            for row in connection.exec_driver_sql(
                "SELECT binding_id,session_id,state,active_generation_id FROM execution_bindings"
            ):
                add("binding", row[0], {"state": row[2], "active_generation_id": row[3]})
                add("session", row[1])
            for row in connection.exec_driver_sql("SELECT job_id,state FROM learning_preparations"):
                add("preparation", row[0], {"state": row[1]})
            for row in connection.exec_driver_sql("SELECT course_id,local_path,state FROM course_roots"):
                add("course",row[0],{"local_path":row[1],"state":row[2]})
            for row in connection.exec_driver_sql("SELECT asset_id,source_version,local_path FROM source_versions"):
                add("source",row[0]+":"+row[1],{"local_path":row[2]})
            for (kind, owner), facts in selected.items():
                found = connection.execute(
                    owners.select().where(owners.c.kind == kind, owners.c.owner_id == owner)
                ).first()
                if found is None:
                    connection.execute(
                        owners.insert().values(
                            kind=kind, owner_id=owner, import_id=identity, facts=facts
                        )
                    )
            # This changes local binding usability, not historical run results.
            # Unknown generation/active IDs and effect/usage hashes stay intact.
            connection.exec_driver_sql("UPDATE execution_bindings SET state='retired'")
            connection.exec_driver_sql(
                "UPDATE learning_preparations SET state='paused',epoch=epoch+1 WHERE state IN ('queued','running')"
            )
            repository._hook("data_import_fence_before_commit", import_id=identity)
        repository._hook("data_import_fence_after_commit", import_id=identity)
        _finish_marker(repository, identity, package_id, package_sha256)
        return _cached(repository, cls(repository))


def imported_executions(repository):
    value = getattr(repository, "_imported_executions", None)
    if value is None:
        value = ImportedExecutions(repository)
        repository._imported_executions = value
    return value
