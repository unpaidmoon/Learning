"""Versioned PDF page reads, exact selection validation and reader state fences."""

from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from sqlalchemy import insert, select, update

from learning_app.config import PROJECT_ROOT
from learning_app.errors import AppError
from learning_app.processes import OwnedProcessGroup
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.storage.repository import _hash

from . import schema as s
from .common import now
from .focus import PreparationFocus
from .models import (
    DocumentPagePart,
    DocumentSelectionRef,
    OcrPageIndex,
    OcrPageLayer,
    OcrPageRevision,
    OcrTextItem,
    PageRegion,
    PdfDocumentInfo,
    PdfGlyph,
    PdfPageContent,
    PdfSelectionInput,
    ReadingOpenInput,
    ReadingPosition,
    ReadingState,
    ReadingStateUpdate,
)
from .pdf_worker import PARSER_VERSION
from .processing_models import ReadingFocusInput
from .sources import SourceStore

_worker_gate = threading.Lock()


def _compact(text: str) -> str:
    # Only line-layout whitespace is ignored for validation. The snapshot keeps
    # exactly the selected text; formulas, punctuation and Unicode are untouched.
    return "".join(char for char in text if not char.isspace())


def _same_layout_text(original: str, selected: str) -> bool:
    def boundaries(text):
        letters, horizontal, any_space = [], set(), set()
        for char in text.strip():
            if char.isspace():
                any_space.add(len(letters))
                if char not in "\r\n":
                    horizontal.add(len(letters))
            else:
                letters.append(char)
        return "".join(letters), horizontal, any_space

    actual, required, allowed = boundaries(original)
    supplied, _horizontal, supplied_spaces = boundaries(selected)
    # DOM ranges may omit a layout-only linebreak. They cannot insert a space
    # inside an original word/formula or erase an original word separator.
    return actual == supplied and supplied_spaces <= allowed and required <= supplied_spaces


