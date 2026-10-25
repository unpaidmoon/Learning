"""Only authenticated user routes may submit answers, flip cards or give ratings."""

from typing import Literal

from fastapi import FastAPI, Query
from sqlalchemy import select

from learning_app.errors import AppError

from . import practice_schema as p
from . import schema as s
from .practice import PracticeStore
from .practice_models import (
    AnswerHistory,
    AnswerInput,
    AnswerView,
    AssessmentHistory,
    PracticeOperation,
    PracticeQuestionPage,
    ReviewControl,
    ReviewItem,
    ReviewItemPage,
    ReviewPreferenceInput,
    ReviewPreferences,
    ReviewRatingInput,
    ReviewRatingReceipt,
    ReviewSession,
    ReviewSessionInput,
    ReviewUndo,
    TodayReview,
    UserVerdict,
)
from .reviews import ReviewStore


def install_practice_routes(app: FastAPI) -> None:
    prefix = "/api/v1/courses/{course_id}/learning"

    def practice():
        return PracticeStore(app.state.repository)

    def reviews():
        return ReviewStore(app.state.repository)

    @app.get(prefix + "/practice-operations/{operation_id}", response_model=PracticeOperation)
    def operation(course_id: str, operation_id: str):
        with app.state.repository.engine.connect() as conn:
            row = (
                conn.execute(
                    select(s.operations).where(
                        s.operations.c.course_id == course_id,
                        s.operations.c.operation_id == operation_id,
                        s.operations.c.kind.in_(
                            [
                                "answer_submit",
                                "answer_override",
                                "review_preferences",
                                "review_control",
                                "review_open",
                                "review_reveal",
                                "review_rating",
                                "review_undo",
                            ]
                        ),
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError(
                    "PRACTICE_OPERATION_NOT_FOUND", "原作答或复习操作尚无已提交回执", 404
                )
            return PracticeOperation(**{key: row[key] for key in PracticeOperation.model_fields})

    @app.get(prefix + "/artifacts/{artifact_id}/questions", response_model=PracticeQuestionPage)
    def questions(course_id: str, artifact_id: str, revision_id: str | None = None):
        return practice().questions(course_id, artifact_id, revision_id)

    @app.post(prefix + "/artifacts/{artifact_id}/answers", response_model=AnswerView)
    def submit_answer(course_id: str, artifact_id: str, value: AnswerInput):
        return practice().submit(course_id, artifact_id, value)

    @app.get(prefix + "/artifacts/{artifact_id}/answers", response_model=AnswerHistory)
    def answer_history(
        course_id: str,
        artifact_id: str,
        block_id: str,
        before_sequence: int | None = Query(None, ge=1),
        limit: int = Query(50, ge=1, le=100),
    ):
        return practice().history(
            course_id, artifact_id, block_id, before_sequence=before_sequence, limit=limit
        )

    @app.get(prefix + "/answers/{answer_id}", response_model=AnswerView)
    def answer(course_id: str, answer_id: str):
        return practice().get(course_id, answer_id)

    @app.get(prefix + "/answers/{answer_id}/assessments", response_model=AssessmentHistory)
    def assessments(course_id: str, answer_id: str):
        return practice().assessments(course_id, answer_id)

    @app.post(prefix + "/answers/{answer_id}/override", response_model=AnswerView)
    def override(course_id: str, answer_id: str, value: UserVerdict):
        return practice().override(course_id, answer_id, value)

    @app.get("/api/v1/learning/reviews/today", response_model=TodayReview)
    def today(
        course_id: str | None = None,
        kind: Literal["card", "wrong_question", "knowledge"] | None = None,
    ):
        return reviews().today(course_id=course_id, kind=kind)

    @app.get("/api/v1/learning/reviews/preferences", response_model=ReviewPreferences)
    def review_preferences():
        return reviews().preferences()

    @app.post(prefix + "/review-preferences", response_model=ReviewPreferences)
    def set_review_preferences(course_id: str, value: ReviewPreferenceInput):
        return reviews().set_preferences(course_id, value)

    @app.get(prefix + "/reviews", response_model=ReviewItemPage)
    def review_list(
        course_id: str,
        state: Literal["active", "paused", "removed"] | None = None,
        after_id: str | None = Query(None, max_length=160),
        limit: int = Query(50, ge=1, le=100),
    ):
        with app.state.repository.engine.connect() as conn:
            ReviewStore._require_course(conn, course_id)
            query = select(p.review_items).where(p.review_items.c.course_id == course_id)
            if state:
                query = query.where(p.review_items.c.queue_state == state)
            if after_id:
                query = query.where(p.review_items.c.review_id > after_id)
            rows = list(
                conn.execute(query.order_by(p.review_items.c.review_id).limit(limit + 1)).mappings()
            )
            return ReviewItemPage(
                items=[ReviewStore.public(conn, dict(row)) for row in rows[:limit]],
                next_cursor=rows[limit - 1]["review_id"] if len(rows) > limit else None,
            )

    @app.get(prefix + "/reviews/{review_id}", response_model=ReviewItem)
    def review(course_id: str, review_id: str):
        return reviews().get(course_id, review_id)

    @app.post(prefix + "/reviews/{review_id}/control", response_model=ReviewItem)
    def review_control(course_id: str, review_id: str, value: ReviewControl):
        return reviews().control(course_id, review_id, value)

    @app.post(prefix + "/reviews/{review_id}/sessions", response_model=ReviewSession)
    def review_open(course_id: str, review_id: str, value: ReviewSessionInput):
        return reviews().open_session(course_id, review_id, value)

    @app.get(prefix + "/reviews/{review_id}/sessions/{session_id}", response_model=ReviewSession)
    def review_session(course_id: str, review_id: str, session_id: str):
        with app.state.repository.engine.connect() as conn:
            _, session = reviews()._session(conn, course_id, review_id, session_id)
            return ReviewSession.model_validate(session)

    @app.post(
        prefix + "/reviews/{review_id}/sessions/{session_id}/reveal", response_model=ReviewSession
    )
    def review_reveal(course_id: str, review_id: str, session_id: str, value: ReviewSessionInput):
        return reviews().reveal(course_id, review_id, session_id, value)

    @app.post(prefix + "/reviews/{review_id}/rating", response_model=ReviewRatingReceipt)
    def review_rating(course_id: str, review_id: str, value: ReviewRatingInput):
        return reviews().rate(course_id, review_id, value)

    @app.post(prefix + "/reviews/{review_id}/undo", response_model=ReviewItem)
    def review_undo(course_id: str, review_id: str, value: ReviewUndo):
        return reviews().undo(course_id, review_id, value)
