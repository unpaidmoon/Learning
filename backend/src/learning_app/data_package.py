"""Versioned whole-business ZIP, optional originals and closed offline chat files."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import SpooledTemporaryFile
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from learning_app.course_files import _is_link
from learning_app.data_activation import STAGE_MARKER
from learning_app.data_originals import collect_originals, relative_name
from learning_app.data_snapshot import BusinessSnapshot, _readonly, validate_database
from learning_app.errors import AppError
from learning_app.media import _read_locked
from learning_app.offline_chat_resources import OfflineChatRuntime
from learning_app.offline_chats import OfflineChats, _data_html

FORMAT = "learning-data-package-v1"
MAX_FILES = 1_000_000
MAX_MANIFEST = 256 * 1024 * 1024
MAX_HTML = 64 * 1024 * 1024
_SHA = re.compile(r"^[a-f0-9]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


def _validate_data_files(root, manifest):
    files = {row["path"] for row in manifest["files"]}
    required = {"data/business.sqlite3"}
    with closing(_readonly(root / "data/business.sqlite3")) as connection:
        for row in connection.execute("SELECT content_hash FROM attachments"):
            digest = row[0]
            if not isinstance(digest, str) or not _SHA.fullmatch(digest):
                raise AppError("DATA_ASSET_ID_INVALID", "附件标识无效")
            required.add(f"data/attachments/objects/{digest[:2]}/{digest}")
        for row in connection.execute("SELECT reference FROM learning_snapshots"):
            ref = json.loads(row[0])
            if ref.get("kind") in {"document_image", "document_region_image"}:
                digest = ref["image_sha256"]
                if not isinstance(digest, str) or not _SHA.fullmatch(digest):
                    raise AppError("DATA_ASSET_ID_INVALID", "来源图标识无效")
                required.add(f"data/learning-source-images/{digest}.png")
        if "data/desktop-ui/theme.json" in files:
            value = json.loads((root / "data/desktop-ui/theme.json").read_text("utf-8"))
            if (
                set(value) != {"version", "preference", "revision"}
                or value["version"] != 1
                or value["preference"] not in {"light", "dark", "system"}
                or type(value["revision"]) is not int
                or value["revision"] < 0
            ):
                raise AppError("DATA_PREFERENCE_UNSUPPORTED", "外观设置内容无效")
            required.add("data/desktop-ui/theme.json")
        if "data/desktop-ui/runtime.json" in files:
            from pydantic import ValidationError

            from learning_app.runtime_preferences import RuntimePreferenceRecord

            try:
                RuntimePreferenceRecord.model_validate_json(
                    (root / "data/desktop-ui/runtime.json").read_bytes()
                )
            except ValidationError:
                raise AppError(
                    "DATA_PREFERENCE_UNSUPPORTED", "运行设置内容不受当前版本支持"
                ) from None
            required.add("data/desktop-ui/runtime.json")
        if "data/desktop-ui/updates.json" in files:
            from learning_app.desktop_updates import UpdatePreferences

            try:
                UpdatePreferences.model_validate_json(
                    (root / "data/desktop-ui/updates.json").read_bytes()
                )
            except Exception:
                raise AppError(
                    "DATA_PREFERENCE_UNSUPPORTED", "更新设置内容不受当前版本支持"
                ) from None
            required.add("data/desktop-ui/updates.json")
        if required != {name for name in files if name.startswith("data/")}:
            raise AppError("DATA_PACKAGE_ASSET_CLOSURE", "业务数据与文件闭包不一致")
        if manifest["include_originals"]:
            actual = {
                (r["asset_id"], r["source_version"]): (r["sha256"], r["bytes"])
                for r in manifest["originals"]["source_versions"]
            }
            expected = {
                (r[0], r[1]): (r[2], r[3])
                for r in connection.execute(
                    "SELECT asset_id,source_version,content_hash,byte_length FROM source_versions"
                )
            }
            if actual != expected or len(actual) != len(manifest["originals"]["source_versions"]):
                raise AppError("DATA_PACKAGE_ORIGINALS_INVALID", "原件版本关联不完整")
            courses = {r[0] for r in connection.execute("SELECT course_id FROM courses")}
            seen = set()
            for row in manifest["originals"]["course_files"]:
                key = (row["course_id"], row["relative_path"].casefold())
                if row["course_id"] not in courses or key in seen:
                    raise AppError("DATA_PACKAGE_ORIGINALS_INVALID", "原件课程或路径关联无效")
                seen.add(key)


def _publish_new_directory(source, target, parent):
    source, target = source.resolve(), target.resolve()
    if source.parent != parent or target.parent != parent or target.exists():
        raise AppError("DATA_IMPORT_TARGET_EXISTS", "目标已存在或位置已改变，原数据未覆盖")
    if os.name == "nt":
        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move.restype = ctypes.c_int
        if not move(str(source), str(target), 0x8):
            code = ctypes.get_last_error()
            _publication_diagnostic(source, target, code)
            raise AppError(
                "DATA_IMPORT_PUBLISH_FAILED", "暂存数据未发布，目标数据保持不变"
            ) from ctypes.WinError(code)
    else:
        os.rename(source, target)


def _publication_diagnostic(source, target, error_code):
    """Inspect delete-sharing on this owned stage only; never deletes a file."""
    try:
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        rows = []
        for item in [source, *source.rglob("*")]:
            # DELETE access and all share flags test sharing, not a mutation.
            handle = create(str(item), 0x10000, 7, None, 3, 0x02000000, None)
            denied = handle == wintypes.HANDLE(-1).value
            failure = ctypes.get_last_error() if denied else None
            if not denied:
                close(handle)
            if denied:
                rows.append({"path": item.relative_to(source).as_posix(), "error": failure})
        record = {
            "error": error_code,
            "stage_exists": source.exists(),
            "target_exists": target.exists(),
            "delete_access_failures": rows,
        }
        (source.parent / ("publish-diagnostic-" + uuid4().hex + ".json")).write_text(
            json.dumps(record), "utf-8"
        )
    except Exception:
        # Diagnostics never replace the original failure or authorize a retry.
        pass


def _member(name):
    relative_name(name)
    if name in {
        "data/business.sqlite3",
        "data/desktop-ui/theme.json",
        "data/desktop-ui/runtime.json",
        "data/desktop-ui/updates.json",
        "offline/index.html",
        "offline/reader/reader.js",
        "offline/reader/reader.css",
        "offline/reader/notices.txt",
    }:
        return name
    match = re.fullmatch(r"data/attachments/objects/([a-f0-9]{2})/([a-f0-9]{64})", name)
    if match and match[1] == match[2][:2]:
        return name
    if re.fullmatch(
        r"data/learning-source-images/[a-f0-9]{64}\.png|originals/objects/[a-f0-9]{64}|offline/chats/[A-Za-z0-9_-]{1,170}\.html",
        name,
    ):
        return name
    raise AppError("DATA_PACKAGE_MEMBER_DENIED", "数据包包含未允许的成员")


def _write_file(archive, source, name, expected=None, expected_size=None, *, check=lambda: None):
    _member(name)
    digest, length = hashlib.sha256(), 0
    with _read_locked(source) as incoming, archive.open(name, "w", force_zip64=True) as outgoing:
        while data := incoming.read(1024 * 1024):
            check()
            digest.update(data)
            length += len(data)
            outgoing.write(data)
    actual = digest.hexdigest()
    if expected is not None and (actual != expected or length != expected_size):
        raise AppError("DATA_PACKAGE_SOURCE_CHANGED", "打包期间资料已变化，本次包未发布")
    return {"path": name, "bytes": length, "sha256": actual}


@dataclass(frozen=True)
class VerifiedPackage:
    manifest: dict
    logical_bytes: int


class DataPackage:
    def __init__(self, repository=None, chat_runtime=None):
        self.repository = repository
        self.chat_runtime = chat_runtime or OfflineChatRuntime()

    def build(self, *, include_originals=False, check=lambda: None):
        if self.repository is None:
            raise AppError("DATA_EXPORT_SOURCE_REQUIRED", "缺少要导出的软件数据")
        spool = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        try:
            with BusinessSnapshot(self.repository).capture(check=check) as cut:
                originals = (
                    collect_originals(cut, self.repository.root, check=check)
                    if include_originals
                    else None
                )
                manifest = {
                    "format": FORMAT,
                    "package_id": "package_" + uuid4().hex,
                    "schema_revision": cut.schema_revision,
                    "created_at": datetime.now(UTC).isoformat(),
                    "include_originals": include_originals,
                    "files": [],
                    "unresolved_operations": list(cut.unresolved_executions),
                    "originals": {
                        "course_files": list(originals.course_files) if originals else [],
                        "source_versions": list(originals.source_versions) if originals else [],
                    },
                    "chat_runtime": {
                        "release_id": self.chat_runtime.lock["release_id"],
                        "files": self.chat_runtime.lock["files"],
                    },
                }
                offline = cut.directory / "offline"
                OfflineChats(self.chat_runtime).write(cut, offline)
                needed = sum(row["bytes"] for row in cut.files)
                needed += (
                    sum(row["bytes"] for row in originals.objects.values()) if originals else 0
                )
                needed += sum(path.stat().st_size for path in offline.rglob("*") if path.is_file())
                if (
                    shutil.disk_usage(tempfile.gettempdir()).free
                    < needed + needed // 100 + 64 * 1024 * 1024
                ):
                    raise AppError("DATA_EXPORT_SPACE", "临时磁盘空间不足，未输出不完整数据包")
                check()
                with ZipFile(
                    spool, "w", compression=ZIP_DEFLATED, compresslevel=6, allowZip64=True
                ) as archive:
                    for row in cut.files:
                        manifest["files"].append(
                            _write_file(
                                archive,
                                cut.directory / row["path"],
                                "data/" + row["path"],
                                row["sha256"],
                                row["bytes"],
                                check=check,
                            )
                        )
                    for path in sorted(offline.rglob("*")):
                        if path.is_file():
                            manifest["files"].append(
                                _write_file(
                                    archive,
                                    path,
                                    "offline/" + path.relative_to(offline).as_posix(),
                                    check=check,
                                )
                            )
                    if originals:
                        for row in originals.objects.values():
                            manifest["files"].append(
                                _write_file(
                                    archive,
                                    row["source"],
                                    row["path"],
                                    row["sha256"],
                                    row["bytes"],
                                    check=check,
                                )
                            )
                    if len(manifest["files"]) > MAX_FILES:
                        raise AppError("DATA_PACKAGE_FILE_LIMIT", "数据包成员过多，未输出部分包")
                    metadata = json.dumps(
                        manifest, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    if len(metadata) > MAX_MANIFEST:
                        raise AppError(
                            "DATA_PACKAGE_MANIFEST_LIMIT", "数据包清单超出支持范围，未截断"
                        )
                    self.repository._hook(
                        "data_package_before_finish", package_id=manifest["package_id"]
                    )
                    check()
                    archive.writestr("manifest.json", metadata)
            spool.seek(0)
            return spool, manifest
        except BaseException:
            spool.close()
            raise

    def verify(self, package, *, check=lambda: None):
        try:
            with ZipFile(package) as archive:
                entries = archive.infolist()
                if not 1 < len(entries) <= MAX_FILES + 1 or len(
                    {e.filename.casefold() for e in entries}
                ) != len(entries):
                    raise AppError("DATA_PACKAGE_FILE_LIMIT", "数据包成员数量或名称存在冲突")
                if "manifest.json" not in archive.namelist():
                    raise AppError("DATA_PACKAGE_MANIFEST_MISSING", "数据包缺少清单")
                info = archive.getinfo("manifest.json")
                if info.file_size > MAX_MANIFEST:
                    raise AppError("DATA_PACKAGE_MANIFEST_LIMIT", "数据包清单过大")
                manifest = json.loads(archive.read(info))
                if manifest.get("format") != FORMAT or not isinstance(
                    manifest.get("include_originals"), bool
                ):
                    raise AppError("DATA_PACKAGE_FORMAT", "数据包格式不受支持")
                rows = manifest.get("files")
                if not isinstance(rows, list) or len(rows) != len(entries) - 1:
                    raise AppError("DATA_PACKAGE_MANIFEST_MISMATCH", "数据包清单与实际成员不符")
                expected = {}
                for row in rows:
                    check()
                    name = _member(row["path"])
                    if (
                        name in expected
                        or type(row["bytes"]) is not int
                        or row["bytes"] < 0
                        or not isinstance(row["sha256"], str)
                        or not _SHA.fullmatch(row["sha256"])
                    ):
                        raise AppError("DATA_PACKAGE_MANIFEST_MISMATCH", "数据包成员声明无效")
                    expected[name] = row
                if set(expected) | {"manifest.json"} != {e.filename for e in entries}:
                    raise AppError("DATA_PACKAGE_MANIFEST_MISMATCH", "数据包存在未声明成员")
                runtime = self.chat_runtime.read()
                if manifest.get("chat_runtime") != {
                    "release_id": self.chat_runtime.lock["release_id"],
                    "files": self.chat_runtime.lock["files"],
                }:
                    raise AppError("DATA_PACKAGE_READER_VERSION", "离线聊天资源版本不受支持")
                for entry in entries:
                    if (
                        entry.is_dir()
                        or entry.flag_bits & 1
                        or entry.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
                        or (entry.external_attr >> 16) & 0o170000 not in {0, 0o100000}
                    ):
                        raise AppError("DATA_PACKAGE_MEMBER_DENIED", "数据包成员类型不受支持")
                    if entry.filename == "manifest.json":
                        continue
                    row = expected[entry.filename]
                    if entry.file_size != row["bytes"]:
                        raise AppError("DATA_PACKAGE_MANIFEST_MISMATCH", "数据包成员长度不符")
                    digest = hashlib.sha256()
                    with archive.open(entry) as stream:
                        while data := stream.read(1024 * 1024):
                            check()
                            digest.update(data)
                    if digest.hexdigest() != row["sha256"]:
                        raise AppError("DATA_PACKAGE_HASH_MISMATCH", "数据包内容校验失败")
                    if entry.filename.startswith("offline/reader/"):
                        if archive.read(entry) != runtime[entry.filename.split("/")[-1]]:
                            raise AppError(
                                "DATA_PACKAGE_READER_VERSION", "离线阅读代码与固定版本不符"
                            )
                    elif entry.filename.endswith(".html"):
                        if entry.file_size > MAX_HTML:
                            raise AppError(
                                "DATA_PACKAGE_CHAT_LIMIT", "单页聊天超出支持范围，未截断"
                            )
                        raw = archive.read(entry)
                        match = re.search(
                            rb'<script type="application/json" id="learning-offline-chat-data">(.*?)</script>',
                            raw,
                            re.S,
                        )
                        prefix = "" if entry.filename == "offline/index.html" else "../"
                        if not match or _data_html(json.loads(match[1]), prefix) != raw:
                            raise AppError("DATA_PACKAGE_CHAT_TEMPLATE", "聊天页不是固定的只读模板")
                required = {
                    "data/business.sqlite3",
                    "offline/index.html",
                    *["offline/reader/" + name for name in runtime],
                }
                if not required <= set(expected):
                    raise AppError(
                        "DATA_PACKAGE_MANIFEST_MISMATCH", "数据包缺少必须的业务或阅读资源"
                    )
                originals = manifest.get("originals")
                if not isinstance(originals, dict) or set(originals) != {
                    "course_files",
                    "source_versions",
                }:
                    raise AppError("DATA_PACKAGE_ORIGINALS_INVALID", "原件关联清单无效")
                if not manifest["include_originals"] and (
                    any(n.startswith("originals/") for n in expected)
                    or originals["course_files"]
                    or originals["source_versions"]
                ):
                    raise AppError(
                        "DATA_PACKAGE_ORIGINALS_INVALID", "默认软件数据包不应包含未声明原件"
                    )
                for record in [*originals["course_files"], *originals["source_versions"]]:
                    digest = record["sha256"]
                    if (
                        not isinstance(digest, str)
                        or not _SHA.fullmatch(digest)
                        or f"originals/objects/{digest}" not in expected
                        or expected[f"originals/objects/{digest}"]["bytes"] != record["bytes"]
                    ):
                        raise AppError("DATA_PACKAGE_ORIGINALS_INVALID", "原件关联与实际字节不符")
                    if "relative_path" in record:
                        relative_name(record["relative_path"])
                    for key in ("course_id", "asset_id", "source_version"):
                        if key in record and (
                            not isinstance(record[key], str) or not _ID.fullmatch(record[key])
                        ):
                            raise AppError("DATA_PACKAGE_ORIGINALS_INVALID", "原件关联身份无效")
                return VerifiedPackage(manifest, sum(r["bytes"] for r in rows))
        except AppError:
            raise
        except Exception as error:
            raise AppError("DATA_PACKAGE_INVALID", "数据包无法完整读取或格式损坏") from error

    def stage(self, package, target, *, check=lambda: None):
        """Extract only into a fresh owned stage. Never activate or replace data."""
        if any(_is_link(p) for p in [target, *target.parents]):
            raise AppError("DATA_PACKAGE_PATH_DENIED", "导入目标不允许重定向链接")
        with _read_locked(Path(package)) as source:
            return self._stage_locked(source, target, check=check)

    def _stage_locked(self, package, target, *, check=lambda: None):
        verified = self.verify(package, check=check)
        if target.exists():
            raise AppError("DATA_IMPORT_TARGET_EXISTS", "目标已存在，不合并或覆盖已有数据")
        parent = target.parent.resolve()
        if shutil.disk_usage(parent).free < verified.logical_bytes + 64 * 1024 * 1024:
            raise AppError("DATA_IMPORT_SPACE", "目标空间不足，原数据保持不变")
        # The exclusive stage has a stable name; readiness is a durable record,
        # never inferred from directory existence or a directory rename.
        from learning_app.data_operations import atomic_record

        target.mkdir(exist_ok=False)
        owned = target
        marker = {"format": 1, "state": "extracting", "package_id": verified.manifest["package_id"]}
        atomic_record(owned / STAGE_MARKER, marker)
        try:
            with ZipFile(package) as archive:
                for row in verified.manifest["files"]:
                    name = row["path"]
                    destination = owned.joinpath(*name.split("/"))
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as incoming, destination.open("xb") as outgoing:
                        digest, length = hashlib.sha256(), 0
                        while data := incoming.read(1024 * 1024):
                            check()
                            digest.update(data)
                            length += len(data)
                            outgoing.write(data)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    if digest.hexdigest() != row["sha256"] or length != row["bytes"]:
                        raise AppError("DATA_PACKAGE_HASH_MISMATCH", "提取期间数据包内容不符")
            revision = validate_database(owned / "data/business.sqlite3")
            if revision != verified.manifest["schema_revision"]:
                raise AppError("DATA_SCHEMA_UNSUPPORTED", "数据包版本声明与数据库不符")
            _validate_data_files(owned, verified.manifest)
            (owned / "manifest.json").write_text(
                json.dumps(verified.manifest, ensure_ascii=False, separators=(",", ":")), "utf-8"
            )
            # No import activation here; callers must install the persistent old
            # execution fence before any service opens the extracted database.
            check()
            atomic_record(owned / STAGE_MARKER, {**marker, "state": "ready"})
        except Exception as error:
            atomic_record(
                owned / STAGE_MARKER,
                {
                    **marker,
                    "state": "failed",
                    "error_code": getattr(error, "code", "DATA_STAGE_INTERRUPTED"),
                },
            )
            raise
        return verified
