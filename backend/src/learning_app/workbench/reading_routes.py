"""Serve only hash-locked product resources; source bytes use existing routes."""

import hashlib
import mimetypes

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from learning_app.errors import AppError
from learning_app.media import _read_locked

from .resources import ReadingResources

READING_RESOURCE_CSP = (
    "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; "
    "frame-ancestors 'self'; base-uri 'self'; object-src 'none'; form-action 'none'"
)


class ReadingResourcesView(BaseModel):
    pdfjs_version: str
    prepared: bool


def install_reading_routes(app: FastAPI) -> None:
    def resources():
        return ReadingResources(app.state.config.project_root)

    @app.get("/api/v1/reading/resources", response_model=ReadingResourcesView)
    def status():
        owned = resources()
        return ReadingResourcesView(pdfjs_version=owned.lock["version"], prepared=owned.prepared())

    @app.get("/api/v1/reading/pdfjs/{version}/{relative:path}")
    def resource(version: str, relative: str):
        owned = resources()
        path = owned.path(version, relative)
        stream = _read_locked(path)
        expected = owned.files[relative]
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected["sha256"]:
            stream.close()
            raise AppError("READING_RESOURCE_CONTENT_INVALID", "阅读资源完整性不符，未加载", 503)
        stream.seek(0)
        media_type = {
            ".mjs": "text/javascript",
            ".wasm": "application/wasm",
            ".ftl": "text/plain",
        }.get(path.suffix, mimetypes.guess_type(path.name)[0] or "application/octet-stream")

        def chunks():
            with stream:
                while chunk := stream.read(256 * 1024):
                    yield chunk

        return StreamingResponse(
            chunks(),
            media_type=media_type,
            background=BackgroundTask(stream.close),
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(expected["bytes"]),
                "Content-Security-Policy": READING_RESOURCE_CSP,
            },
        )
