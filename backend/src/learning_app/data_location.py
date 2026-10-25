"""Stable local profile pointer; publish only after the owned services close."""

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from learning_app.course_files import _is_link
from learning_app.data_activation import STAGE_MARKER, activate_stage, stage_identity
from learning_app.data_operations import _ID, atomic_record
from learning_app.data_package import _publish_new_directory
from learning_app.data_snapshot import validate_database
from learning_app.errors import AppError
from learning_app.media import _read_locked

_TOP = {
    "maintenance-state.json",
    "business.sqlite3",
    "attachments",
    "execution",
    "learning-source-images",
    "preparation-evidence",
    "migration-recovery",
    "file-transfers",
    "desktop-ui",
    "video-previews",
    "credential-route.json",
    "data-import.json",
}


def local_directory(value):
    path = Path(value).absolute()
    if not path.is_dir() or any(_is_link(p) for p in [path, *path.parents]):
        raise AppError("DATA_LOCATION_UNAVAILABLE", "请选择可用的本地文件夹，不能使用重定向链接")
    return path.resolve()


def inventory(directory):
    result = []
    for parent, dirs, files in os.walk(directory, followlinks=False):
        for name in dirs + files:
            if _is_link(Path(parent) / name):
                raise AppError("DATA_LOCATION_LINK_DENIED", "软件数据含重定向链接，未迁移数据")
        for name in sorted(files):
            path = Path(parent) / name
            with _read_locked(path) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                size = os.fstat(stream.fileno()).st_size
            result.append(
                {"path": path.relative_to(directory).as_posix(), "sha256": digest, "bytes": size}
            )
    return sorted(result, key=lambda row: row["path"])


def activation_inventory(directory):
    return [row for row in inventory(directory) if row["path"] != STAGE_MARKER]


