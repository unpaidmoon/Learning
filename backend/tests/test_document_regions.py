"""Actual PDFium document crops; no model access or native image grants yet."""

import hashlib
import importlib.util
import io
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from PIL import Image
from sqlalchemy import select
from workbench_support import learning_course, note, settings

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.storage import repository as storage
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.document_regions import DocumentRegions
from learning_app.workbench.models import DocumentRef, PageRegion
from learning_app.workbench.offline import OfflineExporter
from learning_app.workbench.pdf_worker import LEGACY_REGION_VERSION, REGION_VERSION
from learning_app.workbench.reading import DocumentReader
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission

_spec = importlib.util.spec_from_file_location(
    "document_regions_fixtures",
    Path(__file__).resolve().parents[2] / "tests/fixtures/workbench/generate.py",
)
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)


def figure_pdf(path, rotation=0, crop=None):
    objects = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for color in ((0, 180, 0), (220, 0, 0)):
        with Image.new("RGB", (260, 100), color) as image:
            output = io.BytesIO()
            image.save(output, "JPEG", quality=100)
        data = output.getvalue()
        objects.append(
            b"<< /Type /XObject /Subtype /Image /Width 260 /Height 100 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length "
            + str(len(data)).encode()
            + b" >>\nstream\n"
            + data
            + b"\nendstream"
        )
    stream = (
        b"q 260 0 0 100 60 470 cm /Im1 Do Q\n"
        b"BT /F1 16 Tf 60 445 Td (Figure 1: the local green diagram.) Tj ET\n"
        b"q 260 0 0 100 60 130 cm /Im2 Do Q\n"
        b"BT /F1 16 Tf 60 105 Td (EXCLUDED_DISTANT_FIGURE.) Tj ET\n"
    )
    geometry = f" /Rotate {rotation}" + (
        " /CropBox [" + " ".join(map(str, crop)) + "]" if crop else ""
    )
    objects.extend(
        [
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]{geometry} /Resources << /Font << /F1 3 0 R >> /XObject << /Im1 4 0 R /Im2 5 0 R >> >> /Contents 7 0 R >>".encode(),
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream",
        ]
    )
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = b"<< /Type /Pages /Kids [6 0 R] /Count 1 >>"
    data, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    path.write_bytes(data)


def document_parent(repo, course, root, *, rotation=0, crop=None):
    path = root / "diagrams.pdf"
    figure_pdf(path, rotation, crop)
    opened = CourseFileStore(repo).open(course, path.name)
    page = DocumentReader(repo).page(course, opened["asset_id"], opened["source_version"], 0)
    start = page.text.index("Figure 1")
    text = "Figure 1: the local green diagram."
    source = SourceStore(repo)._save_verified(
        course,
        DocumentRef(
            asset_id=opened["asset_id"],
            source_version=opened["source_version"],
            source_digest=page.source_digest,
            page_index=0,
            parser_version=page.parser_version,
            start=start,
            end=start + len(text),
        ),
        text,
        "original",
    )
    return path, source


@pytest.mark.parametrize(
    "rotation,crop",
    [(0, None), (90, (20, 30, 600, 770)), (270, None)],
    ids=["normal", "rotated-cropbox", "rotated-270"],
)
def test_local_image_location_and_quantized_crop_preserve_original_coordinate_system(
    tmp_path, rotation, crop
):
    repo, course, root, _ = learning_course(tmp_path)
    path, parent = document_parent(repo, course, root, rotation=rotation, crop=crop)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    store = DocumentRegions(repo)
    candidates = store.candidates(course, parent.snapshot_id)
    assert len(candidates) == 1 and candidates[0].kind == "embedded_image"
    assert candidates == store.candidates(course, parent.snapshot_id)
    result = store.render(course, candidates[0])
    with Image.open(io.BytesIO(result.image)) as image:
        pixels = list(image.convert("RGB").getdata())
        assert sum(g > 150 and r < 30 and b < 30 for r, g, b in pixels) > len(pixels) * 0.97
        assert not any(r > 150 and g < 30 for r, g, b in pixels)
    assert result.width < 1000 and result.height < 400
    a, b, c, d, e, f = result.image_to_pdf
    assert (e, f) == pytest.approx((60, 570), abs=0.5)
    assert (a * result.width, -d * result.height) == pytest.approx((260, 100), abs=1)
    assert result.actual_region.x0 >= candidates[0].region.x0
    assert result.actual_region.y0 >= candidates[0].region.y0
    assert result.actual_region.x1 <= candidates[0].region.x1
    assert result.actual_region.y1 <= candidates[0].region.y1
    assert hashlib.sha256(result.image).hexdigest() == result.image_sha256
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert list(repo.staging.iterdir()) == []
    repo.close()


