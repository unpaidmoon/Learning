"""Original line-cut hints, verified against exact project Unicode ranges."""

import json
import shutil
from pathlib import Path

from learning_app.config import PROJECT_ROOT
from learning_app.errors import AppError

from .essay_processing import EssayProcessor
from .scheduler import CalculationHelper, _digest

CHUNKER_VERSION = "deepstudent-lines-lossless-v1"


class ContentProcessor(CalculationHelper):
    def __init__(self, project_root: Path = PROJECT_ROOT):
        self.project_root = project_root.resolve()
        self.name = "content"
        self.lock = json.loads(
            (self.project_root / "resources/workbench/content-runtime.lock.json").read_text("utf-8")
        )
        self.directory = self.project_root / ".runtime/workbench/content" / self.lock["release_id"]
        self.executable = self.project_root / ".tools/node/node.exe"

    def verified(self):
        if not self.executable.is_file() or _digest(self.executable) != self.lock["node_sha256"]:
            raise AppError("CONTENT_NODE_VERSION_MISMATCH", "文字处理所用项目Node制品摘要不符")
        for spec in self.lock["files"]:
            path = EssayProcessor._path(self.directory, spec["path"])
            if (
                not path.is_file()
                or path.stat().st_size != spec["bytes"]
                or _digest(path) != spec["sha256"]
            ):
                raise AppError("CONTENT_RESOURCE_NOT_PREPARED", "固定文字处理资源缺失或摘要不符")
        return self.executable

    def prepare(self, source):
        if self.directory.exists():
            self.verified()
            return self.directory
        for spec in self.lock["files"]:
            path = EssayProcessor._path(source, spec["path"])
            if (
                not path.is_file()
                or path.stat().st_size != spec["bytes"]
                or _digest(path) != spec["sha256"]
            ):
                raise AppError("CONTENT_RESOURCE_IMPORT_INVALID", "导入切块资源不符合固定清单")
        self.directory.mkdir(parents=True)
        for spec in self.lock["files"]:
            target = EssayProcessor._path(self.directory, spec["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with (
                EssayProcessor._path(source, spec["path"]).open("rb") as incoming,
                target.open("xb") as outgoing,
            ):
                shutil.copyfileobj(incoming, outgoing)
        self.verified()
        return self.directory

    def arguments(self, executable):
        return [str(executable), str(self.directory / "helper.cjs")]

    def ranges(self, text, maximum):
        result = self.invoke(
            "source_ranges",
            fields={"operation": "lossless_ranges", "text": text, "max_codepoints": maximum},
        )
        if result.get("status") == "bypass":
            return None
        parts = result.get("parts")
        if result.get("status") != "hinted" or not isinstance(parts, list):
            raise AppError("CONTENT_RANGES_INVALID", "切块返回的范围无法核对")
        cursor = 0
        for pair in parts:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or any(type(value) is not int for value in pair)
                or pair[0] != cursor
                or not pair[0] < pair[1] <= len(text)
                or pair[1] - pair[0] > maximum
            ):
                raise AppError("CONTENT_RANGES_INVALID", "切块范围存在缺口、重复或越界")
            cursor = pair[1]
        if cursor != len(text):
            raise AppError("CONTENT_RANGES_INVALID", "切块没有保留完整原文")
        return [tuple(pair) for pair in parts]

    def aligned_translation(self, raw):
        """Strictly audited original object scanner; no source or model operations."""
        result = self.invoke(
            "aligned_translation",
            fields={"operation": "aligned_translation", "text": raw},
        )
        if result.get("status") not in {"complete", "invalid"}:
            raise AppError("CONTENT_ALIGNED_RESULT_INVALID", "翻译解析结果无法核对")
        return result
