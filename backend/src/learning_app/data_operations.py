"""Durable user data-operation receipts outside the exported business cut."""

import hashlib
import json
import logging
import os
import re
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from learning_app.course_files import _is_link
from learning_app.data_package import DataPackage
from learning_app.errors import AppError
from learning_app.media import _read_locked
from learning_app.storage.repository import _hash

_LOCKS = {}
_ACTIVE = {}
_LOCK = threading.Lock()
_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


def atomic_record(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "-" + uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


class DataOperations:
    def __init__(self, repository, profile):
        self.repository = repository
        self.directory = Path(profile).resolve() / "data-operations"
        if any(_is_link(p) for p in [self.directory, *self.directory.parents]):
            raise AppError("DATA_OPERATION_PATH_DENIED", "数据操作目录不允许重定向链接")
        self.directory.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            self.lock = _LOCKS.setdefault(self.directory, threading.RLock())
            self.active = _ACTIVE.setdefault(self.directory, {})

    def folder(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise AppError("DATA_OPERATION_ID_INVALID", "数据操作身份无效")
        return self.directory / identity

    def _read(self, identity):
        path = self.folder(identity) / "operation.json"
        if not path.is_file():
            raise AppError("DATA_OPERATION_NOT_FOUND", "没有此数据操作记录", 404)
        if path.stat().st_size > 1024 * 1024:
            raise AppError("DATA_OPERATION_INVALID", "数据操作记录无法可靠读取")
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as error:
            raise AppError("DATA_OPERATION_INVALID", "数据操作记录损坏") from error

    def view(self, record):
        return {
            key: record[key]
            for key in (
                "operation_id",
                "kind",
                "state",
                "created_at",
                "updated_at",
                "error_code",
                "result",
            )
            if key in record
        }

    def prepare_export(self, identity, *, include_originals=False):
        payload = {"kind": "export", "include_originals": include_originals}
        return self.prepare(identity, payload)

    def prepare(self, identity, payload):
        with self.lock:
            folder = self.folder(identity)
            path = folder / "operation.json"
            if path.exists():
                old = self._read(identity)
                if old["request_hash"] != _hash(payload):
                    raise AppError("DATA_OPERATION_CONFLICT", "操作身份已用于不同内容")
                return self.view(old)
            now = datetime.now(UTC).isoformat()
            folder.mkdir(exist_ok=False)
            record = {
                "operation_id": identity,
                "kind": payload["kind"],
                "request_hash": _hash(payload),
                "request": payload,
                "source_data": str(self.repository.root),
                "state": "prepared",
                "created_at": now,
                "updated_at": now,
            }
            atomic_record(path, record)
            return self.view(record)

    def perform_transfer(self, identity, location):
        from learning_app.data_import import prepare_import

        with self.lock:
            record = self._read(identity)
            if identity in self.active or record["state"] != "prepared":
                return self.view(record)
            cancelled = threading.Event()
            self.active[identity] = cancelled
            record.update(state="running", updated_at=datetime.now(UTC).isoformat())
            atomic_record(self.folder(identity) / "operation.json", record)

        def check():
            if cancelled.is_set():
                raise AppError("DATA_OPERATION_CANCELLED", "数据准备已停止，当前数据保持完整")

        try:
            request = record["request"]
            plan = location.plan(identity, request["kind"], request["parent"])
            check()
            if request["kind"] == "import":
                target = Path(plan["target"])
                result = prepare_import(
                    Path(request["package"]),
                    Path(plan["stage"]),
                    target / "data",
                    final_materials=target / "materials",
                    check=check,
                )
                record["result"] = {
                    "target": plan["target"],
                    "include_originals": result["include_originals"],
                    "package_id": result["package_id"],
                    "old_execution_read_only": True,
                }
            else:
                record["result"] = {"target": plan["target"], "copies_after_exit": True}
            check()
            location.ready(plan)
            record.update(state="ready", updated_at=datetime.now(UTC).isoformat())
        except Exception as error:
            if not isinstance(error, AppError):
                logging.getLogger(__name__).exception("Data transfer preparation failed")
            record.update(
                state="cancelled"
                if getattr(error, "code", None) == "DATA_OPERATION_CANCELLED"
                else "failed",
                error_code=getattr(error, "code", "DATA_TRANSFER_INTERRUPTED"),
                updated_at=datetime.now(UTC).isoformat(),
            )
        finally:
            atomic_record(self.folder(identity) / "operation.json", record)
            with self.lock:
                self.active.pop(identity, None)
        return self.view(record)

    def perform_export(self, identity):
        with self.lock:
            record = self._read(identity)
            if identity in self.active or record["state"] != "prepared":
                return self.view(record)
            cancelled = threading.Event()
            self.active[identity] = cancelled
            record.update(state="running", updated_at=datetime.now(UTC).isoformat())
            atomic_record(self.folder(identity) / "operation.json", record)
        folder = self.folder(identity)
        partial = folder / "archive.partial"
        published = folder / "archive.zip"

        def check():
            if cancelled.is_set():
                raise AppError("DATA_OPERATION_CANCELLED", "数据导出已停止，原数据保持完整")

        try:
            self.repository._hook("data_export_after_intent", operation_id=identity)
            check()
            stream, manifest = DataPackage(self.repository).build(
                include_originals=record["request"]["include_originals"], check=check
            )
            stream.seek(0, os.SEEK_END)
            size_needed = stream.tell()
            stream.seek(0)
            if shutil.disk_usage(folder).free < size_needed + 64 * 1024 * 1024:
                stream.close()
                raise AppError("DATA_EXPORT_SPACE", "导出目录空间不足，当前数据保持完整")
            digest = hashlib.sha256()
            size = 0
            with stream, partial.open("xb") as output:
                while data := stream.read(1024 * 1024):
                    check()
                    digest.update(data)
                    size += len(data)
                    output.write(data)
                output.flush()
                os.fsync(output.fileno())
            DataPackage().verify(partial, check=check)
            check()
            record["result"] = {
                "package_id": manifest["package_id"],
                "sha256": digest.hexdigest(),
                "bytes": size,
                "include_originals": manifest["include_originals"],
                "files": len(manifest["files"]),
            }
            record["updated_at"] = datetime.now(UTC).isoformat()
            atomic_record(folder / "operation.json", record)
            os.replace(partial, published)
            self.repository._hook("data_export_after_publish", operation_id=identity)
            record.update(state="completed", updated_at=datetime.now(UTC).isoformat())
            atomic_record(folder / "operation.json", record)
        except Exception as error:
            record.update(
                state="unknown"
                if published.exists()
                else "cancelled"
                if getattr(error, "code", None) == "DATA_OPERATION_CANCELLED"
                else "failed",
                error_code=getattr(error, "code", "DATA_EXPORT_INTERRUPTED"),
                updated_at=datetime.now(UTC).isoformat(),
            )
            atomic_record(folder / "operation.json", record)
        finally:
            with self.lock:
                self.active.pop(identity, None)
        return self.view(record)

    def status(self, identity):
        with self.lock:
            record = self._read(identity)
            switch = self.folder(identity) / "switch.json"
            if record["kind"] != "export" and switch.is_file():
                value = json.loads(switch.read_text("utf-8"))
                if value["state"] in {
                    "completed",
                    "failed",
                    "unknown",
                    "cancelled",
                    "publish_blocked",
                }:
                    record = {
                        **record,
                        "state": value["state"],
                        "error_code": value.get("error_code"),
                    }
            return self.view(record)

    def list(self):
        with self.lock:
            records = [
                self.status(path.name)
                for path in self.directory.iterdir()
                if path.is_dir()
                and _ID.fullmatch(path.name)
                and (path / "operation.json").is_file()
            ]
        return sorted(records, key=lambda row: row["created_at"], reverse=True)

    def cancel(self, identity):
        with self.lock:
            record = self._read(identity)
            if record["state"] in {"completed", "failed", "cancelled"}:
                return self.view(record)
            atomic_record(self.folder(identity) / "cancel-request.json", {"operation_id": identity})
            event = self.active.get(identity)
            if event:
                event.set()
            elif record["state"] == "prepared":
                record.update(
                    state="cancelled",
                    error_code="DATA_OPERATION_CANCELLED",
                    updated_at=datetime.now(UTC).isoformat(),
                )
                atomic_record(self.folder(identity) / "operation.json", record)
            return self.view(record)

    def reconcile(self, identity):
        with self.lock:
            record = self._read(identity)
            if identity in self.active:
                return self.view(record)
            if record["state"] in {"completed", "failed", "cancelled"}:
                return self.view(record)
            if record["kind"] != "export":
                switch = self.folder(identity) / "switch.json"
                value = json.loads(switch.read_text("utf-8")) if switch.is_file() else {}
                record.update(
                    state="ready"
                    if value.get("state") == "prepared"
                    else value.get("state", "failed"),
                    updated_at=datetime.now(UTC).isoformat(),
                )
                record["error_code"] = value.get("error_code")
                if record["state"] == "failed" and not record["error_code"]:
                    record["error_code"] = "DATA_TRANSFER_INTERRUPTED"
                atomic_record(self.folder(identity) / "operation.json", record)
                return self.view(record)
            archive = self.folder(identity) / "archive.zip"
            if archive.is_file() and record.get("result"):
                with _read_locked(archive) as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                verified = DataPackage().verify(archive)
                if (
                    digest != record["result"]["sha256"]
                    or verified.manifest["package_id"] != record["result"]["package_id"]
                ):
                    raise AppError("DATA_EXPORT_INTEGRITY", "导出结果与原操作不符，未重建数据包")
                record.update(
                    state="completed", error_code=None, updated_at=datetime.now(UTC).isoformat()
                )
            else:
                record.update(
                    state="failed",
                    error_code="DATA_EXPORT_INTERRUPTED",
                    updated_at=datetime.now(UTC).isoformat(),
                )
            atomic_record(self.folder(identity) / "operation.json", record)
            return self.view(record)

    def download(self, identity):
        record = self._read(identity)
        if record["kind"] != "export" or record["state"] != "completed":
            raise AppError("DATA_EXPORT_NOT_READY", "导出包尚未完成，请先核对原操作")
        archive = self.folder(identity) / "archive.zip"
        if not archive.is_file():
            raise AppError("DATA_EXPORT_MISSING", "原导出包缺失，未自动生成新包")
        with _read_locked(archive) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != record["result"]["sha256"]:
            raise AppError("DATA_EXPORT_INTEGRITY", "原导出包内容已变化")
        return archive, self.view(record)
