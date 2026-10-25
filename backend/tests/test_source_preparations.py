"""Source queue leases and actual installed local OCR on synthetic pages."""

import asyncio
import hashlib
import json
import runpy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from learning_app.app import create_app
from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.workbench import schema as s
from learning_app.workbench.models import (
    PageRegion,
    PdfSelectionInput,
    PdfSelectionPage,
    ReadingOpenInput,
)
from learning_app.workbench.ocr_resources import OcrResources
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.processing import PreparationService
from learning_app.workbench.processing_models import (
    PreparationControl,
    PreparationCreate,
    ReadingFocusInput,
)
from learning_app.workbench.reading import DocumentReader

generators = runpy.run_path(str(PROJECT_ROOT / "tests/fixtures/workbench/generate.py"))


def setup(tmp_path, *, scan=False, count=3, dense=False):
    root = tmp_path / "materials"
    root.mkdir()
    if scan:
        generators["scanned_pdf"](root / "lesson.pdf", count, dense=dense)
    else:
        generators["reading_pdf"](root / "lesson.pdf", count)
    repo = Repository(tmp_path / "data")
    files = CourseFileStore(repo)
    cid = files.bind(None, str(root))["course_id"]
    opened = files.open(cid, "lesson.pdf")
    request = PreparationCreate(
        kind="pdf_ocr", asset_id=opened["asset_id"], source_version=opened["source_version"]
    )
    return repo, cid, root, request


def fixture_result(lease):
    return {
        "kind": "cached_page",
        "source_digest": lease["source_digest"],
        "page_index": lease["unit_index"],
        "engine_invocations": 0,
    }


def test_priority_manual_pause_and_unit_job_do_not_revive_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(OcrResources, "prepared", lambda self: True)
    repo, cid, _root, request = setup(tmp_path)
    store = PreparationStore(repo)
    batch = store.create_pdf(cid, request.model_copy(update={"priority_unit": 2}), PROJECT_ROOT)
    lease = store.claim()
    assert lease["unit_index"] == 2
    current = store.get(cid, batch.job_id)
    action = PreparationControl(operation_id="pause_batch", expected_revision=current.revision)
    paused = store.control(cid, batch.job_id, "pause", action)
    assert paused.state == "paused" and store.claim() is None
    assert not store.finish(lease, fixture_result(lease))
    assert store.control(cid, batch.job_id, "pause", action) == paused
    same = store.create_pdf(cid, request.model_copy(update={"priority_unit": 1}), PROJECT_ROOT)
    assert same.job_id == batch.job_id and same.state == "paused"
    single = store.create_pdf(
        cid, request.model_copy(update={"scope": "unit", "unit_index": 1}), PROJECT_ROOT
    )
    assert single.job_id != batch.job_id
    active = store.claim()
    assert active["job_id"] == single.job_id and active["unit_index"] == 1
    assert store.finish(active, fixture_result(active))
    assert store.get(cid, single.job_id).state == "completed"
    assert store.get(cid, batch.job_id).state == "paused"
    other = repo.create_course("另课")["course_id"]
    with pytest.raises(AppError) as denied:
        store.get(other, batch.job_id)
    assert denied.value.code == "PREPARATION_NOT_FOUND"


def test_current_page_preempts_batch_and_restart_keeps_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(OcrResources, "prepared", lambda self: True)
    repo, cid, _root, request = setup(tmp_path)
    store = PreparationStore(repo)
    batch = store.create_pdf(cid, request, PROJECT_ROOT)
    first = store.claim()
    assert first["unit_index"] == 0
    store.create_pdf(cid, request.model_copy(update={"priority_unit": 2}), PROJECT_ROOT)
    assert store.should_yield(first)
    assert store.release(first)
    urgent = store.claim()
    assert urgent["unit_index"] == 2
    assert store.finish(urgent, fixture_result(urgent))
    leftover = store.claim()
    root = repo.root
    repo.close()
    repo = Repository(root)
    store = PreparationStore(repo)
    store.recover()
    restored = store.get(cid, batch.job_id)
    assert restored.units[2].state == "completed"
    assert restored.units[0].state == "pending"
    assert not store.finish(leftover, fixture_result(leftover))
    next_lease = store.claim()
    bad = fixture_result(next_lease) | {"source_digest": "0" * 64}
    with pytest.raises(AppError) as mismatch:
        store.finish(next_lease, bad)
    assert mismatch.value.code == "PREPARATION_RESULT_SCOPE_CONFLICT"


