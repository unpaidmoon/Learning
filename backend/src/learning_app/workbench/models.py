"""Public learning contracts; third-party IR stays behind these types."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .visualization import LocalVisualizationEnvelope

Identity = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ContentRole = Literal["original", "user_note", "ai_generated", "mixed"]
ArtifactKind = Literal[
    "note",
    "translation",
    "handout",
    "question_set",
    "card_set",
    "essay",
    "solution",
    "visualization",
    "learning_path",
    "book",
    "report",
]
TaskKind = Literal[
    "note",
    "aligned_translation",
    "translation",
    "handout",
    "question_set",
    "card_set",
    "essay_review",
    "answer_review",
    "deep_solution",
    "visualization",
    "learning_path",
    "book",
    "research",
]
TaskState = Literal[
    "queued",
    "awaiting_outline",
    "running",
    "paused",
    "needs_sources",
    "unknown",
    "completed",
    "partial",
    "failed",
    "cancelled",
]
UnitState = Literal["pending", "read", "dispatching", "completed", "missing", "failed", "unknown"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageRegion(Contract):
    """Normalized original-page coordinates, origin at top left, before zoom."""

    x0: float = Field(ge=0, le=1, allow_inf_nan=False)
    y0: float = Field(ge=0, le=1, allow_inf_nan=False)
    x1: float = Field(ge=0, le=1, allow_inf_nan=False)
    y1: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self) -> PageRegion:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("region must have positive width and height")
        return self


class DocumentRef(Contract):
    kind: Literal["document"] = "document"
    asset_id: Identity
    source_version: Identity
    source_digest: Digest
    page_index: int = Field(ge=0)
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"
    page_label: str | None = Field(default=None, max_length=100)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    regions: list[PageRegion] = Field(default_factory=list, max_length=256)
    parser_version: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def ordered(self) -> DocumentRef:
        if self.end < self.start:
            raise ValueError("selection end precedes start")
        return self


class DocumentPagePart(Contract):
    page_index: int = Field(ge=0)
    page_label: str | None = Field(default=None, max_length=100)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    regions: list[PageRegion] = Field(min_length=1, max_length=256)
    parser_version: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def ordered(self) -> DocumentPagePart:
        if self.end <= self.start:
            raise ValueError("page selection must not be empty")
        return self


class DocumentSelectionRef(Contract):
    kind: Literal["document_selection"] = "document_selection"
    asset_id: Identity
    source_version: Identity
    source_digest: Digest
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"
    parts: list[DocumentPagePart] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def ordered(self) -> DocumentSelectionRef:
        pages = [part.page_index for part in self.parts]
        if pages != sorted(set(pages)):
            raise ValueError("selection pages must be unique and ordered")
        return self


class DocumentGapRef(Contract):
    kind: Literal["document_gap"] = "document_gap"
    asset_id: Identity
    source_version: Identity
    source_digest: Digest
    page_index: int = Field(ge=0)
    error_code: Identity
    parser_version: str = Field(min_length=1, max_length=100)


class DocumentImageRef(Contract):
    kind: Literal["document_image"] = "document_image"
    asset_id: Identity
    source_version: Identity
    source_digest: Digest
    page_index: int = Field(ge=0)
    parser_version: str = Field(min_length=1, max_length=100)
    image_sha256: Digest
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)


class SubmittedImageRef(Contract):
    kind: Literal["submitted_image"] = "submitted_image"
    context_snapshot_id: Identity
    context_hash: Digest
    message_id: Identity
    revision_id: Identity
    attachment_id: Identity
    image_sha256: Digest
    media_type: Literal["image/png", "image/jpeg", "image/webp"]


class DocumentRegionImageRef(DocumentImageRef):
    kind: Literal["document_region_image"] = "document_region_image"
    region_id: Identity
    parent_text_snapshot_id: Identity
    parent_text_hash: Digest
    region_parser_version: str = Field(min_length=1, max_length=100)
    region: PageRegion
    pixel_rect: tuple[int, int, int, int]
    image_to_pdf: tuple[float, float, float, float, float, float]

    @model_validator(mode="after")
    def actual_crop(self):
        import math

        left, top, right, bottom = self.pixel_rect
        if (
            not 0 <= left < right
            or not 0 <= top < bottom
            or right - left != self.width
            or bottom - top != self.height
            or not all(math.isfinite(v) for v in self.image_to_pdf)
        ):
            raise ValueError("document crop dimensions or mapping do not match its pixel rectangle")
        return self


class SnapshotSliceRef(Contract):
    kind: Literal["snapshot_slice"] = "snapshot_slice"
    parent_snapshot_id: Identity
    parent_hash: Digest
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> SnapshotSliceRef:
        if self.end <= self.start:
            raise ValueError("snapshot slice is empty")
        return self


class SubtitleRef(Contract):
    kind: Literal["subtitle"] = "subtitle"
    asset_id: Identity
    source_version: Identity
    source_digest: Digest
    transcript_revision_id: Identity
    segment_id: Identity
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> SubtitleRef:
        if self.end_ms <= self.start_ms:
            raise ValueError("subtitle range must have positive duration")
        return self


class ArtifactRef(Contract):
    kind: Literal["artifact"] = "artifact"
    artifact_id: Identity
    revision_id: Identity
    block_id: Identity


class AnswerRef(Contract):
    kind: Literal["practice_answer"] = "practice_answer"
    answer_id: Identity
    question_revision_id: Identity
    block_id: Identity


class MessageRef(Contract):
    kind: Literal["message"] = "message"
    message_id: Identity
    revision_id: Identity
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self) -> MessageRef:
        if self.end <= self.start:
            raise ValueError("message selection is empty")
        return self


class WebEvidenceRef(Contract):
    kind: Literal["web_evidence"] = "web_evidence"
    evidence_kind: Literal["page_text", "search_excerpt"]
    url: str = Field(max_length=4096)
    final_url: str | None = Field(default=None, max_length=4096)
    title: str = Field(default="", max_length=500)
    retrieved_at: str
    published_at: str | None = None
    response_sha256: Digest
    target_http_status: int | None = Field(default=None, ge=100, le=599)
    parser_version: str

    @model_validator(mode="after")
    def public_addresses(self):
        from .external_http import public_url

        public_url(self.url)
        if self.final_url:
            public_url(self.final_url)
        return self


SourceRef = Annotated[
    DocumentRef
    | DocumentSelectionRef
    | DocumentGapRef
    | DocumentImageRef
    | DocumentRegionImageRef
    | SubmittedImageRef
    | SnapshotSliceRef
    | SubtitleRef
    | ArtifactRef
    | AnswerRef
    | MessageRef
    | WebEvidenceRef,
    Field(discriminator="kind"),
]


class SourceSnapshot(Contract):
    snapshot_id: Identity
    course_id: Identity
    reference: SourceRef
    content_role: ContentRole
    text: str
    content_hash: Digest
    created_at: str


class MarkdownBlock(Contract):
    block_id: Identity
    kind: Literal["markdown"] = "markdown"
    markdown: str = Field(max_length=200_000)
    attribution: Literal[
        "source_quote", "source_paraphrase", "source_inference", "ai_supplement", "user_authored"
    ]
    source_snapshot_ids: list[Identity] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def source_attribution(self) -> MarkdownBlock:
        if (
            self.attribution in {"source_quote", "source_paraphrase", "source_inference"}
            and not self.source_snapshot_ids
        ):
            raise ValueError("source-attributed blocks require a source snapshot")
        return self


class SourceTextRange(Contract):
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("source text range is empty")
        return self


class TranslationSourcePart(SourceTextRange):
    snapshot_id: Identity


class AlignedTranslation(Contract):
    source_text: str = Field(min_length=1, max_length=200_000)
    source_parts: list[TranslationSourcePart] = Field(min_length=1, max_length=96)

    @model_validator(mode="after")
    def actual_length(self):
        if sum(part.end - part.start for part in self.source_parts) != len(self.source_text):
            raise ValueError("translation source ranges do not cover the actual text")
        return self


class TranslationBlock(MarkdownBlock):
    kind: Literal["translation"] = "translation"
    translation: AlignedTranslation

    @model_validator(mode="after")
    def source_parts(self):
        expected = list(dict.fromkeys(part.snapshot_id for part in self.translation.source_parts))
        if self.source_snapshot_ids != expected:
            raise ValueError("translation references must match its exact source parts")
        if self.attribution not in {"source_paraphrase", "user_authored"}:
            raise ValueError(
                "translation cannot be presented as an original quotation or free supplement"
            )
        return self


class QuestionOption(Contract):
    option_id: str = Field(pattern=r"^[A-Z]$")
    text: str = Field(min_length=1, max_length=20_000)


class QuestionContent(Contract):
    question_type: Literal[
        "single_choice",
        "multiple_choice",
        "true_false",
        "short_answer",
        "essay",
        "calculation",
        "proof",
        "numeric",
        "ordering",
        "matching",
    ]
    options: list[QuestionOption] = Field(default_factory=list, max_length=26)
    reference_answer: str = Field(default="", max_length=100_000)
    explanation: str = Field(default="", max_length=100_000)
    answer_authority: Literal["user_confirmed", "ai_reference", "missing"] = "ai_reference"

    @model_validator(mode="after")
    def valid_options(self):
        if len({item.option_id for item in self.options}) != len(self.options):
            raise ValueError("duplicate question option")
        if self.question_type in {"single_choice", "multiple_choice"} and len(self.options) < 2:
            raise ValueError("choice questions require distinct options")
        if self.answer_authority != "missing" and not self.reference_answer.strip():
            raise ValueError("an answer authority requires a reference answer")
        return self


class QuestionBlock(MarkdownBlock):
    kind: Literal["question"] = "question"
    question: QuestionContent

    @model_validator(mode="after")
    def actual_prompt(self):
        if not self.markdown.strip() or self.attribution == "source_quote":
            raise ValueError("structured questions need a prompt and separate source attribution")
        return self


class CardContent(Contract):
    back: str = Field(min_length=1, max_length=100_000)


class CardBlock(MarkdownBlock):
    kind: Literal["card"] = "card"
    card: CardContent

    @model_validator(mode="after")
    def actual_front(self):
        if not self.markdown.strip() or self.attribution == "source_quote":
            raise ValueError("structured cards need a front and separate source attribution")
        return self


class VisualizationBlock(MarkdownBlock):
    kind: Literal["visualization"] = "visualization"
    visualization: LocalVisualizationEnvelope

    @model_validator(mode="after")
    def derived_visualization_identity(self):
        if self.attribution == "source_quote":
            raise ValueError(
                "generated diagram data must not be presented as an original figure quote"
            )
        return self


class PathQuestionLink(Contract):
    artifact_id: Identity
    revision_id: Identity
    block_id: Identity


class PathStepContent(Contract):
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=3000)
    suggested_order: int = Field(default=0, ge=0, le=1_000_000)
    recall_prompt: str = Field(min_length=1, max_length=5000)
    explanation: str = Field(min_length=1, max_length=20_000)
    questions: list[PathQuestionLink] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_questions(self):
        keys = [(q.artifact_id, q.revision_id, q.block_id) for q in self.questions]
        if len(keys) != len(set(keys)):
            raise ValueError("path question references must be unique")
        return self


class PathStepBlock(MarkdownBlock):
    kind: Literal["path_step"] = "path_step"
    step: PathStepContent


class AssessmentContent(Contract):
    answer_id: Identity
    expected_revision: int = Field(ge=1)
    verdict: Literal["correct", "wrong", "partial", "pending"]
    score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def pending_score(self):
        if self.verdict == "pending" and self.score is not None:
            raise ValueError("pending assessments have no numeric score")
        return self


class AssessmentBlock(MarkdownBlock):
    kind: Literal["assessment"] = "assessment"
    assessment: AssessmentContent


class EssayDimension(Contract):
    name: str = Field(min_length=1, max_length=120)
    max_score: float = Field(gt=0, le=10_000, allow_inf_nan=False)
    criteria: str = Field(default="", max_length=5000)


class EssayRubric(Contract):
    max_total: float = Field(default=100, gt=0, le=10_000, allow_inf_nan=False)
    aggregation: Literal["sum", "independent"] = "independent"
    dimensions: list[EssayDimension] = Field(default_factory=list, max_length=32)
    description: str = Field(
        default="按题目要求、内容、结构和语言给参考评价，不冒充正式考试成绩。", max_length=20_000
    )

    @model_validator(mode="after")
    def coherent(self):
        if len({item.name for item in self.dimensions}) != len(self.dimensions):
            raise ValueError("duplicate essay criterion")
        if self.aggregation == "sum" and (
            not self.dimensions
            or abs(sum(item.max_score for item in self.dimensions) - self.max_total) > 0.000001
        ):
            raise ValueError("summed rubric dimensions must equal its maximum")
        return self


class EssayDraftContent(Contract):
    topic: str = Field(default="", max_length=20_000)
    rubric: EssayRubric = Field(default_factory=EssayRubric)


class EssayDraftBlock(MarkdownBlock):
    kind: Literal["essay_draft"] = "essay_draft"
    markdown: str = Field(min_length=1, max_length=50_000)
    essay: EssayDraftContent

    @model_validator(mode="after")
    def actual_draft(self):
        try:
            self.markdown.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("essay text must contain complete Unicode codepoints") from error
        if not self.markdown.strip() or self.attribution == "source_quote":
            raise ValueError("essay drafts require their actual text and separate identity")
        return self


class EssayFeedbackContent(Contract):
    round_id: Identity
    draft_revision_id: Identity
    draft_content_hash: Digest
    annotated_text: str = Field(default="", max_length=256_000)


class EssayFeedbackBlock(MarkdownBlock):
    kind: Literal["essay_feedback"] = "essay_feedback"
    markdown: str = Field(min_length=1, max_length=512_000)
    feedback: EssayFeedbackContent


LearningBlock = Annotated[
    MarkdownBlock
    | TranslationBlock
    | QuestionBlock
    | CardBlock
    | AssessmentBlock
    | EssayDraftBlock
    | EssayFeedbackBlock
    | PathStepBlock
    | VisualizationBlock,
    Field(discriminator="kind"),
]


def block_text(block: LearningBlock) -> str:
    """Canonical content only; answers and review state live in their own tables."""
    if isinstance(block, TranslationBlock):
        return block.translation.source_text + "\n\n" + block.markdown
    if isinstance(block, QuestionBlock):
        return "\n".join(
            [
                block.markdown,
                *(f"{item.option_id}. {item.text}" for item in block.question.options),
                block.question.reference_answer,
                block.question.explanation,
            ]
        )
    if isinstance(block, CardBlock):
        return block.markdown + "\n\n" + block.card.back
    if isinstance(block, PathStepBlock):
        return "\n\n".join(
            [
                block.step.title,
                block.step.objective,
                block.markdown,
                block.step.recall_prompt,
                block.step.explanation,
            ]
        )
    if isinstance(block, VisualizationBlock):
        return block.markdown + "\n\n" + block.visualization.model_dump_json()
    if isinstance(block, EssayDraftBlock):
        import json

        return json.dumps(
            {
                "essay_text": block.markdown,
                "topic": block.essay.topic,
                "rubric": block.essay.rubric.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
    if isinstance(block, EssayFeedbackBlock):
        return block.markdown + "\n\n" + block.feedback.annotated_text
    return block.markdown


class ArtifactBody(Contract):
    blocks: list[LearningBlock] = Field(min_length=1, max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def legacy_markdown_default(cls, value):
        # Keep the original MarkdownBlock default; never infer a structured kind.
        if isinstance(value, dict) and isinstance(value.get("blocks"), list):
            return {
                **value,
                "blocks": [
                    {"kind": "markdown", **block}
                    if isinstance(block, dict) and "kind" not in block
                    else {**block, "kind": "markdown"}
                    if isinstance(block, dict) and block.get("kind") == "text"
                    else block
                    for block in value["blocks"]
                ],
            }
        return value

    @model_validator(mode="after")
    def unique_blocks(self) -> ArtifactBody:
        ids = [block.block_id for block in self.blocks]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate block identity")
        if sum(len(block_text(block)) for block in self.blocks) > 4_000_000:
            raise ValueError("artifact exceeds supported content size")
        return self


class ArtifactCreate(Contract):
    operation_id: Identity
    kind: ArtifactKind
    title: str = Field(min_length=1, max_length=200)
    body: ArtifactBody


class BlockChange(Contract):
    block_id: Identity
    replacement: LearningBlock | None

    @model_validator(mode="before")
    @classmethod
    def legacy_markdown_default(cls, value):
        if (
            isinstance(value, dict)
            and isinstance(value.get("replacement"), dict)
            and "kind" not in value["replacement"]
        ):
            return {**value, "replacement": {"kind": "markdown", **value["replacement"]}}
        return value

    @model_validator(mode="after")
    def same_identity(self) -> BlockChange:
        if self.replacement is not None and self.replacement.block_id != self.block_id:
            raise ValueError("replacement must retain block identity")
        return self


class ArtifactUpdate(Contract):
    operation_id: Identity
    expected_revision_id: Identity
    title: str | None = Field(default=None, min_length=1, max_length=200)
    changes: list[BlockChange] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def valid_changes(self) -> ArtifactUpdate:
        ids = [change.block_id for change in self.changes]
        if len(ids) != len(set(ids)) or (not ids and self.title is None):
            raise ValueError("changes must be nonempty and unique")
        return self


class ArtifactRestore(Contract):
    operation_id: Identity
    expected_revision_id: Identity
    restore_revision_id: Identity


class ArtifactRevision(Contract):
    revision_id: Identity
    artifact_id: Identity
    parent_revision_id: Identity | None
    title: str
    body: ArtifactBody
    content_role: Literal["user_note", "ai_generated", "mixed"]
    author: Literal["user", "model"]
    task_id: Identity | None
    disposition: Literal["published", "conflict"]
    created_at: str


class ArtifactView(Contract):
    artifact_id: Identity
    course_id: Identity
    kind: ArtifactKind
    title: str
    head_revision_id: Identity
    content_role: Literal["user_note", "ai_generated", "mixed"]
    indexed: bool
    created_at: str
    updated_at: str
    revision: ArtifactRevision


class RevisionConflict(Contract):
    conflict_id: Identity
    artifact_id: Identity
    expected_revision_id: Identity
    current_revision_id: Identity
    proposed_revision_id: Identity
    created_at: str


class ArtifactReceipt(Contract):
    outcome: Literal["applied", "conflict"]
    artifact: ArtifactView
    conflict: RevisionConflict | None = None


class SearchHit(Contract):
    artifact_id: Identity
    revision_id: Identity
    course_id: Identity
    title: str
    kind: ArtifactKind
    content_role: Literal["user_note", "ai_generated", "mixed"]
    excerpt: str


class ModelSettings(Contract):
    model: str = Field(min_length=1, max_length=120)
    thinking: Literal["none", "low", "medium", "high", "xhigh", "max"]
    max_output_tokens: int = Field(ge=256, le=131072)
    timeout_seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)


class TaskUnitInput(Contract):
    unit_id: Identity
    label: str = Field(min_length=1, max_length=200)
    source_snapshot_id: Identity


class OutlineItem(Contract):
    chapter_id: Identity
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(default="", max_length=2000)


class TaskCreate(Contract):
    operation_id: Identity
    chat_id: Identity | None = None
    kind: TaskKind
    instruction: str = Field(min_length=1, max_length=100_000)
    coverage_mode: Literal["selection", "complete"] = "selection"
    scope_manifest_id: Identity | None = None
    units: list[TaskUnitInput] = Field(default_factory=list, max_length=20_000)
    settings: ModelSettings
    sources_only: bool = False
    allow_external: bool = False
    outline: list[OutlineItem] = Field(default_factory=list, max_length=500)
    # A supplied outline is already explicit; absent book outline needs one confirmation.
    outline_supplied: bool = False

    @model_validator(mode="after")
    def scope_and_outline(self) -> TaskCreate:
        if self.kind == "aligned_translation":
            raise ValueError("aligned translation requires an explicit task intent and text scope")
        if self.coverage_mode == "complete" and (self.scope_manifest_id is None or self.units):
            raise ValueError(
                "complete tasks require a server-frozen manifest, not caller-supplied units"
            )
        if self.coverage_mode == "selection" and self.scope_manifest_id is not None:
            raise ValueError("manifest coverage must be declared complete")
        if (
            self.kind in {"note", "translation", "handout", "deep_solution"}
            and self.coverage_mode == "selection"
            and not self.units
        ):
            raise ValueError("this task requires actual selected input")
        if self.allow_external and self.kind != "research":
            raise ValueError("external research permission is task-specific")
        if self.sources_only and self.allow_external:
            raise ValueError("sources-only task cannot request external research")
        if self.outline_supplied and not self.outline:
            raise ValueError("an explicit outline cannot be empty")
        if len({unit.unit_id for unit in self.units}) != len(self.units):
            raise ValueError("duplicate task unit")
        if len({item.chapter_id for item in self.outline}) != len(self.outline):
            raise ValueError("duplicate chapter identity")
        return self


class DesktopLearningTaskInput(Contract):
    operation_id: Identity
    kind: TaskKind
    instruction: str = Field(min_length=1, max_length=100_000)
    source_snapshot_ids: list[Identity] = Field(default_factory=list, max_length=20_000)
    pdf_selection: PdfSelectionInput | None = None
    coverage_mode: Literal["selection", "complete"] = "selection"
    scope_manifest_id: Identity | None = None
    sources_only: bool = False
    allow_external: bool = False
    outline: list[OutlineItem] = Field(default_factory=list, max_length=500)
    outline_supplied: bool = False
    cached_task_id: Identity | None = None

    @model_validator(mode="after")
    def translation_cache_hint(self):
        if self.cached_task_id and self.kind != "translation":
            raise ValueError("cached task hints are limited to explicit translations")
        return self


class TaskUnitView(Contract):
    unit_id: Identity
    ordinal: int
    label: str
    source_snapshot_id: Identity | None
    state: UnitState
    run_id: Identity | None = None
    result_artifact_id: Identity | None
    error_code: str | None
    excluded_at_scope_revision: int | None = None


class TranslationReuseInfo(Contract):
    operation_id: Identity
    kind: Literal["saved_result", "in_progress"]
    original_task_id: Identity
    original_run_id: Identity | None = None
    requested_source_snapshot_id: Identity | None = None
    artifact_id: Identity | None = None
    revision_id: Identity | None = None
    text_sha256: Digest | None = None
    actual_outbound_attempts: Literal[0] = 0


class TaskView(Contract):
    task_id: Identity
    course_id: Identity
    chat_id: Identity | None
    kind: TaskKind
    instruction: str
    coverage_mode: Literal["selection", "complete"]
    scope_manifest_id: Identity | None
    revision: int
    scope_revision: int
    state: TaskState
    settings: ModelSettings
    sources_only: bool
    allow_external: bool
    outline: list[OutlineItem]
    outline_confirmed: bool
    layout_version: int = 1
    plan_revision: int = 0
    units: list[TaskUnitView]
    created_at: str
    updated_at: str
    translation_reuse: TranslationReuseInfo | None = None


class TaskAction(Contract):
    operation_id: Identity
    expected_revision: int = Field(ge=0)


class TaskOutline(TaskAction):
    outline: list[OutlineItem] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_chapters(self) -> TaskOutline:
        if len({item.chapter_id for item in self.outline}) != len(self.outline):
            raise ValueError("duplicate chapter identity")
        return self


class TaskGapDecision(TaskAction):
    decision: Literal["retry", "exclude", "accept_partial"]
    unit_ids: list[Identity] = Field(min_length=1, max_length=20_000)


class TextSelectionInput(Contract):
    asset_id: Identity
    source_version: Identity
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class PdfSelectionPage(Contract):
    page_index: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=200_000)
    regions: list[PageRegion] = Field(min_length=1, max_length=256)


class PdfSelectionInput(Contract):
    asset_id: Identity
    source_version: Identity
    text: str = Field(min_length=1, max_length=200_000)
    pages: list[PdfSelectionPage] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def ordered(self) -> PdfSelectionInput:
        pages = [page.page_index for page in self.pages]
        if pages != sorted(set(pages)):
            raise ValueError("selection pages must be unique and ordered")
        if not self.text.strip():
            raise ValueError("selection is blank")
        return self


class PdfGlyph(Contract):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=16)
    region: PageRegion | None = None


class PdfPageCoverage(Contract):
    object_count: int = Field(ge=0)
    nontext_object_count: int = Field(ge=0)
    annotation_count: int = Field(ge=0)
    unmapped_characters: int = Field(ge=0)


class PdfPageContent(Contract):
    asset_id: Identity
    source_version: Identity
    source_digest: Digest
    page_index: int = Field(ge=0)
    page_count: int = Field(gt=0)
    page_label: str | None = None
    view_box: tuple[float, float, float, float]
    rotation: Literal[0, 90, 180, 270]
    parser_version: str
    text: str = Field(max_length=500_000)
    glyphs: list[PdfGlyph] = Field(max_length=500_000)
    text_status: Literal["native", "needs_ocr", "ocr"]
    # A local PDF object inspection, never an AI assertion of completeness.
    coverage: PdfPageCoverage | None = None


class PdfDocumentInfo(Contract):
    asset_id: Identity
    source_version: Identity
    source_digest: Digest
    page_count: int = Field(gt=0)
    parser_version: str


class OcrPixelBox(Contract):
    x0: float = Field(ge=0, allow_inf_nan=False)
    y0: float = Field(ge=0, allow_inf_nan=False)
    x1: float = Field(gt=0, allow_inf_nan=False)
    y1: float = Field(gt=0, allow_inf_nan=False)


class OcrTextItem(Contract):
    text: str = Field(min_length=1, max_length=16)
    bbox: OcrPixelBox


class OcrPageLayer(Contract):
    asset_id: Identity
    source_version: Identity
    source_digest: Digest
    page_number: int = Field(ge=1)
    revision: Digest
    text: str = Field(max_length=500_000)
    words: list[OcrTextItem] = Field(max_length=100_000)
    bitmap_width: int = Field(gt=0, le=16384)
    bitmap_height: int = Field(gt=0, le=16384)
    image_to_pdf: list[Annotated[float, Field(allow_inf_nan=False)]] = Field(
        min_length=6, max_length=6
    )


class OcrPageRevision(Contract):
    page_index: int = Field(ge=0)
    revision: Digest


class OcrPageIndex(Contract):
    pages: list[OcrPageRevision]
    next_page: int | None


class ReadingPosition(Contract):
    page_index: int = Field(default=0, ge=0)
    scale: str = Field(
        default="page-width", pattern=r"^(page-width|page-fit|auto|[0-9]+(?:\.[0-9]+)?)$"
    )
    rotation: Literal[0, 90, 180, 270] = 0
    scroll_fraction: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_scale(self) -> ReadingPosition:
        if (
            self.scale not in {"auto", "page-width", "page-fit"}
            and not 0.01 <= float(self.scale) <= 100
        ):
            raise ValueError("reading scale is outside the supported range")
        return self


class ReadingOpenInput(Contract):
    reader_client_id: Identity
    request_id: Identity
    view_sequence: int = Field(ge=0, le=9_007_199_254_740_991)


class ReadingState(ReadingPosition):
    asset_id: Identity
    source_version: Identity
    reader_epoch: Identity
    revision: int = Field(ge=0)
    updated_at: str


class ReadingStateUpdate(ReadingPosition):
    reader_epoch: Identity
    expected_revision: int = Field(ge=0)


class ArtifactSelectionInput(Contract):
    artifact_id: Identity
    revision_id: Identity
    block_id: Identity


class MessageSelectionInput(Contract):
    message_id: Identity
    revision_id: Identity
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class LearningOperationView(Contract):
    operation_id: Identity
    course_id: Identity
    kind: Literal[
        "artifact_create",
        "artifact_update",
        "artifact_restore",
        "task_create",
        "task_control",
        "task_outline",
        "task_gap",
    ]
    result: ArtifactReceipt | TaskView
    created_at: str


class UsageCounters(Contract):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None


class TaskAttemptView(Contract):
    attempt_id: Identity
    request_id: Identity
    model: str
    thinking: str
    kind: Literal["model"]
    actual_outbound: bool
    actual_outbound_attempt: int | None
    status: str | None
    received_ns: int | None
    finished_ns: int | None
    request_sha256: Digest | None


class TaskUsageView(Contract):
    settlement_id: str
    request_id: Identity
    source: Literal["fixture", "provider"]
    raw_usage: dict[str, JsonValue] | None
    field_presence: dict[str, bool]
    normalized: UsageCounters


class TaskRunView(Contract):
    run_id: Identity
    task_id: Identity
    unit_id: Identity
    scope_revision: int
    state: Literal[
        "prepared",
        "dispatching",
        "accepted",
        "running",
        "completed",
        "failed",
        "truncated",
        "cancelled",
        "unknown",
    ]
    settings: ModelSettings
    body: str
    reasoning: str
    cancel_requested: bool
    error_code: str | None
    event_watermark: int
    attempts: list[TaskAttemptView]
    usage: list[TaskUsageView]
    usage_status: Literal["known", "unknown"]
    totals: UsageCounters
    actual_outbound_attempts: int
    created_at: str
    updated_at: str


class DocumentRangeInput(Contract):
    asset_id: Identity
    source_version: Identity
    first_page: int = Field(default=0, ge=0)
    last_page: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ordered(self) -> DocumentRangeInput:
        if self.last_page is not None and self.last_page < self.first_page:
            raise ValueError("document range ends before it starts")
        return self


class ArtifactRangeInput(Contract):
    artifact_id: Identity
    revision_id: Identity
    block_ids: list[Identity] = Field(default_factory=list, max_length=2000)


class ManifestCreate(Contract):
    documents: list[DocumentRangeInput] = Field(default_factory=list, max_length=1000)
    artifacts: list[ArtifactRangeInput] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def nonempty_unique(self) -> ManifestCreate:
        if not self.documents and not self.artifacts:
            raise ValueError("a complete scope requires explicit sources")
        keys = [
            (item.asset_id, item.source_version, item.first_page, item.last_page)
            for item in self.documents
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate document range")
        if len({(item.artifact_id, item.revision_id) for item in self.artifacts}) != len(
            self.artifacts
        ):
            raise ValueError("duplicate artifact range")
        return self


class SourceManifest(Contract):
    manifest_id: Identity
    course_id: Identity
    documents: list[DocumentRangeInput]
    artifacts: list[ArtifactRangeInput]
    units: list[TaskUnitInput]
    scope_hash: Digest
    created_at: str
