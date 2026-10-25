"""Local PDF image candidates anchored in an exact document text snapshot.

These helpers do not grant model access. NativeBusiness must first prove the
same run actually read the parent text, and later commit an encoding receipt.
"""

import hashlib
import io
import math
import tempfile
from dataclasses import dataclass

from pydantic import Field

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from .models import Contract, Digest, DocumentRef, Identity, PageRegion
from .pdf_worker import LEGACY_REGION_VERSION, REGION_VERSION
from .reading import DocumentReader
from .sources import SourceStore


class DocumentRegionCandidate(Contract):
    region_id: Identity
    parent_snapshot_id: Identity
    parent_hash: Digest
    asset_id: Identity
    source_version: Identity
    source_digest: Digest
    page_index: int = Field(ge=0)
    parser_version: str
    region_parser_version: str
    object_index: int = Field(ge=0)
    object_path: tuple[int, ...] | None = None
    kind: str
    anchor_start: int = Field(ge=0)
    anchor_end: int = Field(gt=0)
    region: PageRegion


@dataclass(frozen=True)
class CroppedDocumentRegion:
    candidate: DocumentRegionCandidate
    image: bytes
    image_sha256: str
    width: int
    height: int
    pixel_rect: tuple[int, int, int, int]
    actual_region: PageRegion
    image_to_pdf: tuple[float, float, float, float, float, float]


