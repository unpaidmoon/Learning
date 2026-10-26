"""UI-only platform covers; no media extraction, attachments, or model-tool entry."""

import hashlib
import os
import threading
import time
from collections import OrderedDict
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
        self.previews = OrderedDict()
        self.preview_images = OrderedDict()
        self.preview_lock = threading.Lock()
        self.preview_slots = threading.BoundedSemaphore(3)

    def preview(self, grant, payload):
        """Publish only an opaque local handle for already-read cover metadata."""
        url = payload.get("cover_url")
        if not isinstance(url, str) or not url:
            return None
        self.host.access.check(grant)
        now = time.monotonic()
        with self.preview_lock:
            for identity, (saved, original, created) in list(self.previews.items()):
                if now - created > 1800:
                    self.previews.pop(identity)
                    self.preview_images.pop(identity, None)
                elif saved == grant and original["cover_url"] == url:
                    return f"/api/v1/integrations/bilibili/covers/{identity}"
            identity = uuid4().hex
            self.previews[identity] = (grant, {"cover_url": url}, now)
            while len(self.previews) > 256:
                removed, _ = self.previews.popitem(last=False)
                self.preview_images.pop(removed, None)
        return f"/api/v1/integrations/bilibili/covers/{identity}"

    def preview_response(self, identity):
        with self.preview_lock:
            entry = self.previews.get(identity)
            data = self.preview_images.get(identity)
        if not entry or time.monotonic() - entry[2] > 1800:
            raise AppError("BILI_COVER_PREVIEW_EXPIRED", "封面预览已过期，请重新读取目录", 404)
        grant, payload, _ = entry
        self.host.access.check(grant)
        if data is None:
            with self.preview_slots:
                with self.preview_lock:
                    data = self.preview_images.get(identity)
                if data is None:
                    self.host.access.check(grant)
                    data = self.host.provider.cover(grant, payload)
                    if not data or len(data) > MAX_COVER_BYTES:
                        raise AppError("BILI_COVER_SIZE_INVALID", "封面字节不完整或过大", 409)
                    image_type(data)
                    self.host.access.check(grant)
                    with self.preview_lock:
                        if identity in self.previews:
                            self.preview_images[identity] = data
                            while sum(map(len, self.preview_images.values())) > 8 * 1024 * 1024:
                                self.preview_images.popitem(last=False)
        self.host.access.check(grant)
        return Response(
            data,
            media_type=image_type(data),
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            },
        )

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