class ProfileLocation:
    def __init__(self, initial, *, fault_hook=None):
        self.initial = Path(initial).resolve()
        self.anchor = self.initial.with_name("." + self.initial.name + "-profile")
        if any(_is_link(p) for p in [self.anchor, *self.anchor.parents]):
            raise AppError("DATA_LOCATION_LINK_DENIED", "数据位置配置不能使用重定向链接")
        self.anchor.mkdir(parents=True, exist_ok=True)
        self.pointer = self.anchor / "location.json"
        self.pending = self.anchor / "pending-switch.json"
        self.fault_hook = fault_hook

    def hook(self, name):
        if self.fault_hook:
            self.fault_hook(name)

    @contextmanager
    def lease(self):
        """One local writer, including when two launches choose different ports."""
        stream = (self.anchor / "profile.lock").open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise AppError("DATA_PROFILE_IN_USE", "此数据位置仍被另一个学习窗口使用") from error
            yield
        finally:
            stream.close()

    def current(self):
        if not self.pointer.exists():
            return self.initial
        try:
            record = json.loads(self.pointer.read_text("utf-8"))
            path = local_directory(record["active_data"])
            if record["format"] != 1 or not (path / "business.sqlite3").is_file():
                raise ValueError()
            return path
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise AppError(
                "DATA_LOCATION_INVALID", "数据位置记录无法核对，未自动创建空数据库"
            ) from error

    def transfer(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise AppError("DATA_OPERATION_ID_INVALID", "数据操作身份无效")
        return self.anchor / "data-operations" / identity / "switch.json"

    def plan(self, identity, kind, parent):
        path = self.transfer(identity)
        parent = local_directory(parent)
        target = parent / ("学习数据-" + identity)
        stage = parent / (".learning-stage-" + identity)
        source = self.current()
        if parent == source or parent.is_relative_to(source) or source.is_relative_to(target):
            raise AppError("DATA_LOCATION_OVERLAP", "目标不能位于当前软件数据内或包含当前数据")
        old = json.loads(path.read_text("utf-8")) if path.exists() else None
        if old:
            if old["kind"] != kind or old["target"] != str(target):
                raise AppError("DATA_OPERATION_CONFLICT", "此操作身份已用于不同位置")
            return old
        if target.exists() or stage.exists():
            raise AppError("DATA_IMPORT_TARGET_EXISTS", "目标位置已有内容，未合并或覆盖")
        record = {
            "format": 1,
            "operation_id": identity,
            "kind": kind,
            "source": str(source),
            "target": str(target),
            "stage": str(stage),
            "state": "planned",
        }
        atomic_record(path, record)
        return record

    def ready(self, record):
        record = {**record, "state": "prepared"}
        if record["kind"] == "import":
            record["prepared_files"] = inventory(Path(record["stage"]))
        atomic_record(self.transfer(record["operation_id"]), record)
        return record

    def schedule(self, identity):
        path = self.transfer(identity)
        if not path.is_file():
            raise AppError("DATA_OPERATION_NOT_FOUND", "没有已准备的数据切换操作", 404)
        record = json.loads(path.read_text("utf-8"))
        if record["state"] == "completed":
            return record
        if record["source"] != str(self.current()):
            raise AppError(
                "DATA_LOCATION_SOURCE_CHANGED", "当前数据位置已变化，请重新准备并核对切换操作"
            )
        if record["state"] not in {"prepared", "completed", "publish_blocked"}:
            raise AppError("DATA_SWITCH_NOT_READY", "请先完成数据校验")
        if self.pending.exists():
            old = json.loads(self.pending.read_text("utf-8"))
            if old["operation_id"] != identity:
                raise AppError("DATA_SWITCH_ALREADY_PENDING", "已有数据切换等待安全退出")
        if record["state"] == "publish_blocked":
            record.update(state="prepared", error_code=None)
            atomic_record(path, record)
        atomic_record(self.pending, {"operation_id": identity})
        return record

    def cancel_schedule(self, identity):
        if self.pending.exists():
            if json.loads(self.pending.read_text("utf-8"))["operation_id"] != identity:
                raise AppError("DATA_OPERATION_CONFLICT", "待切换的是另一个数据操作")
            self.pending.unlink()

    def _copy_closed(self, record):
        source, stage = Path(record["source"]), Path(record["stage"])
        unexpected = {p.name for p in source.iterdir()} - _TOP
        if unexpected:
            raise AppError(
                "DATA_LOCATION_UNKNOWN_FILES", "软件数据含未识别文件，原位置保留，请先核对"
            )
        files = inventory(source)
        if shutil.disk_usage(stage.parent).free < sum(r["bytes"] for r in files) + 64 * 1024 * 1024:
            raise AppError("DATA_LOCATION_SPACE", "目标空间不足，未切换数据位置")
        stage.mkdir(exist_ok=False)
        atomic_record(stage / STAGE_MARKER, {"format": 1, "state": "extracting", "kind": "move"})
        for row in files:
            incoming, outgoing = source / row["path"], stage / "data" / row["path"]
            outgoing.parent.mkdir(parents=True, exist_ok=True)
            with _read_locked(incoming) as original, outgoing.open("xb") as copied:
                shutil.copyfileobj(original, copied, 1024 * 1024)
                copied.flush()
                os.fsync(copied.fileno())
        if inventory(stage / "data") != files or inventory(source) != files:
            raise AppError("DATA_LOCATION_CONTENT_CHANGED", "复制校验不一致，未切换数据位置")
        record["source_files"] = files
        validate_database(stage / "data/business.sqlite3")
        if (source / "execution/host/home").is_dir():
            relocation_file = stage / "data/execution/workspace-relocations.json"
            origins = [str(source / "execution/host/workspace")]
            if relocation_file.exists():
                prior = json.loads(relocation_file.read_text("utf-8"))
                if prior.get("format") != 1 or prior.get("target") != origins[0]:
                    raise AppError("DATA_NATIVE_WORKSPACE_MAPPING", "原执行目录迁移记录无法核对")
                origins.extend(prior["origins"])
            atomic_record(
                relocation_file,
                {
                    "format": 1,
                    "origins": list(dict.fromkeys(origins)),
                    "target": str(Path(record["target"]) / "data/execution/host/workspace"),
                },
            )

        atomic_record(stage / STAGE_MARKER, {"format": 1, "state": "ready", "kind": "move"})

    def apply_closed(self):
        """Only the outer owner calls this after closing its complete process group."""
        if not self.pending.exists():
            return None
        identity = json.loads(self.pending.read_text("utf-8"))["operation_id"]
        path = self.transfer(identity)
        record = json.loads(path.read_text("utf-8"))
        if record["state"] in {"failed", "cancelled", "publish_blocked"}:
            raise AppError(
                "DATA_SWITCH_NEEDS_REVIEW", "原数据切换未完成，请在数据管理中核对后创建新操作"
            )
        target, stage = Path(record["target"]), Path(record["stage"])
        parent = local_directory(target.parent)
        if (
            target != parent / ("学习数据-" + identity)
            or stage != parent / (".learning-stage-" + identity)
            or _is_link(target)
            or _is_link(stage)
        ):
            raise AppError("DATA_LOCATION_INVALID", "目标位置已改变，未切换数据")
        marker = {"operation_id": identity, "source": record["source"], "kind": record["kind"]}
        try:
            if target.exists():
                if json.loads((target / "activation.json").read_text("utf-8")) != marker:
                    raise AppError("DATA_IMPORT_TARGET_EXISTS", "目标不是原操作结果，未覆盖")
                if (
                    record["kind"] == "move"
                    and self.current() == Path(record["source"])
                    and inventory(Path(record["source"])) != record.get("source_files")
                ):
                    raise AppError(
                        "DATA_LOCATION_CONTENT_CHANGED",
                        "原位置已有新的学习数据，请重新准备迁移，两份数据均保留",
                    )
            else:
                if record["source"] != str(self.current()):
                    raise AppError(
                        "DATA_LOCATION_SOURCE_CHANGED", "当前数据位置与原操作不符，未切换"
                    )
                if (
                    record["kind"] == "move"
                    and record.get("activation_files")
                    and inventory(Path(record["source"])) != record.get("source_files")
                ):
                    raise AppError(
                        "DATA_LOCATION_CONTENT_CHANGED",
                        "原位置已有新的学习数据，请重新准备迁移，旧暂存和原数据均保留",
                    )
                expected = record.get("activation_files")
                if (expected and activation_inventory(stage) != expected) or (
                    record["kind"] == "import"
                    and not expected
                    and inventory(stage) != record.get("prepared_files")
                ):
                    raise AppError(
                        "DATA_IMPORT_STAGE_CHANGED", "已校验的导入暂存内容发生变化，原数据保持不变"
                    )
                if record["kind"] == "move" and not record.get("activation_files"):
                    self._copy_closed(record)
                elif record["kind"] not in {"move", "import"} or (
                    record["kind"] == "import" and not (stage / "prepared-import.json").is_file()
                ):
                    raise AppError("DATA_SWITCH_NOT_READY", "导入暂存校验尚未完成")
                validate_database(stage / "data/business.sqlite3")
                identity_record = stage_identity(stage, {"ready"})
                if record.get("stage_identity", identity_record) != identity_record:
                    raise AppError("DATA_STAGE_NOT_READY", "数据暂存身份发生变化，未切换位置")
                record["stage_identity"] = identity_record
                atomic_record(stage / "activation.json", marker)
                record["activation_files"] = activation_inventory(stage)
                atomic_record(path, record)
                self.hook("before_publish")
                _publish_new_directory(stage, target, target.parent.resolve())
            self.hook("after_publish")
            if stage_identity(target, {"ready", "active"}) != record.get("stage_identity"):
                raise AppError("DATA_STAGE_NOT_READY", "已发布数据的暂存身份无法核对")
            if activation_inventory(target) != record.get("activation_files"):
                raise AppError(
                    "DATA_LOCATION_CONTENT_CHANGED", "已发布数据与原操作校验不符，未切换位置"
                )
            validate_database(target / "data/business.sqlite3")
            activate_stage(target)
            atomic_record(
                self.pointer,
                {"format": 1, "operation_id": identity, "active_data": str(target / "data")},
            )
            self.hook("after_pointer")
            record.update(state="completed", active_data=str(target / "data"), error_code=None)
            atomic_record(path, record)
            self.pending.unlink()
            return record
        except Exception as error:
            blocked = (
                getattr(error, "code", None) == "DATA_IMPORT_PUBLISH_FAILED"
                and stage.exists()
                and not target.exists()
            )
            record.update(
                state="publish_blocked"
                if blocked
                else "unknown"
                if target.exists() and getattr(error, "code", None) != "DATA_IMPORT_TARGET_EXISTS"
                else "failed",
                error_code=getattr(error, "code", "DATA_SWITCH_INTERRUPTED"),
            )
            atomic_record(path, record)
            if blocked and self.pending.exists():
                # The verified bytes remain. Startup/reconciliation never
                # retries a blocked publication without a new explicit action.
                self.pending.unlink()
            raise
