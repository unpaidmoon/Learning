"""Local-only, resumable video thumbnails; never an attachment or model tool.

Course enrollment authorizes this worker to decode nearby frames for the UI.
The sole worker yields between file chunks and frames. Foreground interaction
or an active question cancels only the worker's currently owned ffmpeg child.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from learning_app.errors import AppError
from learning_app.media import VIDEO_TYPES, MediaStore, _read_locked, _signature
from learning_app.processes import OwnedProcessGroup

REVISION = "thumbnail-jpeg-240x135-v1"
WIDTH, HEIGHT = 240, 135
TOOL_SHA256 = "d1e2a156261ecc675081943197a85f08f2868784a0af499171ede89353edad31"
TOOL_VERSION = "8.1"
MAX_FRAMES = 1200
DEFAULT_INTERVAL_MS = 2000
_TOOL_VERIFICATIONS: dict[tuple[str, tuple[int, ...]], str] = {}
_TOOL_VERIFICATION_LOCK = threading.Lock()


class PreviewFrame(BaseModel):
    index: int
    time_ms: int
    url: str


class PreviewManifest(BaseModel):
    asset_id: str
    source_version: str
    status: Literal["queued", "building", "ready", "unavailable", "failed"]
    duration_ms: int | None = None
    interval_ms: int = DEFAULT_INTERVAL_MS
    width: int = WIDTH
    height: int = HEIGHT
    frames: list[PreviewFrame] = Field(default_factory=list)
    error_code: str | None = None


class CourseCoverResponse(BaseModel):
    status: Literal["queued", "ready", "unavailable"]
    source_version: str
    url: str | None = None
    width: int = WIDTH
    height: int = HEIGHT


class PreviewPriorityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    active: bool
    kind: Literal["playback", "interaction"] = "interaction"
    ttl_ms: int = Field(default=3000, ge=100, le=15000)


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial.json")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _key(content_hash: str) -> str:
    return hashlib.sha256((REVISION + ":" + content_hash).encode()).hexdigest()


def _file_key(path: Path, fingerprint: list[int]) -> str:
    return hashlib.sha256((str(path.resolve()).casefold() + ":" + json.dumps(fingerprint)).encode()).hexdigest()


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def thumbnail_times(duration_ms: int | None) -> tuple[int, list[int]]:
    if not duration_ms or duration_ms < 1:
        return DEFAULT_INTERVAL_MS, [0]
    interval = max(DEFAULT_INTERVAL_MS, (duration_ms + MAX_FRAMES - 1) // MAX_FRAMES)
    times = list(range(0, duration_ms, interval))
    return interval, times


def _frame_order(count: int) -> list[int]:
    # First a start frame, then coarse coverage across the whole video. Hover
    # can immediately reuse a nearby cached frame while finer coverage grows.
    coarse = list(dict.fromkeys(round(index * (count - 1) / 15) for index in range(16)))
    return coarse + [index for index in range(count) if index not in set(coarse)]


class _Interrupted(Exception):
    pass


class VideoPreviewCache:
    def __init__(self, repository, project_root: Path, *, is_busy: Callable[[], bool] | None = None,
                 ffmpeg_path: Path | None = None):
        self.repository = repository
        self.project_root = Path(project_root)
        self.root = repository.root / "video-previews"
        self.root.mkdir(parents=True, exist_ok=True)
        self.media = MediaStore(repository)
        self.is_busy = is_busy or (lambda: False)
        self._requested_tool = ffmpeg_path
        self._tool: Path | None = None
        self._tool_error: str | None = None
        self._lock = threading.RLock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._leases: dict[str, tuple[str, float]] = {}
        self._courses: deque[str] = deque()
        self._jobs: dict[str, dict] = {}
        self._queue: deque[str] = deque()
        self._thread: threading.Thread | None = None
        self._current_group: OwnedProcessGroup | None = None
        state = self.root / "queue.json"
        if state.is_file():
            try:
                saved = json.loads(state.read_text("utf-8"))
                if saved.get("revision") == REVISION:
                    self._jobs = saved.get("jobs", {})
                    self._courses.extend(saved.get("courses", []))
                    self._queue.extend(key for key, job in self._jobs.items() if not job.get("done"))
            except (OSError, ValueError, TypeError):
                # A corrupt derived queue never changes any source or asset.
                self._jobs = {}
        repository.video_previews = self

    def _persist(self) -> None:
        with self._lock:
            _write(self.root / "queue.json", {"revision": REVISION, "jobs": self._jobs,
                                             "courses": list(self._courses)})

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="learning-video-previews", daemon=True)
        self._thread.start()
        for course in self.repository.list_courses():
            self.enqueue_course(course["course_id"])

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=8)
        self._persist()
        if getattr(self.repository, "video_previews", None) is self:
            delattr(self.repository, "video_previews")

    def priority(self, value: PreviewPriorityInput) -> dict:
        with self._lock:
            if value.active:
                self._leases[value.lease_id] = (value.kind, time.monotonic() + value.ttl_ms / 1000)
            else:
                self._leases.pop(value.lease_id, None)
        self._wake.set()
        return {"accepted": True}

    def _foreground(self) -> bool:
        now = time.monotonic()
        with self._lock:
            self._leases = {key: lease for key, lease in self._leases.items() if lease[1] > now}
            interaction = any(kind == "interaction" for kind, _ in self._leases.values())
        try:
            return interaction or bool(self.is_busy())
        except Exception:
            return True

    def has_active_lease(self, kind: str) -> bool:
        """Shared foreground signal; reading this never refreshes a lease."""
        with self._lock:
            timestamp = time.monotonic()
            return any(saved_kind == kind and until > timestamp
                       for saved_kind, until in self._leases.values())

    def _delay(self) -> float:
        with self._lock:
            return .3 if any(kind == "playback" and until > time.monotonic()
                             for kind, until in self._leases.values()) else .03

    def _idle(self) -> None:
        while self._foreground():
            if self._stop.wait(.05):
                raise _Interrupted()
        if self._stop.is_set():
            raise _Interrupted()

    def enqueue_course(self, course_id: str) -> None:
        with self._lock:
            if course_id not in self._courses:
                self._courses.append(course_id)
        self._wake.set()

    def enqueue_asset(self, asset_id: str, source_version: str) -> None:
        row = self.media._row(asset_id, source_version)
        self._enqueue(Path(row["local_path"]), content_hash=row["content_hash"], priority=True)

    def _enqueue(self, path: Path, *, content_hash: str | None = None, priority: bool = False) -> None:
        if _linked(path) or path.suffix.lower() not in VIDEO_TYPES:
            return
        try:
            fingerprint = _signature(path)
        except OSError:
            return
        key = _file_key(path, fingerprint)
        with self._lock:
            known = self._jobs.get(key)
            if known and known.get("fingerprint") == fingerprint:
                if content_hash and known.get("content_hash") != content_hash:
                    known["content_hash"] = content_hash
                    known["done"] = False
                elif known.get("done"):
                    return
                elif key in self._queue:
                    if priority:
                        self._queue.remove(key)
                        self._queue.appendleft(key)
                    return
            else:
                self._jobs[key] = {"path": str(path.resolve()), "fingerprint": fingerprint,
                                   "content_hash": content_hash, "done": False}
            if key not in self._queue:
                if priority:
                    self._queue.appendleft(key)
                else:
                    self._queue.append(key)
        self._wake.set()

    def _scan(self, course_id: str) -> None:
        from learning_app.course_files_schema import course_roots, course_visibility

        with self.repository.engine.connect() as connection:
            row = connection.execute(select(course_roots.c.local_path, course_roots.c.state,
                                            course_visibility.c.hidden).outerjoin(
                course_visibility, course_visibility.c.course_id == course_roots.c.course_id
            ).where(course_roots.c.course_id == course_id)).mappings().first()
        if not row or row["state"] != "active" or row["hidden"]:
            return
        root = Path(row["local_path"])
        if not root.is_dir() or _linked(root):
            return
        for parent, directories, files in os.walk(root, followlinks=False):
            self._idle()
            directories[:] = [name for name in directories if not _linked(Path(parent) / name)]
            for name in files:
                self._idle()
                path = Path(parent) / name
                if path.suffix.lower() in VIDEO_TYPES:
                    self._enqueue(path)

    def _resolve_tool(self) -> None:
        installed = Path(os.environ.get("LOCALAPPDATA", "")) / (
            "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/"
            "ffmpeg-8.1-full_build/bin/ffmpeg.exe"
        )
        candidates = [self._requested_tool] if self._requested_tool else [
            self.project_root / ".tools/ffmpeg/ffmpeg.exe", installed,
        ]
        for candidate in candidates:
            if candidate is None or not candidate.is_file():
                continue
            try:
                signature = tuple(_signature(candidate))
                cache_key = (str(candidate.resolve()), signature)
                with _TOOL_VERIFICATION_LOCK:
                    digest = _TOOL_VERIFICATIONS.get(cache_key)
                    if digest is None:
                        with candidate.open("rb") as stream:
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        if tuple(_signature(candidate)) != signature:
                            self._tool_error = "PREVIEW_TOOL_HASH_MISMATCH"
                            continue
                        _TOOL_VERIFICATIONS[cache_key] = digest
            except OSError:
                self._tool_error = "PREVIEW_TOOL_UNAVAILABLE"
                continue
            if digest != TOOL_SHA256:
                self._tool_error = "PREVIEW_TOOL_HASH_MISMATCH"
                continue
            self._tool = candidate.resolve()
            _write(self.root / "tool.json", {"version": TOOL_VERSION, "sha256": digest,
                                            "revision": REVISION})
            self._tool_error = None
            return
        self._tool_error = self._tool_error or "PREVIEW_TOOL_UNAVAILABLE"

    def _command(self, arguments: list[str], *, timeout: float = 12) -> str:
        self._idle()
        group = OwnedProcessGroup("learning-video-preview-ffmpeg")
        self._current_group = group
        try:
            process = group.spawn([str(self._tool), "-hide_banner", "-nostdin", "-threads", "1",
                                   "-filter_threads", "1", "-filter_complex_threads", "1", *arguments],
                                  cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE)
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes

                priority = ctypes.WinDLL("kernel32", use_last_error=True).SetPriorityClass
                priority.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                priority.restype = wintypes.BOOL
                priority(int(process._handle), 0x40)
            elif hasattr(os, "setpriority"):
                try:
                    os.setpriority(os.PRIO_PROCESS, process.pid, 10)
                except OSError:
                    pass
            deadline = time.monotonic() + timeout
            while True:
                if self._stop.is_set() or self._foreground():
                    raise _Interrupted()
                if time.monotonic() >= deadline:
                    raise AppError("PREVIEW_FRAME_TIMEOUT", "缩略图生成超时", 503)
                try:
                    _, errors = process.communicate(timeout=.05)
                    if process.returncode:
                        raise AppError("PREVIEW_DECODE_FAILED", "无法生成此视频的缩略图", 422)
                    return errors or ""
                except subprocess.TimeoutExpired:
                    continue
        finally:
            group.close(grace_seconds=.2)
            self._current_group = None

    def _hash(self, path: Path, fingerprint: list[int]) -> str:
        digest = hashlib.sha256()
        offset = 0
        while True:
            self._idle()
            # Release the read handle before yielding to a file operation.
            with _read_locked(path) as stream:
                if _signature(path) != fingerprint:
                    raise AppError("SOURCE_VERSION_MISMATCH", "预览来源已经改变", 409)
                stream.seek(offset)
                chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
        if _signature(path) != fingerprint:
            raise AppError("SOURCE_VERSION_MISMATCH", "预览来源已经改变", 409)
        return digest.hexdigest()

    def _manifest_path(self, content_hash: str) -> Path:
        return self.root / _key(content_hash) / "manifest.json"

    def _load_manifest(self, content_hash: str) -> dict:
        path = self._manifest_path(content_hash)
        if path.is_file():
            try:
                value = json.loads(path.read_text("utf-8"))
                if value.get("revision") == REVISION and value.get("content_hash") == content_hash:
                    return value
            except (OSError, ValueError, TypeError):
                pass
        return {"revision": REVISION, "content_hash": content_hash, "status": "queued",
                "duration_ms": None, "interval_ms": DEFAULT_INTERVAL_MS, "times": [],
                "frames": [], "error_code": None}

    def _step(self, job: dict) -> bool:
        self._idle()
        path = Path(job["path"])
        if _signature(path) != job["fingerprint"]:
            raise AppError("SOURCE_VERSION_MISMATCH", "预览来源已经改变", 409)
        if not job.get("content_hash"):
            job["content_hash"] = self._hash(path, job["fingerprint"])
            self._persist()
        content_hash = job["content_hash"]
        manifest = self._load_manifest(content_hash)
        cache = self._manifest_path(content_hash).parent
        cache.mkdir(parents=True, exist_ok=True)
        if manifest["status"] == "ready" and all((cache / f"{index}.jpg").is_file()
                                                  for index in manifest["frames"]):
            return True
        if not manifest["times"]:
            metadata = self._command(["-protocol_whitelist", "file,pipe", "-i", str(path),
                                      "-t", "0", "-f", "null", "-"])
            match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", metadata)
            duration = round((int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])) * 1000) if match else None
            interval, times = thumbnail_times(duration)
            manifest.update(duration_ms=duration, interval_ms=interval, times=times)
        manifest["status"] = "building"
        manifest["error_code"] = None
        _write(self._manifest_path(content_hash), manifest)
        existing = {index for index in manifest["frames"] if (cache / f"{index}.jpg").is_file()}
        pending = [index for index in _frame_order(len(manifest["times"])) if index not in existing]
        if pending:
            index = pending[0]
            temporary = cache / f"{index}.partial.jpg"
            try:
                self._command(["-y", "-loglevel", "error", "-protocol_whitelist", "file,pipe",
                               "-ss", f"{manifest['times'][index] / 1000:.3f}", "-i", str(path),
                               "-frames:v", "1", "-an", "-sn", "-dn", "-vf",
                               "format=yuv444p,scale=240:135:force_original_aspect_ratio=decrease,pad=240:135:(ow-iw)/2:(oh-ih)/2",
                               "-q:v", "5", str(temporary)])
                with Image.open(temporary) as image:
                    if image.format != "JPEG" or image.size != (WIDTH, HEIGHT):
                        raise AppError("PREVIEW_IMAGE_INVALID", "缩略图内容无效", 422)
                    image.verify()
                if _signature(path) != job["fingerprint"]:
                    raise AppError("SOURCE_VERSION_MISMATCH", "预览来源已经改变", 409)
                temporary.replace(cache / f"{index}.jpg")
                existing.add(index)
            finally:
                temporary.unlink(missing_ok=True)
        complete = len(existing) == len(manifest["times"])
        manifest.update(frames=sorted(existing), status="ready" if complete else "building")
        _write(self._manifest_path(content_hash), manifest)
        return complete

    def _run(self) -> None:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentThread.restype = wintypes.HANDLE
            kernel.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
            kernel.SetThreadPriority.restype = wintypes.BOOL
            # Background mode lowers this worker's CPU, I/O and memory priority
            # without changing the HTTP thread or the media player's process.
            if not kernel.SetThreadPriority(kernel.GetCurrentThread(), 0x10000):
                kernel.SetThreadPriority(kernel.GetCurrentThread(), -2)
        self._resolve_tool()
        while not self._stop.is_set():
            try:
                self._idle()
                with self._lock:
                    course = self._courses.popleft() if self._courses else None
                if course:
                    try:
                        self._scan(course)
                    except _Interrupted:
                        with self._lock:
                            self._courses.appendleft(course)
                        raise
                    except (OSError, AppError):
                        pass
                    self._persist()
                with self._lock:
                    key = self._queue.popleft() if self._queue else None
                    job = dict(self._jobs[key]) if key in self._jobs else None
                if not job or self._tool is None:
                    if job:
                        with self._lock:
                            self._queue.append(key)
                    self._wake.wait(.5)
                    self._wake.clear()
                    continue
                try:
                    job["done"] = self._step(job)
                    job.pop("error_code", None)
                except _Interrupted:
                    job["done"] = False
                except (OSError, ValueError, AppError) as error:
                    job.update(done=True, error_code=getattr(error, "code", "PREVIEW_DECODE_FAILED"))
                    if job.get("content_hash"):
                        manifest = self._load_manifest(job["content_hash"])
                        manifest.update(status="failed", error_code=job["error_code"])
                        _write(self._manifest_path(job["content_hash"]), manifest)
                with self._lock:
                    self._jobs[key] = job
                    if not job["done"]:
                        self._queue.append(key)
                    self._persist()
                self._stop.wait(self._delay())
            except _Interrupted:
                continue

    def _course_cover(self, course_id: str, relative: str) -> tuple[Path, str, Path | None]:
        from learning_app.course_files import CourseFileStore

        store = CourseFileStore(self.repository)
        path = store._path(store._root(course_id), relative)
        if not path.is_file() or path.suffix.lower() not in VIDEO_TYPES:
            raise AppError("VIDEO_REQUIRED", "只为当前课程的视频提供封面", 422)
        version = _file_key(path, _signature(path))
        with self._lock:
            job = dict(self._jobs.get(version, {}))
        digest = job.get("content_hash")
        if not digest:
            return path, version, None
        manifest = self._load_manifest(digest)
        for index in sorted(manifest["frames"]):
            frame = self._manifest_path(digest).parent / f"{index}.jpg"
            if frame.is_file():
                return path, version, frame
        return path, version, None

    def course_cover(self, course_id: str, relative: str) -> CourseCoverResponse:
        # Read only an existing derivative; do not open/register a business
        # video, hash its bytes, or change its last-used order on file browsing.
        _, version, frame = self._course_cover(course_id, relative)
        return CourseCoverResponse(
            status="ready" if frame else "unavailable" if self._tool_error else "queued",
            source_version=version,
            url=f"/api/v1/courses/{course_id}/files/cover.jpg?" + urlencode(
                {"path": relative, "source_version": version}) if frame else None,
        )

    def course_cover_frame(self, course_id: str, relative: str, version: str) -> FileResponse:
        _, current, frame = self._course_cover(course_id, relative)
        if current != version:
            raise AppError("SOURCE_VERSION_MISMATCH", "封面来源已变化，请刷新文件目录", 409)
        if frame is None:
            raise AppError("PREVIEW_FRAME_NOT_READY", "此封面尚未生成", 404)
        return FileResponse(frame, media_type="image/jpeg", headers={"Cache-Control": "private, no-cache"})

    def manifest(self, asset_id: str, version: str) -> PreviewManifest:
        row = self.media._row(asset_id, version)
        # A metadata signature check is enough for a completed immutable cache;
        # no foreground file hashing or decoding occurs on hover.
        path = Path(row["local_path"])
        try:
            if _signature(path) != row["file_signature"]:
                raise AppError("SOURCE_VERSION_MISMATCH", "视频内容已变化，请重新打开", 409)
        except OSError as error:
            raise AppError("SOURCE_MISSING", "视频原件缺失，请重新定位", 404) from error
        self.enqueue_asset(asset_id, version)
        value = self._load_manifest(row["content_hash"])
        if self._tool_error:
            value.update(status="unavailable", error_code=self._tool_error)
        elif self._thread is None:
            value.update(status="unavailable", error_code="PREVIEW_WORKER_NOT_STARTED")
        return PreviewManifest(asset_id=asset_id, source_version=version, status=value["status"],
            duration_ms=value["duration_ms"], interval_ms=value["interval_ms"],
            error_code=value.get("error_code"), frames=[PreviewFrame(index=index,
                time_ms=value["times"][index], url=f"/api/v1/videos/{asset_id}/previews/{index}.jpg?source_version={version}")
                for index in value["frames"] if index < len(value["times"])
                and (self._manifest_path(row["content_hash"]).parent / f"{index}.jpg").is_file()])

    def frame(self, asset_id: str, version: str, index: int) -> FileResponse:
        manifest = self.manifest(asset_id, version)
        if index not in {frame.index for frame in manifest.frames}:
            raise AppError("PREVIEW_FRAME_NOT_READY", "此预览图尚未生成", 404)
        row = self.media._row(asset_id, version)
        return FileResponse(self._manifest_path(row["content_hash"]).parent / f"{index}.jpg",
                            media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable"})


def install_video_preview_routes(app: FastAPI) -> None:
    def cache() -> VideoPreviewCache:
        value = getattr(app.state, "video_previews", None)
        if value is None:
            raise AppError("PREVIEW_WORKER_UNAVAILABLE", "视频预览尚未就绪", 503)
        return value

    @app.get("/api/v1/videos/{asset_id}/previews", response_model=PreviewManifest)
    def manifest(asset_id: str, source_version: str):
        return cache().manifest(asset_id, source_version)

    @app.get("/api/v1/videos/{asset_id}/previews/{index}.jpg", include_in_schema=False)
    def frame(asset_id: str, index: int, source_version: str):
        return cache().frame(asset_id, source_version, index)

    @app.post("/api/v1/video-previews/priority")
    def priority(value: PreviewPriorityInput):
        return cache().priority(value)

    @app.get("/api/v1/courses/{course_id}/files/preview", response_model=CourseCoverResponse)
    def course_cover(course_id: str, path: str):
        return cache().course_cover(course_id, path)

    @app.get("/api/v1/courses/{course_id}/files/cover.jpg", include_in_schema=False)
    def course_cover_frame(course_id: str, path: str, source_version: str):
        return cache().course_cover_frame(course_id, path, source_version)
