"""Fixed real local ASR + real native Host; synthetic source and provider SSE."""

import asyncio
import hashlib
import io
import json
import runpy
import wave

import pytest
from sqlalchemy import select
from test_course_corpus_host import ROOT, ConceptFixture

from learning_app.config import AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.boundary import MessagesBoundary
from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
from learning_app.processes import OwnedProcessGroup
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.workbench import corpus_schema as c
from learning_app.workbench import schema as s
from learning_app.workbench.asr_resources import AsrResources
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.processing import PreparationService
from learning_app.workbench.processing_models import PreparationControl, PreparationCreate


def wav_bytes(pcm):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(pcm)
    return output.getvalue()


def speech_timeline(directory):
    base = directory / "original-speech.wav"
    runpy.run_path(str(ROOT / "tests/fixtures/workbench/generate.py"))["synthetic_speech"](base)
    with wave.open(str(base), "rb") as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (
            1,
            2,
            16000,
        )
        speech = reader.readframes(reader.getnframes())
    assert len(speech) < 30 * 32000
    timeline = bytearray(180 * 32000)
    for second in (0, 60, 140):
        begin = second * 32000
        timeline[begin : begin + len(speech)] = speech
    return bytes(timeline)


@pytest.mark.host
async def test_actual_asr_fills_only_question_grids_while_full_batch_stays_paused(tmp_path):
    resources = AsrResources(ROOT)
    assert resources.prepared(), "fixed ASR runtime must be prepared for this explicit engine test"
    pcm = speech_timeline(tmp_path)
    material = tmp_path / "course"
    material.mkdir()
    audio = material / "synthetic-timeline.wav"
    audio.write_bytes(wav_bytes(pcm))
    original = hashlib.sha256(audio.read_bytes()).hexdigest()
    repo = Repository(tmp_path / "data")
    course = CourseFileStore(repo).bind(None, str(material))["course_id"]
    source = repo.register_source(course, audio)
    repo.create_chat(course, "asr_question")
    registry = NativeCallRegistry(tmp_path / "native-facts.jsonl")
    boundary = ConceptFixture(
        tmp_path / "boundary", repo, registry, query=("read_nearby_subtitles", {"anchor_id": None})
    )
    adapter = ExecutionAdapter(
        ROOT,
        tmp_path / "host",
        boundary=boundary,
        native_tools=NativeToolTransport(registry, NativeBusiness(repo)),
    )
    record = {
        "layer": "actual_local_asr_and_native_host_with_synthetic_provider",
        "provider_attempts": 0,
        "asr_manifest": resources.manifest_hash,
        "input_sha256": original,
        "status": "not_run",
    }
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        preparations = PreparationService(
            repo, ROOT, is_busy=lambda: any(not task.done() for task in service.tasks.values())
        )
        batch = preparations.create_asr(
            course,
            PreparationCreate(
                kind="asr",
                asset_id=source["asset_id"],
                source_version=source["source_version"],
                language="en",
            ),
        )
        paused = preparations.store.control(
            course,
            batch.job_id,
            "pause",
            PreparationControl(operation_id="manual_batch_pause", expected_revision=batch.revision),
        )
        await preparations.open()
        try:
            submitted = await service.submit(
                {
                    "chat_id": "asr_question",
                    "submission_id": "missing_nearby",
                    "text": "按需读取本次时间附近字幕",
                    "model": "deepseek-flash",
                    "thinking": "high",
                    "question_video": {
                        "asset_id": source["asset_id"],
                        "source_version": source["source_version"],
                        "time_ms": 75000,
                    },
                }
            )
            await asyncio.wait_for(service.tasks[submitted["generation_id"]], 65)
            generation = repo.get_generation(submitted["generation_id"])
            record["generation"] = generation
            with repo.engine.connect() as conn:
                receipt = conn.execute(
                    select(c.query_receipts.c.result).where(
                        c.query_receipts.c.run_id == submitted["generation_id"]
                    )
                ).scalar_one()
                units = list(conn.execute(select(s.transcript_units)).mappings())
            record["receipt"] = receipt
            record["units"] = [dict(unit) for unit in units]
            assert generation["state"] == "completed", generation
            assert receipt["status"] == "matched" and not receipt["missing_units"], receipt
            assert "synthetic" in " ".join(
                item["source"]["text"].lower() for item in receipt["results"]
            )
            assert {unit["unit_index"] for unit in units} == {1, 2, 3}
            for unit in units:
                index, payload = unit["unit_index"], unit["payload"]
                metadata = payload["audio"]
                expected = wav_bytes(pcm[index * 30 * 32000 : (index + 1) * 30 * 32000])
                assert metadata["requested_start_ms"] == index * 30000
                assert metadata["requested_end_ms"] == (index + 1) * 30000
                assert metadata["wav_sha256"] == hashlib.sha256(expected).hexdigest()
                assert metadata["image_frames_requested"] == 0
                assert payload["engine_invocations"] == 1
            assert preparations.store.get(course, batch.job_id) == paused
            assert len(boundary.requests) == 4 and boundary.actual_outbound_attempts == 0
            assert submitted["generation_id"] in registry.released
            assert hashlib.sha256(audio.read_bytes()).hexdigest() == original
            record["status"] = "passed"
        finally:
            record["paused_batch_after"] = preparations.store.get(course, batch.job_id).model_dump(
                mode="json"
            )
            await preparations.close()
            await service.close()
    record["host_cleanup"] = adapter.host.cleanup
    (tmp_path / "asr-native-evidence.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
    )
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()


