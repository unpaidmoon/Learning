"""Task-specific source grants and reserved result identity, without host paths."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import (
    ArtifactKind,
    Contract,
    DocumentRangeInput,
    Identity,
    ModelSettings,
    OutlineItem,
    TaskKind,
)
from .research_models import ResearchScope


class SelectedSource(Contract):
    kind: Literal["selection"] = "selection"
    snapshot_id: Identity


class DocumentSource(DocumentRangeInput):
    kind: Literal["document"] = "document"
    source_course_id: Identity | None = None
    text_only: bool = False


class TranscriptSource(Contract):
    kind: Literal["transcript"] = "transcript"
    source_course_id: Identity | None = None
    asset_id: Identity
    source_version: Identity
    track_id: Identity | None = None
    start_ms: int = Field(default=0, ge=0)
    end_ms: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("transcript scope is empty")
        return self


class ArtifactSource(Contract):
    kind: Literal["artifact"] = "artifact"
    source_course_id: Identity | None = None
    artifact_id: Identity
    revision_id: Identity
    block_ids: list[Identity] = Field(default_factory=list, max_length=2000)


class CourseSource(Contract):
    kind: Literal["course"] = "course"
    # Only explicit user task declarations can name another read course.
    source_course_id: Identity | None = None
    include_generated: bool = False


TaskSourceDeclaration = Annotated[
    SelectedSource | DocumentSource | TranscriptSource | ArtifactSource | CourseSource,
    Field(discriminator="kind"),
]


class NewTaskTarget(Contract):
    mode: Literal["create"] = "create"
    title: str = Field(min_length=1, max_length=200)


class EditTaskTarget(Contract):
    mode: Literal["edit"] = "edit"
    artifact_id: Identity
    expected_revision_id: Identity
    block_ids: list[Identity] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.block_ids)) != len(self.block_ids):
            raise ValueError("duplicate target block")
        return self


class AnswerReviewScope(Contract):
    answer_id: Identity
    expected_revision: int = Field(ge=1)


class EssayReviewScope(Contract):
    artifact_id: Identity
    revision_id: Identity
    block_id: Identity
    expected_round_number: int = Field(default=0, ge=0)


class TaskIntent(Contract):
    operation_id: Identity
    chat_id: Identity | None = None
    kind: TaskKind
    instruction: str = Field(min_length=1, max_length=100_000)
    coverage_mode: Literal["selection", "complete"] = "selection"
    sources: list[TaskSourceDeclaration] = Field(default_factory=list, max_length=1000)
    target: Annotated[NewTaskTarget | EditTaskTarget, Field(discriminator="mode")]
    sources_only: bool = False
    allow_external: bool = False
    outline: list[OutlineItem] = Field(default_factory=list, max_length=500)
    outline_supplied: bool = False
    answer_review: AnswerReviewScope | None = None
    essay_review: EssayReviewScope | None = None
    research: ResearchScope | None = None

    @model_validator(mode="after")
    def scope(self):
        if self.kind == "aligned_translation":
            if (
                not self.sources
                or not self.sources_only
                or self.target.mode != "create"
                or any(
                    not isinstance(source, (SelectedSource, DocumentSource, ArtifactSource))
                    for source in self.sources
                )
                or any(
                    isinstance(source, DocumentSource) and not source.text_only
                    for source in self.sources
                )
            ):
                raise ValueError(
                    "aligned translation needs an explicit selected/document text scope and a new result"
                )
        elif any(
            isinstance(source, DocumentSource) and source.text_only for source in self.sources
        ):
            raise ValueError("text-only document scope is specific to aligned translation")
        if self.research is not None and self.kind != "research":
            raise ValueError("research scope is task-specific")
        if self.research and self.research.urls and not self.allow_external:
            raise ValueError("external URLs require explicit research permission")
        if self.kind == "answer_review":
            if (
                self.answer_review is None
                or self.sources
                or self.target.mode != "create"
                or self.allow_external
                or self.coverage_mode != "selection"
                or self.sources_only
            ):
                raise ValueError(
                    "answer review uses its exact stored question and actual answer only"
                )
        elif self.answer_review is not None:
            raise ValueError("answer review scope is task-specific")
        if self.kind == "essay_review":
            if (
                self.essay_review is None
                or self.sources
                or self.target.mode != "create"
                or self.allow_external
                or self.coverage_mode != "selection"
                or self.sources_only
            ):
                raise ValueError(
                    "essay review uses one exact existing draft and its grading standard"
                )
        elif self.essay_review is not None:
            raise ValueError("essay review scope is task-specific")
        if (
            (self.sources_only or self.coverage_mode == "complete")
            and not self.sources
            and self.target.mode != "edit"
            and not (self.kind == "research" and self.allow_external)
        ):
            raise ValueError("this task requires explicit sources")
        if self.allow_external and (self.kind != "research" or self.sources_only):
            raise ValueError("external permission is for an explicit research task only")
        if self.kind == "translation" and (
            len(self.sources) != 1
            or not isinstance(self.sources[0], SelectedSource)
            or self.coverage_mode != "selection"
            or self.allow_external
        ):
            raise ValueError("translation receives one exact selection only")
        if self.outline_supplied and not self.outline:
            raise ValueError("supplied outline is empty")
        if len({item.chapter_id for item in self.outline}) != len(self.outline):
            raise ValueError("duplicate chapter identity")
        docs = {}
        for item in self.sources:
            if isinstance(item, DocumentSource):
                key = (item.source_course_id, item.asset_id, item.source_version)
                low, high = (
                    item.first_page,
                    item.last_page if item.last_page is not None else float("inf"),
                )
                if any(low <= end and begin <= high for begin, end in docs.get(key, [])):
                    raise ValueError("overlapping document ranges")
                docs.setdefault(key, []).append((low, high))
        serialized = [item.model_dump_json() for item in self.sources]
        if len(set(serialized)) != len(serialized):
            raise ValueError("duplicate task source")
        if any(isinstance(item, CourseSource) for item in self.sources) and len(self.sources) != 1:
            raise ValueError("whole-course scope cannot overlap explicitly selected sources")
        return self


class TaskAdmission(TaskIntent):
    settings: ModelSettings


class TaskSourceView(Contract):
    source_id: Identity
    label: str = ""
    ordinal: int
    declaration: dict
    state: str
    expected_leaf_count: int | None
    ready_leaf_count: int
    processed_leaf_count: int
    error_code: str | None
    preparation_job_id: Identity | None
    excluded_at_scope_revision: int | None


class TaskLeafView(Contract):
    leaf_id: Identity
    source_id: Identity
    ordinal: int
    label: str
    locator: dict
    state: str
    required_representations: list[str]
    snapshot_ids: list[Identity]
    error_code: str | None
    excluded_at_scope_revision: int | None


class TaskTargetView(Contract):
    artifact_id: Identity
    materialized: bool
    mode: Literal["create", "edit"]
    artifact_kind: ArtifactKind
    title: str
    expected_revision_id: Identity | None
    state: str
    last_receipt: dict | None


class TaskCoverageView(Contract):
    task_id: Identity
    scope_revision: int
    plan_revision: int
    sources: list[TaskSourceView]
    leaves: list[TaskLeafView]
    target: TaskTargetView


class SourceGapDecision(Contract):
    operation_id: Identity
    expected_revision: int = Field(ge=0)
    decision: Literal["retry", "exclude", "accept_partial"]
    source_ids: list[Identity] = Field(default_factory=list, max_length=1000)
    leaf_ids: list[Identity] = Field(default_factory=list, max_length=20_000)

    @model_validator(mode="after")
    def selected(self):
        if self.decision != "accept_partial" and not (self.source_ids or self.leaf_ids):
            raise ValueError("select an actual source gap")
        return self
