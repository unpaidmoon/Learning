"""Pinned original essay parsers and exact anchors; no model, files or mutable round DB."""

from __future__ import annotations

import json
import shutil
from pathlib import Path, PurePosixPath

from learning_app.config import PROJECT_ROOT
from learning_app.errors import AppError

from .scheduler import CalculationHelper, _digest


class EssayProcessor(CalculationHelper):
    def __init__(self, project_root: Path = PROJECT_ROOT):
        self.project_root = project_root.resolve()
        self.name = "essay"
        self.lock = json.loads(
            (self.project_root / "resources/workbench/essay-runtime.lock.json").read_text("utf-8")
        )
        self.directory = self.project_root / ".runtime/workbench/essay" / self.lock["release_id"]
        self.executable = self.project_root / ".tools/node/node.exe"

    @staticmethod
    def _path(root: Path, relative: str) -> Path:
        parts = PurePosixPath(relative).parts
        target = root.joinpath(*parts)
        if (
            not parts
            or ".." in parts
            or "\\" in relative
            or ":" in relative
            or "\x00" in relative
            or PurePosixPath(relative).is_absolute()
            or not target.resolve().is_relative_to(root.resolve())
            or any(
                part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction())
                for part in [target, *target.parents]
            )
        ):
            raise AppError("ESSAY_RESOURCE_PATH_DENIED", "作文计算资源路径不在固定根内")
        return target

    def verified(self) -> Path:
        if _digest(self.executable) != self.lock["node_sha256"]:
            raise AppError("ESSAY_NODE_VERSION_MISMATCH", "作文解析所用项目Node制品摘要不符")
        for spec in self.lock["files"]:
            path = self._path(self.directory, spec["path"])
            if (
                not path.is_file()
                or path.stat().st_size != spec["bytes"]
                or _digest(path) != spec["sha256"]
            ):
                raise AppError("ESSAY_RESOURCE_NOT_PREPARED", "固定作文解析资源缺失或摘要不符")
        return self.executable

    def prepare(self, source: Path) -> Path:
        if self.directory.exists():
            self.verified()
            return self.directory
        for spec in self.lock["files"]:
            original = self._path(source, spec["path"])
            if (
                not original.is_file()
                or original.stat().st_size != spec["bytes"]
                or _digest(original) != spec["sha256"]
            ):
                raise AppError("ESSAY_RESOURCE_IMPORT_INVALID", "导入作文解析资源不符合固定清单")
        self.directory.mkdir(parents=True)
        for spec in self.lock["files"]:
            target = self._path(self.directory, spec["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with (
                self._path(source, spec["path"]).open("rb") as incoming,
                target.open("xb") as outgoing,
            ):
                shutil.copyfileobj(incoming, outgoing)
        self.verified()
        return self.directory

    def arguments(self, executable: Path) -> list[str]:
        return [
            str(executable),
            str(self.directory / "helper.cjs"),
            "--compiled-root",
            str(self.directory / "dist"),
        ]

    def analyze(self, request_id: str, draft: dict, feedback: dict, rubric: dict) -> dict:
        # Host completion/cancellation facts are supplied by TaskRun, never by the model.
        return self.invoke(
            request_id,
            fields={
                "operation": "validate_feedback",
                "payload": {"draft": draft, "feedback": feedback, "rubric": rubric},
            },
        )
