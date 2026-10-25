"""Persistent local preparation units. A lease cannot commit after pause or reuse."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import func, insert, select, update

from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.storage.repository import _hash

from . import schema as s
from .asr_resources import AsrResources
from .audio_sources import AudioSources
from .common import new_id, now
from .ocr_resources import OcrResources
from .processing_models import (
    PreparationControl,
    PreparationCreate,
    PreparationProgress,
    PreparationUnit,
    PreparationView,
)
from .reading import DocumentReader


class PreparationStore:
    def __init__(self, repository: Repository):
        self.repository = repository

    @staticmethod
    def _row(conn, course_id: str, job_id: str):
        row = (
            conn.execute(
                select(s.preparations).where(
                    s.preparations.c.course_id == course_id,
                    s.preparations.c.job_id == job_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("PREPARATION_NOT_FOUND", "指定课程的资料处理任务不存在", 404)
        return row

    @classmethod
    def _view(cls, conn, course_id: str, job_id: str) -> PreparationView:
        row = cls._row(conn, course_id, job_id)
        units = [
            PreparationUnit.model_validate({key: unit[key] for key in PreparationUnit.model_fields})
            for unit in conn.execute(
                select(s.preparation_units)
                .where(s.preparation_units.c.job_id == job_id)
                .order_by(s.preparation_units.c.unit_index)
            ).mappings()
        ]
        return PreparationView(**row, units=units)

    def get(self, course_id: str, job_id: str) -> PreparationView:
        with self.repository.engine.connect() as conn:
            return self._view(conn, course_id, job_id)

    def list(self, course_id: str) -> list[PreparationView]:
        with self.repository.engine.connect() as conn:
            ids = (
                conn.execute(
                    select(s.preparations.c.job_id)
                    .where(s.preparations.c.course_id == course_id)
                    .order_by(s.preparations.c.created_at.desc())
                )
                .scalars()
                .all()
            )
            return [self._view(conn, course_id, identity) for identity in ids]

    @staticmethod
    def _counts(conn, job_id: str) -> dict:
        return dict(
            conn.execute(
                select(s.preparation_units.c.state, func.count())
                .where(s.preparation_units.c.job_id == job_id)
                .group_by(s.preparation_units.c.state)
            ).all()
        )

    @classmethod
    def _summary(cls, conn, course_id: str, job_id: str) -> PreparationProgress:
        row = cls._row(conn, course_id, job_id)
        counts = cls._counts(conn, job_id)
        active = conn.execute(
            select(s.preparation_units.c.unit_index)
            .where(
                s.preparation_units.c.job_id == job_id,
                s.preparation_units.c.state == "running",
            )
            .limit(1)
        ).scalar_one_or_none()
        fields = {
            key: row[key]
            for key in (
                "job_id",
                "course_id",
                "asset_id",
                "source_version",
                "kind",
                "scope",
                "state",
                "revision",
                "updated_at",
            )
        }
        return PreparationProgress(
            **fields,
            units_total=sum(counts.values()),
            units_completed=counts.get("completed", 0),
            units_failed=counts.get("failed", 0),
            units_missing=counts.get("source_missing", 0),
            active_unit_index=active,
        )

    def summary(self, course_id: str, job_id: str) -> PreparationProgress:
        with self.repository.engine.connect() as conn:
            return self._summary(conn, course_id, job_id)

    @staticmethod
    def _event(conn, row, action: str, request: dict, *, operation_id=None, **changes):
        stamp, revision = now(), row["revision"] + 1
        conn.execute(
            update(s.preparations)
            .where(
                s.preparations.c.job_id == row["job_id"],
                s.preparations.c.revision == row["revision"],
            )
            .values(revision=revision, updated_at=stamp, **changes)
        )
        conn.execute(
            insert(s.preparation_events).values(
                job_id=row["job_id"],
                revision=revision,
                operation_id=operation_id,
                action=action,
                request_hash=_hash(request),
                created_at=stamp,
            )
        )
        return revision

    def create_pdf(self, course_id: str, value: PreparationCreate, project_root) -> PreparationView:
        return self.publish_pdf(self.define_pdf(course_id, value, project_root))

    def create_pdf_range(self, course_id, asset_id, source_version, first, last, project_root):
        """Internal task grant: local OCR only for the frozen declared PDF pages."""
        definition = self.define_pdf(
            course_id,
            PreparationCreate(
                kind="pdf_ocr",
                asset_id=asset_id,
                source_version=source_version,
                scope="unit",
                unit_index=first,
            ),
            project_root,
        )
        payload = definition["payload"]
        count = payload["parameters"]["page_count"]
        if not 0 <= first <= last < count or last - first + 1 > 20_000:
            raise AppError("SOURCE_PAGE_RANGE_INVALID", "OCR范围不在冻结文档页范围内", 422)
        indexes = list(range(first, last + 1))
        payload["parameters"]["scope_indexes"] = indexes
        payload["scope"] = (
            "full" if first == 0 and last == count - 1 else "unit" if first == last else "range"
        )
        definition["priority_unit"] = None
        return self.publish_pdf(definition)

    def define_source(self, course_id: str, value: PreparationCreate, project_root) -> dict:
        if value.kind == "asr":
            return self.define_asr(course_id, value, project_root)
        return self.define_pdf(course_id, value, project_root)

    def define_asr(self, course_id: str, value: PreparationCreate, project_root, *, explicit=False) -> dict:
        if not explicit:
            raise AppError("SUBTITLE_REQUEST_REQUIRED", "请在对话中明确要求处理字幕", 403)
        resources = AsrResources(project_root)
        if not resources.prepared():
            raise AppError("ASR_RESOURCES_NOT_PREPARED", "须先准备固定本地语音资源", 503)
        source = AudioSources(self.repository).current(
            course_id, value.asset_id, value.source_version
        )
        engine = {
            "manifest_sha256": resources.manifest_hash,
            "window_ms": 30_000,
            "decoder_version": "pyav171-audio-only-phase16k-v1",
            "parameters_version": "fw121-vad6-beam5-context0-temp0-raw-v2",
            "language": value.language,
            "track_ordinal": 0,
        }
        if value.language is None:
            # Background preparation follows the latest compatible explicit
            # track instead of making a fresh default track hide user edits.
            with self.repository.engine.connect() as conn:
                candidates = conn.execute(
                    select(s.transcript_tracks)
                    .where(
                        s.transcript_tracks.c.course_id == course_id,
                        s.transcript_tracks.c.asset_id == value.asset_id,
                        s.transcript_tracks.c.source_version == value.source_version,
                    )
                    .order_by(
                        s.transcript_tracks.c.created_at.desc(), s.transcript_tracks.c.track_id
                    )
                ).mappings()
                for candidate in candidates:
                    existing = candidate["parameters"]
                    if all(
                        existing.get(key) == part
                        for key, part in engine.items()
                        if key != "language"
                    ):
                        engine = existing
                        break
        track = "transcript_" + _hash(
            {
                "course_id": course_id,
                "asset_id": value.asset_id,
                "source_version": value.source_version,
                "source_digest": source["content_hash"],
                "engine": engine,
            }
        )
        payload = dict(
            course_id=course_id,
            asset_id=value.asset_id,
            source_version=value.source_version,
            source_digest=source["content_hash"],
            kind="asr",
            scope=value.scope,
            parameters={
                "engine": engine,
                "user_requested": True,
                "track_id": track,
                "scope_indexes": [0],
                "requested_unit_index": value.unit_index,
            },
        )
        priority = value.priority_unit if value.priority_unit is not None else value.unit_index
        return {"payload": payload, "priority_unit": priority + 1 if priority is not None else None}

    def define_asr_track(self, course_id: str, track_id: str, project_root, *, explicit=False) -> dict:
        """Expand a current-only track without changing its engine or edit head."""
        from .transcripts import TranscriptStore

        with self.repository.engine.connect() as conn:
            track = dict(TranscriptStore._track(conn, course_id, track_id))
        definition = self.define_asr(
            course_id,
            PreparationCreate(
                kind="asr",
                asset_id=track["asset_id"],
                source_version=track["source_version"],
                language=track["parameters"].get("language"),
            ),
            project_root,
            explicit=explicit,
        )
        payload = definition["payload"]
        engine = payload["parameters"]["engine"]
        if (
            payload["source_digest"] != track["source_digest"]
            or any(
                engine.get(key) != value
                for key, value in track["parameters"].items()
                if key != "language"
            )
            or set(engine) != set(track["parameters"])
        ):
            raise AppError(
                "ASR_RESOURCE_VERSION_CONFLICT",
                "指定字幕轨的固定引擎版本当前不可补齐，旧轨和手改仍保留",
            )
        # None language normally follows the latest compatible track. Here the
        # caller explicitly chose a track; retain its exact original language.
        payload["parameters"]["engine"] = track["parameters"]
        payload["parameters"]["track_id"] = track_id
        if track_id != "transcript_" + _hash(
            {
                "course_id": course_id,
                "asset_id": track["asset_id"],
                "source_version": track["source_version"],
                "source_digest": track["source_digest"],
                "engine": track["parameters"],
            }
        ):
            raise AppError("ASR_TRACK_IDENTITY_CONFLICT", "字幕轨与冻结的引擎身份不符")
        return definition

    def define_pdf(self, course_id: str, value: PreparationCreate, project_root) -> dict:
        raise AppError("OCR_REMOVED", "已取消OCR；请直接选取原文或截图提问", 410)

    def _legacy_define_pdf(self, course_id: str, value: PreparationCreate, project_root) -> dict:
        if value.kind != "pdf_ocr":
            raise AppError("ASR_PREPARATION_HANDLER_NOT_READY", "语音任务的检查点接口尚未接通", 503)
        resources = OcrResources(project_root)
        if not resources.prepared():
            raise AppError("OCR_RESOURCES_NOT_PREPARED", "须先准备固定OCR资源", 503)
        reader = DocumentReader(self.repository)
        info = reader.info(course_id, value.asset_id, value.source_version)
        if value.scope == "full" and info.page_count > 20_000:
            raise AppError("PREPARATION_UNIT_LIMIT", "单任务页数超过当前处理上限，未截断", 422)
        indexes = [value.unit_index] if value.scope == "unit" else list(range(info.page_count))
        if any(index is None or not 0 <= index < info.page_count for index in indexes) or (
            value.priority_unit is not None and value.priority_unit not in indexes
        ):
            raise AppError("SOURCE_PAGE_RANGE_INVALID", "处理范围不在原PDF页数内", 422)
        parameters = {
            "manifest_sha256": resources.manifest_hash,
            "renderer_version": "pdfium514-rgb-unrotated-edge3200-v2",
            "psm": 6 if value.ocr_layout == "text_block" else 3,
            "page_count": info.page_count,
            "scope_indexes": indexes,
        }
        payload = dict(
            course_id=course_id,
            asset_id=value.asset_id,
            source_version=value.source_version,
            source_digest=info.source_digest,
            kind=value.kind,
            scope=value.scope,
            parameters=parameters,
        )
        priority_unit = value.priority_unit if value.priority_unit is not None else value.unit_index
        return {"payload": payload, "priority_unit": priority_unit}

    def publish_pdf(self, definition: dict, *, summary: bool = False):
        payload, priority_unit = definition["payload"], definition["priority_unit"]
        course_id, indexes = payload["course_id"], payload["parameters"]["scope_indexes"]
        identity = "prepare_" + _hash(payload)
        with self.repository._lock, self.repository.engine.begin() as conn:
            if payload["kind"] == "asr":
                track = payload["parameters"]["track_id"]
                if (
                    conn.execute(
                        select(s.transcript_tracks.c.track_id).where(
                            s.transcript_tracks.c.track_id == track
                        )
                    ).first()
                    is None
                ):
                    conn.execute(
                        insert(s.transcript_tracks).values(
                            track_id=track,
                            **{
                                key: payload[key]
                                for key in (
                                    "course_id",
                                    "asset_id",
                                    "source_version",
                                    "source_digest",
                                )
                            },
                            parameters=payload["parameters"]["engine"],
                            created_at=now(),
                        )
                    )
            previous = conn.execute(
                select(s.preparations.c.job_id).where(s.preparations.c.job_id == identity)
            ).first()
            if previous is None:
                stamp = now()
                conn.execute(
                    insert(s.preparations).values(
                        job_id=identity,
                        **payload,
                        state="queued",
                        revision=0,
                        epoch=1,
                        created_at=stamp,
                        updated_at=stamp,
                    )
                )
                conn.execute(
                    insert(s.preparation_units),
                    [
                        dict(
                            job_id=identity,
                            unit_index=index,
                            state="pending",
                            priority=0
                            if index == priority_unit or (payload["kind"] == "asr" and index == 0)
                            else 10,
                            local_attempts=0,
                        )
                        for index in indexes
                    ],
                )
                conn.execute(
                    insert(s.preparation_events).values(
                        job_id=identity,
                        revision=0,
                        action="created",
                        request_hash=_hash(payload),
                        created_at=stamp,
                    )
                )
            elif priority_unit is not None:
                # Reopening or moving a page never clears a manual pause.
                conn.execute(
                    update(s.preparation_units)
                    .where(s.preparation_units.c.job_id == identity)
                    .values(priority=10)
                )
                conn.execute(
                    update(s.preparation_units)
                    .where(
                        s.preparation_units.c.job_id == identity,
                        s.preparation_units.c.unit_index.in_(
                            [priority_unit, 0] if payload["kind"] == "asr" else [priority_unit]
                        ),
                    )
                    .values(priority=0)
                )
            if payload["kind"] == "asr" and priority_unit is not None:
                row = self._row(conn, course_id, identity)
                conn.execute(
                    update(s.preparations)
                    .where(s.preparations.c.job_id == identity)
                    .values(parameters={**row["parameters"], "priority_unit_index": priority_unit})
                )
            return (self._summary if summary else self._view)(conn, course_id, identity)

    def control(
        self,
        course_id: str,
        job_id: str,
        action: Literal["pause", "resume", "cancel", "retry"],
        value: PreparationControl,
    ) -> PreparationView:
        if action not in {"pause", "resume", "cancel", "retry"}:
            raise AppError("PREPARATION_ACTION_INVALID", "该本地处理操作不被支持", 422)
        request = {"action": action, **value.model_dump()}
        with self.repository._lock, self.repository.engine.begin() as conn:
            row = self._row(conn, course_id, job_id)
            prior = (
                conn.execute(
                    select(s.preparation_events).where(
                        s.preparation_events.c.job_id == job_id,
                        s.preparation_events.c.operation_id == value.operation_id,
                    )
                )
                .mappings()
                .first()
            )
            if prior:
                if prior["request_hash"] != _hash(request) or prior["receipt"] is None:
                    raise AppError(
                        "PREPARATION_OPERATION_CONFLICT", "同一处理操作身份的内容发生变化"
                    )
                return PreparationView.model_validate(prior["receipt"])
            if action in {"resume", "retry"} and row["kind"] == "pdf_ocr":
                raise AppError("OCR_REMOVED", "OCR功能已取消，旧结果仍保留", 410)
            if action in {"resume", "retry"} and not row["parameters"].get("user_requested"):
                raise AppError("SUBTITLE_REQUEST_REQUIRED", "请在对话中明确发起字幕请求", 403)
            if row["revision"] != value.expected_revision:
                changed_control = conn.execute(
                    select(s.preparation_events.c.revision)
                    .where(
                        s.preparation_events.c.job_id == job_id,
                        s.preparation_events.c.revision > value.expected_revision,
                        s.preparation_events.c.action.in_(
                            ["pause", "resume", "cancel", "retry", "application_exit"]
                        ),
                    )
                    .limit(1)
                ).first()
                if value.expected_revision > row["revision"] or changed_control:
                    raise AppError(
                        "PREPARATION_REVISION_CONFLICT", "处理控制状态已变化，请读取新状态", 409
                    )
            if row["state"] == "completed" or (row["state"] == "cancelled" and action != "resume"):
                raise AppError("PREPARATION_CLOSED", "该处理任务已结束，原结果仍保留")
            if action == "resume" and row["state"] not in {"paused", "cancelled"}:
                raise AppError("PREPARATION_STATE_INVALID", "仅能恢复已暂停的处理任务")
            if action == "retry" and row["state"] not in {"failed", "needs_source"}:
                raise AppError("PREPARATION_STATE_INVALID", "仅能明确重试已失败或缺失的单位")
            states = ["running"] + (["failed", "source_missing"] if action == "retry" else [])
            conn.execute(
                update(s.preparation_units)
                .where(
                    s.preparation_units.c.job_id == job_id, s.preparation_units.c.state.in_(states)
                )
                .values(
                    state="pending", lease_id=None, lease_epoch=None, error_code=None, progress=None
                )
            )
            state = {
                "pause": "paused",
                "cancel": "cancelled",
                "resume": "queued",
                "retry": "queued",
            }[action]
            revision = self._event(
                conn,
                row,
                action,
                request,
                operation_id=value.operation_id,
                state=state,
                epoch=row["epoch"] + 1,
            )
            result = self._view(conn, course_id, job_id)
            conn.execute(
                update(s.preparation_events)
                .where(
                    s.preparation_events.c.job_id == job_id,
                    s.preparation_events.c.revision == revision,
                )
                .values(receipt=result.model_dump(mode="json"))
            )
            return result

    def claim(self, *, native_wait_only=False) -> dict | None:
        from .subtitle_waits import SubtitleWaits

        with self.repository._lock, self.repository.engine.begin() as conn:
            selected = (
                conn.execute(
                    select(
                        s.preparations,
                        s.preparation_units.c.unit_index,
                        s.preparation_units.c.priority,
                    )
                    .join(s.preparation_units)
                    .where(
                        s.preparations.c.state.in_(["queued", "running"]),
                        s.preparations.c.kind == "asr",
                        s.preparations.c.parameters["user_requested"].as_boolean().is_(True),
                        s.preparation_units.c.state == "pending",
                        SubtitleWaits.schedulable(),
                        s.preparations.c.job_id.in_(SubtitleWaits.active_ids())
                        if native_wait_only
                        else True,
                    )
                    .order_by(
                        s.preparation_units.c.priority,
                        s.preparations.c.created_at,
                        s.preparation_units.c.unit_index,
                    )
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if selected is None:
                return None
            lease = {**selected, "lease_id": new_id("local"), "lease_epoch": selected["epoch"]}
            conn.execute(
                update(s.preparation_units)
                .where(
                    s.preparation_units.c.job_id == lease["job_id"],
                    s.preparation_units.c.unit_index == lease["unit_index"],
                    s.preparation_units.c.state == "pending",
                )
                .values(
                    state="running",
                    lease_id=lease["lease_id"],
                    lease_epoch=lease["epoch"],
                    local_attempts=s.preparation_units.c.local_attempts + 1,
                )
            )
            self._event(
                conn,
                selected,
                "claimed",
                {"unit_index": lease["unit_index"], "lease_id": lease["lease_id"]},
                state="running",
            )
            return lease

    @staticmethod
    def _live(conn, lease):
        return (
            conn.execute(
                select(s.preparation_units)
                .join(s.preparations)
                .where(
                    s.preparation_units.c.job_id == lease["job_id"],
                    s.preparation_units.c.unit_index == lease["unit_index"],
                    s.preparation_units.c.lease_id == lease["lease_id"],
                    s.preparation_units.c.lease_epoch == lease["lease_epoch"],
                    s.preparations.c.epoch == lease["lease_epoch"],
                    s.preparation_units.c.state == "running",
                    s.preparations.c.state.in_(["queued", "running"]),
                )
            )
            .mappings()
            .first()
        )

    def progress(self, lease: dict, value: dict) -> bool:
        with self.repository._lock, self.repository.engine.begin() as conn:
            if self._live(conn, lease) is None:
                return False
            conn.execute(
                update(s.preparation_units)
                .where(
                    s.preparation_units.c.job_id == lease["job_id"],
                    s.preparation_units.c.unit_index == lease["unit_index"],
                )
                .values(progress=value)
            )
            return True

    def release(self, lease: dict) -> bool:
        with self.repository._lock, self.repository.engine.begin() as conn:
            if self._live(conn, lease) is None:
                return False
            conn.execute(
                update(s.preparation_units)
                .where(
                    s.preparation_units.c.job_id == lease["job_id"],
                    s.preparation_units.c.unit_index == lease["unit_index"],
                )
                .values(state="pending", lease_id=None, lease_epoch=None, progress=None)
            )
            row = self._row(conn, lease["course_id"], lease["job_id"])
            self._event(conn, row, "yielded", {"unit_index": lease["unit_index"]}, state="queued")
            return True

    def finish(self, lease: dict, result: dict | None, error_code: str | None = None) -> bool:
        with self.repository._lock, self.repository.engine.begin() as conn:
            if self._live(conn, lease) is None:
                return False
            if result is not None and (
                result.get("source_digest") != lease["source_digest"]
                or result.get("page_index", result.get("unit_index")) != lease["unit_index"]
            ):
                raise AppError("PREPARATION_RESULT_SCOPE_CONFLICT", "本地结果与冻结来源单元不符")
            state = (
                "completed"
                if error_code is None
                else (
                    "source_missing"
                    if error_code.startswith("SOURCE_")
                    or error_code
                    in {
                        "ASR_SOURCE_VERSION_CONFLICT",
                        "ASR_SOURCE_CHANGED",
                        "ASR_SOURCE_LEASE_DENIED",
                    }
                    else "failed"
                )
            )
            digest = _hash(result) if result is not None else None
            if result is not None and lease["kind"] == "asr":
                from .transcripts import TranscriptStore

                TranscriptStore.publish_local(conn, lease, result)
            if result is not None and result.get("kind") == "ocr":
                raw = result["raw"]
                parameters = raw["parameters_version"]
                condition = (
                    s.document_ocr.c.course_id == lease["course_id"],
                    s.document_ocr.c.asset_id == lease["asset_id"],
                    s.document_ocr.c.source_version == lease["source_version"],
                    s.document_ocr.c.page_index == lease["unit_index"],
                    s.document_ocr.c.parameters_version == parameters,
                )
                prior = conn.execute(select(s.document_ocr).where(*condition)).mappings().first()
                if prior is not None and prior["payload_hash"] != digest:
                    raise AppError("OCR_RESULT_CONFLICT", "相同参数的OCR结果不同，旧文字层未覆盖")
                if prior is None:
                    conn.execute(
                        insert(s.document_ocr).values(
                            course_id=lease["course_id"],
                            asset_id=lease["asset_id"],
                            source_version=lease["source_version"],
                            page_index=lease["unit_index"],
                            parameters_version=parameters,
                            payload=result,
                            payload_hash=digest,
                            created_at=now(),
                        )
                    )
            conn.execute(
                update(s.preparation_units)
                .where(
                    s.preparation_units.c.job_id == lease["job_id"],
                    s.preparation_units.c.unit_index == lease["unit_index"],
                )
                .values(
                    state=state,
                    error_code=error_code,
                    result_digest=digest,
                    progress=None,
                    lease_id=None,
                    lease_epoch=None,
                )
            )
            counts = self._counts(conn, lease["job_id"])
            if counts.get("source_missing"):
                job_state = "needs_source"
            elif counts.get("failed"):
                job_state = "failed"
            elif sum(counts.values()) == counts.get("completed", 0):
                job_state = "completed"
            else:
                job_state = "queued"
            self.repository._hook(
                "preparation_before_commit", job_id=lease["job_id"], unit_index=lease["unit_index"]
            )
            row = self._row(conn, lease["course_id"], lease["job_id"])
            self._event(
                conn,
                row,
                "unit_finished",
                {"unit_index": lease["unit_index"], "error_code": error_code},
                state=job_state,
            )
            return True

    def should_yield(self, lease: dict, *, native_wait_only=False) -> bool:
        from .subtitle_waits import SubtitleWaits

        with self.repository.engine.connect() as conn:
            live = self._live(conn, lease)
            if live is None:
                return True
            urgent = conn.execute(
                select(s.preparation_units.c.job_id)
                .join(s.preparations)
                .where(
                    s.preparations.c.state.in_(["queued", "running"]),
                    s.preparation_units.c.state == "pending",
                    s.preparation_units.c.priority < live["priority"],
                    SubtitleWaits.schedulable(),
                    s.preparations.c.job_id.in_(SubtitleWaits.active_ids())
                    if native_wait_only
                    else True,
                )
                .limit(1)
            ).first()
            return urgent is not None

    def recover(self) -> None:
        with self.repository._lock, self.repository.engine.begin() as conn:
            rows = (
                conn.execute(select(s.preparations).where(s.preparations.c.state == "running"))
                .mappings()
                .all()
            )
            for row in rows:
                conn.execute(
                    update(s.preparation_units)
                    .where(
                        s.preparation_units.c.job_id == row["job_id"],
                        s.preparation_units.c.state == "running",
                    )
                    .values(state="pending", lease_id=None, lease_epoch=None, progress=None)
                )
                self._event(
                    conn,
                    row,
                    "recovered_local_checkpoint",
                    {},
                    state="queued",
                    epoch=row["epoch"] + 1,
                )

    def active(self) -> list[dict]:
        with self.repository.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(
                        s.preparations.c.job_id,
                        s.preparations.c.course_id,
                        s.preparations.c.kind,
                        s.preparations.c.state,
                    ).where(s.preparations.c.state.in_(["queued", "running"]))
                ).mappings()
            ]

    def freeze_for_exit(self) -> list[dict]:
        with self.repository._lock, self.repository.engine.begin() as conn:
            rows = (
                conn.execute(
                    select(s.preparations).where(s.preparations.c.state.in_(["queued", "running"]))
                )
                .mappings()
                .all()
            )
            for row in rows:
                conn.execute(
                    update(s.preparation_units)
                    .where(
                        s.preparation_units.c.job_id == row["job_id"],
                        s.preparation_units.c.state == "running",
                    )
                    .values(state="pending", lease_id=None, lease_epoch=None, progress=None)
                )
                self._event(
                    conn, row, "application_exit", {}, state="paused", epoch=row["epoch"] + 1
                )
            return [dict(row) for row in rows]

    def has_urgent(self) -> bool:
        from .subtitle_waits import SubtitleWaits

        with self.repository.engine.connect() as conn:
            return (
                conn.execute(
                    select(s.preparation_units.c.job_id)
                    .join(s.preparations)
                    .where(
                        s.preparations.c.state.in_(["queued", "running"]),
                        s.preparation_units.c.state.in_(["pending", "running"]),
                        s.preparation_units.c.priority == 0,
                        SubtitleWaits.schedulable(),
                    )
                    .limit(1)
                ).first()
                is not None
            )
