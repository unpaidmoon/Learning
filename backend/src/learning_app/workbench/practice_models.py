"""Actual user activity and append-only assessments, independent of content."""

from typing import Literal

from pydantic import Field, model_validator

from .models import Contract, Identity, QuestionBlock

Verdict = Literal["correct", "wrong", "partial", "pending"]


class AnswerInput(Contract):
    operation_id: Identity
    question_revision_id: Identity
    block_id: Identity
    text: str = Field(min_length=1, max_length=100_000)
    review_session_id: Identity | None = None
    expected_review_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def actual_input(self):
        if not self.text.strip():
            raise ValueError("an actual answer cannot be blank")
        if (self.review_session_id is None) != (self.expected_review_revision is None):
            raise ValueError("review answers require the original session and schedule version")
        return self


class AssessmentView(Contract):
    assessment_id: Identity
    answer_id: Identity
    revision: int
    parent_assessment_id: Identity | None
    verdict: Verdict
    author: Literal["deterministic", "ai_reference", "user_override"]
    reason: str
    score: float | None
    run_id: Identity | None
    evidence: dict
    created_at: str


class AnswerView(Contract):
    answer_id: Identity
    course_id: Identity
    artifact_id: Identity
    question_revision_id: Identity
    block_id: Identity
    text: str
    sequence: int
    submitted_at: str
    assessment_revision: int
    effective_assessment: AssessmentView


class AnswerHistory(Contract):
    answers: list[AnswerView]


class AssessmentHistory(Contract):
    assessments: list[AssessmentView]


class UserVerdict(Contract):
    operation_id: Identity
    expected_revision: int = Field(ge=1)
    verdict: Verdict
    reason: str = Field(min_length=1, max_length=20_000)
    score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def pending_score(self):
        if self.verdict == "pending" and self.score is not None:
            raise ValueError("pending assessments do not have a numeric score")
        return self


class ReviewItem(Contract):
    review_id: Identity
    course_id: Identity
    kind: Literal["card", "wrong_question", "knowledge"]
    artifact_id: Identity
    block_id: Identity
    content_revision_id: Identity
    source_answer_id: Identity | None
    assessment_id: Identity | None
    queue_state: Literal["active", "paused", "removed"]
    eligible: bool
    inactive_reason: str | None
    schedule_revision: int
    due_ms: int
    scheduler: str
    scheduler_state: dict
    scheduler_config: dict
    created_at: str
    updated_at: str
    label: str = ""


class ReviewPreferences(Contract):
    revision: int
    daily_limit: int
    timezone: str
    day_rollover_hour: int


class ReviewPreferenceInput(Contract):
    operation_id: Identity
    expected_revision: int = Field(ge=0)
    daily_limit: int = Field(ge=1, le=200)


class ReviewControl(Contract):
    operation_id: Identity
    expected_revision: int = Field(ge=1)
    action: Literal["pause", "remove", "resume"]


class TodayReview(Contract):
    logical_day: str
    daily_limit: int
    admitted_count: int
    completed_count: int
    items: list[ReviewItem]


class ReviewItemPage(Contract):
    items: list[ReviewItem]
    next_cursor: Identity | None


class ReviewSessionInput(Contract):
    operation_id: Identity
    expected_revision: int = Field(ge=1)


class ReviewSession(Contract):
    session_id: Identity
    review_id: Identity
    logical_day: str
    schedule_revision: int
    content_revision_id: Identity
    state: Literal["opened", "revealed", "answered", "rated"]
    opened_at: str
    revealed_at: str | None
    rating_event_id: Identity | None


class ReviewRatingInput(ReviewSessionInput):
    session_id: Identity
    rating: int = Field(ge=1, le=4)


class ReviewRatingReceipt(Contract):
    event_id: Identity
    item: ReviewItem
    session: ReviewSession
    audit: dict


class ReviewUndo(ReviewSessionInput):
    event_id: Identity


class PracticeQuestion(Contract):
    artifact_id: Identity
    revision_id: Identity
    block: QuestionBlock
    latest_answer: AnswerView | None


class PracticeQuestionPage(Contract):
    items: list[PracticeQuestion]


class PracticeOperation(Contract):
    operation_id: Identity
    kind: str
    result: AnswerView | ReviewItem | ReviewPreferences | ReviewSession | ReviewRatingReceipt
    created_at: str