def test_changed_candidate_wrong_anchor_and_ai_ancestor_never_authorize_other_pixels(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    _, parent = document_parent(repo, course, root)
    store = DocumentRegions(repo)
    candidate = store.candidates(course, parent.snapshot_id)[0]
    forged = candidate.model_copy(update={"region": PageRegion(x0=0, y0=0, x1=1, y1=1)})
    with pytest.raises(AppError) as rejected:
        store.render(course, forged)
    assert rejected.value.code == "DOCUMENT_REGION_SCOPE_DENIED"
    artifact = ArtifactStore(repo).create(course, note(snapshot_id=parent.snapshot_id)).artifact
    generated = SourceStore(repo).capture_artifact(
        course, artifact.artifact_id, artifact.head_revision_id, "paragraph_1"
    )
    with pytest.raises(AppError) as rejected:
        store.candidates(course, generated.snapshot_id)
    assert rejected.value.code == "DOCUMENT_REGION_TEXT_REQUIRED"
    false_anchor = SourceStore(repo)._save_verified(
        course, parent.reference, "Unverified text", "original"
    )
    with pytest.raises(AppError) as rejected:
        store.candidates(course, false_anchor.snapshot_id)
    assert rejected.value.code == "DOCUMENT_REGION_ANCHOR_UNAVAILABLE"
    repo.close()


def test_original_source_version_change_blocks_new_region_and_no_new_version_fallback(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    path, parent = document_parent(repo, course, root)
    store = DocumentRegions(repo)
    candidate = store.candidates(course, parent.snapshot_id)[0]
    path.write_bytes(path.read_bytes() + b"\n% intentionally changed owned fixture\n")
    with pytest.raises(AppError) as rejected:
        store.render(course, candidate)
    assert rejected.value.code == "SOURCE_VERSION_UNAVAILABLE"
    repo.close()


@pytest.mark.parametrize("layout", ["far", "tiles", "tile_overlay", "full_image", "overlap_scan"])
def test_real_glyph_distances_scan_tiles_and_independent_overlay(tmp_path, layout):
    repo, course, root, _ = learning_course(tmp_path)
    path = root / "layout.pdf"
    texts = [(285, 390, "ANCHOR")]
    tiles = [
        (0, 396, 306, 396, (220, 0, 0)),
        (306, 396, 306, 396, (0, 0, 220)),
        (0, 0, 306, 396, (180, 180, 0)),
        (306, 0, 306, 396, (180, 0, 180)),
    ]
    if layout == "far":
        images = [(60, 590, 260, 100, (0, 180, 0)), (60, 330, 260, 100, (220, 0, 0))]
        texts = [(60, 720, "ANCHOR TOP: local green diagram."), (60, 60, "FOOTER: unrelated text.")]
    elif layout == "tiles":
        images = tiles
    elif layout == "tile_overlay":
        images = [*tiles, (260, 350, 80, 80, (0, 180, 0))]
    elif layout == "full_image":
        images = [(0, 0, 612, 792, (220, 0, 0))]
    else:
        images = [(0, 0, 400, 792, (220, 0, 0)), (212, 0, 400, 792, (0, 0, 220))]
    _fixtures.document_regions_pdf(path, images, texts)
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
    candidates = DocumentRegions(repo).candidates(course, parent.snapshot_id)
    if layout in {"tiles", "full_image", "overlap_scan"}:
        assert candidates == []  # No complete scan reconstructed as local crops.
    else:
        assert len(candidates) == 1
        expected_index = 0 if layout == "far" else 4
        assert candidates[0].object_index == expected_index
        result = DocumentRegions(repo).render(course, candidates[0])
        with Image.open(io.BytesIO(result.image)) as image:
            assert not any(r > 150 and g < 30 for r, g, b in image.convert("RGB").getdata())
    repo.close()


@pytest.mark.parametrize("version", [LEGACY_REGION_VERSION, REGION_VERSION])
def test_saved_crop_keeps_exact_bytes_and_offline_scope_and_refuses_incompatible_downgrade(
    tmp_path,
    version,
):
    import json
    import re
    import zipfile

    repo, course, root, _ = learning_course(tmp_path)
    _, parent = document_parent(repo, course, root)
    candidate = DocumentRegions(repo)._candidates(course, parent.snapshot_id, version)[0]
    source = SourceStore(repo).capture_document_region(course, candidate)
    assert source.reference.kind == "document_region_image" and source.text == ""
    assert source.reference.parent_text_snapshot_id == parent.snapshot_id
    SourceStore(repo).require_current(source)
    assert SourceStore(repo).capture_document_region(course, candidate) == source
    image = SourceStore(repo).image_path(source.reference.image_sha256).read_bytes()
    artifact = ArtifactStore(repo).create(course, note(snapshot_id=source.snapshot_id)).artifact
    with OfflineExporter(repo).build(
        course, artifact.artifact_id, artifact.head_revision_id
    ) as stream:
        with zipfile.ZipFile(stream) as archive:
            pictures = [name for name in archive.namelist() if name.startswith("assets/")]
            assert pictures == [f"assets/{source.reference.image_sha256}.png"]
            assert archive.read(pictures[0]) == image
            embedded = re.search(
                r'<script type="application/json" id="learning-offline-data">(.*?)</script>',
                archive.read("index.html").decode("utf-8"),
                re.S,
            )
            document = json.loads(embedded.group(1))
            location = document["sources"][0]["location"]
            assert location["kind"] == "document_region_image"
            assert location["region"] == source.reference.region.model_dump(mode="json")
            assert location["parent_text_hash"] == parent.content_hash
    config = Config()
    config.set_main_option("script_location", str(Path(storage.__file__).parent / "migrations"))
    before = repo.counts()
    code = (
        "DOCUMENT_REGION_DOWNGRADE_DENIED"
        if version == LEGACY_REGION_VERSION
        else "DOCUMENT_GRAPHICS_DOWNGRADE_DENIED"
    )
    with pytest.raises(RuntimeError, match=code):
        with repo.engine.begin() as conn:
            config.attributes["connection"] = conn
            command.downgrade(config, "0027_learning_range_edits")
    assert (
        repo.counts() == before
        and repo.runtime_info()["schema_revision"] == storage.SCHEMA_REVISION
    )
    repo.close()


def test_text_translation_cannot_send_document_crop_as_selected_text(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    _, parent = document_parent(repo, course, root)
    source = SourceStore(repo).capture_document_region(
        course, DocumentRegions(repo).candidates(course, parent.snapshot_id)[0]
    )
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="crop_not_text",
            kind="translation",
            instruction="只翻译选择文字",
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "文字翻译"},
            settings=settings(),
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id)
    assert not TaskPlanner(repo).plan(course, task.task_id)
    coverage = TaskCoverage(repo).get(course, task.task_id)
    assert coverage.leaves[0].error_code == "SOURCE_TRANSLATION_TEXT_REQUIRED"
    with repo.engine.connect() as conn:
        assert not conn.execute(select(s.task_runs)).first()
    repo.close()
