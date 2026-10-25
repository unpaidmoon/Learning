"""Actual pinned PDFium geometry, crop bytes and old candidate compatibility."""

import hashlib
import io
from contextlib import closing
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from alembic import command
from alembic.config import Config
from pdf_graphics_support import GraphicsPDF, clipped_form_pdf
from PIL import Image
from test_document_regions import document_parent
from workbench_support import learning_course

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.storage import repository as storage
from learning_app.workbench import pdf_graphics
from learning_app.workbench.document_regions import DocumentRegionCandidate, DocumentRegions
from learning_app.workbench.models import DocumentRef, SnapshotSliceRef
from learning_app.workbench.pdf_worker import (
    LEGACY_REGION_VERSION,
    REGION_VERSION,
    _scan_image_indices,
)
from learning_app.workbench.reading import DocumentReader
from learning_app.workbench.sources import SourceStore


def parent_for(repo, course, path):
    opened = CourseFileStore(repo).open(course, path.name)
    page = DocumentReader(repo).page(course, opened["asset_id"], opened["source_version"], 0)
    parent = SourceStore(repo)._save_verified(
        course,
        DocumentRef(
            asset_id=opened["asset_id"],
            source_version=opened["source_version"],
            source_digest=page.source_digest,
            page_index=0,
            parser_version=page.parser_version,
            start=0,
            end=len(page.text),
        ),
        page.text,
        "original",
    )
    return parent


def inspect(path):
    with closing(pdfium.PdfDocument(path)) as doc, closing(doc[0]) as page:
        return pdf_graphics.scan_graphics(page, page.get_bbox(), _scan_image_indices)


