"""Server-only short-lived playback sessions and verified local copies."""

import hashlib
import re
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import update
from starlette.background import BackgroundTask

from learning_app.errors import AppError

from . import schema as s
from .common import content_hash as _hash


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def content_range(response, requested=None):
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
    if not match:
        raise AppError("BILI_DOWNLOAD_RANGE_CONFLICT", "平台字节范围信息无效，原检查点保留", 409)
    start, end, total = map(int, match.groups())
    length = response.headers.get("content-length")
    if not 0 <= start <= end < total or (
        length is not None and (not length.isdecimal() or int(length) != end - start + 1)
    ):
        raise AppError(
            "BILI_DOWNLOAD_RANGE_CONFLICT", "平台字节范围和长度不一致，原检查点保留", 409
        )
    if requested:
        first, last = requested.removeprefix("bytes=").split("-", 1)
        expected_start = int(first) if first else max(0, total - int(last))
        expected_end = min(int(last), total - 1) if first and last else total - 1
        if start != expected_start or end > expected_end:
            raise AppError("BILI_DOWNLOAD_RANGE_CONFLICT", "平台没有返回所请求的字节范围", 409)
    return start, end, total


class RemoteMedia:
    def __init__(self, host):
        self.host = host
        self.sessions = {}
        self.verified = {}
        self.resolution_locks = {}
        self.lock = threading.RLock()

    def revoke(self, connection_id=None):
        with self.lock:
            for key, session in list(self.sessions.items()):
                if connection_id is None or session["grant"].connection_id == connection_id:
                    self.sessions.pop(key, None)

    def local(self, row, audio=False, force=False):
        name = "audio" if audio else "local"
        path = Path(row[name + "_path"]) if row[name + "_path"] else None
        if path and path.is_file() and path.resolve().is_relative_to(self.host.repository.root):
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
            if force or self.verified.get(str(path)) != signature:
                if digest(path) != row[name + "_sha256"]:
                    raise AppError(
                        "PLATFORM_LOCAL_CHANGED", "本地副本内容已经改变，旧来源未改指", 409
                    )
                self.verified[str(path)] = signature
            return path
        return None

    def availability(self, asset_id, version):
        row = self.host.source(asset_id, version)
        if self.local(row):
            return "available"
        # Listing stays local; explicit open/stream produces the actionable access error.
        return "available"

    def session(self, row, cancelled=None, *, refresh=False):
        grant = self.host.access.grant(row["connection_id"])
        operation_grant = self.host.access.scoped(grant, cancelled)
        self.host.access.check(operation_grant)
        key = (row["asset_id"], row["source_version"])

        def cached():
            saved = self.sessions.get(key)
            if saved and saved["grant"] == grant and not refresh:
                if saved.get("error"):
                    raise AppError(
                        "BILI_MEDIA_SESSION_STALE", "媒体会话已失效，请重新打开此视频核对连接", 409
                    )
                if time.monotonic() < saved["expires"]:
                    return {**saved, "grant": operation_grant}
            return None

        with self.lock:
            if ready := cached():
                return ready
            resolver = self.resolution_locks.setdefault(key, threading.Lock())
        with resolver:
            with self.lock:
                if ready := cached():
                    return ready
                prior = self.sessions.get(key)
            try:
                tracks = self.host.provider.media(operation_grant, row["payload"])
                self.host.access.check(operation_grant)
            except AppError as error:
                if error.code != "PREPARATION_INTERRUPTED":
                    with self.lock:
                        if self.sessions.get(key) is prior:
                            self.sessions[key] = {
                                "grant": grant,
                                "expires": 0,
                                "video": None,
                                "audio": None,
                                "session_id": uuid4().hex,
                                "source_key": key,
                                "error": error.code,
                            }
                raise
            result = {
                **tracks,
                "grant": grant,
                "expires": time.monotonic() + 180,
                "session_id": uuid4().hex,
                "source_key": key,
                "cid": row["payload"]["cid"],
            }
            with self.lock:
                self.sessions[key] = result
            return {**result, "grant": operation_grant}

    def open(self, asset_id, version, *, refresh=False):
        row = self.host.source(asset_id, version)
        if not self.local(row, force=True):
            saved = self.sessions.get((asset_id, version))
            self.session(row, refresh=refresh or bool(saved and saved.get("error")))

    def fail_session(self, session, code):
        with self.lock:
            saved = self.sessions.get(session["source_key"])
            if saved and saved["session_id"] == session["session_id"]:
                saved["error"] = code

    def view(self, asset_id, version):
        row = self.host.source(asset_id, version)
        path = self.local(row)
        session = self.sessions.get((asset_id, version))
        audio = (
            f"/api/v1/videos/{asset_id}/content?source_version={version}&track=audio&playback_session={session['session_id']}"
            if not path and session and session["audio"]
            else None
        )
        return {
            "platform": "bilibili",
            "size_bytes": path.stat().st_size if path else None,
            "audio_content_url": audio,
            "playback_session_id": session["session_id"] if not path and session else None,
        }

    def upstream(
        self,
        grant,
        url,
        range_header=None,
        *,
        if_range=None,
        allow_full_response=False,
        expected_cid=None,
    ):
        from .bilibili.provider import allowed_url

        allowed_url(url)
        extra_headers = {}
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if (
                not match
                or not any(match.groups())
                or (match[1] and match[2] and int(match[1]) > int(match[2]))
                or (not match[1] and int(match[2]) == 0)
            ):
                raise AppError("MEDIA_RANGE_INVALID", "媒体Range格式无效", 416)
            extra_headers["Range"] = range_header
        if if_range:
            extra_headers["If-Range"] = if_range
        client = httpx.Client(timeout=httpx.Timeout(30, read=30), follow_redirects=False)
        response = None
        seen = set()
        try:
            for hop in range(4):
                self.host.access.check(grant)
                allowed_url(url, media_redirect=hop > 0)
                seen.add(url)
                headers = {**self.host.provider.headers(grant, url), **extra_headers}
                identity = self.host.access.request(
                    grant, "media-range" if hop == 0 else "media-redirect"
                )
                response = client.send(
                    client.build_request("GET", url, headers=headers), stream=True
                )
                self.host.access.receipt(identity, response.status_code)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                if not location or hop == 3:
                    raise AppError(
                        "BILI_MEDIA_REDIRECT_LIMIT",
                        "媒体跳转缺少目标或超过限定次数，未继续读取",
                        409,
                    )
                target = urljoin(url, location)
                try:
                    allowed_url(target, media_redirect=expected_cid is not None)
                except ValueError as error:
                    raise AppError(
                        "PLATFORM_URL_DENIED", "媒体跳转目标格式无效，未继续读取", 403
                    ) from error
                if target in seen:
                    raise AppError("BILI_MEDIA_REDIRECT_LIMIT", "媒体地址循环跳转，未继续读取", 409)
                if expected_cid is not None and str(expected_cid) not in urlsplit(
                    target
                ).path.split("/"):
                    raise AppError(
                        "BILI_MEDIA_IDENTITY_UNKNOWN",
                        "跳转轨道无法对应冻结CID，未作为原来源读取",
                        409,
                    )
                response.close()
                response = None
                url = target
            if response.status_code == 416:
                if allow_full_response and range_header:
                    raise AppError(
                        "BILI_DOWNLOAD_RANGE_REJECTED", "平台拒绝当前下载断点范围，原断点保留", 409
                    )
                raise AppError("MEDIA_RANGE_INVALID", "所请求范围超出媒体长度", 416)
            if response.status_code not in {200, 206}:
                raise AppError(
                    "BILI_MEDIA_UNAVAILABLE", "平台媒体已失效或访问受限，请重新打开核对", 409
                )
            if (
                range_header
                and response.status_code != 206
                and not (allow_full_response and response.status_code == 200)
            ):
                raise AppError("BILI_RANGE_UNSUPPORTED", "平台未返回所请求的字节范围", 409)
            if range_header and response.status_code == 206:
                content_range(response, range_header)
            return client, response
        except (httpx.HTTPError, AppError) as exc:
            if response is not None:
                response.close()
            client.close()
            if isinstance(exc, AppError):
                raise
            self.host.access.receipt(identity)
            raise AppError(
                "BILI_MEDIA_NETWORK_FAILED", "平台媒体连接中断，未自动重试", 502
            ) from exc

    def content(self, asset_id, version, request):
        row = self.host.source(asset_id, version)
        audio = request.query_params.get("track", "video") == "audio"
        path = self.local(row, audio=audio)
        if path:
            return FileResponse(
                path,
                media_type="audio/mp4" if audio else "video/mp4",
                filename=None,
                content_disposition_type="inline",
            )
        session = self.session(row)
        requested_session = request.query_params.get("playback_session")
        if requested_session and requested_session != session["session_id"]:
            raise AppError("BILI_MEDIA_SESSION_STALE", "原播放会话已更新，请重新打开此视频", 409)
        track = session["audio" if audio else "video"]
        if not track:
            raise AppError("BILI_AUDIO_NOT_SEPARATE", "当前媒体不需要独立音轨", 404)
        try:
            client, response = self.upstream(
                session["grant"],
                track["url"],
                request.headers.get("range"),
                expected_cid=row["payload"]["cid"],
            )
        except AppError as error:
            if error.code != "MEDIA_RANGE_INVALID":
                self.fail_session(session, error.code)
            raise
        headers = {
            k: response.headers[k]
            for k in ["content-length", "content-range", "accept-ranges"]
            if k in response.headers
        }

        def close():
            response.close()
            client.close()

        def chunks():
            try:
                for block in response.iter_bytes(128 * 1024):
                    self.host.access.check(session["grant"])
                    yield block
            except httpx.HTTPError as error:
                self.fail_session(session, "BILI_MEDIA_NETWORK_FAILED")
                raise AppError(
                    "BILI_MEDIA_NETWORK_FAILED", "媒体读取中断，请重新打开核对，未自动重试", 502
                ) from error
            finally:
                close()

        return StreamingResponse(
            chunks(),
            status_code=response.status_code,
            headers=headers,
            media_type="audio/mp4" if audio else "video/mp4",
            background=BackgroundTask(close),
        )

    def verify_checkpoint_source(self, session):
        row = self.host.source(*session["source_key"])
        part = row["payload"]
        plan = self.host.provider.resolve(session["grant"], part.get("url") or part["bvid"])
        self.host.access.check(session["grant"])
        candidate = next((p for p in plan["parts"] if p["object_id"] == part["object_id"]), None)
        identity = ("object_id", "cid", "aid", "kind", "episode_id", "page", "duration_ms")
        if candidate is None or any(candidate.get(key) != part.get(key) for key in identity):
            raise AppError(
                "BILI_SOURCE_VERSION_CHANGED",
                "平台来源身份已变化，旧断点和旧版本保留；请重新选择导入",
                409,
            )

    def download_track(self, session, track, target, cancelled, progress):
        from .track_download import download_track

        return download_track(self, session, track, target, cancelled, progress)

    def download(self, lease, cancelled, progress):
        from .mux import mux

        row = self.host.source(lease["asset_id"], lease["source_version"], lease["course_id"])
        if existing := self.local(row):
            return {"path": str(existing), "sha256": row["local_sha256"]}
        session = self.session(row, cancelled, refresh=True)
        directory = (
            self.host.repository.root / "platform-copies" / row["asset_id"] / row["source_version"]
        )
        directory.mkdir(parents=True, exist_ok=True)
        tracks = []
        for name in ["video", "audio"]:
            track = session[name]
            if not track:
                continue
            target = directory / (
                name
                + "-"
                + _hash({"format": track.get("format_id"), "size": track.get("filesize")})[:16]
                + ".mp4"
            )
            # Completed track still has to pass container/timeline validation below.
            if not target.exists():
                self.download_track(session, track, target, cancelled, progress)
            tracks.append(target)
        target = directory / "complete.mp4"
        mux(self.host.project_root, tracks, target, row["payload"]["duration_ms"], cancelled)
        local_digest = digest(target)
        return {"path": str(target), "sha256": local_digest}

    def prepare_audio(self, lease, cancelled, progress):
        from .mux import mux

        row = self.host.source(lease["asset_id"], lease["source_version"], lease["course_id"])
        if self.local(row, audio=True):
            return
        session = self.session(row, cancelled, refresh=True)
        if session["audio"] is None:
            raise AppError(
                "BILI_AUDIO_ONLY_UNAVAILABLE", "当前来源没有独立音轨，未自动下载完整画面", 409
            )
        directory = (
            self.host.repository.root / "platform-copies" / row["asset_id"] / row["source_version"]
        )
        directory.mkdir(parents=True, exist_ok=True)
        track = directory / "audio-only-input.m4a"
        if not track.exists():
            self.download_track(session, session["audio"], track, cancelled, progress)
        target = directory / "audio-only.m4a"
        mux(self.host.project_root, [track], target, row["payload"]["duration_ms"], cancelled)
        with self.host.repository.engine.begin() as conn:
            conn.execute(
                update(s.sources)
                .where(s.sources.c.asset_id == row["asset_id"])
                .values(audio_path=str(target), audio_sha256=digest(target))
            )
