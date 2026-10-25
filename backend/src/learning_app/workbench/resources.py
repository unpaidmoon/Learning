"""Fixed reading resources. Runtime reads never fetch a missing dependency."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from learning_app.errors import AppError


class ReadingResources:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        manifests = self.project_root / "resources/workbench"
        lock = json.loads((manifests / "engines.lock.json").read_text("utf-8"))["pdfjs"]
        manifest = manifests / lock["files_manifest"]
        raw_manifest = manifest.read_bytes()
        # This pinned manifest was recorded with Windows newlines; Git's LF
        # checkout must not invalidate identical JSON. Accept only bytes whose
        # exact original or CRLF projection matches the pinned digest.
        manifest_hashes = {
            hashlib.sha256(raw_manifest).hexdigest(),
            hashlib.sha256(raw_manifest.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")).hexdigest(),
        }
        if lock["files_manifest_sha256"] not in manifest_hashes:
            raise AppError("READING_RESOURCE_MANIFEST_INVALID", "阅读资源清单完整性不符")
        value = json.loads(manifest.read_text("utf-8"))
        if (
            value["version"] != lock["version"]
            or value["artifact_sha256"] != lock["archive_sha256"]
        ):
            raise AppError("READING_RESOURCE_VERSION_CONFLICT", "阅读资源版本不一致")
        self.lock = lock
        self.files = {item["path"]: item for item in value["files"]}
        self.base = self.project_root / ".runtime/workbench/resources/pdfjs"
        self.directory = self.base / lock["version"]

    def prepared(self) -> bool:
        try:
            stamp = json.loads((self.directory / ".prepared.json").read_text("utf-8"))
        except (OSError, ValueError):
            return False
        return stamp == {
            "version": self.lock["version"],
            "manifest_sha256": self.lock["files_manifest_sha256"],
        }

    def path(self, version: str, relative: str) -> Path:
        if (
            version != self.lock["version"]
            or relative not in self.files
            or ".." in relative.split("/")
            or PureWindowsPath(relative).drive
        ):
            raise AppError("READING_RESOURCE_DENIED", "阅读资源不在固定清单内", 404)
        if not self.prepared():
            raise AppError("READING_RESOURCES_NOT_PREPARED", "阅读资源尚未准备完成", 503)
        path = self.directory.joinpath(*PurePosixPath(relative).parts)
        if not path.resolve().is_relative_to(self.directory.resolve()) or any(
            item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction())
            for item in [path, *path.parents]
            if item != self.directory.parent
        ):
            raise AppError("READING_RESOURCE_PATH_DENIED", "阅读资源路径不符", 403)
        if not path.is_file():
            raise AppError("READING_RESOURCE_MISSING", "阅读资源文件缺失", 503)
        return path

    def prepare(self, archive: Path | None = None) -> Path:
        """Explicit launcher preparation; preserve mismatched/partial directories."""
        if self.prepared():
            return self.directory
        if self.directory.exists():
            raise AppError(
                "READING_RESOURCE_REPAIR_REQUIRED", "已有阅读资源未通过校验，未覆盖原目录"
            )
        cache = self.project_root / ".local/cache/workbench/artifacts"
        cache.mkdir(parents=True, exist_ok=True)
        target_archive = cache / f"pdfjs-{self.lock['version']}.zip"
        source = archive or target_archive
        if not source.is_file():
            temporary = cache / f"pdfjs-{uuid4().hex}.zip.part"
            with (
                urllib.request.urlopen(self.lock["archive_url"], timeout=60) as response,
                temporary.open("xb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            source = temporary
        if (
            source.stat().st_size != self.lock["archive_bytes"]
            or hashlib.sha256(source.read_bytes()).hexdigest() != self.lock["archive_sha256"]
        ):
            raise AppError("READING_RESOURCE_ARCHIVE_INVALID", "阅读制品摘要不符，未展开")
        if not target_archive.exists():
            with source.open("rb") as original, target_archive.open("xb") as copy:
                while chunk := original.read(1024 * 1024):
                    copy.write(chunk)
                copy.flush()
                os.fsync(copy.fileno())
        self.base.mkdir(parents=True, exist_ok=True)
        stage = self.base / f"{self.lock['version']}-stage-{uuid4().hex}"
        stage.mkdir()
        with zipfile.ZipFile(source) as packed:
            for name, expected in self.files.items():
                target = stage.joinpath(*PurePosixPath(name).parts)
                if ".." in name.split("/") or not target.resolve().is_relative_to(stage.resolve()):
                    raise AppError("READING_RESOURCE_MANIFEST_INVALID", "阅读资源条目路径不符")
                content = packed.read(name)
                if (
                    len(content) != expected["bytes"]
                    or hashlib.sha256(content).hexdigest() != expected["sha256"]
                ):
                    raise AppError("READING_RESOURCE_CONTENT_INVALID", "阅读资源条目完整性不符")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        (stage / ".prepared.json").write_text(
            json.dumps(
                {
                    "version": self.lock["version"],
                    "manifest_sha256": self.lock["files_manifest_sha256"],
                }
            ),
            "utf-8",
        )
        # Both resolved targets must remain in this owned resource directory.
        if not stage.resolve().is_relative_to(
            self.base.resolve()
        ) or not self.directory.resolve().is_relative_to(self.base.resolve()):
            raise AppError("READING_RESOURCE_PATH_DENIED", "阅读资源发布路径越界")
        os.replace(stage, self.directory)
        return self.directory