def test_pause_commutes_with_progress_but_not_a_newer_manual_control(tmp_path, monkeypatch):
    monkeypatch.setattr(OcrResources, "prepared", lambda self: True)
    repo, cid, _root, request = setup(tmp_path)
    store = PreparationStore(repo)
    original = store.create_pdf(cid, request, PROJECT_ROOT)
    lease = store.claim()
    paused = store.control(
        cid,
        original.job_id,
        "pause",
        PreparationControl(
            operation_id="pause_after_progress", expected_revision=original.revision
        ),
    )
    assert paused.state == "paused" and not store.finish(lease, fixture_result(lease))
    with pytest.raises(AppError) as stale:
        store.control(
            cid,
            original.job_id,
            "resume",
            PreparationControl(operation_id="stale_resume", expected_revision=original.revision),
        )
    assert stale.value.code == "PREPARATION_REVISION_CONFLICT"


def test_focus_fences_span_documents_and_late_preparation_cannot_take_priority(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(OcrResources, "prepared", lambda self: True)
    repo, cid, root, request = setup(tmp_path)
    generators["reading_pdf"](root / "another.pdf", 2)
    other = CourseFileStore(repo).open(cid, "another.pdf")
    reader, store = DocumentReader(repo), PreparationStore(repo)
    first = reader.begin(
        cid,
        request.asset_id,
        request.source_version,
        ReadingOpenInput(
            reader_client_id="fixture_reader", request_id="open_first", view_sequence=1
        ),
    )
    old_focus = ReadingFocusInput(
        reader_client_id="fixture_reader",
        reader_epoch=first.reader_epoch,
        request_id="page_first",
        focus_sequence=2,
        page_index=1,
    )
    reader.focus(cid, request.asset_id, request.source_version, old_focus)
    definition = store.define_pdf(
        cid, request.model_copy(update={"priority_unit": 1}), PROJECT_ROOT
    )
    batch = store.publish_pdf(definition)
    assert batch.units[1].priority == 0
    second = reader.begin(
        cid,
        other["asset_id"],
        other["source_version"],
        ReadingOpenInput(
            reader_client_id="fixture_reader", request_id="open_second", view_sequence=3
        ),
    )
    assert all(unit.priority == 10 for unit in store.get(cid, batch.job_id).units)
    with pytest.raises(AppError, match="迟到") as stale:
        reader.assert_focus(cid, request.asset_id, request.source_version, old_focus)
    assert stale.value.code == "READING_VIEW_STALE"
    with pytest.raises(AppError) as stale_open:
        reader.begin(
            cid,
            request.asset_id,
            request.source_version,
            ReadingOpenInput(
                reader_client_id="fixture_reader", request_id="late_old_open", view_sequence=2
            ),
        )
    assert stale_open.value.code == "READING_VIEW_STALE"
    current = ReadingFocusInput(
        reader_client_id="fixture_reader",
        reader_epoch=second.reader_epoch,
        request_id="page_second",
        focus_sequence=4,
        page_index=1,
    )
    reader.focus(cid, other["asset_id"], other["source_version"], current)
    reader.assert_focus(cid, other["asset_id"], other["source_version"], current)
    assert reader.release_focus(cid, other["asset_id"], other["source_version"], current)
    with pytest.raises(AppError):
        reader.assert_focus(cid, other["asset_id"], other["source_version"], current)
    assert store.get(cid, batch.job_id).state == "queued"
    newer = reader.begin(
        cid,
        request.asset_id,
        request.source_version,
        ReadingOpenInput(
            reader_client_id="fixture_reader", request_id="new_activation", view_sequence=5
        ),
    )
    assert not reader.release_focus(cid, other["asset_id"], other["source_version"], current)
    future_focus = ReadingFocusInput(
        reader_client_id="fixture_reader",
        reader_epoch=newer.reader_epoch,
        request_id="focus_not_yet_reserved",
        focus_sequence=6,
        page_index=0,
    )
    assert reader.release_focus(cid, request.asset_id, request.source_version, future_focus)
    with pytest.raises(AppError):
        reader.focus(cid, request.asset_id, request.source_version, future_focus)
    # A sibling's already allocated newer activation still succeeds after a
    # release effect, regardless of React child/effect ordering.
    reader.begin(
        cid,
        other["asset_id"],
        other["source_version"],
        ReadingOpenInput(
            reader_client_id="fixture_reader", request_id="sibling_activation", view_sequence=7
        ),
    )


async def test_ocr_commit_failure_reuses_private_checkpoint_only_after_explicit_retry(tmp_path):
    if not OcrResources(PROJECT_ROOT).prepared():
        pytest.skip("Fixed local OCR resources must be explicitly prepared")
    repo, cid, _root, request = setup(tmp_path, scan=True, count=1)
    service = PreparationService(repo, PROJECT_ROOT)
    fired = False

    def fail_commit(name, _detail):
        nonlocal fired
        if name == "preparation_before_commit" and not fired:
            fired = True
            raise OSError("fixture power loss before OCR transaction commit")

    repo.fault_hook = fail_commit
    job = service.create_pdf(cid, request)
    await service.open()
    try:
        async with asyncio.timeout(40):
            while service.store.get(cid, job.job_id).state in {"queued", "running"}:
                await asyncio.sleep(0.1)
        failed = service.store.get(cid, job.job_id)
        assert failed.state == "failed" and fired
        with repo.engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(s.document_ocr)).scalar_one() == 0
        evidence = repo.root / "preparation-evidence"
        assert len(list(evidence.rglob("*.result.json"))) == 1
        service.store.control(
            cid,
            job.job_id,
            "retry",
            PreparationControl(
                operation_id="explicit_commit_retry", expected_revision=failed.revision
            ),
        )
        service.wake()
        async with asyncio.timeout(20):
            while service.store.get(cid, job.job_id).state in {"queued", "running"}:
                await asyncio.sleep(0.1)
        assert service.store.get(cid, job.job_id).state == "completed"
        assert len(list(evidence.rglob("*.jsonl"))) == 1
        index = DocumentReader(repo).ocr_index(cid, request.asset_id, request.source_version)
        layer = DocumentReader(repo).ocr_layer(cid, request.asset_id, request.source_version, 0)
        assert index.pages[0].revision == layer.revision and index.next_page is None
        assert all(len(item.text) == 1 for item in layer.words)
        assert "LEARNING WORKBENCH" in layer.text
        assert (
            DocumentReader(repo).ocr_index(cid, request.asset_id, request.source_version, 0).pages
            == []
        )
        assert repo.counts()["generations"] == 0
    finally:
        await service.close()
        repo.close()


