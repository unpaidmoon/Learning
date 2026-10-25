"""DeepTutor typed transaction excerpts; legacy string-option import removed."""

from __future__ import annotations

from enum import Enum
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PendingOption(BaseModel):
    """One choice a mastery question offers: a stable label and its answer text.

    Two fields rather than one ``"A: body"`` string because they are two
    different things — the learner picks the label, the card renders the body,
    and grading compares labels. The flat string was inherited from the
    generic ``ask_user`` card, and it had to be split back apart with a regex
    on every read: that is how the maths option ``"x - 1 = 0"`` once
    registered as label ``X`` with the body ``"1 = 0"``. Mastery questions now
    carry the split the tutor made.
    """

    model_config = ConfigDict(extra="ignore")

    label: str
    body: str



class PendingQuestion(BaseModel):
    """A question posed to the learner and awaiting their answer.

    Persisted so grading is deterministic across turns: the expected answer
    lives here server-side and never round-trips through the model. The tutor
    poses a question with ``mastery_quiz`` (storing this), the learner answers
    on a later turn, and ``mastery_grade`` scores the stored answer.
    """

    model_config = ConfigDict(extra="ignore")

    question_id: str
    knowledge_point_id: str
    module_id: str = ""
    prompt: str = ""
    question_type: str = "short"
    expected_answer: str = ""
    options: list[PendingOption] = Field(default_factory=list)
    # Reference explanation and difficulty, captured when the question is
    # posed. Server-side like ``expected_answer`` — ``public_pending_question``
    # never projects them, so an explanation cannot leak the answer into the
    # card the learner is about to answer. They travel with the attempt into
    # the question bank, which is what makes a mastery mistake reviewable
    # later instead of a bare right/wrong.
    explanation: str = ""
    difficulty: str = ""
    # Server-side provenance snapshot; never projected onto an answer card.
    source_evidence: dict[str, Any] = Field(default_factory=dict)
    # Server-owned exposure evidence, independent of visual-task metadata.
    # Never included in the learner's question/answer-key projection.
    assistance_used: bool = False
    # Native registration identity; later teaching must not contaminate an
    # unrelated question merely because it cites the same physical page.
    assistance_context: dict[str, str] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    visual_context: dict[str, Any] = Field(default_factory=dict)
    accepted_answers: list[str] = Field(default_factory=list)
    # One active interaction can present multiple independent questions. The
    # parent owns lifecycle only; answer keys and credit remain on its leaves.
    questions: list[PendingQuestion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_group(self) -> PendingQuestion:
        if self.question_type != "group":
            if self.questions:
                raise ValueError("Only a group may contain questions")
            return self
        if not 2 <= len(self.questions) <= 4:
            raise ValueError("A question group needs two to four questions")
        ids = [q.question_id for q in self.questions]
        if len(set(ids)) != len(ids) or self.question_id in ids or not all(ids):
            raise ValueError("Group question ids must be distinct and nonempty")
        question_keys = {(q.knowledge_point_id, q.prompt.strip()) for q in self.questions}
        if len(question_keys) != len(self.questions):
            raise ValueError(
                "A group needs distinct questions, not copies of the same objective prompt"
            )
        if any(
            q.question_type not in {"choice", "short", "open"}
            or q.questions
            or not q.expected_answer.strip()
            or not q.prompt.strip()
            or not q.knowledge_point_id.strip()
            for q in self.questions
        ):
            raise ValueError("Group leaves need independent, non-nested answer contracts")
        if self.expected_answer or self.options or self.visual_context:
            raise ValueError("A group parent cannot carry an answer key or visual task")
        return self

    @property
    def choice_map(self) -> dict[str, str]:
        """The ``{label: body}`` form grading and the question bank compare on."""
        return {option.label: option.body for option in self.options}



class InteractionStatus(str, Enum):
    """Durable lifecycle for one learner-facing mastery interaction.

    The chat runtime may disappear at any point; this state is the source of
    truth for whether a question still needs an answer or has already been
    graded.  Terminal interactions are retained for idempotent retries and
    audit history.
    """

    REGISTERED = "registered"
    AWAITING_INPUT = "awaiting_input"
    ANSWERED = "answered"
    GRADED = "graded"
    ABANDONED = "abandoned"



class MasteryInteraction(BaseModel):
    """A persisted question/answer transaction for a mastery path."""

    model_config = ConfigDict(extra="ignore")

    interaction_id: str
    path_id: str
    question: PendingQuestion
    status: InteractionStatus = InteractionStatus.REGISTERED
    session_id: str = ""
    turn_id: str = ""
    user_answer: str = ""
    answers: dict[str, str] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)



class MasteryEvent(BaseModel):
    """Committed path event consumed by recovery and future live UIs."""

    model_config = ConfigDict(extra="ignore")

    id: int = 0
    path_id: str
    revision: int
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    session_id: str = ""
    turn_id: str = ""
    created_at: float = Field(default_factory=time.time)



class MasteryPathLease(BaseModel):
    """The one active mutating turn allowed for a mastery path."""

    model_config = ConfigDict(extra="ignore")

    path_id: str
    session_id: str
    turn_id: str
    acquired_at: float = Field(default_factory=time.time)