class DocumentRegions:
    def __init__(self, repository):
        self.repository = repository
        self.sources = SourceStore(repository)
        self.reader = DocumentReader(repository)

    def _parent(self, course_id, snapshot_id):
        parent = self.sources.snapshot(course_id, snapshot_id)
        self.sources.require_current(parent)
        start, end, source = 0, len(parent.text), parent
        for _ in range(16):
            if source.reference.kind != "snapshot_slice":
                break
            ref = source.reference
            ancestor = self.sources.snapshot(source.course_id, ref.parent_snapshot_id)
            if ancestor.content_hash != ref.parent_hash:
                raise AppError("DOCUMENT_REGION_PARENT_CONFLICT", "原文字片段的来源链不完整")
            start, end = start + ref.start, end + ref.start
            source = ancestor
        if (
            not isinstance(source.reference, DocumentRef)
            or not parent.text.strip()
            or parent.content_role != "original"
        ):
            raise AppError(
                "DOCUMENT_REGION_TEXT_REQUIRED",
                "局部图须定位到原文档的实际文字，不沿AI成果读取祖先",
                403,
            )
        ref = source.reference
        page = self.reader.page(source.course_id, ref.asset_id, ref.source_version, ref.page_index)
        start, end = ref.start + start, ref.start + end
        if (
            ref.source_digest != page.source_digest
            or ref.parser_version != page.parser_version
            or not 0 <= start < end <= len(page.text)
            or parent.text != page.text[start:end]
        ):
            raise AppError(
                "DOCUMENT_REGION_ANCHOR_UNAVAILABLE", "原文字版本或定位无法精确核对，未扩大到整页"
            )
        return parent, ref, page, start, end

    def candidates(self, course_id, snapshot_id):
        return self._candidates(course_id, snapshot_id, REGION_VERSION)

    def _candidates(self, course_id, snapshot_id, version):
        if version not in {REGION_VERSION, LEGACY_REGION_VERSION}:
            raise AppError("DOCUMENT_REGION_VERSION_UNAVAILABLE", "局部图定位版本不可用")
        parent, ref, page, start, end = self._parent(course_id, snapshot_id)
        boxes = [
            glyph.region
            for glyph in page.glyphs
            if glyph.region and glyph.end > start and glyph.start < end
        ]
        if not boxes:
            raise AppError(
                "DOCUMENT_REGION_ANCHOR_UNAVAILABLE", "实际文字没有可核对区域，未发送整页"
            )
        if len(boxes) > 20_000:
            raise AppError("DOCUMENT_REGION_ANCHOR_LIMIT", "文字范围过大，请先定位更具体的文档片段")
        source = self.reader._pdf_source(parent.course_id, ref.asset_id, ref.source_version)
        raw = self.reader._run(
            source, ref.page_index, object_regions=True, region_parser_version=version
        )
        if raw["region_parser_version"] != version:
            raise AppError("DOCUMENT_REGION_VERSION_UNAVAILABLE", "局部图定位版本不符")
        nearby = []
        for item in raw["regions"]:
            region = PageRegion.model_validate(item["region"])
            # An enclosing box can bridge whole unread gaps between lines or
            # columns. Distance is to actual glyph boxes, never that envelope.
            distance = min(
                math.hypot(
                    max(box.x0 - region.x1, region.x0 - box.x1, 0),
                    max(box.y0 - region.y1, region.y0 - box.y1, 0),
                )
                for box in boxes
            )
            if distance > 0.18:
                continue
            fields = dict(
                parent_snapshot_id=parent.snapshot_id,
                parent_hash=parent.content_hash,
                asset_id=ref.asset_id,
                source_version=ref.source_version,
                source_digest=ref.source_digest,
                page_index=ref.page_index,
                parser_version=ref.parser_version,
                region_parser_version=version,
                object_index=item["object_index"],
                kind=item["kind"],
                anchor_start=start,
                anchor_end=end,
                region=region.model_dump(mode="json"),
            )
            if version == REGION_VERSION:
                fields["object_path"] = item["object_path"]
            nearby.append(
                (
                    distance,
                    DocumentRegionCandidate(
                        region_id="docregion_" + _hash(fields),
                        **fields,
                    ),
                )
            )
        nearby.sort(key=lambda pair: (pair[0], pair[1].object_path or (pair[1].object_index,)))
        return [candidate for _, candidate in nearby[:4]]

    def render(self, course_id, candidate: DocumentRegionCandidate):
        from PIL import Image

        allowed = self._candidates(
            course_id, candidate.parent_snapshot_id, candidate.region_parser_version
        )
        if candidate not in allowed:
            raise AppError(
                "DOCUMENT_REGION_SCOPE_DENIED",
                "区域不是原文字片段对应的固定候选，未读取其他位置",
                403,
            )
        with tempfile.TemporaryDirectory(
            prefix="document-region-", dir=self.repository.staging
        ) as temporary:
            from pathlib import Path

            parent = self.sources.snapshot(course_id, candidate.parent_snapshot_id)
            full, geometry = self.reader.render(
                parent.course_id,
                candidate.asset_id,
                candidate.source_version,
                candidate.page_index,
                Path(temporary),
            )
        with Image.open(io.BytesIO(full)) as page:
            width, height = page.size
            region = candidate.region
            # New clip-aware candidates include only whole pixels inside the
            # verified visible rectangle. Keep v2 rounding for frozen IDs.
            low = (
                math.floor
                if candidate.region_parser_version == LEGACY_REGION_VERSION
                else math.ceil
            )
            high = (
                math.ceil
                if candidate.region_parser_version == LEGACY_REGION_VERSION
                else math.floor
            )
            rect = (
                max(0, low(region.x0 * width)),
                max(0, low(region.y0 * height)),
                min(width, high(region.x1 * width)),
                min(height, high(region.y1 * height)),
            )
            if rect[0] >= rect[2] or rect[1] >= rect[3]:
                raise AppError("DOCUMENT_REGION_EMPTY", "图形区域没有实际像素，未扩大读取")
            with page.crop(rect) as crop:
                output = io.BytesIO()
                crop.save(output, "PNG")
                data = output.getvalue()
                crop_width, crop_height = crop.size
        a, b, c, d, e, f = geometry["image_to_pdf"]
        return CroppedDocumentRegion(
            candidate=candidate,
            image=data,
            image_sha256=hashlib.sha256(data).hexdigest(),
            width=crop_width,
            height=crop_height,
            pixel_rect=rect,
            actual_region=PageRegion(
                x0=rect[0] / width, y0=rect[1] / height, x1=rect[2] / width, y1=rect[3] / height
            ),
            image_to_pdf=(a, b, c, d, e + a * rect[0] + c * rect[1], f + b * rect[0] + d * rect[1]),
        )
