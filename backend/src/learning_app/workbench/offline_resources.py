"""Verify the fixed browser runtime; exports never compile or fetch dependencies."""

import hashlib
import json
import shutil
from pathlib import Path

from learning_app.config import PROJECT_ROOT
from learning_app.errors import AppError


class OfflineRuntime:
    def __init__(self, project_root: Path = PROJECT_ROOT):
        self.project_root = project_root.resolve()
        self.lock = json.loads(
            (self.project_root / "resources/workbench/offline-runtime.lock.json").read_text("utf-8")
        )
        if self.lock.get("format") != 1 or self.lock.get("release_id") != "offline-reader-v3":
            raise AppError("OFFLINE_RUNTIME_VERSION_INVALID", "离线阅读资源版本不匹配")
        self.directory = self.project_root / ".runtime/workbench/offline-reader" / self.lock["release_id"]

    @staticmethod
    def path(directory, name):
        if name not in {"reader.js", "reader.css", "notices.txt"}:
            raise AppError("OFFLINE_RESOURCE_PATH_DENIED", "离线资源不在固定清单内")
        target = directory / name
        if any(
            p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
            for p in [target, *target.parents]
        ):
            raise AppError("OFFLINE_RESOURCE_PATH_DENIED", "离线资源不允许符号链接或重定向目录")
        return target

    def read(self, directory=None):
        result = {}
        for spec in self.lock["files"]:
            path = self.path(directory or self.directory, spec["path"])
            if (
                not path.is_file()
                or path.stat().st_size != spec["bytes"]
                or spec["bytes"] > 16 * 1024 * 1024
            ):
                raise AppError("OFFLINE_RUNTIME_NOT_PREPARED", "固定离线阅读资源缺失或版本不符")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != spec["sha256"]:
                raise AppError("OFFLINE_RUNTIME_HASH_MISMATCH", "离线阅读资源摘要不符")
            result[spec["path"]] = data
        if set(result) != {"reader.js", "reader.css", "notices.txt"}:
            raise AppError("OFFLINE_RUNTIME_INCOMPLETE", "离线资源清单不完整")
        return result

    def prepare(self, source: Path):
        if self.directory.exists():
            self.read()
            return self.directory
        self.read(source)
        self.directory.mkdir(parents=True)
        for spec in self.lock["files"]:
            with (
                self.path(source, spec["path"]).open("rb") as incoming,
                self.path(self.directory, spec["path"]).open("xb") as outgoing,
            ):
                shutil.copyfileobj(incoming, outgoing)
        self.read()
        return self.directory
