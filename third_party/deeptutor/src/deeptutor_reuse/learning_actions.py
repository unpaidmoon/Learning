"""Project pure action adapter; the original models remain attribution boundaries.

Skip and reveal are events, never new QuizAttempt grades. Application-owned
transactions persist/dedupe the returned event and decide actual mastery.
"""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict

from .book_models import Progress
from .book_progress import mark_visited
from .learning_transactions import InteractionStatus, MasteryEvent, MasteryInteraction


class LearningActionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    progress: Progress
    interaction: MasteryInteraction
    event: MasteryEvent


def _expose(question):
    question.assistance_used = True
    for leaf in question.questions:
        _expose(leaf)


def apply_learning_action(
    progress: Progress, interaction: MasteryInteraction, *,
    action: Literal["skip", "view_explanation", "navigate"],
    event_id: int, revision: int, now: float, target_page_id: str = "",
) -> LearningActionResult:
    if action not in {"skip", "view_explanation", "navigate"} or event_id < 1 or revision < 1:
        raise ValueError("LEARNING_ACTION_INVALID")
    result = progress.model_copy(deep=True)
    transaction = interaction.model_copy(deep=True)
    active = transaction.status not in {InteractionStatus.GRADED, InteractionStatus.ABANDONED}
    if action == "skip" and active:
        transaction.status = InteractionStatus.ABANDONED
    elif action == "view_explanation" and active:
        _expose(transaction.question)
    if target_page_id:
        # Free navigation: original reading function has no mastery/prereq gate.
        # Target scope/visibility must already be checked by the application.
        mark_visited(result, target_page_id)
        result.updated_at = now
    transaction.updated_at = now
    event = MasteryEvent(
        id=event_id, path_id=transaction.path_id, revision=revision,
        event_type="learning_" + action,
        payload={"interaction_id": transaction.interaction_id, "question_id": transaction.question.question_id,
                 "target_page_id": target_page_id, "grants_mastery": False, "adds_quiz_attempt": False},
        session_id=transaction.session_id, turn_id=transaction.turn_id, created_at=now,
    )
    return LearningActionResult(progress=result, interaction=transaction, event=event)


def independent_grade_eligible(interaction: MasteryInteraction, *, adjudicated_correct: bool | None) -> bool:
    """Application supplies the actual adjudication; this grants no mastery itself."""
    def exposed(question):
        return question.assistance_used or any(exposed(leaf) for leaf in question.questions)
    return (interaction.status == InteractionStatus.GRADED and adjudicated_correct is True
            and not exposed(interaction.question))
