"""A suggested route and user evidence; no inferred mastery or prerequisite gate."""

from typing import Literal

from pydantic import Field, model_validator

from .models import Contract, Identity, PathQuestionLink, PathStepBlock
from .practice_models import AnswerView, ReviewItem


class PathAction(Contract):
    operation_id: Identity
    expected_revision: int = Field(ge=0)
    content_revision_id: Identity
    block_id: Identity
    action: Literal["navigate", "skip", "view_explanation", "self_assess"]
    self_assessment: Literal["uncertain", "needs_practice", "confident"] | None = None

    @model_validator(mode="after")
    def assessment_only_for_its_action(self):
        if (self.action == "self_assess") != (self.self_assessment is not None):
            raise ValueError("only self_assess takes an actual user self assessment")
        return self


class PathQuestionEvidence(Contract):
    question: PathQuestionLink
    latest_answer: AnswerView | None


class PathStepView(Contract):
    block: PathStepBlock
    visited: bool
    skipped: bool
    explanation_seen: bool
    activity_current: bool
    self_assessment: Literal["uncertain", "needs_practice", "confident"] | None
    self_assessment_current: bool
    answers: list[PathQuestionEvidence]
    review: ReviewItem | None


class PathView(Contract):
    artifact_id: Identity
    content_revision_id: Identity
    revision: int
    current_block_id: Identity | None
    steps: list[PathStepView]


class PathEvent(Contract):
    event_id: Identity
    artifact_id: Identity
    content_revision_id: Identity
    block_id: Identity
    revision: int
    action: str
    detail: dict
    created_at: str


class PathEventPage(Contract):
    events: list[PathEvent]
    next_before: int | None


class PathReceipt(Contract):
    event: PathEvent
    path: PathView


class PathOperation(Contract):
    operation_id: Identity
    result: PathReceipt
    created_at: str
