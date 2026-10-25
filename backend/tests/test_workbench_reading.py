"""Actual fixed PDFium worker, synthetic originals, no model/OCR/video inputs."""

import runpy
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import update
from workbench_support import settings

from learning_app.course_files import CourseFileStore
from learning_app.drafts import (
    DraftClearInput,
    DraftInput,
    clear_draft,
    read_draft,
    save_draft,
    submission_selection_conditions,
)
from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.workbench import schema as s
from learning_app.workbench.manifests import ManifestStore
from learning_app.workbench.models import (
    DocumentRangeInput,
    ManifestCreate,
    PageRegion,
    PdfSelectionInput,
    PdfSelectionPage,
    ReadingOpenInput,
    ReadingStateUpdate,
    TaskCreate,
    TaskGapDecision,
)
from learning_app.workbench.reading import DocumentReader
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.tasks import TaskStore

generate = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "tests/fixtures/workbench/generate.py")
)["reading_pdf"]


def course_pdf(tmp_path, **kwargs):
    root = tmp_path / "materials"
    root.mkdir()
    generate(root / "lesson.pdf", **kwargs)
    repository = Repository(tmp_path / "data")
    files = CourseFileStore(repository)
    course = files.bind(None, str(root))
    opened = files.open(course["course_id"], "lesson.pdf")
    return repository, course["course_id"], opened, root


def select_word(page, word="Learning"):
    start = page.text.index(word)
    glyphs = [g for g in page.glyphs if start <= g.start < start + len(word)]
    boxes = [g.region for g in glyphs if g.region]
    box = PageRegion(
        x0=min(g.x0 for g in boxes),
        y0=min(g.y0 for g in boxes),
        x1=max(g.x1 for g in boxes),
        y1=max(g.y1 for g in boxes),
    )
    return PdfSelectionPage(page_index=page.page_index, text=word, regions=[box])


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_original_crop_coordinates_and_exact_cross_page_selection(tmp_path, rotation):
    repo, cid, opened, root = course_pdf(
        tmp_path, page_count=3, rotation=rotation, crop_box=(20, 30, 600, 780)
    )
    reader = DocumentReader(repo)
    aid, version = opened["asset_id"], opened["source_version"]
    pages = [reader.page(cid, aid, version, index) for index in (0, 1)]
    assert pages[0].view_box == (20, 30, 600, 780)
    assert pages[0].rotation == rotation
    assert "immutable synthetic source" in pages[0].text
    selected = [select_word(page) for page in pages]
    value = PdfSelectionInput(
        asset_id=aid, source_version=version, text="Learning\nLearning", pages=selected
    )
    captured = reader.capture(cid, value)
    assert captured.text == "Learning\nLearning"
    assert captured.content_role == "original"
    assert [p.page_index for p in captured.reference.parts] == [0, 1]
    assert captured.reference.parts[0].start == pages[0].text.index("Learning")
    assert reader.capture(cid, value).snapshot_id == captured.snapshot_id
    assert "immutable" not in captured.text
    bad = value.model_copy(
        update={
            "text": "Outside secret",
            "pages": [selected[0].model_copy(update={"text": "Outside secret"})],
        }
    )
    with pytest.raises(AppError, match="原页选区"):
        reader.capture(cid, bad)
    (root / "lesson.pdf").write_bytes(b"%PDF-replaced")
    with pytest.raises(AppError):
        SourceStore(repo).require_current(captured)
    assert SourceStore(repo).snapshot(cid, captured.snapshot_id).text == captured.text


def test_pdf_full_manifest_keeps_all_34_pages_and_explicit_empty_gap(tmp_path):
    repo, cid, opened, _root = course_pdf(tmp_path, page_count=34, blank_pages=(10,))
    aid, version = opened["asset_id"], opened["source_version"]
    manifest = ManifestStore(repo).create(
        cid, ManifestCreate(documents=[DocumentRangeInput(asset_id=aid, source_version=version)])
    )
    assert len(manifest.units) == 34
    assert manifest.documents[0].last_page == 33
    assert (
        SourceStore(repo).snapshot(cid, manifest.units[33].source_snapshot_id).reference.page_index
        == 33
    )
    assert "page 34" in SourceStore(repo).snapshot(cid, manifest.units[33].source_snapshot_id).text
    task = TaskStore(repo).create(
        cid,
        TaskCreate(
            operation_id="complete_pdf",
            kind="handout",
            instruction="整理全部页",
            coverage_mode="complete",
            scope_manifest_id=manifest.manifest_id,
            settings=settings(),
            sources_only=True,
        ),
    )
    assert task.state == "needs_sources"
    assert task.units[10].state == "missing"
    assert task.units[10].error_code == "SOURCE_TEXT_NOT_READY"
    assert task.units[33].state == "pending"


