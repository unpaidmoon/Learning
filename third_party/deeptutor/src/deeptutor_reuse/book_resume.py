"""Project deterministic checkpoint transitions over the original Book models.

This module has no queue, storage, provider, authorization or implicit retry.
The application owns durable transactions and reconciles unknown attempts.
Ready-page aggregation calls the retained DeepTutor terminal function.
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .book_compiler_state import finalize_terminal_page
from .book_block_compiler import BlockContext
from .book_models import Block, BlockStatus, Book, BookInputs, BookStatus, Page, PageStatus, Spine


class BlockOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=160)
    page_id: str
    block_id: str
    state: Literal["in_flight", "unknown", "committed", "not_started"] = "in_flight"
    output_digest: str = ""
    reconciliation_ref: str = ""


class BookCompileSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    book: Book
    inputs: BookInputs
    spine: Spine
    pages: list[Page]
    confirmed_outline_digest: str = ""
    confirmation_count: int = Field(default=0, ge=0, le=1)
    operations: dict[str, BlockOperation] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph(self):
        if self.spine.book_id != self.book.id:
            raise ValueError("BOOK_ID_CONFLICT")
        chapters = {c.id: c for c in self.spine.chapters}
        pages = {p.id: p for p in self.pages}
        if len(chapters) != len(self.spine.chapters) or len(pages) != len(self.pages):
            raise ValueError("BOOK_DUPLICATE_ID")
        ordered = [pid for c in self.spine.chapters for pid in c.page_ids]
        if len(ordered) != len(set(ordered)) or set(ordered) != set(pages):
            raise ValueError("BOOK_PAGE_PLAN_CONFLICT")
        block_ids: set[str] = set()
        for page in self.pages:
            if page.book_id != self.book.id or page.chapter_id not in chapters or page.id not in chapters[page.chapter_id].page_ids:
                raise ValueError("BOOK_PAGE_SCOPE_CONFLICT")
            for block in page.blocks:
                if not block.id or block.id in block_ids:
                    raise ValueError("BOOK_DUPLICATE_ID")
                block_ids.add(block.id)
            if page.status == PageStatus.READY and (not page.blocks or any(b.status != BlockStatus.READY for b in page.blocks)):
                raise ValueError("BOOK_PAGE_STATE_CONFLICT")
        if self.book.status == BookStatus.READY and (not self.pages or any(p.status != PageStatus.READY for p in self.pages)):
            raise ValueError("BOOK_PAGE_STATE_CONFLICT")
        active: set[str] = set()
        for key, operation in self.operations.items():
            if key != operation.operation_id or operation.page_id not in pages:
                raise ValueError("BOOK_OPERATION_SCOPE_CONFLICT")
            if not pages[operation.page_id].block_by_id(operation.block_id):
                raise ValueError("BOOK_OPERATION_SCOPE_CONFLICT")
            if operation.state in {"in_flight", "unknown"}:
                if operation.block_id in active:
                    raise ValueError("BOOK_OPERATION_ACTIVE")
                active.add(operation.block_id)
                if pages[operation.page_id].block_by_id(operation.block_id).status != BlockStatus.GENERATING:
                    raise ValueError("BOOK_OPERATION_STATE_CONFLICT")
        return self


class BookWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter_id: str
    page_id: str
    block_id: str


class BeginBlockResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot: BookCompileSnapshot
    dispatch: bool


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def outline_digest(snapshot: BookCompileSnapshot) -> str:
    """Frozen plan/source-input identity; generated payload and status excluded."""
    pages = {p.id: p for p in snapshot.pages}
    ordered = [pages[pid] for c in snapshot.spine.chapters for pid in c.page_ids]
    return _hash({
        "book_id": snapshot.book.id, "title": snapshot.book.title,
        "depth": snapshot.book.depth.value, "inputs": snapshot.inputs.model_dump(mode="json"),
        "spine": snapshot.spine.model_dump(mode="json", exclude={"updated_at"}),
        "pages": [{"id": p.id, "book_id": p.book_id, "chapter_id": p.chapter_id,
                   "title": p.title, "order": p.order, "content_type": p.content_type.value,
                   "learning_objectives": p.learning_objectives,
                   "blocks": [{"id": b.id, "type": b.type.value, "title": b.title,
                               "params": b.params} for b in p.blocks]} for p in ordered],
    })


def _copy(snapshot: BookCompileSnapshot, *, confirmed=True) -> BookCompileSnapshot:
    result = BookCompileSnapshot.model_validate(snapshot.model_dump(mode="json"))
    if confirmed and (result.confirmation_count != 1 or not result.confirmed_outline_digest):
        raise ValueError("BOOK_OUTLINE_CONFIRMATION_REQUIRED")
    if result.confirmed_outline_digest and outline_digest(result) != result.confirmed_outline_digest:
        raise ValueError("BOOK_OUTLINE_VERSION_CONFLICT")
    return result


def confirm_outline(snapshot: BookCompileSnapshot, expected_digest: str) -> BookCompileSnapshot:
    result = _copy(snapshot, confirmed=False)
    if outline_digest(result) != expected_digest:
        raise ValueError("BOOK_OUTLINE_VERSION_CONFLICT")
    if not result.pages or any(not p.blocks for p in result.pages):
        raise ValueError("BOOK_PLAN_EMPTY")
    if result.confirmation_count:
        return result
    result.confirmation_count = 1
    result.confirmed_outline_digest = expected_digest
    result.book.status = BookStatus.SPINE_READY
    return result


def _page(snapshot, page_id):
    for page in snapshot.pages:
        if page.id == page_id:
            return page
    raise ValueError("BOOK_PAGE_UNKNOWN")


def _ensure_reconciled(snapshot):
    if any(op.state in {"in_flight", "unknown"} for op in snapshot.operations.values()) or any(
        b.status == BlockStatus.GENERATING for p in snapshot.pages for b in p.blocks
    ):
        raise ValueError("BOOK_RESULT_UNKNOWN")


def resume_book(snapshot: BookCompileSnapshot) -> BookCompileSnapshot:
    result = _copy(snapshot)
    _ensure_reconciled(result)
    if any(b.status == BlockStatus.ERROR for p in result.pages for b in p.blocks):
        raise ValueError("BOOK_BLOCK_ERROR_REQUIRES_DECISION")
    result.book.status = BookStatus.READY if all(p.status == PageStatus.READY for p in result.pages) else BookStatus.COMPILING
    return result


def next_block(snapshot: BookCompileSnapshot) -> BookWorkItem | None:
    result = _copy(snapshot)
    _ensure_reconciled(result)
    if result.book.status == BookStatus.PAUSED:
        raise ValueError("BOOK_EXPLICIT_RESUME_REQUIRED")
    for chapter in result.spine.chapters:
        for page_id in chapter.page_ids:
            for block in _page(result, page_id).blocks:
                if block.status == BlockStatus.PENDING:
                    return BookWorkItem(chapter_id=chapter.id, page_id=page_id, block_id=block.id)
                if block.status == BlockStatus.ERROR:
                    raise ValueError("BOOK_BLOCK_ERROR_REQUIRES_DECISION")
                if block.status != BlockStatus.READY:
                    raise ValueError("BOOK_BLOCK_STATE_UNSUPPORTED")
    return None


def begin_block(snapshot: BookCompileSnapshot, item: BookWorkItem, operation_id: str) -> BeginBlockResult:
    result = _copy(snapshot)
    previous = result.operations.get(operation_id)
    if previous:
        if (previous.page_id, previous.block_id) != (item.page_id, item.block_id):
            raise ValueError("BOOK_OPERATION_ID_CONFLICT")
        return BeginBlockResult(snapshot=result, dispatch=False)
    expected = next_block(result)
    if expected != item:
        raise ValueError("BOOK_WORK_ITEM_CONFLICT")
    page = _page(result, item.page_id)
    page.block_by_id(item.block_id).status = BlockStatus.GENERATING
    page.status = PageStatus.GENERATING
    result.book.status = BookStatus.COMPILING
    result.operations[operation_id] = BlockOperation(operation_id=operation_id, page_id=item.page_id, block_id=item.block_id)
    return BeginBlockResult(snapshot=result, dispatch=True)


def make_block_context(snapshot: BookCompileSnapshot, item: BookWorkItem) -> BlockContext:
    """Isolated context: original generators mutate their Block in place.

    No implicit KB/source lookup; the caller supplies its already authorized
    generation implementation. It must commit the returned receipt separately.
    """
    result = _copy(snapshot)
    page = _page(result, item.page_id)
    block = page.block_by_id(item.block_id)
    chapter = result.spine.chapter_by_id(item.chapter_id)
    if (chapter is None or page.chapter_id != item.chapter_id or block is None
            or block.status != BlockStatus.GENERATING):
        raise ValueError("BOOK_WORK_ITEM_CONFLICT")
    return BlockContext(book_id=result.book.id, chapter=chapter, page=page, block=block,
                        language=result.book.language, rag_enabled=False, knowledge_bases=[])


def complete_block(snapshot: BookCompileSnapshot, operation_id: str, generated: Block) -> BookCompileSnapshot:
    """Also accepts a reconciled receipt for an UNKNOWN operation. Never calls a model."""
    result = _copy(snapshot)
    operation = result.operations.get(operation_id)
    if operation is None:
        raise ValueError("BOOK_OPERATION_UNKNOWN")
    output_digest = _hash(generated.model_dump(mode="json"))
    if operation.state == "committed":
        if operation.output_digest != output_digest:
            raise ValueError("BOOK_OPERATION_RESULT_CONFLICT")
        return result
    if operation.state not in {"in_flight", "unknown"}:
        raise ValueError("BOOK_OPERATION_NOT_ACTIVE")
    page = _page(result, operation.page_id)
    block = page.block_by_id(operation.block_id)
    if generated.id != block.id or generated.type != block.type or generated.params != block.params or generated.title != block.title:
        raise ValueError("BOOK_BLOCK_PLAN_CONFLICT")
    if generated.status not in {BlockStatus.READY, BlockStatus.ERROR}:
        raise ValueError("BOOK_BLOCK_NOT_TERMINAL")
    page.blocks[page.blocks.index(block)] = generated.model_copy(deep=True)
    operation.state = "committed"
    operation.output_digest = output_digest
    if all(b.status in {BlockStatus.READY, BlockStatus.ERROR} for b in page.blocks):
        finalize_terminal_page(page)
    if generated.status == BlockStatus.ERROR:
        result.book.status = BookStatus.PAUSED
    elif all(p.status == PageStatus.READY for p in result.pages):
        result.book.status = BookStatus.READY
    return result


def interrupt_block(snapshot: BookCompileSnapshot, operation_id: str) -> BookCompileSnapshot:
    result = _copy(snapshot)
    operation = result.operations.get(operation_id)
    if operation is None or operation.state not in {"in_flight", "unknown"}:
        raise ValueError("BOOK_OPERATION_NOT_ACTIVE")
    operation.state = "unknown"
    result.book.status = BookStatus.PAUSED
    return result


def reconcile_not_started(snapshot: BookCompileSnapshot, operation_id: str, reconciliation_ref: str) -> BookCompileSnapshot:
    """Application must already possess evidence that the attempt never started."""
    result = _copy(snapshot)
    operation = result.operations.get(operation_id)
    if not reconciliation_ref or operation is None or operation.state != "unknown":
        raise ValueError("BOOK_RECONCILIATION_REQUIRED")
    operation.state = "not_started"
    operation.reconciliation_ref = reconciliation_ref
    _page(result, operation.page_id).block_by_id(operation.block_id).status = BlockStatus.PENDING
    return result


def readable_chapters(snapshot: BookCompileSnapshot) -> list[str]:
    result = _copy(snapshot)
    return [c.id for c in result.spine.chapters if c.page_ids and all(_page(result, pid).status == PageStatus.READY for pid in c.page_ids)]


def readable_pages(snapshot: BookCompileSnapshot) -> list[Page]:
    result = _copy(snapshot)
    return [p for p in result.pages if p.status == PageStatus.READY]
