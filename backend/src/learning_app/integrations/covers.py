"""UI-only platform covers; no media extraction, attachments, or model-tool entry."""

import hashlib
import os
import threading
from uuid import uuid4

from fastapi.responses import Response

from learning_app.errors import AppError

MAX_COVER_BYTES = 2 * 1024 * 1024


def image_type(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise AppError("BILI_COVER_FORMAT_UNSUPPORTED", "封面格式不可用，仍可正常打开视频", 409)


class CoverCache:
    def __init__(self, host):
        self.host = host
        self.lock = threading.Lock()
        self.root = host.repository.root / "platform-covers"

    def response(self, course_id, asset_id, version):
        row = self.host.source(asset_id, version, course_id)
        if not row:
            raise AppError("PLATFORM_SOURCE_NOT_FOUND", "平台来源不存在", 404)
        key = hashlib.sha256((asset_id + ":" + version).encode()).hexdigest()
        target = self.root / (key + ".image")
        # Serialize small UI requests and recheck the cache after waiting. Browsing
        # never starts a video stream, ASR, or a separate persistent task scheduler.
        with self.lock:
            if target.is_file():
                with target.open("rb") as stream:
                    data = stream.read(MAX_COVER_BYTES + 1)
                if not data or len(data) > MAX_COVER_BYTES:
                    raise AppError("BILI_COVER_SIZE_INVALID", "本地封面缓存字节不完整或过大", 409)
            else:
                grant = self.host.access.grant(row["connection_id"])
                data = self.host.provider.cover(grant, row["payload"])
                if not data or len(data) > MAX_COVER_BYTES:
                    raise AppError("BILI_COVER_SIZE_INVALID", "封面字节不完整或过大", 409)
                image_type(data)
                self.host.access.check(grant)
                self.root.mkdir(parents=True, exist_ok=True)
                partial = target.with_suffix("." + uuid4().hex + ".partial")
                try:
                    partial.write_bytes(data)
                    self.host.access.check(grant)
                    os.replace(partial, target)
                finally:
                    partial.unlink(missing_ok=True)
        return Response(
            data,
            media_type=image_type(data),
            headers={
                "Cache-Control": "private, no-cache",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            },
        )
