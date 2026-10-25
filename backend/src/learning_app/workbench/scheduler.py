"""Fixed stateless calculation helpers; SQLite owns all activity and queue state."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path

from learning_app.config import PROJECT_ROOT
from learning_app.errors import AppError
from learning_app.processes import OwnedProcessGroup

from .local_workers import worker_environment

FSRS_CONFIG = {
    "parameters": [],
    "desired_retention": 0.9,
    "learning_steps_minutes": [1, 10],
    "relearning_steps_minutes": [10],
    "maximum_interval_days": 36500,
    "enable_fuzz": False,
}


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class CalculationHelper:
    def __init__(self, name: str, project_root: Path = PROJECT_ROOT):
        self.project_root = project_root.resolve()
        self.name = name
        self.lock = json.loads(
            (self.project_root / "resources/workbench" / f"{name}-runtime.lock.json").read_text(
                "utf-8"
            )
        )
        self.directory = (
            self.project_root / ".runtime/workbench/schedulers" / self.lock["release_id"]
        )
        self.executable = self.directory / self.lock["executable"]["name"]

    def verified(self) -> Path:
        spec = self.lock["executable"]
        try:
            valid = (
                self.executable.is_file()
                and not self.executable.is_symlink()
                and self.executable.resolve().is_relative_to(self.directory.resolve())
                and self.executable.stat().st_size == spec["bytes"]
                and _digest(self.executable) == spec["sha256"]
            )
        except OSError:
            valid = False
        if not valid:
            raise AppError(
                "LEARNING_HELPER_NOT_PREPARED", "固定学习计算制品缺失或摘要不符，请准备对应版本"
            )
        return self.executable

    def prepare(self, source: Path) -> Path:
        """Explicit verified import only; never build, fetch, replace or repair implicitly."""
        spec = self.lock["executable"]
        if self.directory.exists():
            return self.verified()
        if source.stat().st_size != spec["bytes"] or _digest(source) != spec["sha256"]:
            raise AppError("LEARNING_HELPER_IMPORT_INVALID", "导入制品不符合固定版本")
        self.directory.mkdir(parents=True)
        with source.open("rb") as incoming, self.executable.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        for item in self.lock["notices"]:
            original = self.project_root / item["source"]
            if _digest(original) != item["sha256"]:
                raise AppError("LEARNING_HELPER_NOTICE_INVALID", "学习计算制品的固定许可通知不符")
            destination = self.directory / "notices" / item["name"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            with original.open("rb") as incoming, destination.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
        return self.verified()

    def arguments(self, executable: Path) -> list[str]:
        return [str(executable)]

    def invoke(
        self, request_id: str, operation: dict | None = None, *, fields: dict | None = None
    ) -> dict:
        executable = self.verified()
        request = {"protocol_version": 1, "request_id": request_id}
        request.update(fields if fields is not None else {"operation": operation})
        raw = json.dumps(request, ensure_ascii=True, allow_nan=False)
        if len(raw.encode("utf-8")) > self.lock["protocol"]["max_input_bytes"]:
            raise AppError("LEARNING_HELPER_INPUT_LIMIT", "学习计算输入超过固定限制，未截断")
        limit = self.lock["protocol"]["max_output_bytes"]
        group = OwnedProcessGroup("learning-calculation")
        readers, outputs = [], {}
        try:
            process = group.spawn(
                self.arguments(executable),
                cwd=self.directory,
                env=worker_environment(self.directory),
            )

            def read(key, stream, maximum):
                outputs[key] = stream.read(maximum + 1)

            for key, stream, maximum in (
                ("stdout", process.stdout, limit),
                ("stderr", process.stderr, 4096),
            ):
                reader = threading.Thread(target=read, args=(key, stream, maximum), daemon=True)
                reader.start()
                readers.append(reader)
            process.stdin.write(raw)
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise AppError(
                    "LEARNING_HELPER_TIMEOUT", "学习计算超时，未提交评分或自动重试"
                ) from exc
            for reader in readers:
                reader.join(timeout=1)

            output = outputs.get("stdout", "")
            if len(output.encode("utf-8")) > limit or len(outputs.get("stderr", "")) > 4096:
                raise AppError("LEARNING_HELPER_OUTPUT_LIMIT", "学习计算返回超过固定限制")
            try:
                result = json.loads(output)
            except ValueError as exc:
                raise AppError("LEARNING_HELPER_PROTOCOL", "学习计算返回结构不符") from exc
            if (
                not isinstance(result, dict)
                or result.get("protocol_version") != 1
                or result.get("request_id") != request_id
            ):
                raise AppError("LEARNING_HELPER_PROTOCOL", "学习计算返回归属不符")
            if not result.get("ok") or process.returncode != 0:
                code = result.get("error", {}).get("code", "LEARNING_HELPER_FAILED")
                raise AppError(code, "学习计算未完成，未修改排期或提交评分")
            return result["result"]
        finally:
            group.close(grace_seconds=0.1)
            for reader in readers:
                reader.join(timeout=1)


class FsrsScheduler:
    def __init__(self, project_root: Path = PROJECT_ROOT):
        self.helper = CalculationHelper("fsrs", project_root)

    def logical_day(self, timing: dict):
        return self.helper.invoke("logical_day", {"type": "logical_day", "timing": timing})

    def calculate(self, review: dict, timing: dict, request_id: str, rating: int | None = None):
        state = review["scheduler_state"]
        card = {
            "card_state_id": review["review_id"],
            **state,
            "suspended": review["queue_state"] == "paused",
            "removed": review["queue_state"] == "removed",
        }
        operation = {
            "type": "preview" if rating is None else "review",
            "card": card,
            "config": review["scheduler_config"],
            "timing": timing,
        }
        if rating is not None:
            operation["rating"] = rating
        return self.helper.invoke(request_id, operation)
