"""Versioned local execution preferences, separate from model selections."""

import json
import os
import re
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from learning_app.course_files import _is_link
from learning_app.data_operations import atomic_record
from learning_app.errors import AppError
from learning_app.storage.repository import _hash


class RuntimePreferenceValues(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    resource_mode: Literal["gentle", "accelerated"] = "gentle"
    keep_awake: bool = False


class RuntimePreferenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    values: RuntimePreferenceValues = Field(default_factory=RuntimePreferenceValues)
    last_operation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,160}$")
    last_request_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class RuntimePreferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")
    expected_revision: int = Field(ge=0)
    values: RuntimePreferenceValues


_LOCKS = {}
_GUARD = threading.Lock()


def resource_policy(mode="gentle"):
    if mode not in {"gentle", "accelerated"}:
        raise AppError("RESOURCE_MODE_INVALID", "本地资源模式无效")
    return {
        "mode": mode,
        "cpu_threads": min(8, max(2, os.cpu_count() or 2)) if mode == "accelerated" else 2,
        "priority": "normal" if mode == "accelerated" else "below_normal",
    }


class RuntimePreferences:
    def __init__(self, data_directory):
        self.path = Path(data_directory) / "desktop-ui/runtime.json"
        self.operations = self.path.parent / "runtime-operations"
        with _GUARD:
            self.lock = _LOCKS.setdefault(self.path.resolve(), threading.RLock())

    def read(self):
        if any(_is_link(path) for path in [self.path, *self.path.parents]):
            raise AppError("RUNTIME_PREFERENCES_PATH_DENIED", "运行设置不能使用重定向链接")
        if not self.path.exists():
            return RuntimePreferenceRecord()
        try:
            if self.path.stat().st_size > 64 * 1024:
                raise ValueError()
            return RuntimePreferenceRecord.model_validate_json(self.path.read_bytes())
        except (OSError, ValueError, ValidationError) as error:
            raise AppError(
                "RUNTIME_PREFERENCES_INVALID", "运行设置无法读取；已有学习数据保留", 503
            ) from error

    def effective(self):
        # Broken optional settings do not prevent reading existing learning data
        # or gain permission to keep the system awake.
        try:
            with self.lock:
                return self.read().values
        except AppError:
            return RuntimePreferenceValues()

    def _operation(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", identity):
            raise AppError("RUNTIME_OPERATION_ID_INVALID", "运行设置操作身份无效")
        return self.operations / (identity + ".json")

    def _finish_current(self, record):
        if record.last_operation_id:
            path = self._operation(record.last_operation_id)
            if path.exists():
                old = json.loads(path.read_text("utf-8"))
                if old["state"] == "prepared" and old["request_hash"] == record.last_request_hash:
                    atomic_record(
                        path,
                        {**old, "state": "completed", "result": record.model_dump(mode="json")},
                    )

    def set(self, value: RuntimePreferenceInput):
        with self.lock:
            current = self.read()
            self._finish_current(current)
            path = self._operation(value.operation_id)
            digest = _hash(value.model_dump(mode="json"))
            if path.exists():
                prior = json.loads(path.read_text("utf-8"))
                if prior["request_hash"] != digest:
                    raise AppError("RUNTIME_OPERATION_CONFLICT", "设置操作身份已用于不同内容")
                if prior["state"] != "completed":
                    raise AppError(
                        "RUNTIME_OPERATION_UNSETTLED", "原设置结果尚待核对，请先核对原操作"
                    )
                return RuntimePreferenceRecord.model_validate(prior["result"])
            if current.revision != value.expected_revision:
                raise AppError(
                    "RUNTIME_PREFERENCE_CONFLICT", "运行设置已有新版本，请刷新后选择", 409
                )
            operation = {
                "operation_id": value.operation_id,
                "state": "prepared",
                "request_hash": digest,
                "expected_revision": value.expected_revision,
            }
            atomic_record(path, operation)
            result = RuntimePreferenceRecord(
                revision=current.revision + 1,
                values=value.values,
                last_operation_id=value.operation_id,
                last_request_hash=digest,
            )
            atomic_record(self.path, result.model_dump(mode="json"))
            self._finish_current(result)
            return result

    def reconcile(self, identity):
        with self.lock:
            path = self._operation(identity)
            if not path.is_file():
                raise AppError("RUNTIME_OPERATION_NOT_FOUND", "没有此设置操作记录", 404)
            self._finish_current(self.read())
            value = json.loads(path.read_text("utf-8"))
            if value["state"] == "prepared":
                value.update(state="failed", error_code="RUNTIME_PREFERENCE_NOT_PUBLISHED")
                atomic_record(path, value)
            return value


class WindowsExecutionPower:
    available = os.name == "nt"

    def set(self, active):
        if not self.available:
            raise AppError("POWER_KEEP_AWAKE_UNAVAILABLE", "当前系统不支持保持唤醒")
        import ctypes

        function = ctypes.WinDLL("kernel32", use_last_error=True).SetThreadExecutionState
        function.argtypes = [ctypes.c_uint32]
        function.restype = ctypes.c_uint32
        if not function(0x80000000 | (0x1 if active else 0)):
            raise AppError("POWER_KEEP_AWAKE_FAILED", "保持唤醒请求未能生效")


class RuntimePowerKeeper:
    def __init__(self, preferences, is_active, *, platform=None):
        self.preferences, self.is_active = preferences, is_active
        self.platform = platform or WindowsExecutionPower()
        self.changed, self.stopped = threading.Event(), threading.Event()
        self.held = False
        self.error_code = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._work, daemon=True, name="learning-power-keeper")
        self.thread.start()

    def wake(self):
        if (
            self.thread
            and not self.thread.is_alive()
            and self.error_code
            and not self.stopped.is_set()
        ):
            self.error_code = None
            self.start()
        self.changed.set()

    def _work(self):
        try:
            while not self.stopped.is_set():
                desired = (
                    self.preferences.effective().keep_awake
                    and self.platform.available
                    and self.is_active()
                )
                if desired != self.held:
                    self.platform.set(desired)
                    self.held, self.error_code = desired, None
                self.changed.wait(2)
                self.changed.clear()
        except Exception as error:
            self.error_code = getattr(error, "code", "POWER_KEEP_AWAKE_FAILED")
        finally:
            if self.held:
                try:
                    self.platform.set(False)
                    self.held = False
                except Exception as error:
                    self.error_code = getattr(error, "code", "POWER_RELEASE_FAILED")

    def close(self):
        self.stopped.set()
        self.wake()
        if self.thread:
            self.thread.join(timeout=5)

    def status(self):
        return {
            "available": self.platform.available,
            "held": self.held if self.error_code is None else None,
            "error_code": self.error_code,
        }
