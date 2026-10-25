"""Explicit optional original-material inventory, with exact version byte identity."""

import hashlib
import os
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from learning_app.course_files import _is_link
from learning_app.data_snapshot import SnapshotCut, _readonly
from learning_app.errors import AppError
from learning_app.media import _read_locked


def relative_name(value):
    path = PurePosixPath(value)
    parts = path.parts
    if (
        not parts
        or path.is_absolute()
        or path.as_posix() != value
        or "\\" in value
        or ":" in value
        or any(
            p in {".", ".."}
            or p.rstrip(" .") != p
            or PureWindowsPath(p).is_reserved()
            or any(ord(c) < 32 for c in p)
            for p in parts
        )
    ):
        raise AppError("DATA_PACKAGE_PATH_DENIED", "数据包成员路径无效")
    return value


@dataclass(frozen=True)
class OriginalInventory:
    objects: dict
    course_files: tuple[dict, ...]
    source_versions: tuple[dict, ...]


def collect_originals(cut: SnapshotCut, data_root, *, check=lambda: None):
    objects = {}
    course_files = []
    versions = []

    def register(path, expected=None, size=None):
        if not path.is_file() or any(_is_link(p) for p in [path, *path.parents]):
            raise AppError(
                "DATA_ORIGINAL_UNAVAILABLE", "原始资料缺失或位置不受支持，未输出不完整迁移包"
            )
        with _read_locked(path) as stream:
            length = os.fstat(stream.fileno()).st_size
            checksum = hashlib.sha256()
            while data := stream.read(1024 * 1024):
                check()
                checksum.update(data)
            digest = checksum.hexdigest()
        if expected is not None and (digest != expected or length != size):
            raise AppError(
                "DATA_ORIGINAL_VERSION_UNAVAILABLE", "原始资料旧版本已改变，未使用同名新版替代"
            )
        objects.setdefault(
            digest,
            {
                "source": path,
                "bytes": length,
                "sha256": digest,
                "path": f"originals/objects/{digest}",
            },
        )
        return digest, length

    with closing(_readonly(cut.database)) as connection:
        for row in connection.execute(
            "SELECT course_id,local_path FROM course_roots ORDER BY course_id"
        ):
            root = Path(row["local_path"])
            if not root.is_dir() or _is_link(root):
                raise AppError(
                    "DATA_ORIGINAL_ROOT_UNAVAILABLE", "课程原件目录不可用，可选择只导出软件数据"
                )
            if data_root.resolve().is_relative_to(root.resolve()):
                raise AppError(
                    "DATA_ORIGINAL_ROOT_OVERLAP", "课程目录包含软件数据，不能递归打包自身"
                )
            folded = set()
            for parent, dirs, files in os.walk(root, followlinks=False):
                for child in [*dirs, *files]:
                    if _is_link(Path(parent) / child):
                        raise AppError(
                            "DATA_ORIGINAL_LINK_DENIED",
                            "课程原件含重定向链接，请明确整理后再包含原件",
                        )
                dirs.sort()
                for name in sorted(files):
                    path = Path(parent) / name
                    relative = relative_name(path.relative_to(root).as_posix())
                    if relative.casefold() in folded:
                        raise AppError(
                            "DATA_ORIGINAL_NAME_COLLISION", "课程原件路径在目标系统存在冲突"
                        )
                    folded.add(relative.casefold())
                    digest, length = register(path)
                    course_files.append(
                        {
                            "course_id": row["course_id"],
                            "relative_path": relative,
                            "sha256": digest,
                            "bytes": length,
                        }
                    )
        for row in connection.execute(
            "SELECT * FROM source_versions ORDER BY asset_id,source_version"
        ):
            digest = row["content_hash"]
            if digest not in objects:
                register(Path(row["local_path"]), digest, row["byte_length"])
            if objects[digest]["bytes"] != row["byte_length"]:
                raise AppError("DATA_ORIGINAL_VERSION_UNAVAILABLE", "原始资料版本长度不符")
            versions.append(
                {
                    "asset_id": row["asset_id"],
                    "source_version": row["source_version"],
                    "sha256": digest,
                    "bytes": row["byte_length"],
                }
            )
    return OriginalInventory(objects, tuple(course_files), tuple(versions))
