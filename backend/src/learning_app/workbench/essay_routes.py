"""User-only essay assessment and acceptance; generation uses the native task route."""

from fastapi import FastAPI, Query
from sqlalchemy import select

from learning_app.errors import AppError

from . import schema as s
from .essay_models import (
    EssayAssessmentInput,
    EssayOperation,
    EssayRound,
    EssayRoundPage,
    EssaySuggestionInput,
)
from .essays import EssayStore
from .models import ArtifactReceipt


def install_essay_routes(app: FastAPI):
    prefix = "/api/v1/courses/{course_id}/learning"

    def store():
        return EssayStore(app.state.repository)

    @app.get(prefix + "/artifacts/{artifact_id}/essay-rounds", response_model=EssayRoundPage)
    def rounds(
        course_id: str,
        artifact_id: str,
        before: int | None = Query(None, ge=1),
        limit: int = Query(30, ge=1, le=100),
    ):
        return store().list(course_id, artifact_id, before=before, limit=limit)

    @app.get(prefix + "/essay-rounds/{round_id}", response_model=EssayRound)
    def essay_round(course_id: str, round_id: str):
        return store().get(course_id, round_id)

    @app.post(prefix + "/essay-rounds/{round_id}/assessments", response_model=EssayRound)
    def assessment(course_id: str, round_id: str, value: EssayAssessmentInput):
        return store().assess(course_id, round_id, value)

    @app.post(prefix + "/essay-rounds/{round_id}/apply-suggestions", response_model=ArtifactReceipt)
    def accept_suggestions(course_id: str, round_id: str, value: EssaySuggestionInput):
        return store().apply_suggestions(course_id, round_id, value)

    @app.get(prefix + "/essay-operations/{operation_id}", response_model=EssayOperation)
    def operation(course_id: str, operation_id: str):
        with app.state.repository.engine.connect() as conn:
            row = (
                conn.execute(
                    select(s.operations).where(
                        s.operations.c.course_id == course_id,
                        s.operations.c.operation_id == operation_id,
                        s.operations.c.kind == "essay_assessment",
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError("ESSAY_OPERATION_NOT_FOUND", "原作文评价操作尚无提交回执", 404)
            return EssayOperation(**{key: row[key] for key in EssayOperation.model_fields})