@pytest.mark.host
async def test_stop_during_actual_asr_wait_keeps_host_alive_and_allows_same_chat_followup(
    tmp_path, monkeypatch
):
    assert AsrResources(ROOT).prepared()
    pcm = speech_timeline(tmp_path)
    material = tmp_path / "course"
    material.mkdir()
    audio = material / "cancel-timeline.wav"
    audio.write_bytes(wav_bytes(pcm))
    repo = Repository(tmp_path / "data")
    course = CourseFileStore(repo).bind(None, str(material))["course_id"]
    source = repo.register_source(course, audio)
    repo.create_chat(course, "cancel_asr_question")
    registry = NativeCallRegistry(tmp_path / "native-facts.jsonl")

    class CancelFixture(ConceptFixture):
        followup = False

        def _fixture(self, handler, request, scenario, row, observer):
            if self.followup:
                self.requests.append(request)
                return MessagesBoundary._fixture(
                    self, handler, request, {"text_chunks": ["停止后原对话继续"]}, row, observer
                )
            return super()._fixture(handler, request, scenario, row, observer)

    boundary = CancelFixture(
        tmp_path / "boundary", repo, registry, query=("read_nearby_subtitles", {"anchor_id": None})
    )
    spawned = []
    spawn = OwnedProcessGroup.spawn

    def observe_spawn(group, arguments, **kwargs):
        process = spawn(group, arguments, **kwargs)
        if str(arguments[-1]).endswith("asr_worker.py"):
            spawned.append(process)
        return process

    monkeypatch.setattr(OwnedProcessGroup, "spawn", observe_spawn)
    adapter = ExecutionAdapter(
        ROOT,
        tmp_path / "host",
        boundary=boundary,
        native_tools=NativeToolTransport(registry, NativeBusiness(repo)),
    )
    record = {"layer": "actual_asr_process_active_cancel_native_host", "status": "not_run"}
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        preparations = PreparationService(
            repo, ROOT, is_busy=lambda: any(not task.done() for task in service.tasks.values())
        )
        batch = preparations.create_asr(
            course,
            PreparationCreate(
                kind="asr",
                asset_id=source["asset_id"],
                source_version=source["source_version"],
                language="en",
            ),
        )
        paused = preparations.store.control(
            course,
            batch.job_id,
            "pause",
            PreparationControl(operation_id="pause_full", expected_revision=batch.revision),
        )
        await preparations.open()
        try:
            submitted = await service.submit(
                {
                    "chat_id": "cancel_asr_question",
                    "submission_id": "cancel_in_asr",
                    "text": "读取本次附近字幕",
                    "model": "deepseek-flash",
                    "thinking": "high",
                    "question_video": {
                        "asset_id": source["asset_id"],
                        "source_version": source["source_version"],
                        "time_ms": 75000,
                    },
                }
            )
            generation_id = submitted["generation_id"]
            async with asyncio.timeout(30):
                while True:
                    lease = preparations._active
                    if (
                        lease
                        and lease["unit_index"] == 3
                        and spawned
                        and spawned[-1].poll() is None
                    ):
                        log = (
                            repo.root
                            / "preparation-evidence"
                            / lease["job_id"]
                            / "3"
                            / f"{lease['lease_id']}.asr.jsonl"
                        )
                        if log.is_file() and '"type": "ready"' in log.read_text("utf-8"):
                            break
                    assert not service.tasks[generation_id].done(), repo.get_generation(
                        generation_id
                    )
                    await asyncio.sleep(0.01)
            record["active_lease"] = lease
            record["active_asr_pid"] = spawned[-1].pid
            assert not service.tasks[generation_id].done()
            assert spawned[-1].poll() is None
            await service.cancel(generation_id)
            await asyncio.wait_for(service.tasks[generation_id], 15)
            async with asyncio.timeout(10):
                while preparations._active is not None or any(
                    process.poll() is None for process in spawned
                ):
                    await asyncio.sleep(0.02)
            # A tool's local cancellation acknowledgment can follow the native
            # terminal. Reconciliation reads original facts, without replay.
            if generation_id not in registry.released:
                await service.reconcile(generation_id, resume_observer=False)
            first = repo.get_generation(generation_id)
            record["cancelled_generation"] = first
            assert first["state"] == "cancelled", first
            assert generation_id in registry.released
            assert adapter.host.process.poll() is None
            assert preparations.store.get(course, batch.job_id) == paused
            assert not preparations.subtitle_waits.has_active()
            assert len(boundary.requests) == 2
            boundary.followup = True
            follow = await service.submit(
                {
                    "chat_id": "cancel_asr_question",
                    "submission_id": "follow_cancelled_asr",
                    "text": "继续原对话",
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[follow["generation_id"]], 20)
            second = repo.get_generation(follow["generation_id"])
            assert second["state"] == "completed", second
            assert second["binding_id"] == first["binding_id"]
            assert len(boundary.requests) == 3 and boundary.actual_outbound_attempts == 0
            record.update(
                status="passed",
                followup=second,
                asr_exit_codes=[process.poll() for process in spawned],
            )
        finally:
            await preparations.close()
            await service.close()
    record["host_cleanup"] = adapter.host.cleanup
    (tmp_path / "cancel-asr-evidence.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
    )
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
