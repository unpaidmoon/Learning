"""Local video references and bounded HTTP byte ranges, outside the model tool surface.

Only a user initiated, capability-protected registration can introduce a path.
The playback route takes a business identity and an immutable source version.
No endpoint extracts a frame, exposes an arbitrary path, or copies a video.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, update
from starlette.background import BackgroundTask

from learning_app.errors import AppError
from learning_app.media_schema import media_assets, media_positions
from learning_app.storage import Repository
from learning_app.storage import schema as business

VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
}
_PICKER_LOCK = threading.Lock()


class MediaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MediaRegisterInput(MediaInput):
    path: str = Field(min_length=1, max_length=32760)


class MediaOpenInput(MediaInput):
    source_version: str = Field(min_length=1, max_length=160)


class MediaRelocateInput(MediaOpenInput):
    path: str | None = Field(default=None, max_length=32760)


class MediaPositionInput(MediaOpenInput):
    position_ms: int = Field(ge=0, le=315_360_000_000)
    expected_revision: int = Field(ge=0)


class MediaPositionResponse(BaseModel):
    asset_id: str
    source_version: str
    position_ms: int
    position_revision: int
    last_opened_at: str | None = None


class MediaAssetResponse(MediaPositionResponse):
    display_name: str
    availability: Literal["available", "missing", "version_mismatch"]
    media_type: str
    size_bytes: int


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _stat_signature(stat: os.stat_result) -> list[int]:
    # Windows Python stat(path) and fstat(fd) disagree about legacy st_ctime.
    # Use the real NTFS change time below instead of treating that as a change.
    return [stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino]


def _handle_signature(stream: BinaryIO) -> list[int]:
    result = _stat_signature(os.fstat(stream.fileno()))
    if os.name != "nt":
        return result + [os.fstat(stream.fileno()).st_ctime_ns]
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("creation_time", ctypes.c_int64),
            ("last_access_time", ctypes.c_int64),
            ("last_write_time", ctypes.c_int64),
            ("change_time", ctypes.c_int64),
            ("attributes", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    basic = FileBasicInfo()
    if not kernel.GetFileInformationByHandleEx(
        msvcrt.get_osfhandle(stream.fileno()), 0, ctypes.byref(basic), ctypes.sizeof(basic)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return result + [basic.change_time]


def _signature(path: Path) -> list[int]:
    with _read_locked(path) as stream:
        return _handle_signature(stream)


def _read_locked(path: Path) -> BinaryIO:
    """Windows read handle denies concurrent source writes or replacement."""
    if os.name != "nt":
        return path.open("rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING, SEQUENTIAL_SCAN.
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x08000000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(descriptor, "rb")


def _hash_file(path: Path) -> str:
    # Registration/open verification is streamed, never read_bytes()/a full Blob.
    before = _signature(path)
    with _read_locked(path) as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if before != _signature(path):
        raise AppError("SOURCE_CHANGED_DURING_READ", "原件正在改变，请稍后主动重新打开")
    return digest


def _video_path(value: str) -> tuple[Path, str]:
    path = Path(value)
    if not path.is_absolute() or value.startswith(("\\\\", "//")):
        raise AppError("VIDEO_PATH_INVALID", "请选择本机磁盘中的视频文件", 422)
    try:
        path = path.resolve(strict=True)
        if not path.is_file():
            raise AppError("VIDEO_PATH_INVALID", "请选择视频文件", 422)
        media_type = VIDEO_TYPES.get(path.suffix.lower())
        if media_type is None:
            raise AppError("VIDEO_FORMAT_UNSUPPORTED", "请选择 MP4、WebM、MOV 或 OGV 视频", 422)
        with path.open("rb") as stream:
            header = stream.read(32)
        expected = (
            header[4:8] == b"ftyp"
            if path.suffix.lower() in {".mp4", ".m4v", ".mov"}
            else header.startswith(b"\x1aE\xdf\xa3")
            if path.suffix.lower() == ".webm"
            else header.startswith(b"OggS")
        )
        if not expected:
            raise AppError("VIDEO_FORMAT_INVALID", "文件内容不是支持的视频容器", 422)
    except (OSError, ValueError) as exc:
        raise AppError("SOURCE_MISSING", "视频原件不可用，请检查路径或重新定位", 404) from exc
    return path, media_type


def choose_video() -> str | None:
    """Native Windows dialog on the request worker; no shell or helper process.

    A nonblocking lock prevents repeated clicks from creating stacked dialogs.
    The dialog is an explicit user action, never part of model execution.
    """
    if os.name != "nt":
        raise AppError("PICKER_UNAVAILABLE", "此环境不支持 Windows 文件选择，请输入完整路径", 503)
    if not _PICKER_LOCK.acquire(blocking=False):
        raise AppError("PICKER_ACTIVE", "视频选择窗口已打开，请先完成选择")
    try:
        import ctypes
        from ctypes import wintypes

        class OpenFileName(ctypes.Structure):
            _fields_ = [
                ("lStructSize", wintypes.DWORD),
                ("hwndOwner", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE),
                ("lpstrFilter", wintypes.LPCWSTR),
                ("lpstrCustomFilter", wintypes.LPWSTR),
                ("nMaxCustFilter", wintypes.DWORD),
                ("nFilterIndex", wintypes.DWORD),
                ("lpstrFile", wintypes.LPWSTR),
                ("nMaxFile", wintypes.DWORD),
                ("lpstrFileTitle", wintypes.LPWSTR),
                ("nMaxFileTitle", wintypes.DWORD),
                ("lpstrInitialDir", wintypes.LPCWSTR),
                ("lpstrTitle", wintypes.LPCWSTR),
                ("Flags", wintypes.DWORD),
                ("nFileOffset", wintypes.WORD),
                ("nFileExtension", wintypes.WORD),
                ("lpstrDefExt", wintypes.LPCWSTR),
                ("lCustData", ctypes.c_ssize_t),
                ("lpfnHook", ctypes.c_void_p),
                ("lpTemplateName", wintypes.LPCWSTR),
                ("pvReserved", ctypes.c_void_p),
                ("dwReserved", wintypes.DWORD),
                ("FlagsEx", wintypes.DWORD),
            ]

        buffer = ctypes.create_unicode_buffer(32768)
        config = OpenFileName()
        config.lStructSize = ctypes.sizeof(config)
        config.lpstrFilter = "视频文件\0*.mp4;*.m4v;*.mov;*.webm;*.ogv\0\0"
        config.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
        config.nMaxFile = len(buffer)
        config.lpstrTitle = "选择本地视频（原地引用，不复制）"
        # FILEMUSTEXIST | PATHMUSTEXIST | EXPLORER | NOCHANGEDIR.
        config.Flags = 0x1000 | 0x800 | 0x80000 | 0x8
        dialog = ctypes.WinDLL("comdlg32", use_last_error=True)
        dialog.GetOpenFileNameW.argtypes = [ctypes.POINTER(OpenFileName)]
        dialog.GetOpenFileNameW.restype = wintypes.BOOL
        dialog.CommDlgExtendedError.restype = wintypes.DWORD
        if dialog.GetOpenFileNameW(ctypes.byref(config)):
            return buffer.value
        if dialog.CommDlgExtendedError():
            raise AppError("PICKER_FAILED", "文件选择未完成，可输入完整路径重试", 503)
        return None
    finally:
        _PICKER_LOCK.release()


class MediaStore:
    """Reuse source identity and the business DB, add only media-specific state."""

    def __init__(self, repository: Repository):
        self.repository = repository
        self.engine = repository.engine
        self._lock = threading.RLock()
        self._verified: dict[tuple[str, str], list[int]] = {}

    def _row(self, asset_id: str, source_version: str | None = None) -> dict:
        query = (
            select(
                business.assets.c.display_name,
                business.source_versions,
                media_assets.c.media_type,
                media_assets.c.file_signature,
                media_positions.c.position_ms,
                media_positions.c.revision,
                media_positions.c.last_opened_at,
            )
            .join(media_assets, business.assets.c.asset_id == media_assets.c.asset_id)
            .join(
                business.source_versions,
                (business.source_versions.c.asset_id == media_assets.c.asset_id)
                & (business.source_versions.c.source_version == media_assets.c.source_version),
            )
            .join(
                media_positions,
                (media_positions.c.asset_id == media_assets.c.asset_id)
                & (media_positions.c.source_version == media_assets.c.source_version),
            )
            .where(business.assets.c.asset_id == asset_id)
        )
        with self.engine.connect() as conn:
            row = conn.execute(query).mappings().first()
        if row is None:
            raise AppError("VIDEO_NOT_FOUND", "视频尚未登记", 404)
        result = dict(row)
        if source_version is not None and result["source_version"] != source_version:
            raise AppError("SOURCE_VERSION_MISMATCH", "来源版本不一致，请核对原提交")
        return result

    def _availability(self, row: dict, *, force: bool = False, catalog: bool = False) -> str:
        path = Path(row["local_path"])
        key = (row["asset_id"], row["source_version"])
        try:
            if not path.is_file():
                return "missing"
            current = _signature(path)
            if not force and self._verified.get(key) == current:
                return "available"
            if catalog and current == row["file_signature"]:
                # Listing must not rescan every large video at startup. An
                # explicit open (and an unverified content GET) rehashes it.
                return "available"
            # On a new process and each explicit open, verify full identity.
            # Subsequent Range reads use the verified stat identity instead.
            if _hash_file(path) != row["content_hash"]:
                return "version_mismatch"
            self._verified[key] = current
            return "available"
        except (OSError, ValueError):
            return "missing"

    def _view(self, row: dict, *, availability: str | None = None) -> dict:
        return {
            "asset_id": row["asset_id"],
            "source_version": row["source_version"],
            "display_name": row["display_name"],
            "availability": availability or self._availability(row),
            "media_type": row["media_type"],
            "size_bytes": row["byte_length"],
            "position_ms": row["position_ms"],
            "position_revision": row["revision"],
            "last_opened_at": row["last_opened_at"],
        }

    def register(self, course_id: str, value: str) -> dict:
        path, media_type = _video_path(value)
        from learning_app.course_files_schema import course_roots

        with self.engine.connect() as conn:
            root = conn.execute(
                select(course_roots.c.local_path).where(course_roots.c.course_id == course_id)
            ).scalar_one_or_none()
        if root is not None and not path.is_relative_to(Path(root)):
            raise AppError(
                "COURSE_SOURCE_OUTSIDE_ROOT", "请先将外部资料显式导入课程根目录，不能继续原地关联", 422
            )
        with self._lock:
            # Reuse a previously registered file identity across courses. A
            # changed file at an old path must not retarget previous sources.
            with self.engine.connect() as conn:
                course = conn.execute(
                    select(business.courses.c.course_id).where(
                        business.courses.c.course_id == course_id
                    )
                ).first()
                existing = (
                    conn.execute(
                        select(business.source_versions)
                        .where(business.source_versions.c.local_path == str(path))
                        .order_by(business.source_versions.c.created_at.desc())
                    )
                    .mappings()
                    .first()
                )
            if course is None:
                raise AppError("COURSE_NOT_FOUND", "课程不存在", 404)
            if existing is not None:
                source = dict(existing)
                if _hash_file(path) != source["content_hash"]:
                    raise AppError(
                        "SOURCE_VERSION_MISMATCH", "同一路径的原件内容已经变化，旧来源已保留"
                    )
                self.repository.associate_source(course_id, source["asset_id"])
            else:
                # Existing repository owns the path/version/hash identity.
                source = self.repository.register_source(course_id, path)
            return self.attach_source(source, path)

    def attach_source(self, source: dict, path: Path) -> dict:
        """Use a course file's exact version without changing historical sources."""
        path, media_type = _video_path(str(path))
        if _hash_file(path) != source["content_hash"]:
            raise AppError("SOURCE_VERSION_MISMATCH", "视频内容在打开时改变，请重新主动打开")
        with self._lock:
            with self.engine.begin() as conn:
                registered = conn.execute(
                    select(media_assets.c.asset_id).where(
                        media_assets.c.asset_id == source["asset_id"]
                    )
                ).first()
                if registered is None:
                    conn.execute(
                        insert(media_assets).values(
                            asset_id=source["asset_id"],
                            source_version=source["source_version"],
                            media_type=media_type,
                            file_signature=_signature(path),
                        )
                    )
                else:
                    conn.execute(
                        update(media_assets)
                        .where(media_assets.c.asset_id == source["asset_id"])
                        .values(
                            source_version=source["source_version"],
                            media_type=media_type,
                            file_signature=_signature(path),
                        )
                    )
                position = conn.execute(
                    select(media_positions.c.asset_id).where(
                        media_positions.c.asset_id == source["asset_id"],
                        media_positions.c.source_version == source["source_version"],
                    )
                ).first()
                if position is None:
                    conn.execute(
                        insert(media_positions).values(
                            asset_id=source["asset_id"],
                            source_version=source["source_version"],
                            position_ms=0,
                            revision=0,
                        )
                    )
            return self._view(self._row(source["asset_id"]))

    def list(self, course_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            ids = (
                conn.execute(
                    select(media_assets.c.asset_id)
                    .join(
                        business.course_assets,
                        business.course_assets.c.asset_id == media_assets.c.asset_id,
                    )
                    .where(business.course_assets.c.course_id == course_id)
                )
                .scalars()
                .all()
            )
        views = []
        for asset_id in ids:
            row = self._row(asset_id)
            views.append(self._view(row, availability=self._availability(row, catalog=True)))
        unused = sorted(
            (view for view in views if not view["last_opened_at"]),
            key=lambda view: [
                (0, int(part)) if part.isdigit() else (1, part.casefold())
                for part in re.split(r"(\d+)", view["display_name"])
            ],
        )
        used = sorted(
            (view for view in views if view["last_opened_at"]),
            key=lambda view: view["last_opened_at"],
            reverse=True,
        )
        return used + unused

    def open(self, asset_id: str, version: str) -> dict:
        row = self._row(asset_id, version)
        availability = self._availability(row, force=True)
        self._require_available(availability)
        with self.engine.begin() as conn:
            # Some Windows clocks return the same instant for consecutive UI
            # operations. Preserve their transaction order without sleeping.
            previous = conn.execute(
                select(media_positions.c.last_opened_at)
                .where(media_positions.c.last_opened_at.is_not(None))
                .order_by(media_positions.c.last_opened_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            now = _now()
            if previous is not None and now <= previous:
                now = (datetime.fromisoformat(previous) + timedelta(microseconds=1)).isoformat(
                    timespec="microseconds"
                )
            conn.execute(
                update(media_positions)
                .where(
                    media_positions.c.asset_id == asset_id,
                    media_positions.c.source_version == version,
                )
                .values(last_opened_at=now)
            )
        row["last_opened_at"] = now
        return self._view(row, availability="available")

    @staticmethod
    def _require_available(availability: str) -> None:
        if availability == "missing":
            raise AppError("SOURCE_MISSING", "视频原件缺失，请主动重新定位", 404)
        if availability == "version_mismatch":
            raise AppError("SOURCE_VERSION_MISMATCH", "视频内容已经变化，不能作为旧来源播放")

    def relocate(self, asset_id: str, version: str, value: str) -> dict:
        path, _media_type = _video_path(value)
        self._row(asset_id, version)
        self.repository.relocate_source(asset_id, version, path)
        self._verified.pop((asset_id, version), None)
        with self.engine.begin() as conn:
            conn.execute(
                update(media_assets)
                .where(media_assets.c.asset_id == asset_id)
                .values(file_signature=_signature(path))
            )
        return self._view(self._row(asset_id, version))

    def save_position(self, asset_id: str, value: MediaPositionInput) -> dict:
        self._row(asset_id, value.source_version)
        with self.engine.begin() as conn:
            result = conn.execute(
                update(media_positions)
                .where(
                    media_positions.c.asset_id == asset_id,
                    media_positions.c.source_version == value.source_version,
                    media_positions.c.revision == value.expected_revision,
                )
                .values(position_ms=value.position_ms, revision=value.expected_revision + 1)
            )
            if result.rowcount != 1:
                raise AppError("POSITION_CONFLICT", "播放位置已有更新，请重新打开核对")
        return self._view(self._row(asset_id, value.source_version))

    def content(self, asset_id: str, version: str, request: Request) -> Response:
        row = self._row(asset_id, version)
        self._require_available(self._availability(row))
        size = row["byte_length"]
        etag = f'"{row["content_hash"]}"'
        headers = {"Accept-Ranges": "bytes", "ETag": etag}
        start, end = 0, size - 1
        range_header = request.headers.get("range")
        if_range = request.headers.get("if-range")
        if range_header and (if_range is None or if_range == etag):
            try:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                if not match or not any(match.groups()):
                    raise ValueError
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    suffix = int(last)
                    if suffix <= 0:
                        raise ValueError
                    start = max(0, size - suffix)
                if start < 0 or start >= size or end < start:
                    raise ValueError
            except ValueError:
                return Response(
                    status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"}
                )
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            status = 206
        else:
            status = 200
        headers["Content-Length"] = str(max(0, end - start + 1))
        if request.method == "HEAD":
            return Response(status_code=status, headers=headers, media_type=row["media_type"])

        try:
            stream = _read_locked(Path(row["local_path"]))
        except OSError as exc:
            raise AppError("SOURCE_UNAVAILABLE", "原件暂时无法读取，请稍后重新打开") from exc
        if _handle_signature(stream) != self._verified.get((asset_id, version)):
            stream.close()
            raise AppError("SOURCE_CHANGED_DURING_READ", "播放前原件已改变，请重新打开核对")

        def chunks():
            # Keep a single source handle for a bounded response. No memory
            # growth with video length; cancellation closes this generator.
            with stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    block = stream.read(min(256 * 1024, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block

        return StreamingResponse(
            chunks(),
            status_code=status,
            headers=headers,
            media_type=row["media_type"],
            background=BackgroundTask(stream.close),
        )


def install_media_routes(app: FastAPI) -> None:
    """Install without opening a DB, file picker or model during OpenAPI export."""
    store_lock = threading.Lock()

    def store() -> MediaStore:
        with store_lock:
            if not hasattr(app.state, "media_store"):
                app.state.media_store = MediaStore(app.state.repository)
        return app.state.media_store

    @app.get("/api/v1/courses/{course_id}/videos", response_model=list[MediaAssetResponse])
    def videos(course_id: str):
        return store().list(course_id)

    @app.post("/api/v1/courses/{course_id}/videos", response_model=MediaAssetResponse)
    def register(course_id: str, value: MediaRegisterInput):
        return store().register(course_id, value.path)

    @app.post("/api/v1/courses/{course_id}/videos/pick", response_model=MediaAssetResponse | None)
    def pick(course_id: str):
        selected = choose_video()
        return store().register(course_id, selected) if selected is not None else None

    @app.post("/api/v1/videos/{asset_id}/open", response_model=MediaAssetResponse)
    def open_video(asset_id: str, value: MediaOpenInput):
        return store().open(asset_id, value.source_version)

    @app.post("/api/v1/videos/{asset_id}/relocate", response_model=MediaAssetResponse | None)
    def relocate(asset_id: str, value: MediaRelocateInput):
        selected = value.path if value.path is not None else choose_video()
        return store().relocate(asset_id, value.source_version, selected) if selected else None

    @app.post("/api/v1/videos/{asset_id}/position", response_model=MediaPositionResponse)
    def position(asset_id: str, value: MediaPositionInput):
        return store().save_position(asset_id, value)

    @app.get("/api/v1/videos/{asset_id}/content")
    @app.head("/api/v1/videos/{asset_id}/content", include_in_schema=False)
    def content(asset_id: str, source_version: str, request: Request):
        return store().content(asset_id, source_version, request)
