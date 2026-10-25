"""Authenticated user caption operations. The model uses separate narrow grants."""

import asyncio

from fastapi import FastAPI, Query

from learning_app.errors import AppError

from .audio_sources import AudioSources
from .common import new_id
from .focus import PreparationFocus
from .models import ReadingOpenInput, SourceSnapshot
from .preparations import PreparationStore
from .processing_models import (
    AudioFocusInput,
    AudioFocusState,
    CurrentPagePreparation,
    PreparationCreate,
)
from .transcript_models import (
    SubtitleCapture,
    TranscriptEdit,
    TranscriptEditReceipt,
    TranscriptView,
)
from .transcripts import WINDOW_MS, TranscriptStore


def install_audio_routes(app: FastAPI):
    prefix = "/api/v1/courses/{course_id}/learning"
    audio = prefix + "/audio/{asset_id}/{source_version}"

    @app.post(audio + "/focus", response_model=AudioFocusState)
    async def open_audio_focus(
        course_id: str, asset_id: str, source_version: str, value: ReadingOpenInput
    ):
        AudioSources(app.state.repository).associated(course_id, asset_id, source_version)
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未接管音轨处理")
            with app.state.repository._lock, app.state.repository.engine.begin() as conn:
                prior = PreparationFocus.row(conn, value.reader_client_id)
                same = (
                    prior
                    and prior["view_sequence"] == value.view_sequence
                    and prior["request_id"] == value.request_id
                )
                epoch = prior["reader_epoch"] if same else new_id("audio_reader")
                PreparationFocus.claim(
                    conn,
                    course_id,
                    asset_id,
                    source_version,
                    value.reader_client_id,
                    epoch,
                    value.view_sequence,
                    value.request_id,
                )
        return AudioFocusState(asset_id=asset_id, source_version=source_version, reader_epoch=epoch)

    @app.post(audio + "/focus/release")
    async def release_audio_focus(
        course_id: str, asset_id: str, source_version: str, value: AudioFocusInput
    ):
        async with app.state.submission_gate:
            with app.state.repository._lock, app.state.repository.engine.begin() as conn:
                released = PreparationFocus.release(
                    conn, course_id, asset_id, source_version, value
                )
        return {"released": released}

    @app.post(audio + "/prepare-current", response_model=CurrentPagePreparation)
    async def prepare_audio_current(
        course_id: str, asset_id: str, source_version: str, value: AudioFocusInput
    ):
        store = PreparationStore(app.state.repository)
        AudioSources(app.state.repository).associated(course_id, asset_id, source_version)
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未准备新的字幕任务")
            with app.state.repository._lock, app.state.repository.engine.begin() as conn:
                PreparationFocus.require_owner(
                    conn,
                    course_id,
                    asset_id,
                    source_version,
                    value.reader_client_id,
                    value.reader_epoch,
                )
                PreparationFocus.claim(
                    conn,
                    course_id,
                    asset_id,
                    source_version,
                    value.reader_client_id,
                    value.reader_epoch,
                    value.focus_sequence,
                    value.request_id,
                )
        grid = value.position_ms // WINDOW_MS
        request = PreparationCreate(
            kind="asr", asset_id=asset_id, source_version=source_version, priority_unit=grid
        )
        definition = await asyncio.to_thread(
            store.define_asr, course_id, request, app.state.config.project_root
        )
        single = {
            "payload": {
                **definition["payload"],
                "scope": "unit",
                "parameters": {**definition["payload"]["parameters"], "requested_unit_index": grid},
            },
            "priority_unit": grid + 1,
        }
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "后台正在退出，未启动字幕任务")
            with app.state.repository._lock:
                with app.state.repository.engine.connect() as conn:
                    PreparationFocus.assert_current(
                        conn, course_id, asset_id, source_version, value
                    )
                batch = store.publish_pdf(definition, summary=True)
                current = (
                    store.publish_pdf(single, summary=True)
                    if batch.state in {"paused", "failed", "needs_source", "cancelled"}
                    else batch
                )
        app.state.preparations.wake()
        return CurrentPagePreparation(batch=batch, current=current)

    @app.get(audio + "/transcripts", response_model=list[str])
    def list_tracks(course_id: str, asset_id: str, source_version: str):
        return TranscriptStore(app.state.repository).list(course_id, asset_id, source_version)

    @app.get(prefix + "/transcripts/{track_id}", response_model=TranscriptView)
    def transcript(
        course_id: str,
        track_id: str,
        first_unit: int = Query(default=0, ge=0),
        limit: int = Query(default=8, ge=1, le=16),
    ):
        return TranscriptStore(app.state.repository).view(
            course_id, track_id, first_unit=first_unit, limit=limit
        )

    @app.post(prefix + "/transcripts/{track_id}/edit", response_model=TranscriptEditReceipt)
    def edit_transcript(course_id: str, track_id: str, value: TranscriptEdit):
        return TranscriptStore(app.state.repository).edit(course_id, track_id, value)

    @app.get(
        prefix + "/transcripts/{track_id}/operations/{operation_id}",
        response_model=TranscriptEditReceipt,
    )
    def transcript_operation(course_id: str, track_id: str, operation_id: str):
        return TranscriptStore(app.state.repository).operation(course_id, track_id, operation_id)

    @app.post(prefix + "/transcripts/{track_id}/selections", response_model=SourceSnapshot)
    def select_subtitle(course_id: str, track_id: str, value: SubtitleCapture):
        return TranscriptStore(app.state.repository).capture(course_id, track_id, value)
