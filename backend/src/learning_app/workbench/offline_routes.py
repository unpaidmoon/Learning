"""Authenticated export of one selected immutable revision; no model dispatch."""

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from .offline import OfflineExporter, OfflineExportInput


def install_offline_routes(app: FastAPI):
    @app.post(
        "/api/v1/courses/{course_id}/learning/artifacts/{artifact_id}/offline-export",
        response_class=StreamingResponse,
        responses={200: {"content": {"application/zip": {}}}},
    )
    def offline_export(course_id: str, artifact_id: str, value: OfflineExportInput):
        stream = OfflineExporter(app.state.repository).build(
            course_id, artifact_id, value.revision_id
        )

        def chunks():
            try:
                while chunk := stream.read(256 * 1024):
                    yield chunk
            finally:
                stream.close()

        return StreamingResponse(
            chunks(),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="learning-offline.zip"',
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )
