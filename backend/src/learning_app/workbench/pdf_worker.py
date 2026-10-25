"""Owned local PDFium worker. Never renders or resolves a video/screen source."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from contextlib import closing
from pathlib import Path

from learning_app.errors import AppError
from learning_app.media import _read_locked

PARSER_VERSION = "pdfium-5.14.0-glyphs-coverage-v2"
LEGACY_REGION_VERSION = "pdfium-5.14.0-image-regions-v2"
REGION_VERSION = "pdfium-5.14.0-local-graphics-v3"
MAX_PAGE_CHARACTERS = 500_000


def _region_union_area(rectangles):
    xs = sorted({v for r in rectangles for v in (r["x0"], r["x1"])})
    total = 0.0
    for left, right in zip(xs, xs[1:]):
        spans = sorted((r["y0"], r["y1"]) for r in rectangles if r["x0"] < right and r["x1"] > left)
        end, covered = 0.0, 0.0
        for low, high in spans:
            covered += max(0, high - max(end, low))
            end = max(end, high)
        total += (right - left) * covered
    return total


def _scan_image_indices(images):
    """Reject page backgrounds/tiles while preserving independent overlays."""

    def area(r):
        return (r["x1"] - r["x0"]) * (r["y1"] - r["y0"])

    scan = {i for i, item in enumerate(images) if area(item["region"]) >= 0.65}
    remaining = set(range(len(images))) - scan
    while remaining:
        pending, component = [remaining.pop()], []
        while pending:
            index = pending.pop()
            component.append(index)
            a = images[index]["region"]
            for other in list(remaining):
                b = images[other]["region"]
                overlap = max(0, min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])) * max(
                    0, min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
                )
                dx = max(a["x0"] - b["x1"], b["x0"] - a["x1"], 0)
                dy = max(a["y0"] - b["y1"], b["y0"] - a["y1"], 0)
                if dx <= 0.003 and dy <= 0.003 and overlap <= min(area(a), area(b)) * 0.02:
                    remaining.remove(other)
                    pending.append(other)
        if (
            len(component) > 1
            and _region_union_area([images[i]["region"] for i in component]) >= 0.65
        ):
            scan.update(component)
    if not scan and _region_union_area([item["region"] for item in images]) >= 0.65:
        # Overlapping, ambiguous page covers do not become a whole-page
        # fallback. A genuinely contained overlay remains a local candidate.
        for i, item in enumerate(images):
            a = item["region"]
            if not any(
                i != j
                and area(b["region"]) > area(a)
                and b["region"]["x0"] <= a["x0"]
                and b["region"]["y0"] <= a["y0"]
                and b["region"]["x1"] >= a["x1"]
                and b["region"]["y1"] >= a["y1"]
                for j, b in enumerate(images)
            ):
                scan.add(i)
    return scan


def inspect_pdf(
    path: Path,
    digest: str,
    page_index: int | None,
    render_to: Path | None = None,
    *,
    object_regions: bool = False,
    region_parser_version: str = REGION_VERSION,
) -> dict:
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    if path.suffix.lower() != ".pdf":
        raise AppError("SOURCE_KIND_DENIED", "只允许指定PDF文档", 403)
    with _read_locked(path) as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise AppError("SOURCE_VERSION_UNAVAILABLE", "读取时原版本已改变")
        stream.seek(0)
        try:
            document = pdfium.PdfDocument(stream)
        except pdfium.PdfiumError as exc:
            raise AppError("PDF_UNREADABLE", "PDF无法读取或需要密码", 422) from exc
        with closing(document):
            count = len(document)
            if not 0 < count <= 1_000_000:
                raise AppError("PDF_PAGE_COUNT_INVALID", "PDF页数无效", 422)
            base = {"page_count": count, "parser_version": PARSER_VERSION}
            if page_index is None:
                return base
            if not 0 <= page_index < count:
                raise AppError("SOURCE_PAGE_RANGE_INVALID", "页码不在原文范围内", 422)
            with closing(document[page_index]) as page:
                left, bottom, right, top = page.get_bbox()
                if not all(math.isfinite(v) for v in (left, bottom, right, top)) or not (
                    0 < right - left <= 14400 and 0 < top - bottom <= 14400
                ):
                    raise AppError("PDF_PAGE_SIZE_INVALID", "页面尺寸无效", 422)
                width, height = right - left, top - bottom
                if object_regions:
                    if region_parser_version == REGION_VERSION:
                        from .pdf_graphics import scan_graphics

                        return {
                            **base,
                            "page_index": page_index,
                            **scan_graphics(page, (left, bottom, right, top), _scan_image_indices),
                        }
                    if region_parser_version != LEGACY_REGION_VERSION:
                        raise AppError(
                            "DOCUMENT_REGION_VERSION_UNAVAILABLE", "局部图定位版本不可用"
                        )
                    count = raw.FPDFPage_CountObjects(page)
                    if not 0 <= count <= 4096:
                        raise AppError(
                            "PDF_REGION_OBJECT_LIMIT", "页面对象过多或不可核对，未扩大读取范围"
                        )
                    regions, unsupported = [], 0
                    for index, obj in enumerate(page.get_objects(max_depth=0)):
                        if obj.level != 0:
                            continue
                        if obj.type != raw.FPDF_PAGEOBJ_IMAGE:
                            unsupported += int(obj.type != raw.FPDF_PAGEOBJ_TEXT)
                            continue
                        x0, y0, x1, y1 = obj.get_bounds()
                        if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
                            continue
                        x0, y0, x1, y1 = (
                            max(left, x0),
                            max(bottom, y0),
                            min(right, x1),
                            min(top, y1),
                        )
                        if x0 >= x1 or y0 >= y1:
                            continue
                        area = (x1 - x0) * (y1 - y0) / (width * height)
                        if area < 0.0001:
                            continue
                        regions.append(
                            {
                                "object_index": index,
                                "kind": "embedded_image",
                                "region": {
                                    "x0": (x0 - left) / width,
                                    "y0": (top - y1) / height,
                                    "x1": (x1 - left) / width,
                                    "y1": (top - y0) / height,
                                },
                            }
                        )
                        if len(regions) > 256:
                            raise AppError("PDF_REGION_OBJECT_LIMIT", "页面图像过多，未扩大到整页")
                    scan = _scan_image_indices(regions)
                    return {
                        **base,
                        "page_index": page_index,
                        "regions": [item for i, item in enumerate(regions) if i not in scan],
                        "scan_like_image_objects": len(scan),
                        "unsupported_nontext_objects": unsupported,
                        "region_parser_version": LEGACY_REGION_VERSION,
                    }
                if render_to is not None:
                    if render_to.suffix != ".png" or render_to.exists():
                        raise AppError("PDF_RENDER_PATH_INVALID", "页面临时输出路径不符")
                    original_rotation = page.get_rotation()
                    # In-memory only. OCR boxes use the unrotated original crop
                    # coordinate system; the locked original is never saved.
                    page.set_rotation(0)
                    scale = min(3.0, 3200 / max(width, height))
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil().convert("RGB")
                        try:
                            if image.width * image.height > 20_000_000:
                                raise AppError("PDF_RENDER_LIMIT", "单页渲染超过上限，未截断")
                            image.save(render_to, "PNG")
                            result = {
                                **base,
                                "page_index": page_index,
                                "view_box": [left, bottom, right, top],
                                "original_rotation": original_rotation,
                                "bitmap_rotation": 0,
                                "width": image.width,
                                "height": image.height,
                                "image_to_pdf": [
                                    width / image.width,
                                    0,
                                    0,
                                    -height / image.height,
                                    left,
                                    top,
                                ],
                                "image_sha256": hashlib.sha256(render_to.read_bytes()).hexdigest(),
                            }
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                    return result
                object_count = raw.FPDFPage_CountObjects(page)
                annotation_count = raw.FPDFPage_GetAnnotCount(page)
                if object_count < 0 or annotation_count < 0:
                    raise AppError("PDF_COVERAGE_UNKNOWN", "页面对象检查失败，未声称完整读取")
                nontext_objects = sum(
                    raw.FPDFPageObj_GetType(raw.FPDFPage_GetObject(page, i))
                    != raw.FPDF_PAGEOBJ_TEXT
                    for i in range(object_count)
                )
                glyphs, characters, offset, unmapped = [], [], 0, 0
                with closing(page.get_textpage()) as text_page:
                    length = text_page.count_chars()
                    if length > MAX_PAGE_CHARACTERS:
                        raise AppError("PDF_PAGE_TEXT_LIMIT", "单页文字超过处理上限，未截断", 422)
                    for index in range(length):
                        # The Unicode API avoids GetText's UCS-2 and inserted-char
                        # indexing ambiguity. Public offsets are Python codepoints.
                        code = raw.FPDFText_GetUnicode(text_page, index)
                        if not code or code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                            unmapped += 1
                            continue
                        char = chr(code)
                        region = None
                        if not char.isspace():
                            x0, y0, x1, y1 = text_page.get_charbox(index)
                            x0, x1 = max(left, x0), min(right, x1)
                            y0, y1 = max(bottom, y0), min(top, y1)
                            if x1 > x0 and y1 > y0:
                                region = {
                                    "x0": (x0 - left) / width,
                                    "y0": (top - y1) / height,
                                    "x1": (x1 - left) / width,
                                    "y1": (top - y0) / height,
                                }
                        glyphs.append(
                            {"start": offset, "end": offset + 1, "text": char, "region": region}
                        )
                        characters.append(char)
                        offset += 1
                text = "".join(characters)
                return {
                    **base,
                    "page_index": page_index,
                    "page_label": document.get_page_label(page_index) or str(page_index + 1),
                    "view_box": [left, bottom, right, top],
                    "rotation": page.get_rotation(),
                    "text": text,
                    "glyphs": glyphs,
                    "text_status": "native" if text.strip() else "needs_ocr",
                    "coverage": {
                        "object_count": object_count,
                        "nontext_object_count": nontext_objects,
                        "annotation_count": annotation_count,
                        "unmapped_characters": unmapped,
                    },
                }


def main() -> int:
    try:
        request = json.loads(sys.stdin.readline(65536))
        result = inspect_pdf(
            Path(request["path"]),
            request["digest"],
            request["page_index"],
            Path(request["render_to"]) if request.get("render_to") else None,
            object_regions=request.get("object_regions") is True,
            region_parser_version=request.get("region_parser_version", REGION_VERSION),
        )
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=True), flush=True)
        return 0
    except AppError as exc:
        print(json.dumps({"ok": False, "code": exc.code}), flush=True)
    except Exception:
        print(json.dumps({"ok": False, "code": "PDF_PROCESSING_FAILED"}), flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
