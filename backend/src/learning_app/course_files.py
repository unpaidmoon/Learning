"""User-owned course folders, with no model or automatic import capability.

Absolute paths enter only through explicit local UI actions. File reads require
an opened business source identity; models receive neither these routes nor a
filesystem tool. A durable operation record precedes every filesystem mutation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, insert, select, update
from starlette.background import BackgroundTask

from learning_app.course_files_schema import (
    course_file_entries as entries,
)
from learning_app.course_files_schema import (
    course_file_operations as operations,
)
from learning_app.course_files_schema import (
    course_roots as roots,
)
from learning_app.course_files_schema import course_visibility as visibility
from learning_app.errors import AppError
from learning_app.media import PLAYBACK_TYPES, MediaAssetResponse, MediaStore, _read_locked
from learning_app.native_files import pick_paths, read_clipboard_paths, recycle_path
from learning_app.storage import Repository
from learning_app.storage import schema as business

Viewer = Literal["video", "image", "text", "pdf", "external", "directory"]
Conflict = Literal["ask", "keep_both", "skip", "replace"]
_PICKER = threading.Lock()
_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_TEXT = {".txt", ".md", ".csv", ".tsv", ".json", ".log", ".srt", ".vtt"}
_OPERATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


class CourseFileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CourseFolderInput(CourseFileInput):
    path: str | None = Field(default=None, max_length=32760)
    title: str | None = Field(default=None, min_length=1, max_length=200)


class CourseRelocationInput(CourseFileInput):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")
    expected_root: str | None = None
    path: str | None = Field(default=None, max_length=32760)


class CourseMutationInput(CourseFileInput):
    operation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,160}$")


class CoursePathInput(CourseFileInput):
    path: str = Field(max_length=32760)


class CourseImportInput(CourseMutationInput):
    paths: list[str] | None = Field(default=None, max_length=1024)
    destination: str = Field(default="", max_length=32760)
    mode: Literal["copy"] = "copy"
    conflict: Literal["keep_both"] = "keep_both"


class CourseTransferInput(CourseMutationInput):
    paths: list[str] = Field(min_length=1, max_length=1024)
    destination: str = Field(default="", max_length=32760)
    mode: Literal["copy", "move"] = "copy"


class CourseClipboardInput(CourseMutationInput):
    destination: str = Field(default="", max_length=32760)


class CourseFolderPickResponse(BaseModel):
    path: str
    name: str


class CourseDirectoryInput(CourseMutationInput):
    path: str = Field(default="", max_length=32760)
    name: str = Field(min_length=1, max_length=200)


class CourseRenameInput(CourseMutationInput):
    path: str = Field(min_length=1, max_length=32760)
    name: str = Field(min_length=1, max_length=200)


class CourseMoveInput(CourseMutationInput):
    path: str = Field(min_length=1, max_length=32760)
    destination: str = Field(default="", max_length=32760)
    conflict: Conflict = "ask"


class CourseDeleteInput(CourseMutationInput):
    path: str = Field(min_length=1, max_length=32760)
    confirm: Literal[True]


class CourseRenameFolderInput(CourseMutationInput):
    name: str = Field(min_length=1, max_length=200)


class CourseDetachInput(CourseMutationInput):
    confirm: Literal[True]


class CourseReconcileInput(CourseFileInput):
    confirm: Literal[True]


class CourseFolderResponse(BaseModel):
    course_id: str
    title: str
    root_path: str | None
    root_status: Literal["ready", "unbound", "missing", "detached"]
    legacy_asset_count: int


class CourseFileEntry(BaseModel):
    name: str
    path: str
    kind: Literal["directory", "file", "blocked"]
    size_bytes: int | None
    viewer: Viewer
    asset_id: str | None = None
    source_version: str | None = None
    modified_at: str | None = None
    created_at: str | None = None
    platform: str | None = None


class CourseFilePropertiesResponse(CourseFileEntry):
    root_path: str


class CourseFileListResponse(CourseFolderResponse):
    path: str
    entries: list[CourseFileEntry]


class GeneratedArtifactReference(BaseModel):
    artifact_id: str
    course_id: str
    revision_id: str
    kind: str


class CourseFileOpenResponse(BaseModel):
    course_id: str
    entry: CourseFileEntry
    asset_id: str
    source_version: str
    viewer: Literal["video", "image", "text", "pdf", "external"]
    content_url: str | None = None
    video: MediaAssetResponse | None = None
    generated_artifact: GeneratedArtifactReference | None = None


class CourseImportResponse(BaseModel):
    operation_id: str
    state: Literal["completed", "conflict", "needs_review"]
    source_paths: list[str]
    entries: list[CourseFileEntry]
    conflicts: list[str]
    content_sha256: str | None = None


class CourseFileOperationResponse(BaseModel):
    operation_id: str
    state: Literal["completed", "conflict", "needs_review"]
    entry: CourseFileEntry | None = None
    root: CourseFolderResponse | None = None
    recycled: bool | None = None


class CourseOperationStatusResponse(BaseModel):
    operation_id: str
    kind: str
    state: str
    error_code: str | None = None
    result: dict | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _local_path(value: str, *, directory: bool | None = None) -> Path:
    path = Path(value)
    if not path.is_absolute() or value.startswith(("\\\\", "//")):
        raise AppError("FILE_PATH_INVALID", "请选择本机磁盘中的完整路径", 422)
    try:
        for candidate in [path, *path.parents]:
            if _is_link(candidate):
                raise AppError("FILE_LINK_DENIED", "课程资料不支持符号链接或目录联接", 422)
        result = path.resolve(strict=True)
        if directory is True and not result.is_dir():
            raise AppError("COURSE_ROOT_INVALID", "请选择真实的课程文件夹", 422)
        if directory is False and not result.is_file():
            raise AppError("FILE_PATH_INVALID", "请选择文件", 422)
        return result
    except (OSError, ValueError) as exc:
        raise AppError("FILE_NOT_FOUND", "文件或文件夹不存在或无法访问", 404) from exc


def _name(value: str) -> str:
    if (
        value != value.strip()
        or value in {".", ".."}
        or any(c in value for c in '<>:"/\\|?*')
        or value.endswith((".", " "))
        or any(ord(c) < 32 for c in value)
        or value.split(".")[0].upper()
        in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *{f"COM{i}" for i in range(1, 10)},
            *{f"LPT{i}" for i in range(1, 10)},
        }
    ):
        raise AppError("FILE_NAME_INVALID", "名称不能包含路径、保留名称或无效字符", 422)
    return value


def _viewer(path: Path) -> Viewer:
    if path.suffix.lower() == ".bili":
        return "video"
    if path.is_dir():
        return "directory"
    suffix = path.suffix.lower()
    if suffix in PLAYBACK_TYPES:
        return "video"
    if suffix in _IMAGES:
        return "image"
    if suffix in _TEXT:
        return "text"
    return "pdf" if suffix == ".pdf" else "external"


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def choose_paths(*, directory: bool) -> list[str]:
    """An explicit native dialog, never opened during startup or model work."""
    if not _PICKER.acquire(blocking=False):
        raise AppError("PICKER_ACTIVE", "文件选择窗口已打开，请先完成选择")
    try:
        return pick_paths(directory=directory)
    except AppError:
        raise
    except Exception as exc:
        raise AppError("PICKER_UNAVAILABLE", "无法打开文件夹选择窗口，请稍后重试", 503) from exc
    finally:
        _PICKER.release()


class CourseFileStore:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.engine = repository.engine
        # Long filesystem copies must not own the repository's event/stop lock.
        # Stores for the same repository share a dedicated mutation lock.
        with repository._lock:
            if not hasattr(repository, "_course_files_lock"):
                repository._course_files_lock = threading.RLock()
            self._lock = repository._course_files_lock
        self.media = MediaStore(repository)

    def folder(self, course_id: str) -> dict:
        with self.engine.connect() as conn:
            course = (
                conn.execute(
                    select(business.courses).where(business.courses.c.course_id == course_id)
                )
                .mappings()
                .first()
            )
            root = (
                conn.execute(select(roots).where(roots.c.course_id == course_id)).mappings().first()
            )
            hidden = conn.execute(
                select(visibility.c.hidden).where(visibility.c.course_id == course_id)
            ).scalar_one_or_none()
            linked = (
                conn.execute(
                    select(business.source_versions.c.local_path)
                    .join(
                        business.course_assets,
                        business.course_assets.c.asset_id == business.source_versions.c.asset_id,
                    )
                    .where(business.course_assets.c.course_id == course_id)
                )
                .scalars()
                .all()
            )
        if course is None:
            raise AppError("COURSE_NOT_FOUND", "课程不存在", 404)
        path = Path(root["local_path"]) if root else None
        status = (
            "detached"
            if hidden
            else "unbound"
            if root is None
            else "detached"
            if root["state"] == "detached"
            else "missing"
            if root["state"] == "needs_source"
            else "ready"
            if path.is_dir() and not _is_link(path)
            else "missing"
        )
        return {
            "course_id": course_id,
            "title": course["title"],
            "root_path": str(path) if path else None,
            "root_status": status,
            "legacy_asset_count": sum(
                1 for item in set(linked) if path is None or not Path(item).is_relative_to(path)
            ),
        }

    def _root(self, course_id: str) -> Path:
        folder = self.folder(course_id)
        if folder["root_status"] != "ready":
            raise AppError("COURSE_ROOT_REQUIRED", "请先为课程选择真实根文件夹；旧资料不会自动搬移")
        return _local_path(folder["root_path"], directory=True)

    def _path(self, root: Path, value: str, *, exists: bool = True) -> Path:
        normalized = value.replace("\\", "/")
        if (
            normalized.startswith("/")
            or PureWindowsPath(value).drive
            or ".." in normalized.split("/")
        ):
            raise AppError("FILE_PATH_ESCAPE", "文件操作只能位于当前课程目录内", 422)
        path = root.joinpath(*[part for part in normalized.split("/") if part and part != "."])
        try:
            for candidate in [path, *path.parents]:
                if candidate == root.parent:
                    break
                if _is_link(candidate):
                    raise AppError("FILE_LINK_DENIED", "不能访问符号链接或目录联接", 422)
            resolved = path.resolve(strict=exists)
        except (OSError, ValueError) as exc:
            raise AppError("FILE_NOT_FOUND", "文件或目录不存在", 404) from exc
        if not resolved.is_relative_to(root):
            raise AppError("FILE_PATH_ESCAPE", "文件操作只能位于当前课程目录内", 422)
        return resolved

    def _entry(self, course_id: str, root: Path, path: Path) -> dict:
        relative = path.relative_to(root).as_posix()
        blocked = _is_link(path)
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(entries).where(
                        entries.c.course_id == course_id, entries.c.relative_path == relative
                    )
                )
                .mappings()
                .first()
            )
        stat = None if blocked else path.stat()
        host = getattr(self.repository, "integrations", None)
        remote = row and host and host.source(row["asset_id"], row["source_version"], course_id)
        return {
            "name": path.name,
            "path": relative,
            "kind": "blocked" if blocked else "directory" if path.is_dir() else "file",
            "size_bytes": None if blocked or path.is_dir() else path.stat().st_size,
            "viewer": "external" if blocked else "video" if remote else _viewer(path),
            "platform": "bilibili" if remote else None,
            "asset_id": row["asset_id"] if row else None,
            "source_version": row["source_version"] if row else None,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat() if stat else None,
            "created_at": datetime.fromtimestamp(stat.st_birthtime, UTC).isoformat()
            if stat and hasattr(stat, "st_birthtime")
            else None,
        }

    def bind(self, course_id: str | None, value: str, title: str | None = None) -> dict:
        path = _local_path(value, directory=True)
        if title is not None:
            title = title.strip()
            if not title or any(ord(c) < 32 for c in title):
                raise AppError("COURSE_NAME_INVALID", "请输入有效的课程名称", 422)
        if not path.name or path == Path(path.anchor):
            raise AppError("COURSE_ROOT_INVALID", "请选择磁盘下的课程文件夹，不能选择整个磁盘", 422)
        with self._lock:
            with self.engine.connect() as conn:
                existing = (
                    conn.execute(select(roots).where(roots.c.local_path == str(path)))
                    .mappings()
                    .first()
                )
                other_paths = conn.execute(select(roots.c.local_path)).scalars().all()
            if existing:
                if course_id is None or existing["course_id"] == course_id:
                    course_id = existing["course_id"]
                    from learning_app.workspaces import UNCLASSIFIED

                    if course_id == UNCLASSIFIED and title not in {None, "未分类"}:
                        raise AppError("WORKSPACE_SYSTEM_PROTECTED", "未分类是默认保存工作区，不能重命名", 403)
                    with self.engine.begin() as conn:
                        conn.execute(
                            update(roots)
                            .where(roots.c.course_id == course_id)
                            .values(state="active")
                        )
                        conn.execute(
                            update(visibility)
                            .where(visibility.c.course_id == course_id)
                            .values(hidden=False)
                        )
                        if title is not None:
                            conn.execute(
                                update(business.courses)
                                .where(business.courses.c.course_id == course_id)
                                .values(title=title)
                            )
                    return self.folder(course_id)
                raise AppError("COURSE_ROOT_IN_USE", "此根文件夹已属于另一门课程")
            if any(
                path.is_relative_to(Path(other)) or Path(other).is_relative_to(path)
                for other in other_paths
            ):
                raise AppError("COURSE_ROOT_OVERLAP", "课程根目录不能相互包含，请选择独立文件夹")
            if course_id is None:
                course_id = self.repository.create_course(title or path.name)["course_id"]
            elif self.folder(course_id)["root_path"] is not None:
                raise AppError(
                    "COURSE_ROOT_ALREADY_BOUND", "课程已有根目录，请使用重命名或显式资料迁移"
                )
            with self.engine.begin() as conn:
                conn.execute(
                    insert(roots).values(
                        course_id=course_id, local_path=str(path), state="active", created_at=_now()
                    )
                )
                conn.execute(
                    update(visibility)
                    .where(visibility.c.course_id == course_id)
                    .values(hidden=False)
                )
                if title is not None:
                    conn.execute(
                        update(business.courses)
                        .where(business.courses.c.course_id == course_id)
                        .values(title=title)
                    )
        previews = getattr(self.repository, "video_previews", None)
        if previews is not None:
            previews.enqueue_course(course_id)
        return self.folder(course_id)

    def relocate_root(self, course_id, value, *, expected_root, operation_id):
        """User-selected association only; never move/delete originals or change IDs."""
        payload = {"old_root": expected_root, "new_path": value}
        replay = self._prior(course_id, "relocate_root", payload, operation_id)
        if replay is not None:
            return replay
        new_root = _local_path(value, directory=True)
        if not new_root.name or new_root == Path(new_root.anchor):
            raise AppError("COURSE_ROOT_INVALID", "请选择独立课程目录")
        with self._lock:
            folder = self.folder(course_id)
            if folder["root_path"] != expected_root:
                raise AppError("COURSE_ROOT_CHANGED", "课程位置已变化，请重新核对")
            with self.engine.connect() as connection:
                others = (
                    connection.execute(
                        select(roots.c.local_path).where(roots.c.course_id != course_id)
                    )
                    .scalars()
                    .all()
                )
                registered = [
                    dict(r)
                    for r in connection.execute(
                        select(business.source_versions)
                        .join(
                            business.course_assets,
                            business.course_assets.c.asset_id
                            == business.source_versions.c.asset_id,
                        )
                        .where(business.course_assets.c.course_id == course_id)
                    ).mappings()
                ]
                known = [
                    dict(r)
                    for r in connection.execute(
                        select(entries).where(entries.c.course_id == course_id)
                    ).mappings()
                ]
                old_locations = {}
                if connection.exec_driver_sql(
                    "SELECT 1 FROM sqlite_master WHERE name='data_imported_owners'"
                ).first():
                    for row in connection.exec_driver_sql(
                        "SELECT owner_id,facts FROM data_imported_owners WHERE kind='source'"
                    ):
                        old_locations[row[0]] = json.loads(row[1]).get("local_path")
            if any(
                new_root.is_relative_to(Path(other)) or Path(other).is_relative_to(new_root)
                for other in others
            ):
                raise AppError("COURSE_ROOT_OVERLAP", "课程目录不能相互包含")
            op_id, _ = self._begin(course_id, "relocate_root", payload, operation_id)
            matches = []
            missing = []
            checked = {}
            for row in registered:
                relative = [
                    entry["relative_path"]
                    for entry in known
                    if entry["asset_id"] == row["asset_id"]
                ]
                previous = old_locations.get(
                    row["asset_id"] + ":" + row["source_version"], row["local_path"]
                )
                if not relative:
                    relative = [PureWindowsPath(previous).name]
                selected = None
                for name in relative:
                    path = self._path(new_root, name, exists=False)
                    if not path.is_file():
                        continue
                    if path not in checked:
                        with _read_locked(path) as stream:
                            signature = (os.fstat(stream.fileno()).st_size, path.stat().st_mtime_ns)
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        checked[path] = (signature, digest)
                    if (
                        checked[path][1] == row["content_hash"]
                        and checked[path][0][0] == row["byte_length"]
                    ):
                        selected = path
                        break
                if selected is None:
                    missing.append(
                        {
                            "asset_id": row["asset_id"],
                            "source_version": row["source_version"],
                            "status": "原版本缺失或内容不符",
                        }
                    )
                else:
                    matches.append((row, selected))
            self.repository._hook("course_root_relocation_after_check", operation_id=op_id)
            with self.engine.begin() as connection:
                current = connection.execute(
                    select(roots.c.local_path).where(roots.c.course_id == course_id)
                ).scalar_one_or_none()
                if current != expected_root:
                    raise AppError("COURSE_ROOT_CHANGED", "课程关联已改变，未覆盖新位置")
                for row, path in matches:
                    stat = path.stat()
                    if (stat.st_size, stat.st_mtime_ns) != checked[path][0]:
                        raise AppError(
                            "SOURCE_VERSION_UNAVAILABLE", "核对后原件又发生变化，未更新关联"
                        )
                    connection.execute(
                        update(business.source_versions)
                        .where(
                            business.source_versions.c.asset_id == row["asset_id"],
                            business.source_versions.c.source_version == row["source_version"],
                        )
                        .values(local_path=str(path))
                    )
                if current is None:
                    connection.execute(
                        insert(roots).values(
                            course_id=course_id,
                            local_path=str(new_root),
                            state="active",
                            created_at=_now(),
                        )
                    )
                else:
                    connection.execute(
                        update(roots)
                        .where(roots.c.course_id == course_id)
                        .values(local_path=str(new_root), state="active")
                    )
                result = {
                    "operation_id": op_id,
                    "state": "completed",
                    "course_id": course_id,
                    "root_path": str(new_root),
                    "matched_versions": len(matches),
                    "unavailable_versions": missing,
                }
                connection.execute(
                    update(operations)
                    .where(operations.c.operation_id == op_id)
                    .values(state="completed", result=result, updated_at=_now())
                )
                self.repository._hook("course_root_relocation_before_commit", operation_id=op_id)
            self.repository._hook("course_root_relocation_after_commit", operation_id=op_id)
            return result

    def list(self, course_id: str, value: str = "") -> dict:
        folder = self.folder(course_id)
        previews = getattr(self.repository, "video_previews", None)
        if previews is not None and not value:
            previews.enqueue_course(course_id)
        if folder["root_status"] != "ready":
            return {**folder, "path": value, "entries": []}
        root = self._root(course_id)
        path = self._path(root, value)
        if not path.is_dir():
            raise AppError("FILE_DIRECTORY_REQUIRED", "请选择文件夹", 422)
        children = [self._entry(course_id, root, child) for child in path.iterdir()]
        children.sort(
            key=lambda item: (
                item["kind"] != "directory",
                [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", item["name"])],
            )
        )
        return {
            **folder,
            "path": path.relative_to(root).as_posix().replace(".", "", 1)
            if path == root
            else path.relative_to(root).as_posix(),
            "entries": children,
        }

    def _begin(
        self, course_id: str, kind: str, payload: dict, operation_id: str | None
    ) -> tuple[str, dict | None]:
        op_id = operation_id or f"fileop_{uuid4().hex}"
        if not _OPERATION_ID.fullmatch(op_id):
            raise AppError("FILE_OPERATION_ID_INVALID", "操作身份无效", 422)
        folder = self.folder(course_id)
        root = Path(folder["root_path"]) if folder["root_path"] else None
        planned = []
        if root is not None and kind != "import":
            source = root / payload.get("path", "")
            if kind == "rename":
                target = source.with_name(payload["name"])
            elif kind == "move":
                target = root / payload["destination"] / source.name
            elif kind == "directory":
                target = source / payload["name"]
            elif kind == "rename_course":
                target = None  # Display alias only, never a filesystem rename.
            elif kind == "relocate_root":
                target = Path(payload["new_path"])
            else:
                target = None
            planned = [
                {
                    "source": str(source),
                    "target": str(target)
                    if target
                    else "移入 Windows 回收站"
                    if kind == "delete"
                    else "仅修改课程显示名"
                    if kind == "rename_course"
                    else "移除课程关联（保留原件）",
                }
            ]
        with self.engine.begin() as conn:
            previous = (
                conn.execute(select(operations).where(operations.c.operation_id == op_id))
                .mappings()
                .first()
            )
            if previous:
                if (
                    previous["course_id"] != course_id
                    or previous["kind"] != kind
                    or previous["payload"] != payload
                ):
                    raise AppError("FILE_OPERATION_CONFLICT", "同一操作身份的内容发生变化")
                if previous["state"] in {"completed", "conflict"}:
                    return op_id, previous["result"]
                raise AppError(
                    "FILE_OPERATION_UNKNOWN", "上次操作结果需要对账，请查看操作记录，不要重复提交"
                )
            pending = conn.execute(
                select(operations.c.operation_id).where(
                    operations.c.course_id == course_id,
                    operations.c.state.in_(["prepared", "filesystem_done", "needs_review"]),
                )
            ).first()
            if pending:
                raise AppError(
                    "FILE_OPERATION_PENDING", "此课程有待对账的文件操作，请先检查操作记录"
                )
            conn.execute(
                insert(operations).values(
                    operation_id=op_id,
                    course_id=course_id,
                    kind=kind,
                    state="prepared",
                    payload=payload,
                    result={"planned_paths": planned},
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
        self.repository._hook("course_file_prepared", operation_id=op_id, kind=kind)
        return op_id, None

    def _prior(
        self, course_id: str, kind: str, payload: dict, operation_id: str | None
    ) -> dict | None:
        """Replays are resolved before looking for an already moved/deleted path."""
        if operation_id is None:
            return None
        with self.engine.connect() as conn:
            previous = (
                conn.execute(select(operations).where(operations.c.operation_id == operation_id))
                .mappings()
                .first()
            )
        if previous is None:
            return None
        if (
            previous["course_id"] != course_id
            or previous["kind"] != kind
            or previous["payload"] != payload
        ):
            raise AppError("FILE_OPERATION_CONFLICT", "同一操作身份的内容发生变化")
        if previous["state"] not in {"completed", "conflict"}:
            raise AppError("FILE_OPERATION_UNKNOWN", "上次操作需要对账，请查看记录，不要重复提交")
        return previous["result"]

    def _checkpoint(
        self, op_id: str, state: str, *, result: dict | None = None, error: str | None = None
    ) -> None:
        values = {"state": state, "error_code": error, "updated_at": _now()}
        if result is not None:
            values["result"] = result
        with self.engine.begin() as conn:
            conn.execute(
                update(operations).where(operations.c.operation_id == op_id).values(**values)
            )

    def operation(self, course_id: str, operation_id: str) -> dict:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    select(operations).where(
                        operations.c.course_id == course_id,
                        operations.c.operation_id == operation_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AppError("FILE_OPERATION_NOT_FOUND", "未找到此文件操作记录", 404)
        return {key: row[key] for key in ("operation_id", "kind", "state", "error_code", "result")}

    def _finish(self, op_id: str, result: dict) -> dict:
        self.repository._hook("course_file_before_complete", operation_id=op_id)
        self._checkpoint(op_id, result["state"], result=result)
        return result

    def reconcile(self, course_id: str, operation_id: str) -> dict:
        """User acknowledges inspected paths; an unknown operation is never redone."""
        with self._lock:
            status = self.operation(course_id, operation_id)
            if status["state"] in {"prepared", "filesystem_done", "needs_review", "failed"}:
                self._checkpoint(operation_id, "reviewed", error=status["error_code"])
            return self.operation(course_id, operation_id)

    def _relocate_registered(self, source: Path, target: Path) -> None:
        digest = _digest(target)
        with self.engine.begin() as conn:
            rows = (
                conn.execute(
                    select(business.source_versions.c.asset_id).where(
                        business.source_versions.c.local_path == str(source),
                        business.source_versions.c.content_hash == digest,
                    )
                )
                .scalars()
                .all()
            )
            conn.execute(
                update(business.source_versions)
                .where(
                    business.source_versions.c.local_path == str(source),
                    business.source_versions.c.content_hash == digest,
                )
                .values(local_path=str(target))
            )
            if rows:
                conn.execute(
                    update(business.assets)
                    .where(business.assets.c.asset_id.in_(rows))
                    .values(display_name=target.name)
                )

    def _changed_paths(self, course_id: str, old: Path, new: Path, root: Path) -> None:
        old_relative = old.relative_to(root).as_posix()
        with self.engine.begin() as conn:
            rows = (
                conn.execute(select(entries).where(entries.c.course_id == course_id))
                .mappings()
                .all()
            )
            sources = (
                conn.execute(
                    select(business.source_versions)
                    .join(
                        business.course_assets,
                        business.course_assets.c.asset_id == business.source_versions.c.asset_id,
                    )
                    .where(business.course_assets.c.course_id == course_id)
                )
                .mappings()
                .all()
            )
            for row in rows:
                relative = row["relative_path"]
                if relative != old_relative and not relative.startswith(old_relative + "/"):
                    continue
                source = root / relative
                destination = new / source.relative_to(old) if source != old else new
                conn.execute(
                    update(entries)
                    .where(entries.c.course_id == course_id, entries.c.relative_path == relative)
                    .values(relative_path=destination.relative_to(root).as_posix())
                )
            # Include legacy media that was already inside the bound root but
            # has not yet been opened through the new file explorer.
            for row in sources:
                source = Path(row["local_path"])
                if source != old and not source.is_relative_to(old):
                    continue
                destination = new / source.relative_to(old) if source != old else new
                conn.execute(
                    update(business.source_versions)
                    .where(
                        business.source_versions.c.asset_id == row["asset_id"],
                        business.source_versions.c.source_version == row["source_version"],
                        business.source_versions.c.local_path == str(source),
                    )
                    .values(local_path=str(destination))
                )
                conn.execute(
                    update(business.assets)
                    .where(business.assets.c.asset_id == row["asset_id"])
                    .values(display_name=destination.name)
                )

    def _register(self, course_id: str, root: Path, path: Path) -> dict:
        relative = path.relative_to(root).as_posix()
        with self.engine.connect() as conn:
            known = (
                conn.execute(
                    select(entries).where(
                        entries.c.course_id == course_id, entries.c.relative_path == relative
                    )
                )
                .mappings()
                .first()
            )
            legacy = (
                conn.execute(
                    select(business.source_versions)
                    .where(business.source_versions.c.local_path == str(path))
                    .order_by(business.source_versions.c.created_at.desc())
                )
                .mappings()
                .first()
            )
        if known:
            source = self.repository.add_source_version(known["asset_id"], path)
        elif legacy:
            source = self.repository.add_source_version(legacy["asset_id"], path)
            self.repository.associate_source(course_id, source["asset_id"])
        else:
            source = self.repository.register_source(course_id, path)
        with self.engine.begin() as conn:
            if known:
                conn.execute(
                    update(entries)
                    .where(entries.c.course_id == course_id, entries.c.relative_path == relative)
                    .values(source_version=source["source_version"])
                )
            else:
                conn.execute(
                    insert(entries).values(
                        course_id=course_id,
                        relative_path=relative,
                        asset_id=source["asset_id"],
                        source_version=source["source_version"],
                    )
                )
        return source

    def open(self, course_id: str, value: str) -> dict:
        root = self._root(course_id)
        path = self._path(root, value)
        if not path.is_file():
            raise AppError("FILE_REQUIRED", "请选择文件", 422)
        with self._lock:
            source = self._register(course_id, root, path)
            host = getattr(self.repository, "integrations", None)
            remote = host and host.source(source["asset_id"], source["source_version"], course_id)
            viewer = "video" if remote else _viewer(path)
            video = (self.media.attach_remote_source(source) if remote else
                     self.media.attach_source(source, path, open_for_playback=True)) if viewer == "video" else None
            if video and remote:
                video = self.media.open(source["asset_id"], source["source_version"])
            from learning_app.workbench import schema as workbench_schema
            from learning_app.workbench.artifact_files import generated_files

            with self.engine.connect() as conn:
                generated = conn.execute(select(
                    generated_files.c.artifact_id, generated_files.c.revision_id,
                    workbench_schema.artifacts.c.course_id, workbench_schema.artifacts.c.kind,
                ).join(workbench_schema.artifacts,
                       workbench_schema.artifacts.c.artifact_id == generated_files.c.artifact_id)
                    .where(generated_files.c.asset_id == source["asset_id"],
                           generated_files.c.source_version == source["source_version"])
                ).mappings().first()
            return {
                "course_id": course_id,
                "entry": self._entry(course_id, root, path),
                "asset_id": source["asset_id"],
                "source_version": source["source_version"],
                "viewer": viewer,
                "video": video,
                "generated_artifact": dict(generated) if generated else None,
                "content_url": f"/api/v1/videos/{source['asset_id']}/content?source_version={source['source_version']}"
                if viewer == "video"
                else f"/api/v1/course-files/{source['asset_id']}/content?source_version={source['source_version']}"
                if viewer != "external"
                else None,
            }

    def _unique(self, target: Path, reserved: set[Path]) -> Path:
        index = 2
        candidate = target
        while candidate.exists() or candidate in reserved:
            candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
            index += 1
        return candidate

    def _tree_safe(self, path: Path) -> None:
        """A copied directory cannot acquire data by following filesystem links."""
        _local_path(str(path))
        if path.is_dir():
            for parent, directories, files in os.walk(path, followlinks=False):
                for name in directories + files:
                    item = Path(parent) / name
                    if _is_link(item):
                        raise AppError("FILE_LINK_DENIED", "文件夹中含链接，不能复制或移动", 422)

    def _copy_file(self, source: Path, target: Path) -> str:
        source, target = Path(source), Path(target)
        _local_path(str(source), directory=False)
        with _read_locked(source) as original, target.open("xb") as copied:
            shutil.copyfileobj(original, copied, length=1024 * 1024)
            copied.flush()
            os.fsync(copied.fileno())
        shutil.copystat(source, target, follow_symlinks=False)
        return str(target)

    def _copy_many(
        self,
        course_id: str,
        sources: list[Path],
        destination_value: str,
        operation_id: str | None,
        *,
        kind: str,
        payload: dict,
        mode: str = "copy",
    ) -> dict:
        with self._lock:
            previous = self._prior(course_id, kind, payload, operation_id)
            if previous is not None:
                return previous
            root = self._root(course_id)
            destination = self._path(root, destination_value)
            if not destination.is_dir():
                raise AppError("FILE_DIRECTORY_REQUIRED", "复制目标必须是文件夹", 422)
            selected: list[Path] = []
            for source in sorted(dict.fromkeys(sources), key=lambda item: len(item.parts)):
                if any(parent.is_dir() and source.is_relative_to(parent) for parent in selected):
                    continue
                self._tree_safe(source)
                if source.is_dir() and destination.is_relative_to(source):
                    raise AppError("FILE_MOVE_INVALID", "不能将文件夹放入自身或其子目录", 422)
                selected.append(source)
            reserved: set[Path] = set()
            plans = []
            for source in selected:
                target = destination / source.name
                if mode == "move" and source == target:
                    plans.append((source, target))
                    continue
                # Existing links are refused rather than treated as an available
                # name; normal conflicts always receive a fresh sibling name.
                self._path(root, target.relative_to(root).as_posix(), exists=False)
                if _is_link(target):
                    raise AppError("FILE_LINK_DENIED", "不能复制到链接目标", 422)
                target = self._unique(target, reserved)
                reserved.add(target)
                plans.append((source, target))
            op_id, previous = self._begin(course_id, kind, payload, operation_id)
            if previous is not None:
                return previous
            self._checkpoint(
                op_id,
                "prepared",
                result={
                    "planned_paths": [
                        {"source": str(source), "target": str(target)} for source, target in plans
                    ]
                },
            )
            touched = False
            try:
                result_entries = []
                for source, target in plans:
                    if source != target:
                        self._path(root, target.relative_to(root).as_posix(), exists=False)
                        if target.exists() or _is_link(target):
                            raise AppError("FILE_CONFLICT", "目标在操作期间变化，请先核对", 409)
                        touched = True
                        if mode == "move":
                            source.rename(target)
                        elif source.is_dir():
                            shutil.copytree(source, target, copy_function=self._copy_file)
                        else:
                            self._copy_file(source, target)
                        self.repository._hook(
                            "course_file_after_filesystem",
                            operation_id=op_id,
                            source=str(source),
                            target=str(target),
                        )
                        self._checkpoint(op_id, "filesystem_done")
                        if mode == "move":
                            self._changed_paths(course_id, source, target, root)
                    if target.is_file():
                        self._register(course_id, root, target)
                    result_entries.append(self._entry(course_id, root, target))
                return self._finish(
                    op_id,
                    {
                        "operation_id": op_id,
                        "state": "completed",
                        "source_paths": [str(p) for p in selected],
                        "entries": result_entries,
                        "conflicts": [],
                    },
                )
            except Exception as exc:
                code = exc.code if isinstance(exc, AppError) else "FILE_OPERATION_FAILED"
                self._checkpoint(op_id, "needs_review" if touched else "failed", error=code)
                if isinstance(exc, AppError):
                    raise
                raise AppError(code, "文件操作结果需要核对，未自动重试", 409) from exc

    def import_files(self, course_id: str, value: CourseImportInput) -> dict:
        if value.mode != "copy" or value.conflict != "keep_both":
            raise AppError("EXTERNAL_COPY_ONLY", "外部资料只支持复制并自动保留同名文件", 422)
        selected = value.paths if value.paths is not None else choose_paths(directory=False)
        payload = {
            "paths": [str(Path(path).absolute()) for path in dict.fromkeys(selected)],
            "destination": value.destination,
            "mode": "copy",
            "conflict": "keep_both",
        }
        previous = self._prior(course_id, "import", payload, value.operation_id)
        if previous is not None:
            return previous
        sources = [_local_path(path) for path in payload["paths"]]
        return self._copy_many(
            course_id,
            sources,
            value.destination,
            value.operation_id,
            kind="import",
            payload=payload,
        )

    def transfer(self, course_id: str, value: CourseTransferInput) -> dict:
        payload = {
            "paths": list(dict.fromkeys(value.paths)),
            "destination": value.destination,
            "mode": value.mode,
        }
        previous = self._prior(course_id, "transfer", payload, value.operation_id)
        if previous is not None:
            return previous
        root = self._root(course_id)
        sources = [self._path(root, path) for path in payload["paths"]]
        if root in sources:
            raise AppError("FILE_MOVE_INVALID", "不能复制或移动课程根目录", 422)
        return self._copy_many(
            course_id,
            sources,
            value.destination,
            value.operation_id,
            kind="transfer",
            payload=payload,
            mode=value.mode,
        )

    def clipboard(self, course_id: str, value: CourseClipboardInput) -> dict:
        return self.import_files(
            course_id,
            CourseImportInput(
                paths=read_clipboard_paths(),
                destination=value.destination,
                operation_id=value.operation_id,
            ),
        )

    def properties(self, course_id: str, value: str) -> dict:
        root = self._root(course_id)
        path = self._path(root, value)
        return {**self._entry(course_id, root, path), "root_path": str(root)}

    async def upload(
        self,
        course_id: str,
        path_value: str,
        destination_value: str,
        operation_id: str,
        request: Request,
    ) -> dict:
        """Stream browser-owned File bytes; no source file path is fabricated."""
        maximum = 8 * 1024 * 1024 * 1024
        raw_length = request.headers.get("content-length")
        try:
            length = int(raw_length) if raw_length is not None else None
        except ValueError as exc:
            raise AppError("FILE_UPLOAD_INVALID", "文件长度格式无效", 422) from exc
        if length is not None and (length < 0 or length > maximum):
            raise AppError("FILE_UPLOAD_TOO_LARGE", "单个文件超过 8 GiB 上传上限", 413)
        root = self._root(course_id)
        destination = self._path(root, destination_value)
        if not destination.is_dir():
            raise AppError("FILE_DIRECTORY_REQUIRED", "复制目标必须是文件夹", 422)
        proposed = self._path(root, (Path(destination_value) / path_value).as_posix(), exists=False)
        relative = proposed.relative_to(destination)
        if not relative.parts:
            raise AppError("FILE_NAME_INVALID", "请选择文件", 422)
        for part in relative.parts:
            _name(part)
        payload = {"path": path_value, "destination": destination_value, "declared_bytes": length}
        with self._lock:
            previous = self._prior(course_id, "upload", payload, operation_id)
            if previous is None:
                op_id, _ = self._begin(course_id, "upload", payload, operation_id)
            else:
                op_id = operation_id
        staging = self.repository.root / "file-transfers"
        staging.mkdir(exist_ok=True)
        temporary = staging / f"{op_id}-{uuid4().hex}.partial"
        total, digest = 0, hashlib.sha256()
        touched = False
        try:
            with temporary.open("xb") as output:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > maximum:
                        raise AppError("FILE_UPLOAD_TOO_LARGE", "单个文件超过 8 GiB 上传上限", 413)
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
            if length is not None and total != length:
                raise AppError("FILE_UPLOAD_INCOMPLETE", "文件未完整传入，请先核对操作状态", 409)
            if previous is not None:
                if previous.get("content_sha256") != digest.hexdigest():
                    raise AppError("FILE_OPERATION_CONFLICT", "同一操作身份的文件内容发生变化", 409)
                return previous

            def publish():
                nonlocal touched
                with self._lock:
                    self._path(root, proposed.relative_to(root).as_posix(), exists=False)
                    parent = proposed.parent
                    if parent.exists() and not parent.is_dir():
                        raise AppError("FILE_DIRECTORY_REQUIRED", "目标父路径不是文件夹", 422)
                    # Every missing parent is rooted/checked above. Never substitute
                    # a filename containing traversal or an absolute Windows path.
                    touched = not parent.exists()
                    parent.mkdir(parents=True, exist_ok=True)
                    self._path(root, parent.relative_to(root).as_posix())
                    target = self._unique(proposed, set())
                    self._checkpoint(
                        op_id,
                        "prepared",
                        result={
                            "planned_paths": [
                                {"source": "浏览器用户选取文件", "target": str(target)}
                            ]
                        },
                    )
                    touched = True
                    self._copy_file(temporary, target)
                    self.repository._hook(
                        "course_file_after_filesystem",
                        operation_id=op_id,
                        source="browser-upload",
                        target=str(target),
                    )
                    self._checkpoint(op_id, "filesystem_done")
                    self._register(course_id, root, target)
                    return self._finish(
                        op_id,
                        {
                            "operation_id": op_id,
                            "state": "completed",
                            "source_paths": [],
                            "entries": [self._entry(course_id, root, target)],
                            "conflicts": [],
                            "content_sha256": digest.hexdigest(),
                        },
                    )

            return await asyncio.to_thread(publish)
        except Exception as exc:
            if previous is None:
                self._checkpoint(
                    op_id,
                    "needs_review" if touched else "failed",
                    error=exc.code if isinstance(exc, AppError) else "FILE_UPLOAD_FAILED",
                )
            if isinstance(exc, AppError):
                raise
            raise AppError("FILE_UPLOAD_FAILED", "文件复制未完成，请先核对操作状态", 409) from exc
        finally:
            # This is our private temporary upload, never a course/user original.
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A cancelled HTTP observer may leave a still-owned publish
                # worker reading this file. Preserve it rather than masking the
                # original result or touching any course file.
                pass

    def directory(self, course_id: str, value: CourseDirectoryInput) -> dict:
        with self._lock:
            root = self._root(course_id)
            target = self._path(root, value.path) / _name(value.name)
            op_id, previous = self._begin(
                course_id, "directory", {"path": value.path, "name": value.name}, value.operation_id
            )
            if previous is not None:
                return previous
            try:
                target.mkdir()
                self._checkpoint(op_id, "filesystem_done")
                return self._finish(
                    op_id,
                    {
                        "operation_id": op_id,
                        "state": "completed",
                        "entry": self._entry(course_id, root, target),
                    },
                )
            except OSError as exc:
                self._checkpoint(
                    op_id,
                    "failed",
                    error="FILE_CONFLICT" if target.exists() else "FILE_OPERATION_FAILED",
                )
                raise AppError(
                    "FILE_CONFLICT" if target.exists() else "FILE_OPERATION_FAILED",
                    "文件夹未创建，请检查名称和目录权限",
                ) from exc

    def rename(self, course_id: str, value: CourseRenameInput) -> dict:
        payload = {"path": value.path, "name": value.name}
        previous = self._prior(course_id, "rename", payload, value.operation_id)
        if previous is not None:
            return previous
        root = self._root(course_id)
        source = self._path(root, value.path)
        target = source.with_name(_name(value.name))
        return self._move(
            course_id,
            source,
            target,
            "rename",
            {"path": value.path, "name": value.name},
            value.operation_id,
            "ask",
        )

    def move(self, course_id: str, value: CourseMoveInput) -> dict:
        payload = {"path": value.path, "destination": value.destination, "conflict": value.conflict}
        previous = self._prior(course_id, "move", payload, value.operation_id)
        if previous is not None:
            return previous
        root = self._root(course_id)
        source = self._path(root, value.path)
        destination = self._path(root, value.destination)
        if not destination.is_dir():
            raise AppError("FILE_DIRECTORY_REQUIRED", "移动目标必须是文件夹", 422)
        return self._move(
            course_id,
            source,
            destination / source.name,
            "move",
            {"path": value.path, "destination": value.destination, "conflict": value.conflict},
            value.operation_id,
            value.conflict,
        )

    def _move(
        self,
        course_id: str,
        source: Path,
        target: Path,
        kind: str,
        payload: dict,
        operation_id: str | None,
        conflict: Conflict,
    ) -> dict:
        with self._lock:
            root = self._root(course_id)
            if source == root or (source.is_dir() and target.is_relative_to(source)):
                raise AppError("FILE_MOVE_INVALID", "不能移动根目录或移入自身子目录", 422)
            op_id, previous = self._begin(course_id, kind, payload, operation_id)
            if previous is not None:
                return previous
            if source == target:
                return self._finish(
                    op_id,
                    {
                        "operation_id": op_id,
                        "state": "completed",
                        "entry": self._entry(course_id, root, source),
                    },
                )
            existed = target.exists()
            if _is_link(target):
                self._checkpoint(op_id, "failed", error="FILE_LINK_DENIED")
                raise AppError("FILE_LINK_DENIED", "不能替换链接目标", 422)
            if existed:
                if _is_link(target):
                    self._checkpoint(op_id, "failed", error="FILE_LINK_DENIED")
                    raise AppError("FILE_LINK_DENIED", "不能替换链接目标", 422)
                if conflict == "ask":
                    return self._finish(op_id, {"operation_id": op_id, "state": "conflict"})
                if conflict == "skip":
                    return self._finish(
                        op_id,
                        {
                            "operation_id": op_id,
                            "state": "completed",
                            "entry": self._entry(course_id, root, source),
                        },
                    )
                if conflict == "keep_both":
                    target = self._unique(target, set())
                    existed = False
                elif source.is_dir() or target.is_dir():
                    self._checkpoint(op_id, "failed", error="FILE_TARGET_INVALID")
                    raise AppError(
                        "FILE_TARGET_INVALID", "文件夹冲突不支持直接替换，请保留两份或跳过"
                    )
            changed = False
            try:
                # Existing entry at a replaced path keeps its own historic asset.
                # Remove only the path mapping before adopting the moved identity.
                os.replace(source, target) if existed else source.rename(target)
                changed = True
                self.repository._hook(
                    "course_file_after_filesystem",
                    operation_id=op_id,
                    source=str(source),
                    target=str(target),
                )
                self._checkpoint(op_id, "filesystem_done")
                if existed:
                    with self.engine.begin() as conn:
                        conn.execute(
                            delete(entries).where(
                                entries.c.course_id == course_id,
                                entries.c.relative_path == target.relative_to(root).as_posix(),
                            )
                        )
                self._changed_paths(course_id, source, target, root)
                return self._finish(
                    op_id,
                    {
                        "operation_id": op_id,
                        "state": "completed",
                        "entry": self._entry(course_id, root, target),
                    },
                )
            except Exception as exc:
                review = existed and changed
                if changed and not existed:
                    try:
                        target.rename(source)
                        self._changed_paths(course_id, target, source, root)
                    except (OSError, ValueError):
                        review = True
                self._checkpoint(
                    op_id, "needs_review" if review else "failed", error="FILE_OPERATION_FAILED"
                )
                raise AppError(
                    "FILE_OPERATION_FAILED", "整理未完成，已保存操作记录，请先对账"
                ) from exc

    def delete(self, course_id: str, value: CourseDeleteInput) -> dict:
        with self._lock:
            payload = {"path": value.path, "confirm": value.confirm}
            previous = self._prior(course_id, "delete", payload, value.operation_id)
            if previous is not None:
                return previous
            root = self._root(course_id)
            path = self._path(root, value.path)
            if path == root:
                raise AppError("COURSE_ROOT_DELETE_DENIED", "不能将课程根目录移入回收站", 422)
            self._tree_safe(path)
            op_id, previous = self._begin(course_id, "delete", payload, value.operation_id)
            if previous is not None:
                return previous
            try:
                recycle_path(path)
                self.repository._hook(
                    "course_file_after_filesystem",
                    operation_id=op_id,
                    source=str(path),
                    target="Windows Recycle Bin",
                )
                self._checkpoint(op_id, "filesystem_done")
                relative = path.relative_to(root).as_posix()
                with self.engine.begin() as conn:
                    conn.execute(
                        delete(entries).where(
                            entries.c.course_id == course_id,
                            (entries.c.relative_path == relative)
                            | entries.c.relative_path.startswith(relative + "/", autoescape=True),
                        )
                    )
                return self._finish(
                    op_id, {"operation_id": op_id, "state": "completed", "recycled": True}
                )
            except Exception as exc:
                code = exc.code if isinstance(exc, AppError) else "FILE_RECYCLE_FAILED"
                self._checkpoint(
                    op_id, "needs_review" if not path.exists() else "failed", error=code
                )
                if isinstance(exc, AppError):
                    raise
                raise AppError(
                    "FILE_RECYCLE_FAILED", "回收站结果需要核对，未执行永久删除", 409
                ) from exc

    def rename_course(self, course_id: str, value: CourseRenameFolderInput) -> dict:
        from learning_app.workspaces import UNCLASSIFIED

        if course_id == UNCLASSIFIED:
            raise AppError("WORKSPACE_SYSTEM_PROTECTED", "未分类是默认保存工作区，不能重命名", 403)
        name = value.name.strip()
        if not name or any(ord(character) < 32 for character in name):
            raise AppError("COURSE_NAME_INVALID", "请输入有效的课程显示名称", 422)
        with self._lock:
            op_id, previous = self._begin(
                course_id, "rename_course", {"name": name}, value.operation_id
            )
            if previous is not None:
                return previous
            try:
                with self.engine.begin() as conn:
                    conn.execute(
                        update(business.courses)
                        .where(business.courses.c.course_id == course_id)
                        .values(title=name)
                    )
                return self._finish(
                    op_id,
                    {"operation_id": op_id, "state": "completed", "root": self.folder(course_id)},
                )
            except Exception as exc:
                self._checkpoint(op_id, "needs_review", error="COURSE_RENAME_UNKNOWN")
                raise AppError("COURSE_RENAME_UNKNOWN", "课程显示名保存结果需要核对", 409) from exc

    def detach(self, course_id: str, value: CourseDetachInput) -> dict:
        from learning_app.workspaces import UNCLASSIFIED

        if course_id == UNCLASSIFIED:
            raise AppError("WORKSPACE_SYSTEM_PROTECTED", "未分类是默认保存工作区，不能移除", 403)
        with self._lock:
            self.folder(course_id)  # Existence check; a missing/unbound root is removable.
            op_id, previous = self._begin(
                course_id, "detach", {"confirm": value.confirm}, value.operation_id
            )
            if previous is not None:
                return previous
            with self.engine.begin() as conn:
                conn.execute(
                    update(roots).where(roots.c.course_id == course_id).values(state="detached")
                )
                exists = conn.execute(
                    select(visibility.c.course_id).where(visibility.c.course_id == course_id)
                ).first()
                if exists:
                    conn.execute(
                        update(visibility)
                        .where(visibility.c.course_id == course_id)
                        .values(hidden=True)
                    )
                else:
                    conn.execute(insert(visibility).values(course_id=course_id, hidden=True))
            return self._finish(
                op_id, {"operation_id": op_id, "state": "completed", "root": self.folder(course_id)}
            )

    def content(self, asset_id: str, version: str, request: Request) -> Response:
        with self.engine.connect() as conn:
            linked = (
                conn.execute(select(entries.c.course_id).where(entries.c.asset_id == asset_id))
                .scalars()
                .all()
            )
        source = self.repository.resolve_source(asset_id, version)
        if source["status"] != "available":
            raise AppError(
                "SOURCE_VERSION_MISMATCH"
                if source["status"] == "version_mismatch"
                else "SOURCE_MISSING",
                "来源文件缺失或内容已改变，旧来源不会自动指向新版",
            )
        path = _local_path(source["local_path"], directory=False)
        if not any(
            self.folder(course)["root_status"] == "ready"
            and path.is_relative_to(self._root(course))
            for course in linked
        ):
            raise AppError("COURSE_SOURCE_OUTSIDE_ROOT", "资料不在当前已绑定课程根目录内", 403)
        viewer = _viewer(path)
        if viewer not in {"text", "image", "pdf"}:
            raise AppError("FILE_VIEWER_UNSUPPORTED", "请主动使用系统默认程序打开此文件", 422)
        media_type = (
            "text/plain; charset=utf-8"
            if viewer == "text"
            else "application/pdf"
            if viewer == "pdf"
            else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        if viewer == "text" and path.stat().st_size > 5_000_000:
            raise AppError("TEXT_PREVIEW_TOO_LARGE", "文本超过5MB，请主动使用系统程序打开", 422)
        stream = _read_locked(path)
        try:
            if hashlib.file_digest(stream, "sha256").hexdigest() != source["content_hash"]:
                raise AppError(
                    "SOURCE_VERSION_MISMATCH", "来源在读取前改变，旧版本不会自动指向新版"
                )
            size = os.fstat(stream.fileno()).st_size
            start, end = 0, size - 1
            headers = {
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                "X-Content-Type-Options": "nosniff",
                "Accept-Ranges": "bytes",
                "ETag": f'"{source["content_hash"]}"',
            }
            status = 200
            range_header = request.headers.get("range")
            if range_header and request.headers.get("if-range", headers["ETag"]) == headers["ETag"]:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                if not match or not any(match.groups()):
                    stream.close()
                    return Response(
                        status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"}
                    )
                first, last = match.groups()
                start = int(first) if first else max(0, size - int(last))
                end = min(int(last), size - 1) if first and last else size - 1
                if start >= size or end < start or (not first and int(last) == 0):
                    stream.close()
                    return Response(
                        status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"}
                    )
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
                status = 206
            headers["Content-Length"] = str(max(0, end - start + 1))
            if request.method == "HEAD":
                stream.close()
                return Response(status_code=status, headers=headers, media_type=media_type)
        except Exception:
            stream.close()
            raise

        def chunks():
            with stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    block = stream.read(min(256 * 1024, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block

        return StreamingResponse(
            chunks(),
            status_code=status,
            headers=headers,
            media_type=media_type,
            background=BackgroundTask(stream.close),
        )

    def external(self, course_id: str, value: str) -> dict:
        root = self._root(course_id)
        path = self._path(root, value)
        if not path.is_file():
            raise AppError("FILE_REQUIRED", "请选择文件", 422)
        if os.name != "nt":
            raise AppError("EXTERNAL_OPEN_UNAVAILABLE", "此环境不支持系统默认程序打开", 503)
        try:
            os.startfile(path)
        except OSError as exc:
            raise AppError(
                "EXTERNAL_OPEN_FAILED", "系统未能打开此文件，请检查默认程序", 503
            ) from exc
        return {"opened": True}


def install_course_file_routes(app: FastAPI) -> None:
    from learning_app.workspace_navigation import install_workspace_navigation_routes

    install_workspace_navigation_routes(app)

    def store() -> CourseFileStore:
        if not hasattr(app.state, "course_files"):
            app.state.course_files = CourseFileStore(app.state.repository)
        return app.state.course_files

    @app.post("/api/v1/courses/folder/pick", response_model=CourseFolderPickResponse | None)
    def pick_folder():
        selected = choose_paths(directory=True)
        if not selected:
            return None
        path = _local_path(selected[0], directory=True)
        if not path.name or path == Path(path.anchor):
            raise AppError("COURSE_ROOT_INVALID", "请选择磁盘下的课程文件夹", 422)
        return {"path": str(path), "name": path.name}

    @app.post("/api/v1/courses/folder", response_model=CourseFolderResponse | None)
    def create_folder(value: CourseFolderInput):
        selected = [value.path] if value.path else choose_paths(directory=True)
        return store().bind(None, selected[0], value.title) if selected else None

    @app.post("/api/v1/courses/{course_id}/root", response_model=CourseFolderResponse | None)
    def bind_folder(course_id: str, value: CourseFolderInput):
        selected = [value.path] if value.path else choose_paths(directory=True)
        return store().bind(course_id, selected[0], value.title) if selected else None

    @app.post("/api/v1/courses/{course_id}/root/relocate")
    def relocate_folder(course_id: str, value: CourseRelocationInput):
        selected = [value.path] if value.path else choose_paths(directory=True)
        return (
            store().relocate_root(
                course_id,
                selected[0],
                expected_root=value.expected_root,
                operation_id=value.operation_id,
            )
            if selected
            else None
        )

    @app.get("/api/v1/courses/{course_id}/files", response_model=CourseFileListResponse)
    def list_files(course_id: str, path: str = ""):
        return store().list(course_id, path)

    @app.post("/api/v1/courses/{course_id}/files/import", response_model=CourseImportResponse)
    def import_files(course_id: str, value: CourseImportInput):
        return store().import_files(course_id, value)

    @app.post("/api/v1/courses/{course_id}/files/transfer", response_model=CourseImportResponse)
    def transfer_files(course_id: str, value: CourseTransferInput):
        return store().transfer(course_id, value)

    @app.post("/api/v1/courses/{course_id}/files/clipboard", response_model=CourseImportResponse)
    def paste_clipboard(course_id: str, value: CourseClipboardInput):
        return store().clipboard(course_id, value)

    @app.post("/api/v1/courses/{course_id}/files/upload", response_model=CourseImportResponse)
    async def upload_file(
        course_id: str, path: str, operation_id: str, request: Request, destination: str = ""
    ):
        return await store().upload(course_id, path, destination, operation_id, request)

    @app.post(
        "/api/v1/courses/{course_id}/files/properties", response_model=CourseFilePropertiesResponse
    )
    def file_properties(course_id: str, value: CoursePathInput):
        return store().properties(course_id, value.path)

    @app.post(
        "/api/v1/courses/{course_id}/files/directory", response_model=CourseFileOperationResponse
    )
    def make_directory(course_id: str, value: CourseDirectoryInput):
        return store().directory(course_id, value)

    @app.post(
        "/api/v1/courses/{course_id}/files/rename", response_model=CourseFileOperationResponse
    )
    def rename_file(course_id: str, value: CourseRenameInput):
        return store().rename(course_id, value)

    @app.post("/api/v1/courses/{course_id}/files/move", response_model=CourseFileOperationResponse)
    def move_file(course_id: str, value: CourseMoveInput):
        return store().move(course_id, value)

    @app.post(
        "/api/v1/courses/{course_id}/files/delete", response_model=CourseFileOperationResponse
    )
    def delete_file(course_id: str, value: CourseDeleteInput):
        return store().delete(course_id, value)

    @app.post("/api/v1/courses/{course_id}/files/open", response_model=CourseFileOpenResponse)
    def open_file(course_id: str, value: CoursePathInput):
        return store().open(course_id, value.path)

    @app.post("/api/v1/courses/{course_id}/files/external")
    def external_file(course_id: str, value: CoursePathInput):
        return store().external(course_id, value.path)

    @app.post(
        "/api/v1/courses/{course_id}/rename-course", response_model=CourseFileOperationResponse
    )
    def rename_course(course_id: str, value: CourseRenameFolderInput):
        return store().rename_course(course_id, value)

    @app.post("/api/v1/courses/{course_id}/detach", response_model=CourseFileOperationResponse)
    def detach_course(course_id: str, value: CourseDetachInput):
        return store().detach(course_id, value)

    @app.get(
        "/api/v1/courses/{course_id}/files/operations/{operation_id}",
        response_model=CourseOperationStatusResponse,
    )
    def get_operation(course_id: str, operation_id: str):
        return store().operation(course_id, operation_id)

    @app.post(
        "/api/v1/courses/{course_id}/files/operations/{operation_id}/reconcile",
        response_model=CourseOperationStatusResponse,
    )
    def reconcile_operation(course_id: str, operation_id: str, value: CourseReconcileInput):
        return store().reconcile(course_id, operation_id)

    @app.get("/api/v1/course-files/{asset_id}/content")
    @app.head("/api/v1/course-files/{asset_id}/content", include_in_schema=False)
    def read_content(asset_id: str, source_version: str, request: Request):
        return store().content(asset_id, source_version, request)
