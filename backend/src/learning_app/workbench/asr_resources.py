"""Deploy the fixed ASR import closure without a second provider or toolchain."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from learning_app.errors import AppError


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _target(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or ".." in parts
        or "\\" in relative
        or ":" in relative
        or "\x00" in relative
        or PurePosixPath(relative).as_posix() != relative
        or PurePosixPath(relative).is_absolute()
        or PureWindowsPath(relative).drive
    ):
        raise AppError("ASR_RESOURCE_PATH_DENIED", "语音资源路径不在固定安装根内")
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root.resolve()) or any(
        part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction())
        for part in [target, *target.parents]
    ):
        raise AppError("ASR_RESOURCE_PATH_DENIED", "语音资源路径含未允许的链接")
    return target


class AsrResources:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        manifests = self.project_root / "resources/workbench"
        spec = json.loads((manifests / "engines.lock.json").read_text("utf-8"))["asr"]
        manifest = _target(manifests, spec["manifest"])
        if _digest(manifest) != spec["sha256"]:
            raise AppError("ASR_MANIFEST_INVALID", "语音资源清单完整性不符")
        self.manifest_hash = spec["sha256"]
        self.lock = json.loads(manifest.read_text("utf-8"))
        self.directory = _target(
            self.project_root / ".runtime/workbench/asr", self.lock["release_id"]
        )
        self.cache = self.project_root / ".local/cache/workbench/artifacts/asr"
        self._stamp = {
            "manifest_sha256": self.manifest_hash,
            "release_id": self.lock["release_id"],
            "deployment": "wheel-imports-v1",
        }

    def prepared(self) -> bool:
        try:
            return json.loads((self.directory / ".prepared.json").read_text("utf-8")) == self._stamp
        except (OSError, ValueError):
            return False

    def _archive(self, spec: dict, wheelhouse: Path | None) -> Path:
        self.cache.mkdir(parents=True, exist_ok=True)
        target = _target(self.cache, spec["filename"])
        if target.is_file():
            source = target
        elif wheelhouse is not None:
            source = wheelhouse / spec["filename"]
            if not source.is_file():
                raise AppError("ASR_ARCHIVE_MISSING", "固定语音依赖归档缺失，未隐式换版本")
        else:
            url = spec["url"]
            if not url.startswith("https://files.pythonhosted.org/packages/"):
                raise AppError("ASR_ARCHIVE_SOURCE_DENIED", "语音依赖来源不在固定源内")
            source = self.cache / f"{uuid4().hex}.partial"
            with urllib.request.urlopen(url, timeout=60) as incoming, source.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        if source.stat().st_size != spec["bytes"] or _digest(source) != spec["sha256"]:
            raise AppError("ASR_ARCHIVE_INVALID", "固定语音归档摘要不符，未展开")
        if source != target and not target.exists():
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if _digest(target) != spec["sha256"]:
                raise AppError("ASR_ARCHIVE_INVALID", "语音归档复制后校验失败")
        return target

    def prepare(self, *, wheelhouse: Path | None = None, model: Path | None = None) -> Path:
        if self.prepared():
            return self.directory
        if self.directory.exists():
            raise AppError("ASR_RESOURCE_REPAIR_REQUIRED", "已有未完成的语音资源目录已保留，未覆盖")
        if model is None:
            raise AppError(
                "ASR_MODEL_IMPORT_REQUIRED", "须先导入匹配固定版本的既有语音模型，未自动下载"
            )
        for item in self.lock["model"]["files"]:
            original = model / item["name"]
            if (
                not original.is_file()
                or original.stat().st_size != item["bytes"]
                or _digest(original) != item["sha256"]
            ):
                raise AppError("ASR_MODEL_VERSION_MISMATCH", "本地语音模型文件缺失或版本不符")
        archives = {
            spec["id"]: self._archive(spec, wheelhouse)
            for spec in [*self.lock["python"]["packages"], *self.lock["native"]["source_archives"]]
        }
        self.directory.mkdir(parents=True)
        library = self.directory / "python/Lib/site-packages"
        # This worker imports only these verified wheels using Python -I -S.
        # No entry-point scripts, package hooks, source builds or pip resolution.
        for spec in self.lock["python"]["packages"]:
            with zipfile.ZipFile(archives[spec["id"]]) as packed:
                for member in packed.infolist():
                    if member.is_dir():
                        continue
                    parts = PurePosixPath(member.filename).parts
                    if parts[0].endswith(".data"):
                        if len(parts) > 2 and parts[1] in {"purelib", "platlib"}:
                            target = _target(library, "/".join(parts[2:]))
                        else:
                            # Preserve non-import wheel data without activating
                            # console scripts or copying into the system prefix.
                            target = _target(
                                self.directory / "wheel-data" / spec["name"], member.filename
                            )
                    else:
                        target = _target(library, member.filename)
                    self._copy_member(packed, member, target)
        for spec in self.lock["native"]["extract_files"]:
            with zipfile.ZipFile(archives[spec["source_id"]]) as packed:
                member = packed.getinfo(spec["archive_member"])
                if member.file_size != spec["bytes"]:
                    raise AppError("ASR_NATIVE_INVALID", "原生语音依赖成员尺寸不符")
                target = _target(self.directory, spec["destination"])
                self._copy_member(packed, member, target)
                if _digest(target) != spec["sha256"]:
                    raise AppError("ASR_NATIVE_INVALID", "原生语音依赖成员摘要不符")
        for spec in self.lock["model"]["files"]:
            target = _target(self.directory, spec["destination"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with (model / spec["name"]).open("rb") as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        self.verify_contents()
        (self.directory / ".prepared.json").write_text(json.dumps(self._stamp), "utf-8")
        return self.directory

    @staticmethod
    def _copy_member(packed: zipfile.ZipFile, member: zipfile.ZipInfo, target: Path) -> None:
        if (member.external_attr >> 16) & 0o170000 == 0o120000:
            raise AppError("ASR_ARCHIVE_PATH_DENIED", "语音归档含链接，未展开")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            with packed.open(member) as source:
                expected = hashlib.file_digest(source, "sha256").hexdigest()
            if _digest(target) != expected:
                raise AppError("ASR_ARCHIVE_COLLISION", "固定语音依赖文件路径冲突，未覆盖")
            return
        with packed.open(member) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output, 1024 * 1024)

    def verify_contents(self) -> None:
        items = [
            (item["destination"], item)
            for item in [*self.lock["native"]["extract_files"], *self.lock["model"]["files"]]
        ]
        items += [
            (item["relative_path"], item) for item in self.lock["native"]["bundled_ct2_files"]
        ]
        items.append((self.lock["bundled_vad"]["relative_path"], self.lock["bundled_vad"]))
        for relative, expected in items:
            target = _target(self.directory, relative)
            if (
                not target.is_file()
                or target.stat().st_size != expected["bytes"]
                or _digest(target) != expected["sha256"]
            ):
                raise AppError("ASR_RESOURCE_INTEGRITY", "已部署的语音资源校验失败，未加载")

    def launch_spec(self) -> dict:
        if not self.prepared():
            raise AppError("ASR_RESOURCES_NOT_PREPARED", "本地语音资源尚未准备", 503)
        self.verify_contents()
        return {
            "runtime": str(self.directory),
            "manifest_sha256": self.manifest_hash,
            "model": str(_target(self.directory, self.lock["model"]["installation_root"])),
            "native_directories": [
                str(_target(self.directory, value))
                for value in self.lock["native"]["dll_search_directories"]
            ],
            "preload": [
                str(_target(self.directory, value))
                for value in self.lock["native"]["preload_libraries"]
            ],
            "model_options": self.lock["worker_launch_contract"]["model_options_tested"],
            "vad": self.lock["worker_launch_contract"]["vad_candidate"],
        }
