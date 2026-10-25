"""Actual installed local ASR and immutable original/user transcript transactions."""

import asyncio
import hashlib
import json
import runpy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from learning_app.app import create_app
from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.storage import Repository
from learning_app.workbench import schema as s
from learning_app.workbench.asr_resources import AsrResources
from learning_app.workbench.models import ReadingOpenInput
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.processing import PreparationService
from learning_app.workbench.processing_models import PreparationCreate
from learning_app.workbench.reading import DocumentReader
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.transcript_models import SubtitleCapture, SubtitleChange, TranscriptEdit
from learning_app.workbench.transcripts import TranscriptStore


async def test_installed_asr_queue_preserves_raw_seconds_and_separate_user_revisions(tmp_path):
    if not AsrResources(PROJECT_ROOT).prepared():
        pytest.skip("The fixed local ASR runtime/model must be explicitly prepared")
    material = tmp_path / "materials"
    material.mkdir()
    speech = material / "synthetic-speech.wav"
    runpy.run_path(str(PROJECT_ROOT / "tests/fixtures/workbench/generate.py"))["synthetic_speech"](
        speech
    )
    original = hashlib.sha256(speech.read_bytes()).hexdigest()
    repo = Repository(tmp_path / "data")
    files = CourseFileStore(repo)
    cid = files.bind(None, str(material))["course_id"]
    opened = files.open(cid, speech.name)
    service = PreparationService(repo, PROJECT_ROOT)
    failed_commit = False

    def fail_raw_commit(name, detail):
        nonlocal failed_commit
        if (
            name == "preparation_before_commit"
            and detail.get("unit_index") == 1
            and not failed_commit
        ):
            failed_commit = True
            raise OSError("fixture raw transcript commit interrupted")

    repo.fault_hook = fail_raw_commit
    request = PreparationCreate(
        kind="asr",
        asset_id=opened["asset_id"],
        source_version=opened["source_version"],
        language="en",
        priority_unit=0,
    )
    job = service.create_asr(cid, request)
    assert len(job.units) == 1 and job.units[0].unit_index == 0
    track_id = job.parameters["track_id"]
    store = TranscriptStore(repo)
    assert store.view(cid, track_id).units_total is None
    await service.open()
    try:
        async with asyncio.timeout(120):
            while service.store.get(cid, job.job_id).state in {"queued", "running"}:
                await asyncio.sleep(0.1)
        final = service.store.get(cid, job.job_id)
        assert final.state == "failed" and failed_commit
        with repo.engine.connect() as conn:
            assert conn.execute(select(s.transcript_units.c.revision_id)).first() is None
        repo.fault_hook = None
        from learning_app.workbench.processing_models import PreparationControl

        service.store.control(
            cid,
            job.job_id,
            "retry",
            PreparationControl(
                operation_id="explicit_raw_commit_retry", expected_revision=final.revision
            ),
        )
        service.wake()
        async with asyncio.timeout(30):
            while service.store.get(cid, job.job_id).state in {"queued", "running"}:
                await asyncio.sleep(0.1)
        final = service.store.get(cid, job.job_id)
        assert final.state == "completed", final.model_dump()
        assert len(list((repo.root / "preparation-evidence").rglob("*.asr.jsonl"))) == 1
        assert len(final.units) >= 2 and all(unit.state == "completed" for unit in final.units)
        view = store.view(cid, track_id)
        assert view.units_total == 1 and view.units_ready == [0] and view.missing_units == []
        assert view.segments and "synthetic" in " ".join(seg.text.lower() for seg in view.segments)
        with repo.engine.connect() as conn:
            raw = (
                conn.execute(
                    select(s.transcript_units).where(s.transcript_units.c.track_id == track_id)
                )
                .mappings()
                .one()
            )
        frozen = json.dumps(raw["payload"], sort_keys=True)
        items = raw["payload"]["raw"]["segments"]
        assert all("raw_start_seconds" in item and "raw_end_seconds" in item for item in items)
        assert all("raw_start_seconds" in word for item in items for word in item["words"])
        assert raw["payload"]["audio"]["image_frames_requested"] == 0
        segment = view.segments[0]
        capture = SubtitleCapture(segment_id=segment.segment_id, revision_id=segment.revision_id)
        selected = store.capture(cid, track_id, capture)
        assert selected.text == segment.text and selected.content_role == "original"
        SourceStore(repo).require_current(selected)
        from workbench_support import settings

        from learning_app.workbench.task_coverage import TaskCoverage
        from learning_app.workbench.task_planning import TaskPlanner
        from learning_app.workbench.task_scope_models import TaskAdmission

        whole_task = TaskCoverage(repo).admit(
            cid,
            TaskAdmission(
                operation_id="explicit_whole_transcript",
                kind="handout",
                instruction="完整整理这段音轨的有效字幕",
                coverage_mode="complete",
                sources=[
                    {
                        "kind": "transcript",
                        "asset_id": opened["asset_id"],
                        "source_version": opened["source_version"],
                        "track_id": track_id,
                    }
                ],
                target={"mode": "create", "title": "整段字幕讲义"},
                settings=settings(),
                sources_only=True,
            ),
        )
        assert (
            TaskCoverage(repo).get(cid, whole_task.task_id).sources[0].expected_leaf_count is None
        )
        TaskCoverage(repo).refresh(cid, whole_task.task_id)
        TaskPlanner(repo).plan(cid, whole_task.task_id)
        frozen_scope = TaskCoverage(repo).get(cid, whole_task.task_id)
        assert frozen_scope.sources[0].expected_leaf_count == view.units_total == 1
        assert all(leaf.state == "ready" for leaf in frozen_scope.leaves)
        assert selected.snapshot_id in {
            identity for leaf in frozen_scope.leaves for identity in leaf.snapshot_ids
        }
        change = SubtitleChange(
            segment_id=segment.segment_id,
            raw_revision_id=segment.raw_revision_id,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            text="用户明确更正的字幕",
        )
        operation = TranscriptEdit(
            operation_id="manual_edit", expected_edit_head_id=None, changes=[change]
        )
        receipt = store.edit(cid, track_id, operation)
        assert receipt.disposition == "published"
        edited = store.view(cid, track_id).segments[0]
        assert (
            edited.text == change.text
            and edited.raw_text == segment.text
            and edited.origin == "user_note"
        )
        conflict = store.edit(
            cid,
            track_id,
            operation.model_copy(
                update={
                    "operation_id": "other_editor",
                    "changes": [change.model_copy(update={"text": "并发保留稿"})],
                }
            ),
        )
        assert (
            conflict.disposition == "conflict" and conflict.head_revision_id == receipt.revision_id
        )
        assert store.edit(cid, track_id, operation) == receipt
        assert store.view(cid, track_id).segments[0].text == change.text
        user_snapshot = store.capture(
            cid,
            track_id,
            SubtitleCapture(segment_id=edited.segment_id, revision_id=edited.revision_id),
        )
        assert user_snapshot.content_role == "user_note" and user_snapshot.text == change.text
        # A later manual edit cannot redirect a previously frozen whole-source task.
        assert TaskCoverage(repo).get(cid, whole_task.task_id).leaves == frozen_scope.leaves
        SourceStore(repo).require_current(user_snapshot)
        assert store.capture(cid, track_id, capture).snapshot_id == selected.snapshot_id
        with repo.engine.connect() as conn:
            preserved = conn.execute(
                select(s.transcript_units.c.payload).where(
                    s.transcript_units.c.revision_id == raw["revision_id"]
                )
            ).scalar_one()
        assert json.dumps(preserved, sort_keys=True) == frozen
        # A repeated full preparation remains the same completed job.
        assert service.create_asr(cid, request).job_id == job.job_id
        assert (
            service.create_asr(cid, request.model_copy(update={"language": None})).job_id
            == job.job_id
        )
        assert store.view(cid, track_id).edit_head_id == receipt.revision_id
        assert repo.counts()["generations"] == 0
        assert hashlib.sha256(speech.read_bytes()).hexdigest() == original
    finally:
        await service.close()
        repo.close()