class DocumentReader:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.sources = SourceStore(repository)

    def _pdf_source(self, course_id: str, asset_id: str, version: str) -> dict:
        source = self.sources._source(course_id, asset_id, version)
        if source["path"].suffix.lower() != ".pdf":
            raise AppError("SOURCE_KIND_DENIED", "该入口只允许课程PDF", 403)
        return source

    @staticmethod
    def _run(
        source: dict,
        page_index: int | None,
        *,
        render_to: Path | None = None,
        cancelled: threading.Event | None = None,
        object_regions: bool = False,
        region_parser_version: str | None = None,
    ) -> dict:
        group = OwnedProcessGroup("reading-pdf")
        with _worker_gate:
            try:
                process = group.spawn(
                    [sys.executable, "-m", "learning_app.workbench.pdf_worker"], cwd=PROJECT_ROOT
                )
                request = {
                    "path": str(source["path"]),
                    "digest": source["content_hash"],
                    "page_index": page_index,
                }
                if render_to is not None:
                    request["render_to"] = str(render_to)
                if object_regions:
                    request["object_regions"] = True
                    if region_parser_version is not None:
                        request["region_parser_version"] = region_parser_version
                try:
                    payload = json.dumps(request)
                    deadline = time.monotonic() + 60
                    while True:
                        if cancelled is not None and cancelled.is_set():
                            raise AppError("PREPARATION_INTERRUPTED", "当前本地处理已让位或暂停")
                        try:
                            stdout, _stderr = process.communicate(payload, timeout=0.1)
                            break
                        except subprocess.TimeoutExpired:
                            payload = None
                            if time.monotonic() >= deadline:
                                raise
                except subprocess.TimeoutExpired as exc:
                    raise AppError("PDF_PROCESSING_TIMEOUT", "页面处理超时，可主动重试") from exc
                try:
                    value = json.loads(stdout)
                except (ValueError, TypeError) as exc:
                    raise AppError("PDF_PROCESSING_FAILED", "页面处理未完成") from exc
                if process.returncode or not value.get("ok"):
                    code = value.get("code", "PDF_PROCESSING_FAILED")
                    allowed = {
                        "SOURCE_VERSION_UNAVAILABLE",
                        "SOURCE_PAGE_RANGE_INVALID",
                        "PDF_UNREADABLE",
                        "PDF_PAGE_COUNT_INVALID",
                        "PDF_PAGE_SIZE_INVALID",
                        "PDF_PAGE_TEXT_LIMIT",
                        "PDF_RENDER_LIMIT",
                        "PDF_RENDER_PATH_INVALID",
                        "PDF_REGION_OBJECT_LIMIT",
                    }
                    raise AppError(
                        code if code in allowed else "PDF_PROCESSING_FAILED",
                        "PDF页面暂不可读取；原件与已有快照仍保留",
                        422,
                    )
                return value["result"]
            finally:
                group.close()

    def info(self, course_id: str, asset_id: str, version: str) -> PdfDocumentInfo:
        source = self._pdf_source(course_id, asset_id, version)
        return PdfDocumentInfo(
            asset_id=asset_id,
            source_version=version,
            source_digest=source["content_hash"],
            **self._run(source, None),
        )

    def page(
        self,
        course_id: str,
        asset_id: str,
        version: str,
        index: int,
        *,
        cancelled: threading.Event | None = None,
    ) -> PdfPageContent:
        source = self._pdf_source(course_id, asset_id, version)
        condition = (
            s.document_pages.c.course_id == course_id,
            s.document_pages.c.asset_id == asset_id,
            s.document_pages.c.source_version == version,
            s.document_pages.c.page_index == index,
            s.document_pages.c.parser_version == PARSER_VERSION,
        )
        with self.repository.engine.connect() as conn:
            prior = conn.execute(select(s.document_pages).where(*condition)).mappings().first()
        if prior:
            if _hash(prior["payload"]) != prior["payload_hash"]:
                raise AppError("PDF_PAGE_CACHE_CORRUPT", "已保存的文字层校验失败，未作为引用")
            return self._with_ocr(course_id, PdfPageContent.model_validate(prior["payload"]))
        result = PdfPageContent(
            asset_id=asset_id,
            source_version=version,
            source_digest=source["content_hash"],
            **self._run(source, index, cancelled=cancelled),
        )
        payload = result.model_dump(mode="json")
        with self.repository._lock, self.repository.engine.begin() as conn:
            if (
                conn.execute(select(s.document_pages.c.payload_hash).where(*condition)).first()
                is None
            ):
                conn.execute(
                    insert(s.document_pages).values(
                        course_id=course_id,
                        asset_id=asset_id,
                        source_version=version,
                        page_index=index,
                        parser_version=PARSER_VERSION,
                        payload=payload,
                        payload_hash=_hash(payload),
                        created_at=now(),
                    )
                )
        return self._with_ocr(course_id, result)

    def ocr_index(
        self, course_id: str, asset_id: str, version: str, after_page: int = -1
    ) -> OcrPageIndex:
        self._pdf_source(course_id, asset_id, version)
        # Page pagination is restarted after each sweep, so late, out-of-order
        # current-page results cannot be lost behind a timestamp cursor.
        with self.repository.engine.connect() as conn:
            indexes = (
                conn.execute(
                    select(s.document_ocr.c.page_index)
                    .where(
                        s.document_ocr.c.course_id == course_id,
                        s.document_ocr.c.asset_id == asset_id,
                        s.document_ocr.c.source_version == version,
                        s.document_ocr.c.page_index > after_page,
                    )
                    .distinct()
                    .order_by(s.document_ocr.c.page_index)
                    .limit(257)
                )
                .scalars()
                .all()
            )
            pages = []
            for index in indexes[:256]:
                revision = conn.execute(
                    select(s.document_ocr.c.payload_hash)
                    .where(
                        s.document_ocr.c.course_id == course_id,
                        s.document_ocr.c.asset_id == asset_id,
                        s.document_ocr.c.source_version == version,
                        s.document_ocr.c.page_index == index,
                    )
                    .order_by(
                        s.document_ocr.c.created_at.desc(),
                        s.document_ocr.c.parameters_version.desc(),
                    )
                    .limit(1)
                ).scalar_one()
                pages.append(OcrPageRevision(page_index=index, revision=revision))
            return OcrPageIndex(pages=pages, next_page=indexes[255] if len(indexes) > 256 else None)

    def ocr_layer(self, course_id: str, asset_id: str, version: str, index: int) -> OcrPageLayer:
        source = self._pdf_source(course_id, asset_id, version)
        with self.repository.engine.connect() as conn:
            row = (
                conn.execute(
                    select(s.document_ocr)
                    .where(
                        s.document_ocr.c.course_id == course_id,
                        s.document_ocr.c.asset_id == asset_id,
                        s.document_ocr.c.source_version == version,
                        s.document_ocr.c.page_index == index,
                    )
                    .order_by(
                        s.document_ocr.c.created_at.desc(),
                        s.document_ocr.c.parameters_version.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AppError("OCR_PAGE_NOT_READY", "该页文字层尚未就绪", 404)
        payload = row["payload"]
        if (
            _hash(payload) != row["payload_hash"]
            or payload.get("source_digest") != source["content_hash"]
        ):
            raise AppError("OCR_RESULT_INTEGRITY", "OCR来源或已保存结果校验失败")
        raw, rendered = payload["raw"], payload["render"]
        if raw["image_sha256"] != rendered["image_sha256"]:
            raise AppError("OCR_RESULT_INTEGRITY", "OCR与原页位图不对应")
        if "image_to_pdf" not in rendered:
            # The v1 producer recorded an unrotated crop and exact pixel size.
            # Derive only from that persisted transform, never the current UI.
            if rendered["bitmap_rotation"] != 0:
                raise AppError("OCR_TRANSFORM_UNSUPPORTED", "旧识别缺少可恢复的页面变换")
            left, bottom, right, top = rendered["view_box"]
            matrix = [
                (right - left) / rendered["width"],
                0,
                0,
                -(top - bottom) / rendered["height"],
                left,
                top,
            ]
        else:
            matrix = rendered["image_to_pdf"]
        symbols = [
            OcrTextItem(text=symbol["text"], bbox=symbol["bbox"])
            for word in raw["words"]
            for symbol in word["symbols"]
            if symbol["text"].strip()
            and symbol["bbox"]["x1"] > symbol["bbox"]["x0"]
            and symbol["bbox"]["y1"] > symbol["bbox"]["y0"]
        ]
        return OcrPageLayer(
            asset_id=asset_id,
            source_version=version,
            source_digest=source["content_hash"],
            page_number=index + 1,
            revision=row["payload_hash"],
            text=raw["text"],
            words=symbols,
            bitmap_width=raw["width"],
            bitmap_height=raw["height"],
            image_to_pdf=matrix,
        )

    def _with_ocr(self, course_id: str, page: PdfPageContent) -> PdfPageContent:
        if page.text.strip():
            return page
        with self.repository.engine.connect() as conn:
            row = (
                conn.execute(
                    select(s.document_ocr)
                    .where(
                        s.document_ocr.c.course_id == course_id,
                        s.document_ocr.c.asset_id == page.asset_id,
                        s.document_ocr.c.source_version == page.source_version,
                        s.document_ocr.c.page_index == page.page_index,
                    )
                    .order_by(
                        s.document_ocr.c.created_at.desc(),
                        s.document_ocr.c.parameters_version.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .first()
            )
        if row is None:
            return page
        value = row["payload"]
        if _hash(value) != row["payload_hash"] or value.get("source_digest") != page.source_digest:
            raise AppError("OCR_RESULT_INTEGRITY", "OCR来源或已保存的结果校验失败")
        raw, rendered = value["raw"], value["render"]
        if raw["image_sha256"] != rendered["image_sha256"] or (raw["width"], raw["height"]) != (
            rendered["width"],
            rendered["height"],
        ):
            raise AppError("OCR_RESULT_INTEGRITY", "OCR与原页位图不对应")
        text, mappings, cursor = raw["text"], {}, 0
        for word in raw["words"]:
            start = text.find(word["text"], cursor)
            if start < 0:
                raise AppError(
                    "OCR_TEXT_MAPPING_UNAVAILABLE", "OCR文字与位置无法精确对应，原结果仍保留"
                )
            offset = start
            for symbol in word["symbols"]:
                local = text.find(symbol["text"], offset, start + len(word["text"]))
                if local < 0:
                    raise AppError(
                        "OCR_TEXT_MAPPING_UNAVAILABLE", "OCR字符位置不完整，原结果仍保留"
                    )
                box = symbol["bbox"]
                region = None
                if (
                    0 <= box["x0"] < box["x1"] <= raw["width"]
                    and 0 <= box["y0"] < box["y1"] <= raw["height"]
                ):
                    region = PageRegion(
                        x0=box["x0"] / raw["width"],
                        y0=box["y0"] / raw["height"],
                        x1=box["x1"] / raw["width"],
                        y1=box["y1"] / raw["height"],
                    )
                mappings[local] = PdfGlyph(
                    start=local, end=local + len(symbol["text"]), text=symbol["text"], region=region
                )
                offset = local + len(symbol["text"])
            cursor = start + len(word["text"])
        glyphs, index = [], 0
        while index < len(text):
            glyph = mappings.get(index) or PdfGlyph(start=index, end=index + 1, text=text[index])
            glyphs.append(glyph)
            index = glyph.end
        return page.model_copy(
            update={
                "text": text,
                "glyphs": glyphs,
                "text_status": "ocr",
                "parser_version": "ocr:" + row["payload_hash"][:32],
            }
        )

    def render(
        self,
        course_id: str,
        asset_id: str,
        version: str,
        index: int,
        directory: Path,
        *,
        cancelled: threading.Event | None = None,
    ) -> tuple[bytes, dict]:
        source = self._pdf_source(course_id, asset_id, version)
        owned = directory.resolve()
        if not owned.is_relative_to(self.repository.staging.resolve()) or not owned.is_dir():
            raise AppError("PDF_RENDER_PATH_INVALID", "页面输出不在本实例临时目录内")
        target = owned / "page.png"
        result = self._run(source, index, render_to=target, cancelled=cancelled)
        image = target.read_bytes()
        if (
            len(image) > 20 * 1024 * 1024
            or hashlib.sha256(image).hexdigest() != result["image_sha256"]
        ):
            raise AppError("PDF_RENDER_INTEGRITY", "页面位图完整性不符")
        return image, result

    def capture(self, course_id: str, value: PdfSelectionInput):
        if not _same_layout_text("\n".join(page.text for page in value.pages), value.text):
            raise AppError("PDF_SELECTION_MISMATCH", "跨页文字与实际选区不符", 422)
        parts, digest = [], None
        for selected in value.pages:
            page = self.page(course_id, value.asset_id, value.source_version, selected.page_index)
            digest = page.source_digest
            hits = []
            for glyph in page.glyphs:
                box = glyph.region
                if box is None:
                    continue
                x, y = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
                if any(r.x0 <= x <= r.x1 and r.y0 <= y <= r.y1 for r in selected.regions):
                    hits.append(glyph)
            if not hits or _compact("".join(g.text for g in hits)) != _compact(selected.text):
                raise AppError(
                    "PDF_SELECTION_MISMATCH", "文字与原页选区无法精确对应，请调整选区", 422
                )
            start, end = hits[0].start, hits[-1].end
            if not _same_layout_text(page.text[start:end], selected.text):
                raise AppError(
                    "PDF_SELECTION_DISCONTIGUOUS", "文字选区不是连续原文，请分别选择", 422
                )
            # Keep the user's exact boxes and codepoint range for a reliable jump.
            parts.append(
                DocumentPagePart(
                    page_index=selected.page_index,
                    page_label=page.page_label,
                    start=start,
                    end=end,
                    regions=selected.regions,
                    parser_version=page.parser_version,
                )
            )
        reference = DocumentSelectionRef(
            asset_id=value.asset_id,
            source_version=value.source_version,
            source_digest=digest,
            parts=parts,
        )
        return self.sources._save_verified(course_id, reference, value.text, "original")

    def begin(
        self, course_id: str, asset_id: str, version: str, value: ReadingOpenInput | None = None
    ) -> ReadingState:
        value = value or ReadingOpenInput(
            reader_client_id="local_" + secrets.token_hex(16),
            request_id="open_" + secrets.token_hex(16),
            view_sequence=0,
        )
        condition = (
            s.reading_states.c.course_id == course_id,
            s.reading_states.c.asset_id == asset_id,
            s.reading_states.c.source_version == version,
        )
        with self.repository._lock, self.repository.engine.begin() as conn:
            allowed = conn.execute(
                select(business.source_versions.c.source_version)
                .join(
                    business.course_assets,
                    business.course_assets.c.asset_id == business.source_versions.c.asset_id,
                )
                .where(
                    business.course_assets.c.course_id == course_id,
                    business.source_versions.c.asset_id == asset_id,
                    business.source_versions.c.source_version == version,
                )
            ).first()
            if allowed is None:
                raise AppError("SOURCE_COURSE_DENIED", "来源不属于指定课程与版本", 403)
            prior = conn.execute(select(s.reading_states).where(*condition)).mappings().first()
            fence_condition = (
                s.reading_open_fences.c.course_id == course_id,
                s.reading_open_fences.c.asset_id == asset_id,
                s.reading_open_fences.c.source_version == version,
                s.reading_open_fences.c.reader_client_id == value.reader_client_id,
            )
            fence = (
                conn.execute(select(s.reading_open_fences).where(*fence_condition))
                .mappings()
                .first()
            )
            latest = PreparationFocus.latest(conn, value.reader_client_id)
            if latest is not None and latest > value.view_sequence:
                raise AppError("READING_VIEW_STALE", "更新的资料视图已取得优先级", 409)
            if latest == value.view_sequence and (not fence or fence["view_sequence"] != latest):
                raise AppError("READING_VIEW_STALE", "阅读序号已用于另一资料", 409)
            if fence and fence["view_sequence"] >= value.view_sequence:
                if (
                    fence["view_sequence"] == value.view_sequence
                    and fence["request_id"] == value.request_id
                    and prior
                    and prior["reader_epoch"] == fence["reader_epoch"]
                ):
                    return ReadingState(
                        asset_id=asset_id,
                        source_version=version,
                        **prior["position"],
                        reader_epoch=prior["reader_epoch"],
                        revision=prior["revision"],
                        updated_at=prior["updated_at"],
                    )
                raise AppError("READING_VIEW_STALE", "较新的阅读视图已打开，旧请求未接管位置", 409)
            state = ReadingState(
                asset_id=asset_id,
                source_version=version,
                reader_epoch="reader_" + secrets.token_hex(16),
                revision=prior["revision"] + 1 if prior else 0,
                updated_at=now(),
                **(prior["position"] if prior else ReadingPosition().model_dump()),
            )
            row = dict(
                reader_epoch=state.reader_epoch,
                revision=state.revision,
                position=ReadingPosition.model_validate(
                    state.model_dump(include=set(ReadingPosition.model_fields))
                ).model_dump(),
                updated_at=state.updated_at,
            )
            if prior:
                conn.execute(update(s.reading_states).where(*condition).values(**row))
            else:
                conn.execute(
                    insert(s.reading_states).values(
                        course_id=course_id, asset_id=asset_id, source_version=version, **row
                    )
                )
            fence_values = dict(
                view_sequence=value.view_sequence,
                request_id=value.request_id,
                reader_epoch=state.reader_epoch,
            )
            if fence:
                conn.execute(
                    update(s.reading_open_fences).where(*fence_condition).values(**fence_values)
                )
            else:
                conn.execute(
                    insert(s.reading_open_fences).values(
                        course_id=course_id,
                        asset_id=asset_id,
                        source_version=version,
                        reader_client_id=value.reader_client_id,
                        **fence_values,
                    )
                )
            PreparationFocus.claim(
                conn,
                course_id,
                asset_id,
                version,
                value.reader_client_id,
                state.reader_epoch,
                value.view_sequence,
                value.request_id,
            )
        self._pdf_source(course_id, asset_id, version)
        return state

    @staticmethod
    def _clear_priority(conn):
        conn.execute(
            update(s.preparation_units)
            .where(
                s.preparation_units.c.job_id.in_(
                    select(s.preparations.c.job_id).where(s.preparations.c.kind == "pdf_ocr")
                ),
                s.preparation_units.c.priority == 0,
            )
            .values(priority=10)
        )

    @staticmethod
    def _focus_condition(course_id, asset_id, version, value):
        return (
            s.reading_open_fences.c.course_id == course_id,
            s.reading_open_fences.c.asset_id == asset_id,
            s.reading_open_fences.c.source_version == version,
            s.reading_open_fences.c.reader_client_id == value.reader_client_id,
            s.reading_open_fences.c.reader_epoch == value.reader_epoch,
        )

    def focus(self, course_id: str, asset_id: str, version: str, value: ReadingFocusInput):
        with self.repository._lock, self.repository.engine.begin() as conn:
            PreparationFocus.require_owner(
                conn, course_id, asset_id, version, value.reader_client_id, value.reader_epoch
            )
            condition = self._focus_condition(course_id, asset_id, version, value)
            fence = conn.execute(select(s.reading_open_fences).where(*condition)).mappings().first()
            current_epoch = conn.execute(
                select(s.reading_states.c.reader_epoch).where(
                    s.reading_states.c.course_id == course_id,
                    s.reading_states.c.asset_id == asset_id,
                    s.reading_states.c.source_version == version,
                )
            ).scalar_one_or_none()
            latest = PreparationFocus.latest(conn, value.reader_client_id)
            if (
                not fence
                or current_epoch != value.reader_epoch
                or (latest is not None and latest > value.focus_sequence)
                or (
                    latest == value.focus_sequence
                    and (
                        fence["view_sequence"] != latest or fence["request_id"] != value.request_id
                    )
                )
            ):
                raise AppError("READING_VIEW_STALE", "更新的资料或页面已取得优先级", 409)
            conn.execute(
                update(s.reading_open_fences)
                .where(*condition)
                .values(view_sequence=value.focus_sequence, request_id=value.request_id)
            )
            PreparationFocus.claim(
                conn,
                course_id,
                asset_id,
                version,
                value.reader_client_id,
                value.reader_epoch,
                value.focus_sequence,
                value.request_id,
            )

    def assert_focus(self, course_id: str, asset_id: str, version: str, value: ReadingFocusInput):
        with self.repository.engine.connect() as conn:
            epoch = conn.execute(
                select(s.reading_states.c.reader_epoch).where(
                    s.reading_states.c.course_id == course_id,
                    s.reading_states.c.asset_id == asset_id,
                    s.reading_states.c.source_version == version,
                )
            ).scalar_one_or_none()
            latest = PreparationFocus.latest(conn, value.reader_client_id)
            row = conn.execute(
                select(s.reading_open_fences).where(
                    *self._focus_condition(course_id, asset_id, version, value),
                    s.reading_open_fences.c.view_sequence == value.focus_sequence,
                    s.reading_open_fences.c.request_id == value.request_id,
                )
            ).first()
            if row is None or latest != value.focus_sequence or epoch != value.reader_epoch:
                raise AppError("READING_VIEW_STALE", "迟到的页面准备结果未接管当前优先级", 409)
            PreparationFocus.assert_current(conn, course_id, asset_id, version, value)

    def release_focus(self, course_id: str, asset_id: str, version: str, value: ReadingFocusInput):
        # Release the activation's existing sequence. Minting a new one from a
        # hidden tab's effect could incorrectly outrank its newly active sibling.
        with self.repository._lock, self.repository.engine.begin() as conn:
            condition = self._focus_condition(course_id, asset_id, version, value)
            fence = conn.execute(select(s.reading_open_fences).where(*condition)).mappings().first()
            latest = PreparationFocus.latest(conn, value.reader_client_id)
            current_epoch = conn.execute(
                select(s.reading_states.c.reader_epoch).where(
                    s.reading_states.c.course_id == course_id,
                    s.reading_states.c.asset_id == asset_id,
                    s.reading_states.c.source_version == version,
                )
            ).scalar_one_or_none()
            if (
                fence is None
                or current_epoch != value.reader_epoch
                or latest is None
                or latest > value.focus_sequence
                or (
                    fence["view_sequence"] == value.focus_sequence
                    and fence["request_id"] != value.request_id
                )
            ):
                return False
            if not PreparationFocus.release(conn, course_id, asset_id, version, value):
                return False
            conn.execute(
                update(s.reading_open_fences)
                .where(*condition)
                .values(
                    view_sequence=value.focus_sequence,
                    request_id="released_" + _hash(value.model_dump()),
                )
            )
            conn.execute(
                update(s.preparation_units)
                .where(
                    s.preparation_units.c.job_id.in_(
                        select(s.preparations.c.job_id).where(
                            s.preparations.c.course_id == course_id,
                            s.preparations.c.asset_id == asset_id,
                            s.preparations.c.source_version == version,
                        )
                    ),
                    s.preparation_units.c.priority == 0,
                )
                .values(priority=10)
            )
            return True

    def save(
        self, course_id: str, asset_id: str, version: str, value: ReadingStateUpdate
    ) -> ReadingState:
        self._pdf_source(course_id, asset_id, version)
        # Checking the target page also prevents a fabricated out-of-range resume.
        page = self.page(course_id, asset_id, version, value.page_index)
        del page
        position = ReadingPosition.model_validate(
            value.model_dump(include=set(ReadingPosition.model_fields))
        )
        with self.repository._lock, self.repository.engine.begin() as conn:
            changed = conn.execute(
                update(s.reading_states)
                .where(
                    s.reading_states.c.course_id == course_id,
                    s.reading_states.c.asset_id == asset_id,
                    s.reading_states.c.source_version == version,
                    s.reading_states.c.reader_epoch == value.reader_epoch,
                    s.reading_states.c.revision == value.expected_revision,
                )
                .values(
                    position=position.model_dump(),
                    revision=value.expected_revision + 1,
                    updated_at=now(),
                )
            )
            if changed.rowcount != 1:
                raise AppError(
                    "READING_STATE_CONFLICT", "另一个阅读视图已接管位置，未覆盖新进度", 409
                )
            row = (
                conn.execute(
                    select(s.reading_states).where(
                        s.reading_states.c.course_id == course_id,
                        s.reading_states.c.asset_id == asset_id,
                        s.reading_states.c.source_version == version,
                    )
                )
                .mappings()
                .one()
            )
            return ReadingState(
                asset_id=asset_id,
                source_version=version,
                **row["position"],
                reader_epoch=row["reader_epoch"],
                revision=row["revision"],
                updated_at=row["updated_at"],
            )
