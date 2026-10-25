"""One consistent business cut and its immutable registered file closure.

No execution service, native home, credential store or user source folder is
opened. This is the local kernel for whole-data transfer, not an import action.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from learning_app.config import PROJECT_ROOT
from learning_app.course_files import _is_link
from learning_app.errors import AppError
from learning_app.media import _read_locked

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SCHEMAS = {"0033_document_graphics": "0033", "0034_data_import_fences": "0034"}


def stream_digest(stream, *, check=lambda: None):
    digest = hashlib.sha256()
    while data := stream.read(1024 * 1024):
        check()
        digest.update(data)
    return digest.hexdigest()


def file_digest(path, *, check=lambda: None):
    with _read_locked(path) as stream:
        return stream_digest(stream, check=check)


def _schema_identity(row):
    # Alembic's batch reflection can emit independent FK constraints in a
    # different order. Preserve every SQL token; normalize only that order.
    value = dict(row)
    if row["type"] != "table" or not row["sql"]:
        return value
    lines, foreign_keys = [], []
    for line in row["sql"].splitlines():
        part = line.strip()
        if part.startswith("FOREIGN KEY(") or re.match(
            r'CONSTRAINT [A-Za-z0-9_" ]+ FOREIGN KEY\(', part
        ):
            foreign_keys.append(part.removesuffix(","))
        else:
            lines.append(line)
    value["sql"] = (tuple(lines), tuple(sorted(foreign_keys)))
    return value


def _readonly(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


def validate_database(path):
    """Only shipped schema/control metadata; user text and raw usage stay opaque."""
    with closing(_readonly(path)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AppError("DATA_DATABASE_INTEGRITY", "数据快照完整性校验失败")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise AppError("DATA_DATABASE_RELATIONS", "数据快照存在不完整的关联")
        try:
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        except (sqlite3.DatabaseError, TypeError):
            raise AppError("DATA_SCHEMA_UNSUPPORTED", "数据包版本不可识别") from None
        suffix = _SCHEMAS.get(revision)
        if suffix is None:
            raise AppError("DATA_SCHEMA_UNSUPPORTED", "数据包版本尚不受支持，未更改当前数据")
        root = PROJECT_ROOT / "resources/data"
        expected = json.loads((root / f"schema-{suffix}.json").read_text("utf-8"))
        actual = [
            {"type": r[0], "name": r[1], "table": r[2], "sql": r[3]}
            for r in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            )
        ]
        if [_schema_identity(row) for row in actual] != [
            _schema_identity(row) for row in expected["objects"]
        ]:
            raise AppError("DATA_SCHEMA_UNEXPECTED", "数据包含未支持的表、字段或数据库逻辑")
        policy = json.loads((root / f"columns-{suffix}.json").read_text("utf-8"))
        for identity, keys in policy["json_control_top_level_keys"].items():
            table, column = identity.split(".")
            if not re.fullmatch(r"[a-z_]+", table) or not re.fullmatch(r"[a-z_]+", column):
                raise AppError("DATA_SCHEMA_POLICY_INVALID", "本机数据格式校验清单无效")
            for row in connection.execute(f'SELECT "{column}" FROM "{table}"'):
                value = json.loads(row[0]) if row[0] is not None else None
                if value is not None and (not isinstance(value, dict) or set(value) - set(keys)):
                    raise AppError(
                        "DATA_CONTROL_METADATA_UNSUPPORTED",
                        "存在未支持的执行元数据，未导出凭据或改写历史",
                    )
        return revision


@dataclass(frozen=True)
class SnapshotCut:
    directory: Path
    schema_revision: str
    files: tuple[dict, ...]
    unresolved_executions: tuple[dict, ...]

    @property
    def database(self):
        return self.directory / "business.sqlite3"


def _copy_registered(source, target, digest, expected_size=None, *, check=lambda: None):
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise AppError("DATA_ASSET_ID_INVALID", "已登记资产摘要无效")
    if not source.is_file() or any(_is_link(p) for p in [source, *source.parents]):
        raise AppError("DATA_ASSET_UNAVAILABLE", "已登记资产缺失或位置不受支持，未替换原资产")
    target.parent.mkdir(parents=True, exist_ok=True)
    checksum, size = hashlib.sha256(), 0
    with _read_locked(source) as incoming, target.open("xb") as outgoing:
        while data := incoming.read(1024 * 1024):
            check()
            checksum.update(data)
            size += len(data)
            outgoing.write(data)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if checksum.hexdigest() != digest or expected_size is not None and size != expected_size:
        raise AppError("DATA_ASSET_INTEGRITY", "已登记资产内容与快照不符，未输出不完整包")
    return {"sha256": digest, "bytes": size}


class BusinessSnapshot:
    def __init__(self, repository):
        self.repository = repository

    @contextmanager
    def capture(self, *, check=lambda: None):
        # SQLite's backup cut is consistent while writers keep running. File
        # publication always precedes DB registration; digest files are immutable.
        with tempfile.TemporaryDirectory(
            prefix="business-cut-", dir=self.repository.staging
        ) as temporary:
            root = Path(temporary)
            database = root / "business.sqlite3"
            with (
                closing(_readonly(self.repository.db_path)) as source,
                closing(sqlite3.connect(database)) as target,
            ):
                source.backup(target, pages=256, sleep=0.01, progress=lambda *_: check())
            with database.open("r+b") as stream:
                os.fsync(stream.fileno())
            revision = validate_database(database)
            self.repository._hook("data_snapshot_after_cut", snapshot=str(database))
            files = [
                {
                    "path": "business.sqlite3",
                    "sha256": file_digest(database, check=check),
                    "bytes": database.stat().st_size,
                }
            ]
            theme = self.repository.root / "desktop-ui/theme.json"
            if theme.exists():
                from learning_app.desktop_theme import DesktopThemePreferences

                preferences = DesktopThemePreferences(self.repository.root)
                with preferences.lock:
                    record = preferences._read()
                    raw = theme.read_bytes()
                    if set(json.loads(raw)) != {"version", "preference", "revision"}:
                        raise AppError(
                            "DATA_PREFERENCE_UNSUPPORTED", "外观偏好包含未支持字段，未导出未知配置"
                        )
                    if json.loads(raw) != record:
                        raise AppError("DATA_PREFERENCE_UNSUPPORTED", "外观偏好记录不一致")
                    digest = hashlib.sha256(raw).hexdigest()
                    result = _copy_registered(
                        theme, root / "desktop-ui/theme.json", digest, len(raw), check=check
                    )
                    files.append({"path": "desktop-ui/theme.json", **result})
            copied = set()
            runtime_preferences = self.repository.root / "desktop-ui/runtime.json"
            if runtime_preferences.exists():
                from learning_app.runtime_preferences import RuntimePreferences
                preferences = RuntimePreferences(self.repository.root)
                with preferences.lock:
                    preferences.read()
                    raw = runtime_preferences.read_bytes()
                    result = _copy_registered(runtime_preferences, root / "desktop-ui/runtime.json", hashlib.sha256(raw).hexdigest(), len(raw), check=check)
                    files.append({"path": "desktop-ui/runtime.json", **result})
            updates = self.repository.root / "desktop-ui/updates.json"
            if updates.exists():
                from learning_app.desktop_updates import UpdatePreferences
                raw = updates.read_bytes()
                try:
                    UpdatePreferences.model_validate_json(raw)
                except Exception:
                    raise AppError("DATA_PREFERENCE_UNSUPPORTED", "更新设置内容不受支持，未输出不完整包") from None
                result = _copy_registered(updates, root / "desktop-ui/updates.json", hashlib.sha256(raw).hexdigest(), len(raw), check=check)
                files.append({"path": "desktop-ui/updates.json", **result})
            with closing(_readonly(database)) as connection:
                for row in connection.execute("SELECT content_hash,byte_length FROM attachments"):
                    digest = row["content_hash"]
                    name = f"attachments/objects/{digest[:2]}/{digest}"
                    if name in copied:
                        continue
                    result = _copy_registered(
                        self.repository._object_path(digest),
                        root / name,
                        digest,
                        row["byte_length"],
                        check=check,
                    )
                    files.append({"path": name, **result})
                    copied.add(name)
                for row in connection.execute("SELECT reference FROM learning_snapshots"):
                    reference = json.loads(row[0])
                    if reference.get("kind") not in {"document_image", "document_region_image"}:
                        continue
                    digest = reference["image_sha256"]
                    name = f"learning-source-images/{digest}.png"
                    if name in copied:
                        continue
                    result = _copy_registered(
                        self.repository.root / name, root / name, digest, check=check
                    )
                    files.append({"path": name, **result})
                    copied.add(name)
                unresolved = [
                    {"owner_kind": "chat", "owner_id": row[0], "state": row[1]}
                    for row in connection.execute(
                        "SELECT generation_id,state FROM generations WHERE state NOT IN ('completed','failed','cancelled','truncated')"
                    )
                ]
                unresolved.extend(
                    {"owner_kind": "task", "owner_id": row[0], "state": row[1]}
                    for row in connection.execute(
                        "SELECT task_id,state FROM learning_tasks WHERE state NOT IN ('completed','partial','cancelled')"
                    )
                )
            self.repository._hook("data_snapshot_after_files", snapshot=str(database))
            check()
            yield SnapshotCut(root, revision, tuple(files), tuple(unresolved))
