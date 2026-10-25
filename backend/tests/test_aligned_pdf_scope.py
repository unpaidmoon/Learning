"""Declared PDF text/OCR scopes; never send page images or process neighbors."""

import asyncio
import hashlib

import pytest
from sqlalchemy import func, select
from test_source_preparations import setup
from workbench_support import settings

from learning_app.config import PROJECT_ROOT
from learning_app.workbench import schema as s
from learning_app.workbench.ocr_resources import OcrResources
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.processing import PreparationService
from learning_app.workbench.processing_models import PreparationControl
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission


def declared(repo, course, request):
    return TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="pdf_text_scope",
            kind="aligned_translation",
            instruction="逐段对照指定页的文字",
            coverage_mode="complete",
            sources_only=True,
            settings=settings(),
            sources=[
                {
                    "kind": "document",
                    "asset_id": request.asset_id,
                    "source_version": request.source_version,
                    "first_page": 1,
                    "last_page": 2,
                    "text_only": True,
                }
            ],
            target={"mode": "create", "title": "指定两页文字对照"},
        ),
    )


@pytest.mark.asyncio
async def test_scanned_translation_waits_for_only_declared_local_ocr_pages(tmp_path):
    if not OcrResources(PROJECT_ROOT).prepared():
        pytest.skip("fixed OCR resources must already be prepared")
    repo, course, root, request = setup(tmp_path, scan=True, count=4)
    original = hashlib.sha256((root / "lesson.pdf").read_bytes()).hexdigest()
    task = declared(repo, course, request)
    coverage = TaskCoverage(repo)
    coverage.refresh(course, task.task_id)
    jobs = PreparationStore(repo).list(course)
    assert len(jobs) == 1 and jobs[0].scope == "range"
    assert [unit.unit_index for unit in jobs[0].units] == [1, 2]
    assert not TaskPlanner(repo).plan(course, task.task_id)
    service = PreparationService(repo, PROJECT_ROOT)
    await service.open()
    try:
        async with asyncio.timeout(60):
            while service.store.get(course, jobs[0].job_id).state in {"queued", "running"}:
                await asyncio.sleep(0.1)
        assert service.store.get(course, jobs[0].job_id).state == "completed"
        coverage.refresh(course, task.task_id)
        assert TaskPlanner(repo).plan(course, task.task_id)
        with repo.engine.connect() as conn:
            units = list(
                conn.execute(
                    select(s.task_units.c.unit_id).where(s.task_units.c.task_id == task.task_id)
                ).scalars()
            )
            ocr_pages = set(conn.execute(select(s.document_ocr.c.page_index)).scalars())
            assert conn.execute(select(func.count()).select_from(s.task_runs)).scalar_one() == 0
        inputs = [
            item
            for unit in units
            for item in TaskPlanner(repo).inputs(course, task.task_id, unit)[1]
        ]
        assert {source.reference.page_index for _, source in inputs} == {1, 2}
        assert all(
            item["representation"] == "text"
            and source.reference.kind == "document"
            and source.reference.parser_version.startswith("ocr:")
            for item, source in inputs
        )
        assert ocr_pages == {1, 2}
        assert hashlib.sha256((root / "lesson.pdf").read_bytes()).hexdigest() == original
    finally:
        await service.close()
        repo.close()


def test_manual_ocr_pause_stays_a_source_gap_and_is_not_woken_by_task_refresh(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(OcrResources, "prepared", lambda self: True)
    repo, course, _, request = setup(tmp_path, scan=True, count=4)
    task = declared(repo, course, request)
    coverage, preparations = TaskCoverage(repo), PreparationStore(repo)
    coverage.refresh(course, task.task_id)
    job = preparations.list(course)[0]
    paused = preparations.control(
        course,
        job.job_id,
        "pause",
        PreparationControl(operation_id="pause", expected_revision=job.revision),
    )
    coverage.refresh(course, task.task_id)
    coverage.refresh(course, task.task_id)
    view = coverage.get(course, task.task_id)
    assert any(leaf.error_code == "SOURCE_TRANSLATION_OCR_PAUSED" for leaf in view.leaves)
    assert preparations.get(course, job.job_id).state == paused.state == "paused"
    assert preparations.claim() is None and not TaskPlanner(repo).plan(course, task.task_id)
    repo.close()