def test_reading_epoch_rejects_stale_view_and_cached_text_integrity(tmp_path):
    repo, cid, opened, _root = course_pdf(tmp_path, page_count=3)
    reader = DocumentReader(repo)
    aid, version = opened["asset_id"], opened["source_version"]
    first = reader.begin(cid, aid, version)
    saved = reader.save(
        cid,
        aid,
        version,
        ReadingStateUpdate(
            reader_epoch=first.reader_epoch,
            expected_revision=first.revision,
            page_index=2,
            rotation=90,
            scale="page-fit",
        ),
    )
    next_view = reader.begin(cid, aid, version)
    assert next_view.page_index == 2 and next_view.rotation == 90
    assert next_view.reader_epoch != saved.reader_epoch
    with pytest.raises(AppError) as conflict:
        reader.save(
            cid,
            aid,
            version,
            ReadingStateUpdate(
                reader_epoch=saved.reader_epoch, expected_revision=saved.revision, page_index=0
            ),
        )
    assert conflict.value.code == "READING_STATE_CONFLICT"
    with repo.engine.begin() as conn:
        conn.execute(update(s.document_pages).values(payload={"text": "forged"}))
    with pytest.raises(AppError) as corrupt:
        reader.page(cid, aid, version, 2)
    assert corrupt.value.code == "PDF_PAGE_CACHE_CORRUPT"


def test_pdf_quote_submission_is_frozen_and_foreign_reference_is_exact(tmp_path):
    repo, cid, opened, _root = course_pdf(tmp_path, page_count=3)
    reader = DocumentReader(repo)
    page = reader.page(cid, opened["asset_id"], opened["source_version"], 0)
    snapshot = reader.capture(
        cid,
        PdfSelectionInput(
            asset_id=opened["asset_id"],
            source_version=opened["source_version"],
            text="Learning",
            pages=[select_word(page)],
        ),
    )
    chat = repo.create_chat(cid)["chat_id"]
    saved = save_draft(
        repo,
        chat,
        DraftInput(
            expected_revision=0, text="解释这个词", source_snapshot_ids=[snapshot.snapshot_id]
        ),
    )
    assert read_draft(repo, chat)["source_snapshot_ids"] == [snapshot.snapshot_id]
    conditions = submission_selection_conditions(repo, chat, [snapshot.snapshot_id])
    assert '"text":"Learning"' in conditions[0]
    assert "immutable synthetic source" not in conditions[0]
    submission = repo.publish_submission(
        chat, saved["text"], "selected_pdf_submit", conditions=conditions
    )
    cleared = clear_draft(
        repo,
        chat,
        DraftClearInput(
            expected_revision=saved["revision"], submission_id=submission["submission_id"]
        ),
    )
    assert cleared["source_snapshot_ids"] == []
    another = repo.create_chat(repo.create_course("另一个课程")["course_id"])["chat_id"]
    with pytest.raises(AppError) as denied:
        SourceStore(repo).snapshot(repo.get_chat(another)["course_id"], snapshot.snapshot_id)
    assert denied.value.code == "SOURCE_SELECTION_DENIED"
    foreign_conditions = submission_selection_conditions(repo, another, [snapshot.snapshot_id])
    assert foreign_conditions == conditions
    assert SourceStore(repo).snapshot(
        repo.get_chat(another)["course_id"], snapshot.snapshot_id
    ) == snapshot
    unselected = reader.capture(
        cid,
        PdfSelectionInput(
            asset_id=opened["asset_id"],
            source_version=opened["source_version"],
            text="Learning",
            pages=[select_word(reader.page(cid, opened["asset_id"], opened["source_version"], 1))],
        ),
    )
    with pytest.raises(AppError) as other_page:
        SourceStore(repo).snapshot(repo.get_chat(another)["course_id"], unselected.snapshot_id)
    assert other_page.value.code == "SOURCE_SELECTION_DENIED"
    repo.close()


