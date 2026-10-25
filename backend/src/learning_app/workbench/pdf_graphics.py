"""Bounded local graphics inspection for the pinned PDFium runtime.

Only verified rectangular visible components become candidates. This module
neither authorizes model access nor renders, reads or mutates application data.
"""

import ctypes
import math
from collections import Counter

import pypdfium2 as pdfium
import pypdfium2.raw as raw

from learning_app.errors import AppError

VERSION = "pdfium-5.14.0-local-graphics-v3"
MAX_OBJECTS = 4096
MAX_DEPTH = 12
MAX_SEGMENTS = 16384
MAX_REGIONS = 256


def _rectangle(points, *, ordered=False):
    if len(points) == 5 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) != 4 or any(not math.isfinite(v) for point in points for v in point):
        return None
    xs, ys = zip(*points, strict=True)
    box = min(xs), min(ys), max(xs), max(ys)
    if box[0] >= box[2] or box[1] >= box[3]:
        return None
    corners = []
    for x, y in points:
        if x not in (box[0], box[2]) or y not in (box[1], box[3]):
            return None
        corners.append((x == box[0], y == box[1]))
    if set(corners) != {(False, False), (False, True), (True, False), (True, True)}:
        return None
    if ordered and any(
        (points[i][0] != points[(i + 1) % 4][0]) and (points[i][1] != points[(i + 1) % 4][1])
        for i in range(4)
    ):
        return None
    return box


def _intersection(a, b):
    box = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    return box if box[0] < box[2] and box[1] < box[3] else None


