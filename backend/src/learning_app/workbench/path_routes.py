"""Authenticated user-only learning actions, never available through model tools."""

from fastapi import FastAPI, Query
from sqlalchemy import select

from learning_app.errors import AppError

from . import schema as s
from .path_models import PathAction, PathEventPage, PathOperation, PathReceipt, PathView
from .paths import PathStore


def install_path_routes(app: FastAPI):
    prefix = "/api/v1/courses/{course_id}/learning"

    @app.get(prefix + "/artifacts/{artifact_id}/path", response_model=PathView)
    def read_path(course_id: str, artifact_id: str):
        return PathStore(app.state.repository).get(course_id, artifact_id)

    @app.post(prefix + "/artifacts/{artifact_id}/path/actions", response_model=PathReceipt)
    def path_action(course_id: str, artifact_id: str, value: PathAction):
        return PathStore(app.state.repository).act(course_id, artifact_id, value)

    @app.get(prefix + "/artifacts/{artifact_id}/path/events", response_model=PathEventPage)
    def path_events(
        course_id: str,
        artifact_id: str,
        before: int | None = Query(None, ge=1),
        limit: int = Query(50, ge=1, le=100),
    ):
        return PathStore(app.state.repository).events(
            course_id, artifact_id, before=before, limit=limit
        )

    @app.get(prefix + "/path-operations/{operation_id}", response_model=PathOperation)
    def path_operation(course_id: str, operation_id: str):
        with app.state.repository.engine.connect() as conn:
            row = (
                conn.execute(
                    select(s.operations).where(
                        s.operations.c.operation_id == operation_id,
                        s.operations.c.course_id == course_id,
                        s.operations.c.kind == "path_action",
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError("PATH_OPERATION_NOT_FOUND", "原路线操作尚无已提交回执", 404)
            return PathOperation(**{key: row[key] for key in PathOperation.model_fields})