def test_pdf_page_failure_keeps_gap_and_explicit_retry_rebinds_only_same_page(
    tmp_path, monkeypatch
):
    repo, cid, opened, _root = course_pdf(tmp_path, page_count=3)
    aid, version = opened["asset_id"], opened["source_version"]
    original = DocumentReader.page

    def one_failure(self, course, asset, source_version, index):
        if index == 1:
            raise AppError("PDF_PROCESSING_TIMEOUT", "合成单页处理失败")
        return original(self, course, asset, source_version, index)

    monkeypatch.setattr(DocumentReader, "page", one_failure)
    manifest = ManifestStore(repo).create(
        cid, ManifestCreate(documents=[DocumentRangeInput(asset_id=aid, source_version=version)])
    )
    task = TaskStore(repo).create(
        cid,
        TaskCreate(
            operation_id="gap_task",
            kind="handout",
            instruction="完整整理",
            settings=settings(),
            coverage_mode="complete",
            scope_manifest_id=manifest.manifest_id,
        ),
    )
    assert task.state == "needs_sources" and len(task.units) == 3
    gap = task.units[1]
    assert gap.error_code == "PDF_PROCESSING_TIMEOUT"
    assert SourceStore(repo).snapshot(cid, gap.source_snapshot_id).reference.kind == "document_gap"
    monkeypatch.setattr(DocumentReader, "page", original)
    retry = TaskGapDecision(
        operation_id="retry_pdf_gap",
        expected_revision=task.revision,
        decision="retry",
        unit_ids=[gap.unit_id],
    )
    refreshed = TaskStore(repo).decide_gap(cid, task.task_id, retry)
    assert refreshed.state == "queued" and refreshed.scope_revision == 1
    assert refreshed.units[1].unit_id == gap.unit_id
    assert refreshed.units[1].source_snapshot_id != gap.source_snapshot_id
    assert SourceStore(repo).snapshot(cid, gap.source_snapshot_id).text == ""
    current = SourceStore(repo).snapshot(cid, refreshed.units[1].source_snapshot_id)
    assert current.reference.page_index == 1 and "page 2" in current.text
    assert TaskStore(repo).decide_gap(cid, task.task_id, retry) == refreshed


def test_overlapping_scope_and_inserted_word_spaces_are_rejected(tmp_path):
    repo, cid, opened, _root = course_pdf(tmp_path, page_count=3)
    aid, version = opened["asset_id"], opened["source_version"]
    with pytest.raises(AppError) as overlap:
        ManifestStore(repo).create(
            cid,
            ManifestCreate(
                documents=[
                    DocumentRangeInput(
                        asset_id=aid, source_version=version, first_page=0, last_page=1
                    ),
                    DocumentRangeInput(
                        asset_id=aid, source_version=version, first_page=1, last_page=2
                    ),
                ]
            ),
        )
    assert overlap.value.code == "SOURCE_RANGE_OVERLAP"
    reader = DocumentReader(repo)
    selection = select_word(reader.page(cid, aid, version, 0))
    for top, part in [("Learn ing", "Learning"), ("Learn ing", "Learn ing")]:
        with pytest.raises(AppError):
            reader.capture(
                cid,
                PdfSelectionInput(
                    asset_id=aid,
                    source_version=version,
                    text=top,
                    pages=[selection.model_copy(update={"text": part})],
                ),
            )


def test_late_open_cannot_take_over_newer_view(tmp_path, monkeypatch):
    repo, cid, opened, _root = course_pdf(tmp_path, page_count=3)
    aid, version = opened["asset_id"], opened["source_version"]
    slow, current = DocumentReader(repo), DocumentReader(repo)
    entered, release = threading.Event(), threading.Event()
    verify = slow._pdf_source

    def delayed(*args):
        entered.set()
        assert release.wait(10)
        return verify(*args)

    monkeypatch.setattr(slow, "_pdf_source", delayed)
    old_request = ReadingOpenInput(
        reader_client_id="browser_a", request_id="open_1", view_sequence=1
    )
    new_request = ReadingOpenInput(
        reader_client_id="browser_a", request_id="open_2", view_sequence=2
    )
    with ThreadPoolExecutor(1) as workers:
        future = workers.submit(slow.begin, cid, aid, version, old_request)
        assert entered.wait(10)
        try:
            newer = current.begin(cid, aid, version, new_request)
        finally:
            release.set()
        older = future.result(10)
    saved = current.save(
        cid,
        aid,
        version,
        ReadingStateUpdate(
            reader_epoch=newer.reader_epoch, expected_revision=newer.revision, page_index=1
        ),
    )
    assert saved.page_index == 1
    with pytest.raises(AppError) as stale:
        current.begin(cid, aid, version, old_request)
    assert stale.value.code == "READING_VIEW_STALE"
    assert current.begin(cid, aid, version, new_request).reader_epoch == newer.reader_epoch
    with pytest.raises(AppError):
        current.save(
            cid,
            aid,
            version,
            ReadingStateUpdate(
                reader_epoch=older.reader_epoch, expected_revision=older.revision, page_index=0
            ),
        )
