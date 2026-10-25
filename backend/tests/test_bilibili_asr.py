"""Synthetic platform HTTP audio + the actually installed, pinned local ASR worker."""

import asyncio
import hashlib
import json
import runpy
import subprocess
import sys
import wave
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from test_integrations import FixtureProvider

from learning_app.config import PROJECT_ROOT
from learning_app.course_files import CourseFileStore
from learning_app.integrations.host import IntegrationHost
from learning_app.integrations.models import ImportSelection, ResolveInput
from learning_app.integrations.mux import av_runtime
from learning_app.storage import Repository
from learning_app.workbench import schema as s
from learning_app.workbench.asr_resources import AsrResources
from learning_app.workbench.processing import PreparationService
from learning_app.workbench.processing_models import PreparationControl
from learning_app.workbench.transcript_models import SubtitleChange, TranscriptEdit
from learning_app.workbench.transcripts import TranscriptStore


def require_idle_gpu():
    output = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    usage, memory = map(int, output.splitlines()[0].split(","))
    if usage > 10 or memory > 1500:
        pytest.skip("Keep the original worktree's active GPU workload undisturbed")


def synthetic_audio(directory):
    wav = directory / "synthetic-speech.wav"
    runpy.run_path(str(PROJECT_ROOT / "tests/fixtures/workbench/generate.py"))["synthetic_speech"](
        wav
    )
    with wave.open(str(wav)) as source:
        duration = round(source.getnframes() * 1000 / source.getframerate())
    m4a = directory / "synthetic-speech.m4a"
    # The fixture encoder uses the same hash-verified PyAV wheel as the product muxer.
    code = """import sys
sys.path.insert(0, sys.argv[1])
import av
with av.open(sys.argv[2]) as source, av.open(sys.argv[3], 'w') as target:
    stream = target.add_stream('aac', rate=16000)
    stream.layout = 'mono'
    resampler = av.AudioResampler(format='fltp', layout='mono', rate=16000)
    for decoded in source.decode(audio=0):
        for frame in resampler.resample(decoded):
            for packet in stream.encode(frame):
                target.mux(packet)
    for packet in stream.encode(None):
        target.mux(packet)
"""
    subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(av_runtime(PROJECT_ROOT)), str(wav), str(m4a)],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    return m4a.read_bytes(), duration


async def test_platform_absent_audio_download_to_actual_local_asr_and_restart(
    tmp_path, monkeypatch
):
    if not AsrResources(PROJECT_ROOT).prepared():
        pytest.skip("The fixed local ASR resources must be explicitly prepared")
    require_idle_gpu()
    media_bytes, duration = synthetic_audio(tmp_path)
    calls = []

    class FixtureAudioProvider(FixtureProvider):
        def headers(self, grant, url):
            self.access.check(grant)
            return {"User-Agent": "Learning-synthetic-audio-fixture"}

        def resolve(self, grant, value):
            result = super().resolve(grant, value)
            for part in result["parts"]:
                part["duration_ms"] = duration
            return result

        def subtitle(self, grant, part):
            self.access.check(grant)
            calls.append("confirmed_absent")
            return {"state": "absent", "tracks": []}

    def audio_http(request):
        assert str(request.url) == "https://example.bilivideo.com/audio"
        assert request.method == "GET" and "Cookie" not in request.headers
        calls.append("audio_http")
        return httpx.Response(
            200, content=media_bytes, headers={"Content-Length": str(len(media_bytes))}
        )

    client_type = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: client_type(
            *args, transport=httpx.MockTransport(audio_http), **kwargs
        ),
    )
    root = tmp_path / "course"
    root.mkdir()
    repo = Repository(tmp_path / "data")
    host = IntegrationHost(repo, PROJECT_ROOT, provider_factory=FixtureAudioProvider)
    repo.integrations = host
    course = CourseFileStore(repo).bind(None, str(root))["course_id"]
    host.access.change("bilibili", True)
    account = host.access.connect().connection_id
    plan = host.resolve(course, ResolveInput(connection_id=account, url="BV1234567890"))
    item = host.import_selected(
        course,
        ImportSelection(
            operation_id="only_first_part",
            plan_id=plan.plan_id,
            object_ids=[plan.parts[0].object_id],
        ),
    ).items[0]
    service = PreparationService(repo, PROJECT_ROOT)
    job = host.queue(course, item.asset_id, item.source_version, "platform_subtitles")
    paused = service.store.control(
        course,
        job.job_id,
        "pause",
        PreparationControl(operation_id="pause_before_dispatch", expected_revision=job.revision),
    )
    assert service.store.claim() is None and calls == []
    service.store.control(
        course,
        job.job_id,
        "resume",
        PreparationControl(operation_id="explicit_resume", expected_revision=paused.revision),
    )
    require_idle_gpu()
    await service.open()
    try:
        async with asyncio.timeout(150):
            while service.store.active():
                if service._worker.done():
                    service._worker.result()
                await asyncio.sleep(0.1)
        final = service.store.get(course, job.job_id)
        assert final.state == "completed", final.model_dump()
        tracks = TranscriptStore(repo)
        ids = tracks.list(course, item.asset_id, item.source_version)
        assert len(ids) == 1
        view = tracks.view(course, ids[0])
        assert view.source_type == "local_asr" and view.segments
        assert "synthetic" in " ".join(segment.text.lower() for segment in view.segments)
        row = host.source(item.asset_id)
        assert row["local_path"] is None and row["audio_path"]
        audio_hash = hashlib.sha256(Path(row["audio_path"]).read_bytes()).hexdigest()
        assert audio_hash == row["audio_sha256"]
        with repo.engine.connect() as conn:
            raw = conn.execute(select(s.transcript_units)).mappings().one()
        assert raw["payload"]["audio"]["image_frames_requested"] == 0
        assert raw["payload"]["audio"]["source"]["sha256"] == audio_hash
        assert raw["payload"]["source_digest"] != audio_hash
        segment = view.segments[0]
        tracks.edit(
            course,
            view.track_id,
            TranscriptEdit(
                operation_id="keep_user_revision",
                expected_edit_head_id=None,
                changes=[
                    SubtitleChange(
                        segment_id=segment.segment_id,
                        raw_revision_id=segment.raw_revision_id,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        text="合成音频的手动更正",
                    )
                ],
            ),
        )
        assert calls == ["confirmed_absent", "audio_http"]
        summary = {
            "synthetic_platform": True,
            "actual_local_asr": True,
            "duration_ms": duration,
            "download_bytes": len(media_bytes),
            "platform_video_downloads": 0,
            "calls": calls.copy(),
            "track_source_type": view.source_type,
            "segments": len(view.segments),
            "image_frames_requested": 0,
        }
    finally:
        try:
            await service.close()
        finally:
            repo.close()
    repo = Repository(tmp_path / "data")
    try:
        host = IntegrationHost(repo, PROJECT_ROOT, provider_factory=FixtureAudioProvider)
        repo.integrations = host
        restored = host.queue(course, item.asset_id, item.source_version, "platform_subtitles")
        assert restored.job_id == job.job_id and restored.state == "completed"
        restored_view = TranscriptStore(repo).view(course, ids[0])
        assert restored_view.segments[0].text == "合成音频的手动更正"
        assert restored_view.source_version == item.source_version
        assert calls == summary["calls"]
        summary.update(restart_reused=True, manual_edit_preserved=True)
        (tmp_path / "result.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), "utf-8"
        )
    finally:
        repo.close()
