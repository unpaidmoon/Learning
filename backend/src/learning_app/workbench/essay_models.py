"""Essay rounds refer to immutable real drafts; model feedback never rewrites one."""

from typing import Literal

from pydantic import Field

from .models import Contract, Digest, EssayDraftBlock, Identity


class EssayAssessment(Contract):
    assessment_id: Identity
    round_id: Identity
    revision: int
    origin: Literal["ai_reference", "user_self", "user_override"]
    total: float | None
    max_total: float
    reason: str
    supersedes: Identity | None
    run_id: Identity | None
    payload: dict
    created_at: str


class EssayAssessmentInput(Contract):
    operation_id: Identity
    expected_revision: int = Field(ge=0)
    origin: Literal["user_self", "user_override"]
    total: float | None = Field(default=None, ge=0, le=10_000, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=20_000)


class EssayRoundSummary(Contract):
    round_id: Identity
    course_id: Identity
    artifact_id: Identity
    block_id: Identity
    draft_revision_id: Identity
    draft_content_hash: Digest
    round_number: int
    task_id: Identity
    state: str
    feedback_artifact_id: Identity | None
    assessment_revision: int
    reference_assessment_id: Identity | None
    user_assessment_id: Identity | None
    created_at: str


class EssayRound(EssayRoundSummary):
    draft: EssayDraftBlock
    content_role: Literal["user_note", "ai_generated", "mixed"]
    result: dict | None
    assessments: list[EssayAssessment]


class EssayRoundPage(Contract):
    items: list[EssayRoundSummary]
    next_before: int | None


class EssayOperation(Contract):
    operation_id: Identity
    kind: Literal["essay_assessment"]
    result: EssayRound
    created_at: str


class EssaySuggestionInput(Contract):
    operation_id: Identity
    expected_revision_id: Identity
    annotation_indices: list[int] = Field(min_length=1, max_length=512)