async def test_formal_local_ocr_persists_real_char_boxes_and_exact_selection(tmp_path):
    resources = OcrResources(PROJECT_ROOT)
    if not resources.prepared():
        pytest.skip("Fixed local OCR resources must be explicitly prepared")
    repo, cid, root, request = setup(tmp_path, scan=True, count=1)
    original = hashlib.sha256((root / "lesson.pdf").read_bytes()).hexdigest()
    reader = DocumentReader(repo)
    before = reader.page(cid, request.asset_id, request.source_version, 0)
    assert before.text_status == "needs_ocr" and before.text == ""
    service = PreparationService(repo, PROJECT_ROOT)
    job = service.create_pdf(cid, request.model_copy(update={"priority_unit": 0}))
    await service.open()
    try:
        async with asyncio.timeout(40):
            while service.store.get(cid, job.job_id).state in {"queued", "running"}:
                await asyncio.sleep(0.1)
        completed = service.store.get(cid, job.job_id)
        assert completed.state == "completed", completed.model_dump()
        assert completed.units[0].result_digest
        page = reader.page(cid, request.asset_id, request.source_version, 0)
        assert page.text_status == "ocr" and "LEARNING WORKBENCH" in page.text
        assert page.parser_version.startswith("ocr:")
        text = "LEARNING"
        start = page.text.index(text)
        glyphs = [g for g in page.glyphs if start <= g.start < start + len(text)]
        assert len(glyphs) == len(text) and all(g.region for g in glyphs)
        region = PageRegion(
            x0=min(g.region.x0 for g in glyphs),
            y0=min(g.region.y0 for g in glyphs),
            x1=max(g.region.x1 for g in glyphs),
            y1=max(g.region.y1 for g in glyphs),
        )
        captured = reader.capture(
            cid,
            PdfSelectionInput(
                asset_id=request.asset_id,
                source_version=request.source_version,
                text=text,
                pages=[PdfSelectionPage(page_index=0, text=text, regions=[region])],
            ),
        )
        assert captured.text == text and "WORKBENCH" not in captured.text
        assert captured.reference.parts[0].parser_version == page.parser_version
        replay = service.create_pdf(cid, request)
        assert replay.job_id == job.job_id and replay.state == "completed"
        results = list((repo.root / "preparation-evidence").rglob("*.result.json"))
        assert len(results) == 1
        assert hashlib.sha256((root / "lesson.pdf").read_bytes()).hexdigest() == original
        assert repo.counts()["generations"] == 0
    finally:
        await service.close()
        repo.close()