@pytest.mark.parametrize(
    "nested,fractional,rotation,crop",
    [
        (False, False, 0, None),
        (True, False, 0, None),
        (False, True, 0, None),
        (False, True, 90, "30 40 370 360"),
    ],
)
def test_form_clip_and_ancestor_transform_bound_actual_saved_pixels(
    tmp_path, nested, fractional, rotation, crop
):
    repo, course, root, _ = learning_course(tmp_path)
    path = clipped_form_pdf(
        root / "form.pdf", nested=nested, fractional=fractional, rotation=rotation, crop=crop
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    parent = parent_for(repo, course, path)
    store = DocumentRegions(repo)
    candidate = next(
        row for row in store.candidates(course, parent.snapshot_id) if row.kind == "form_image"
    )
    assert candidate.region_parser_version == REGION_VERSION
    assert len(candidate.object_path) == (3 if nested else 2)
    result = store.render(course, candidate)
    assert result.actual_region.x0 >= candidate.region.x0
    assert result.actual_region.y0 >= candidate.region.y0
    assert result.actual_region.x1 <= candidate.region.x1
    assert result.actual_region.y1 <= candidate.region.y1
    with Image.open(io.BytesIO(result.image)) as image:
        assert image.width < 160 and image.height < 110
        assert all(
            g > 100 and r < 80 and b < 80 for r, g, b in image.convert("RGB").get_flattened_data()
        )
    # The unclipped bounds would read a red neighbor; the visible clip does not.
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert not list(repo.staging.iterdir())
    repo.close()


def test_repeated_form_instances_have_distinct_paths_and_geometry(tmp_path):
    pdf = GraphicsPDF()
    image = pdf.raster()
    form = pdf.form("q 40 0 0 30 0 0 cm /Im Do Q", f"/XObject << /Im {image} 0 R >>")
    path = pdf.write(
        tmp_path / "repeat.pdf",
        "q 1 0 0 1 60 240 cm /Fm Do Q\nq 1 0 0 1 260 240 cm /Fm Do Q",
        f"/XObject << /Fm {form} 0 R >>",
    )
    rows = inspect(path)["regions"]
    assert len(rows) == 2 and rows[0]["object_path"] != rows[1]["object_path"]
    assert rows[0]["visible_pdf_rect"] == pytest.approx([60, 240, 100, 270])
    assert rows[1]["visible_pdf_rect"] == pytest.approx([260, 240, 300, 270])


@pytest.mark.parametrize("in_form", [False, True])
def test_pure_vector_component_is_local_and_not_the_page_background(tmp_path, in_form):
    pdf = GraphicsPDF()
    body = "0 .1 .8 rg 60 240 80 50 re f"
    resources = ""
    if in_form:
        form = pdf.form(body, bbox="0 0 400 400")
        body, resources = "/Fm Do", f"/XObject << /Fm {form} 0 R >>"
    path = pdf.write(tmp_path / "vector.pdf", "1 1 1 rg 0 0 400 400 re f\n" + body, resources)
    result = inspect(path)
    assert len(result["regions"]) == 1 and result["scan_like_image_objects"] == 1
    row = result["regions"][0]
    assert row["kind"] == "vector_component"
    assert len(row["object_path"]) == (2 if in_form else 1)
    assert row["visible_pdf_rect"] == pytest.approx([60, 240, 140, 290])


@pytest.mark.parametrize(
    "case",
    [
        "disconnected",
        "open_connector",
        "curve",
        "shear",
        "tiny_shear",
        "complex_clip",
        "text_clip",
        "text_and_rect_clip",
        "vector_tiles",
        "form_tiles",
    ],
)
def test_unverifiable_masks_paths_and_scan_tiles_do_not_become_local_candidates(tmp_path, case):
    pdf, resources = GraphicsPDF(), ""
    bodies = {
        "disconnected": "0 .1 .8 rg 60 240 80 50 re 290 70 50 40 re f",
        "open_connector": "0 0 0 RG 60 240 m 140 240 l 140 290 l S",
        "curve": "0 .1 .8 rg 60 240 m 90 350 140 290 60 240 c f",
        "complex_clip": "q 60 240 m 100 300 l 140 240 l h W n 0 1 0 rg 60 240 80 50 re f Q",
        "text_clip": "q BT /F0 40 Tf 60 250 Td 7 Tr (MASK) Tj ET 0 1 0 rg 60 240 120 50 re f Q",
        "text_and_rect_clip": "q 60 240 120 50 re W n BT /F0 40 Tf 60 250 Td 7 Tr (MASK) Tj ET 0 1 0 rg 60 240 120 50 re f Q",
        "vector_tiles": "0 .1 .8 rg 0 0 200 200 re f 200 0 200 200 re f 0 200 200 200 re f 200 200 200 200 re f",
    }
    if case in {"shear", "tiny_shear"}:
        image = pdf.raster()
        form = pdf.form(
            "q 100 0 0 60 0 0 cm /Im Do Q",
            f"/XObject << /Im {image} 0 R >>",
            matrix="1 .2 .1 1 80 180" if case == "shear" else "1 .000001 0 1 80 180",
        )
        body, resources = "/Fm Do", f"/XObject << /Fm {form} 0 R >>"
    elif case == "form_tiles":
        image = pdf.raster()
        form = pdf.form(
            "\n".join(
                f"q 200 0 0 200 {x} {y} cm /Im Do Q"
                for x, y in [(0, 0), (200, 0), (0, 200), (200, 200)]
            ),
            f"/XObject << /Im {image} 0 R >>",
            bbox="0 0 400 400",
        )
        body, resources = "/Fm Do", f"/XObject << /Fm {form} 0 R >>"
    else:
        body = bodies[case]
    result = inspect(pdf.write(tmp_path / f"{case}.pdf", body, resources))
    assert result["regions"] == [], result
    if case.startswith("text"):
        assert result["unresolved_reasons"].get("text_clip_mask") == 1


def test_short_parent_slice_does_not_bridge_unread_lines(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    pdf = GraphicsPDF()
    path = pdf.write(
        root / "glyphs.pdf",
        "1 0 0 rg 50 175 55 40 re f",
        anchor=("BT /F0 12 Tf 50 350 Td (TOP) Tj ET\nBT /F0 12 Tf 50 40 Td (BOTTOM) Tj ET\n"),
    )
    parent = parent_for(repo, course, path)
    store = DocumentRegions(repo)
    assert store.candidates(course, parent.snapshot_id) == []
    child = SourceStore(repo)._save_verified(
        course,
        SnapshotSliceRef(
            parent_snapshot_id=parent.snapshot_id, parent_hash=parent.content_hash, start=0, end=3
        ),
        "TOP",
        "original",
    )
    assert store.candidates(course, child.snapshot_id) == []
    repo.close()


@pytest.mark.parametrize(
    "limit,value,code",
    [
        ("MAX_OBJECTS", 1, "PDF_REGION_OBJECT_LIMIT"),
        ("MAX_DEPTH", 0, "PDF_GRAPHICS_DEPTH_LIMIT"),
        ("MAX_SEGMENTS", 2, "PDF_GRAPHICS_SEGMENT_LIMIT"),
        ("MAX_REGIONS", 0, "PDF_REGION_OBJECT_LIMIT"),
    ],
)
def test_graphics_budgets_fail_instead_of_returning_partial_candidates(
    tmp_path, monkeypatch, limit, value, code
):
    path = clipped_form_pdf(tmp_path / "limits.pdf")
    monkeypatch.setattr(pdf_graphics, limit, value)
    with pytest.raises(AppError) as failure:
        inspect(path)
    assert failure.value.code == code


def test_old_candidate_id_rounding_and_bytes_remain_version_dispatched(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    _, parent = document_parent(repo, course, root, rotation=90, crop=(20, 30, 600, 770))
    store = DocumentRegions(repo)
    old = store._candidates(course, parent.snapshot_id, LEGACY_REGION_VERSION)[0]
    legacy_data = old.model_dump(mode="json", exclude={"object_path"})
    restored = DocumentRegionCandidate.model_validate(legacy_data)
    assert restored == old and restored.object_path is None
    before = store.render(course, restored)
    assert before.actual_region.x0 <= old.region.x0
    assert before.actual_region.y0 <= old.region.y0
    new = store.candidates(course, parent.snapshot_id)[0]
    assert new.object_path is not None and new.region_id != old.region_id
    assert store.render(course, restored).image == before.image
    forged = new.model_copy(update={"object_path": (999,)})
    with pytest.raises(AppError, match="区域不是"):
        store.render(course, forged)
    unsupported = old.model_copy(update={"region_parser_version": "unavailable"})
    with pytest.raises(AppError) as failure:
        store.render(course, unsupported)
    assert failure.value.code == "DOCUMENT_REGION_VERSION_UNAVAILABLE"
    SourceStore(repo).capture_document_region(course, new)
    config = Config()
    config.set_main_option("script_location", str(Path(storage.__file__).parent / "migrations"))
    with pytest.raises(RuntimeError, match="DOCUMENT_GRAPHICS_DOWNGRADE_DENIED"):
        with repo.engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, "0032_stage_failures")
    repo.close()