def test_audio_focus_is_shared_with_pdf_and_stale_audio_cannot_reclaim_priority(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(AsrResources, "prepared", lambda self: True)
    generators = runpy.run_path(str(PROJECT_ROOT / "tests/fixtures/workbench/generate.py"))
    material = tmp_path / "materials"
    material.mkdir()
    generators["reading_pdf"](material / "lesson.pdf", 2)
    import wave

    with wave.open(str(material / "lesson.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    repo = Repository(tmp_path / "data")
    files = CourseFileStore(repo)
    cid = files.bind(None, str(material))["course_id"]
    audio, pdf = files.open(cid, "lesson.wav"), files.open(cid, "lesson.pdf")
    app = create_app(
        AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run"), repository=repo
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        app.state.preparations.is_busy = lambda: True
        headers = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        endpoint = (
            f"/api/v1/courses/{cid}/learning/audio/{audio['asset_id']}/{audio['source_version']}"
        )
        opened = client.post(
            endpoint + "/focus",
            headers=headers,
            json={
                "reader_client_id": "shared_reader",
                "view_sequence": 1,
                "request_id": "open_audio",
            },
        )
        assert opened.status_code == 200
        value = dict(
            reader_client_id="shared_reader",
            reader_epoch=opened.json()["reader_epoch"],
            focus_sequence=2,
            request_id="focus_audio",
            position_ms=0,
        )
        response = client.post(endpoint + "/prepare-current", headers=headers, json=value)
        assert response.status_code == 200, response.text
        job = response.json()["batch"]["job_id"]
        current = DocumentReader(repo).begin(
            cid,
            pdf["asset_id"],
            pdf["source_version"],
            ReadingOpenInput(
                reader_client_id="shared_reader", view_sequence=3, request_id="open_pdf"
            ),
        )
        assert current.reader_epoch
        # A delayed audio prepare, even with a bigger client counter, cannot
        # borrow the epoch of an audio tab that is no longer the current source.
        stale = client.post(
            endpoint + "/prepare-current",
            headers=headers,
            json={**value, "focus_sequence": 4, "request_id": "late_audio"},
        )
        assert stale.status_code == 409 and stale.json()["code"] == "READING_VIEW_STALE"
        release = client.post(endpoint + "/focus/release", headers=headers, json=value)
        assert release.json() == {"released": False}
        view = client.get(f"/api/v1/courses/{cid}/learning/preparations/{job}").json()
        assert all(unit["priority"] == 10 for unit in view["units"])
        wrong = client.post(
            endpoint + "/focus",
            headers=headers,
            json={
                "reader_client_id": "shared_reader",
                "view_sequence": 2,
                "request_id": "late_open",
            },
        )
        assert wrong.status_code == 409
        assert repo.counts()["generations"] == 0


def test_current_interval_without_audio_does_not_fabricate_silence_or_complete_whole_track(
    tmp_path, monkeypatch
):
    import wave

    monkeypatch.setattr(AsrResources, "prepared", lambda self: True)
    material = tmp_path / "materials"
    material.mkdir()
    recording = material / "recording.wav"
    with wave.open(str(recording), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    repo = Repository(tmp_path / "data")
    files = CourseFileStore(repo)
    cid = files.bind(None, str(material))["course_id"]
    opened = files.open(cid, recording.name)
    request = PreparationCreate(
        kind="asr",
        asset_id=opened["asset_id"],
        source_version=opened["source_version"],
        scope="unit",
        unit_index=1,
    )
    store = PreparationStore(repo)
    job = store.publish_pdf(store.define_asr(cid, request, PROJECT_ROOT))
    lease = store.claim()
    assert lease["unit_index"] == 0
    assert store.finish(
        lease,
        {
            "kind": "audio_probe",
            "track_id": job.parameters["track_id"],
            "unit_index": 0,
            "source_digest": opened["source_version"],
            "probe": {
                "source": {
                    "asset_id": opened["asset_id"],
                    "source_version": opened["source_version"],
                    "bytes": recording.stat().st_size,
                    "sha256": opened["source_version"],
                },
                "audio_start_ms": 0,
                "audio_end_ms": 1000,
                "audio_start_seconds": "0",
                "audio_end_seconds": "1",
            },
        },
    )
    current = store.get(cid, job.job_id)
    assert current.state == "completed" and len(current.units) == 1
    transcript = TranscriptStore(repo).view(cid, job.parameters["track_id"])
    assert (
        transcript.units_total == 1
        and transcript.units_ready == []
        and transcript.missing_units == [0]
    )
    assert transcript.segments == []
    assert repo.counts()["generations"] == 0
