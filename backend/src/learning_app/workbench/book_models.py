"""Project book contracts; the upstream Book IR remains an internal projection."""

from typing import Literal

from pydantic import Field, model_validator

from .models import Contract, Digest, Identity, OutlineItem


class BookChapterOutline(OutlineItem):
    source_ids: list[Identity] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("duplicate book source slot")
        return self


class BookOutlineDraft(Contract):
    chapters: list[BookChapterOutline] = Field(min_length=1, max_length=500)
    rationale: str = Field(default="", max_length=5000)

    @model_validator(mode="after")
    def unique_chapters(self):
        if len({c.chapter_id for c in self.chapters}) != len(self.chapters):
            raise ValueError("duplicate chapter identity")
        return self


class BookSourceItem(Contract):
    source_id: Identity
    kind: str
    label: str
    declaration: dict


class BookOutlineView(Contract):
    task_id: Identity
    task_revision: int
    proposal_hash: Digest | None
    proposal: BookOutlineDraft | None
    confirmed: bool
    confirmation_count: int
    supplied: bool
    sources: list[BookSourceItem]


class BookOutlineConfirm(Contract):
    operation_id: Identity
    expected_revision: int = Field(ge=0)
    proposal_hash: Digest
    outline: BookOutlineDraft


class BookPageLayout(Contract):
    page_id: Identity
    chapter_id: Identity
    unit_id: Identity
    title: str = Field(max_length=200)
    order_key: tuple[int, int, int]
    block_ids: list[Identity] = Field(min_length=1, max_length=2000)
    block_hashes: dict[str, Digest]
    question_revisions: dict[str, Identity] = Field(default_factory=dict)
    source_snapshot_ids: list[Identity] = Field(default_factory=list, max_length=96)


class BookRevisionLayout(Contract):
    schema_version: Literal[1] = 1
    origin_task_id: Identity
    outline_hash: Digest
    chapters: list[BookChapterOutline] = Field(min_length=1, max_length=500)
    pages: list[BookPageLayout] = Field(default_factory=list, max_length=20_000)

    @model_validator(mode="after")
    def valid_graph(self):
        chapter_ids = {chapter.chapter_id for chapter in self.chapters}
        page_ids = [page.page_id for page in self.pages]
        blocks = [identity for page in self.pages for identity in page.block_ids]
        if len(chapter_ids) != len(self.chapters) or len(page_ids) != len(set(page_ids)):
            raise ValueError("duplicate book graph identity")
        if len(blocks) != len(set(blocks)) or any(
            p.chapter_id not in chapter_ids for p in self.pages
        ):
            raise ValueError("book page membership conflict")
        for page in self.pages:
            if set(page.block_hashes) != set(page.block_ids) or not set(
                page.question_revisions
            ).issubset(page.block_ids):
                raise ValueError("book block revision conflict")
        return self


class BookPageView(Contract):
    layout: BookPageLayout
    state: Literal["ready", "content_removed"]


class BookChapterView(Contract):
    chapter: BookChapterOutline
    pages: list[BookPageView]
    state: Literal[
        "ready",
        "generating",
        "needs_sources",
        "paused",
        "unknown",
        "content_removed",
        "partial",
        "excluded",
    ]


class BookView(Contract):
    artifact_id: Identity
    revision_id: Identity
    title: str
    layout_hash: Digest
    origin_task_id: Identity
    task_state: str
    book_status: str
    is_historical: bool
    chapters: list[BookChapterView]


class BookChapterLabel(OutlineItem):
    pass


class BookDirectoryUpdate(Contract):
    operation_id: Identity
    expected_revision_id: Identity
    chapters: list[BookChapterLabel] = Field(min_length=1, max_length=500)