def scan_graphics(page, page_box, scan_image_indices):
    count = 0
    segments_read = 0
    regions = []
    unresolved = Counter()
    text_clip = False

    def segments(total, getter):
        nonlocal segments_read
        if total < 0 or segments_read + total > MAX_SEGMENTS:
            raise AppError("PDF_GRAPHICS_SEGMENT_LIMIT", "图形路径检查超限，未扩大读取")
        segments_read += total
        values = []
        for index in range(total):
            segment = getter(index)
            if not segment:
                raise AppError("PDF_GRAPHICS_PATH_UNAVAILABLE", "图形路径不能完整核验")
            x, y = ctypes.c_float(), ctypes.c_float()
            if not raw.FPDFPathSegment_GetPoint(segment, x, y):
                return None
            values.append(
                (
                    raw.FPDFPathSegment_GetType(segment),
                    (x.value, y.value),
                    bool(raw.FPDFPathSegment_GetClose(segment)),
                )
            )
        return values

    def clipped(obj, ancestor, inherited):
        clip = raw.FPDFPageObj_GetClipPath(obj)
        if not clip:
            return inherited, True
        paths = raw.FPDFClipPath_CountPaths(clip)
        # PDFium returns -1 for a valid object's clip with no backing ref;
        # CountPaths=0 with a ref can instead hold text clipping, not "none".
        if paths == -1:
            return inherited, True
        if paths < -1:
            raise AppError("PDF_GRAPHICS_CLIP_UNAVAILABLE", "图形裁剪不能完整核验")
        if paths != 1:
            return None, False
        points = segments(
            raw.FPDFClipPath_CountPathSegments(clip, 0),
            lambda index: raw.FPDFClipPath_GetPathSegment(clip, 0, index),
        )
        if (
            not points
            or sum(kind == raw.FPDF_SEGMENT_MOVETO for kind, _, _ in points) != 1
            or any(
                kind not in {raw.FPDF_SEGMENT_MOVETO, raw.FPDF_SEGMENT_LINETO}
                for kind, _, _ in points
            )
        ):
            return None, False
        box = _rectangle([ancestor.on_point(*point) for _, point, _ in points], ordered=True)
        if box is None:
            return None, False
        return _intersection(inherited, box) if inherited else None, True

    def walk(parent, path, ancestor, inherited_clip):
        nonlocal count, text_clip
        if len(path) > MAX_DEPTH:
            raise AppError("PDF_GRAPHICS_DEPTH_LIMIT", "图形嵌套过深，未扩大读取")
        total = (
            raw.FPDFPage_CountObjects(page)
            if parent is None
            else raw.FPDFFormObj_CountObjects(parent)
        )
        if total < 0 or count + total > MAX_OBJECTS:
            raise AppError("PDF_REGION_OBJECT_LIMIT", "图形对象检查超限，未扩大读取")
        count += total
        for index in range(total):
            handle = (
                raw.FPDFPage_GetObject(page, index)
                if parent is None
                else raw.FPDFFormObj_GetObject(parent, index)
            )
            if not handle:
                raise AppError("PDF_GRAPHICS_OBJECT_UNAVAILABLE", "图形对象不能完整核验")
            obj = pdfium.PdfObject(
                handle, page=page, pdf=page.pdf, container=parent, level=len(path)
            )
            object_path = (*path, index)
            if obj.type == raw.FPDF_PAGEOBJ_TEXT:
                mode = raw.FPDFTextObj_GetTextRenderMode(obj)
                if mode < 0 or mode >= raw.FPDF_TEXTRENDERMODE_FILL_CLIP:
                    # CountPaths does not expose text masks when a rectangular
                    # path clip also exists. Decline the page rather than infer
                    # a visible rectangle through an unverifiable glyph mask.
                    text_clip = True
                continue
            try:
                matrix = obj.get_matrix()
                values = matrix.get()
                if any(not math.isfinite(value) for value in values):
                    raise ValueError("nonfinite matrix")
                if abs(values[0] * values[3] - values[1] * values[2]) < 1e-10:
                    unresolved["singular_transform"] += 1
                    continue
                clip, valid = clipped(obj, ancestor, inherited_clip)
                if not valid:
                    unresolved["complex_clip"] += 1
                    continue
                if clip is None:
                    continue
                if obj.type == raw.FPDF_PAGEOBJ_FORM:
                    walk(obj, object_path, matrix.multiply(ancestor), clip)
                    continue
                if obj.type not in {raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_PATH}:
                    unresolved["unsupported_object"] += 1
                    continue
                if obj.type == raw.FPDF_PAGEOBJ_IMAGE:
                    quad = obj.get_quad_points()
                    box = _rectangle([ancestor.on_point(*point) for point in quad])
                    kind = "embedded_image" if not path else "form_image"
                else:
                    points = segments(
                        raw.FPDFPath_CountSegments(obj),
                        lambda number: raw.FPDFPath_GetPathSegment(obj, number),
                    )
                    fill, stroke = ctypes.c_int(), ctypes.c_int()
                    draw_mode = raw.FPDFPath_GetDrawMode(obj, fill, stroke)
                    # First candidate accepts one filled rectangle only. Open
                    # connectors, curves, compound paths and loose bounding
                    # boxes are not inferred to be a complete figure.
                    if (
                        not points
                        or not draw_mode
                        or fill.value == 0
                        or sum(kind == raw.FPDF_SEGMENT_MOVETO for kind, _, _ in points) != 1
                        or any(
                            kind not in {raw.FPDF_SEGMENT_MOVETO, raw.FPDF_SEGMENT_LINETO}
                            for kind, _, _ in points
                        )
                    ):
                        unresolved["complex_or_open_vector"] += 1
                        continue
                    local = _rectangle(
                        [matrix.on_point(*point) for _, point, _ in points], ordered=True
                    )
                    if local is None:
                        unresolved["nonrectangular_vector"] += 1
                        continue
                    box = _rectangle(
                        [
                            ancestor.on_point(x, y)
                            for x, y in (
                                (local[0], local[1]),
                                (local[2], local[1]),
                                (local[2], local[3]),
                                (local[0], local[3]),
                            )
                        ]
                    )
                    kind = "vector_component"
                if box is None:
                    unresolved["nonrectangular_quad"] += 1
                    continue
                visible = _intersection(box, clip)
                if visible is None:
                    continue
                left, bottom, right, top = page_box
                x0, y0, x1, y1 = visible
                region = {
                    "x0": (x0 - left) / (right - left),
                    "y0": (top - y1) / (top - bottom),
                    "x1": (x1 - left) / (right - left),
                    "y1": (top - y0) / (top - bottom),
                }
                if (region["x1"] - region["x0"]) * (region["y1"] - region["y0"]) < 0.0001:
                    continue
                regions.append(
                    {
                        "object_index": object_path[0],
                        "object_path": list(object_path),
                        "kind": kind,
                        "region": region,
                        "visible_pdf_rect": list(visible),
                    }
                )
                if len(regions) > MAX_REGIONS:
                    raise AppError("PDF_REGION_OBJECT_LIMIT", "局部图形过多，未扩大读取")
            except (pdfium.PdfiumError, ValueError, RuntimeError):
                unresolved["unverifiable_geometry"] += 1

    walk(None, (), pdfium.PdfMatrix(), page_box)
    if text_clip:
        unresolved["text_clip_mask"] += 1
        regions.clear()
    scan_like = scan_image_indices(regions)
    return {
        "regions": [row for index, row in enumerate(regions) if index not in scan_like],
        "scan_like_image_objects": len(scan_like),
        "unsupported_nontext_objects": sum(unresolved.values()),
        "unresolved_reasons": dict(unresolved),
        "region_parser_version": VERSION,
        "scanned_objects": count,
        "scanned_segments": segments_read,
    }