async def test_active_ocr_pause_stops_owned_worker_and_late_result_cannot_publish(
    tmp_path, monkeypatch
):
    if not OcrResources(PROJECT_ROOT).prepared():
        pytest.skip("Fixed local OCR resources must be explicitly prepared")
    repo, cid, _root, request = setup(tmp_path, scan=True, count=1, dense=True)
    service = PreparationService(repo, PROJECT_ROOT)
    recognized = asyncio.Event()
    loop = asyncio.get_running_loop()
    progress = service.store.progress

    def observe(lease, value):
        saved = progress(lease, value)
        if value.get("status") == "recognizing text" and 0 < (value.get("progress") or 0) < 1:
            loop.call_soon_threadsafe(recognized.set)
        return saved

    monkeypatch.setattr(service.store, "progress", observe)
    job = service.create_pdf(cid, request.model_copy(update={"priority_unit": 0}))
    await service.open()
    try:
        await asyncio.wait_for(recognized.wait(), timeout=30)
        current = service.store.get(cid, job.job_id)
        assert current.state == "running"
        paused = service.store.control(
            cid,
            job.job_id,
            "pause",
            PreparationControl(operation_id="active_pause", expected_revision=current.revision),
        )
        service.interrupt(job.job_id)
        async with asyncio.timeout(20):
            while service._active is not None:
                await asyncio.sleep(0.05)
        assert service.store.get(cid, job.job_id).state == "paused"
        assert paused.epoch > current.epoch and paused.units[0].state == "pending"
        with repo.engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(s.document_ocr)).scalar_one() == 0
        records = [
            json.loads(line)
            for path in (repo.root / "preparation-evidence").rglob("*.jsonl")
            for line in path.read_text("utf-8").splitlines()
        ]
        assert any(
            row.get("status") == "recognizing text" and (row.get("progress") or 0) > 0
            for row in records
        )
        assert not any(row.get("type") == "result" for row in records)
        assert repo.counts()["generations"] == 0
    finally:
        await service.close()
        repo.close()


def test_queued_preparation_is_in_exit_guard_and_exit_freezes_future_admission(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(OcrResources, "prepared", lambda self: True)
    repo, cid, _root, request = setup(tmp_path)
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run")
    application = create_app(config, repository=repo)
    with TestClient(application, base_url="http://127.0.0.1:8765") as client:
        application.state.preparations.is_busy = lambda: True
        headers = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        created = client.post(
            f"/api/v1/courses/{cid}/learning/preparations",
            headers=headers,
            json=request.model_dump(mode="json"),
        )
        assert created.status_code == 200
        identity = created.json()["job_id"]
        status = client.get("/api/v1/runtime").json()
        assert status["active_preparations"][0]["job_id"] == identity
        rejected = client.post(
            "/api/v1/runtime/prepare-exit",
            headers=headers,
            json={"instance_id": config.instance_id},
        )
        assert rejected.status_code == 409 and not application.state.exit_prepared
        allowed = client.post(
            "/api/v1/runtime/prepare-exit",
            headers=headers,
            json={"instance_id": config.instance_id, "confirm_active": True},
        )
        assert allowed.status_code == 200 and allowed.json()["active_preparations"] == []
        assert PreparationStore(repo).get(cid, identity).state == "paused"
        late = client.post(
            f"/api/v1/courses/{cid}/learning/preparations",
            headers=headers,
            json=request.model_dump(mode="json"),
        )
        assert late.status_code == 409 and late.json()["code"] == "APPLICATION_EXIT_PREPARED"
        resume = client.post(
            f"/api/v1/courses/{cid}/learning/preparations/{identity}/resume",
            headers=headers,
            json={
                "operation_id": "late_resume",
                "expected_revision": PreparationStore(repo).get(cid, identity).revision,
            },
        )
        assert resume.status_code == 409
