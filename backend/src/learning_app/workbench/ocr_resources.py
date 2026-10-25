"""Fixed Node/WASM OCR assets; prepare explicitly, never fetch while recognizing."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath
from uuid import uuid4

from learning_app.errors import AppError


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _path(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if (
        parts.is_absolute()
        or ".." in parts.parts
        or not parts.parts
        or "\\" in relative
        or ":" in relative
        or "\0" in relative
        or relative != parts.as_posix()
    ):
        raise AppError("OCR_RESOURCE_PATH_DENIED", "OCR资源路径不符")
    target = root.joinpath(*parts.parts)
    if not target.resolve().is_relative_to(root.resolve()) or any(
        item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction())
        for item in [target, *target.parents]
    ):
        raise AppError("OCR_RESOURCE_PATH_DENIED", "OCR资源路径含未允许的链接")
    return target


class OcrResources:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.resources = self.project_root / "resources/workbench"
        spec = json.loads((self.resources / "engines.lock.json").read_text("utf-8"))["ocr"]
        manifest = _path(self.resources, spec["manifest"])
        if _hash(manifest) != spec["sha256"]:
            raise AppError("OCR_MANIFEST_INVALID", "OCR资源清单校验失败")
        self.manifest_hash = spec["sha256"]
        self.lock = json.loads(manifest.read_text("utf-8"))
        if (
            _hash(_path(self.resources, self.lock["package_lock"]["path"]))
            != self.lock["package_lock"]["sha256"]
        ):
            raise AppError("OCR_PACKAGE_LOCK_INVALID", "OCR包依赖锁校验失败")
        self.directory = self.project_root / ".runtime/workbench/ocr"
        self.cache = self.project_root / ".local/cache/workbench/artifacts/ocr"
        self.record = {
            "manifest_version": 1,
            "release_id": self.lock["release_id"],
            "runtime_lock_sha256": self.manifest_hash,
            "package_lock_sha256": self.lock["package_lock"]["sha256"],
            "file_count": self.lock["file_count"],
            "install_bytes": self.lock["total_install_bytes"],
            "complete": True,
            "runtime_uses_import_cache": False,
            "install_scripts_executed": False,
        }

    def prepared(self) -> bool:
        try:
            return (
                json.loads((self.directory / ".ocr-runtime-install.json").read_text("utf-8"))
                == self.record
            )
        except (OSError, ValueError):
            return False

    def verify(self) -> None:
        if not self.prepared():
            raise AppError("OCR_RESOURCES_NOT_PREPARED", "本地OCR资源尚未准备", 503)
        for item in self.lock["files"]:
            target = _path(self.directory, item["install_path"])
            if (
                not target.is_file()
                or target.stat().st_size != item["bytes"]
                or _hash(target) != item["sha256"]
            ):
                raise AppError("OCR_RESOURCE_INTEGRITY", "本地OCR资源缺失或已改变，未加载")

    def _download(self, url: str, filename: str, size: int, digest: str) -> Path:
        self.cache.mkdir(parents=True, exist_ok=True)
        target = _path(self.cache, filename)
        source = target
        if not target.is_file():
            if not url.startswith(
                (
                    "https://registry.npmjs.org/",
                    "https://raw.githubusercontent.com/naptha/tessdata/",
                )
            ):
                raise AppError("OCR_RESOURCE_SOURCE_DENIED", "OCR资源来源未固定")
            source = self.cache / f"{uuid4().hex}.partial"
            with urllib.request.urlopen(url, timeout=60) as incoming, source.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        if source.stat().st_size != size or _hash(source) != digest:
            raise AppError("OCR_RESOURCE_ARCHIVE_INVALID", "OCR资源归档校验失败，未展开")
        if source != target:
            os.replace(source, target)
        return target

    def prepare(self, import_from: Path | None = None) -> Path:
        if self.prepared():
            self.verify()
            return self.directory
        if self.directory.exists():
            raise AppError("OCR_RESOURCE_REPAIR_REQUIRED", "已有OCR目录已保留，未覆盖未核实文件")
        packages = {item["name"]: item for item in self.lock["packages"]}
        archives = {}
        if import_from is None:
            for name, spec in packages.items():
                archives[name] = self._download(
                    spec["url"], spec["archive_filename"], spec["archive_bytes"], spec["sha256"]
                )
            for spec in self.lock["models"]:
                archives[spec["id"]] = self._download(
                    spec["raw_model_url"],
                    f"{spec['id']}-{spec['sha256']}.gz",
                    spec["bytes"],
                    spec["sha256"],
                )
        staged = []
        for item in self.lock["files"]:
            if item["source_kind"] == "resources":
                content = _path(self.resources, item["source_relative"]).read_bytes()
            elif import_from is not None:
                content = _path(import_from.resolve(), item["source_relative"]).read_bytes()
            elif item.get("package"):
                package = packages[item["package"]]
                relative = (
                    PurePosixPath(item["install_path"])
                    .relative_to(package["install_path"])
                    .as_posix()
                )
                with tarfile.open(archives[item["package"]], "r:gz") as packed:
                    member = packed.getmember("package/" + relative)
                    if not member.isfile() or member.size != item["bytes"]:
                        raise AppError("OCR_RESOURCE_ARCHIVE_INVALID", "OCR归档成员不符")
                    content = packed.extractfile(member).read(item["bytes"] + 1)
            else:
                model = next(
                    (
                        model
                        for model in self.lock["models"]
                        if model["install_path"] == item["install_path"]
                    ),
                    None,
                )
                if model is None:
                    raise AppError("OCR_MANIFEST_INVALID", "OCR资源条目缺少固定来源")
                content = archives[model["id"]].read_bytes()
            if (
                len(content) != item["bytes"]
                or hashlib.sha256(content).hexdigest() != item["sha256"]
            ):
                raise AppError("OCR_RESOURCE_CONTENT_INVALID", "OCR资源实际字节不符")
            staged.append((item["install_path"], content))
        self.directory.mkdir(parents=True)
        for relative, content in staged:
            target = _path(self.directory, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(content)
        (self.directory / ".ocr-runtime-install.json").write_text(json.dumps(self.record), "utf-8")
        self.verify()
        return self.directory

    def launch_spec(self) -> dict:
        self.verify()
        return {
            "runtime": str(self.directory),
            "manifest_sha256": self.manifest_hash,
            "engine_version": self.lock["library"]["observed_engine_version"],
            "languages": self.lock["library"]["languages"],
        }
