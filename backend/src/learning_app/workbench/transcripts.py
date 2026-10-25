"""Immutable raw transcription units, with independent, conflict-preserving edits."""

from fractions import Fraction

from sqlalchemy import insert, select, update

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from . import schema as s
from .audio_sources import AudioSources
from .common import new_id, now
from .models import SourceSnapshot, SubtitleRef
from .sources import SourceStore
from .transcript_models import (
    SubtitleCapture,
    SubtitleSegment,
    TranscriptEdit,
    TranscriptEditReceipt,
    TranscriptView,
)

WINDOW_MS = 30_000


class TranscriptStore:
    def __init__(self, repository):
        self.repository = repository

    def publish_platform(self, course_id, asset_id, version, language, origin, segments, duration_ms):
        """Publish immutable provider text; edits are never overwritten by a refreshed track."""
        from learning_app.workbench.audio_sources import AudioSources
        source = AudioSources(self.repository).associated(course_id, asset_id, version)
        if origin not in {"platform_cc", "platform_ai"} or not 0 < duration_ms <= 600_000_000:
            raise AppError("PLATFORM_SUBTITLE_INVALID", "平台字幕类型或范围无效", 422)
        for segment in segments:
            if (type(segment.get("start_ms")) is not int or type(segment.get("end_ms")) is not int
                    or not 0 <= segment["start_ms"] < segment["end_ms"] <= duration_ms
                    or not isinstance(segment.get("text"), str)):
                raise AppError("PLATFORM_SUBTITLE_INVALID", "平台字幕不在原分P范围内", 422)
        parameters = {"origin": origin, "language": language, "subtitle_digest": _hash(segments)}
        identity = "transcript_" + _hash({"course": course_id, "asset": asset_id,
                                         "version": version, "parameters": parameters})
        indexes = list(range((duration_ms + WINDOW_MS - 1) // WINDOW_MS))
        with self.repository.engine.begin() as conn:
            if conn.execute(select(s.transcript_tracks).where(s.transcript_tracks.c.track_id == identity)).first():
                return identity
            # A new provider revision must not displace a user's current edited track.
            edited = conn.execute(select(s.transcript_tracks.c.track_id).where(
                s.transcript_tracks.c.course_id == course_id, s.transcript_tracks.c.asset_id == asset_id,
                s.transcript_tracks.c.source_version == version,
                s.transcript_tracks.c.edit_head_id.is_not(None))).first()
            if edited:
                return edited[0]
            conn.execute(insert(s.transcript_tracks).values(track_id=identity, course_id=course_id,
                asset_id=asset_id, source_version=version, source_digest=source["content_hash"],
                parameters=parameters, probe={"audio_start_ms": 0, "audio_end_ms": duration_ms,
                    "unit_indexes": indexes}, created_at=now()))
            for index in indexes:
                start, end = index * WINDOW_MS, min(duration_ms, (index + 1) * WINDOW_MS)
                payload = {"kind": origin, "raw": {"segments": [
                    {**part, "start_ms": max(start, part["start_ms"]), "end_ms": min(end, part["end_ms"])}
                    for part in segments if part["start_ms"] < end and part["end_ms"] > start]}}
                digest = _hash(payload)
                conn.execute(insert(s.transcript_units).values(track_id=identity, unit_index=index,
                    revision_id="trraw_" + _hash({"track": identity, "index": index, "payload": digest}),
                    payload=payload, payload_hash=digest, created_at=now()))
        return identity

    @classmethod
    def publish_local(cls, conn, lease, result):
        """Runs in the preparation checkpoint transaction, never from a model."""
        track_id = lease["parameters"]["track_id"]
        track = cls._track(conn, lease["course_id"], track_id)
        if result.get("track_id") != track_id or track["source_digest"] != lease["source_digest"]:
            raise AppError("ASR_RESULT_SCOPE_CONFLICT", "字幕结果与冻结音轨不符")
        if result["kind"] == "audio_probe":
            probe = result["probe"]
            identity = probe["source"]
            if (
                identity["asset_id"] != lease["asset_id"]
                or identity["source_version"] != lease["source_version"]
                or identity["sha256"] != track['parameters'].get('audio_source_sha256', lease["source_digest"])
            ):
                raise AppError("ASR_RESULT_SCOPE_CONFLICT", "音轨探测来源与冻结任务不符")
            begin, end = probe["audio_start_ms"], probe["audio_end_ms"]
            if type(begin) is not int or type(end) is not int or not 0 <= begin < end:
                raise AppError("ASR_AUDIO_DURATION_UNKNOWN", "音轨时长不能形成完整范围")
            if end > 20_000 * WINDOW_MS:
                raise AppError("PREPARATION_UNIT_LIMIT", "音轨时间范围超过当前上限，未截断")
            indexes = [
                i
                for i in range(end // WINDOW_MS + 1)
                if i * WINDOW_MS < end and (i + 1) * WINDOW_MS > begin
            ]
            if len(indexes) > 20_000:
                raise AppError("PREPARATION_UNIT_LIMIT", "整份音轨超过单任务范围上限，未截断")
            probe = {**probe, "unit_indexes": indexes}
            if track["probe"] and _hash(track["probe"]) != _hash(probe):
                raise AppError("ASR_PROBE_CONFLICT", "同一冻结音轨的范围发生冲突，旧结果保留")
            if not track["probe"]:
                conn.execute(
                    update(s.transcript_tracks)
                    .where(s.transcript_tracks.c.track_id == track_id)
                    .values(probe=probe)
                )
            job = (
                conn.execute(
                    select(s.preparations).where(s.preparations.c.job_id == lease["job_id"])
                )
                .mappings()
                .one()
            )
            params = job["parameters"]
            requested = params["requested_unit_index"]
            if lease["scope"] == "unit":
                # No fabricated silence before/after an actual audio track.
                # Whole-track coverage remains separate in track.probe.
                indexes = [requested] if requested in indexes else []
            units = [i + 1 for i in indexes]
            existing = set(
                conn.execute(
                    select(s.preparation_units.c.unit_index).where(
                        s.preparation_units.c.job_id == lease["job_id"]
                    )
                ).scalars()
            )
            additions = [
                dict(
                    job_id=lease["job_id"],
                    unit_index=i,
                    state="pending",
                    local_attempts=0,
                    priority=0 if i == params.get("priority_unit_index") else 10,
                )
                for i in units
                if i not in existing
            ]
            if additions:
                conn.execute(insert(s.preparation_units), additions)
            conn.execute(
                update(s.preparations)
                .where(s.preparations.c.job_id == lease["job_id"])
                .values(parameters={**params, "scope_indexes": [0, *units], "audio_probe": probe})
            )
            return
        grid_index = lease["unit_index"] - 1
        prior = (
            conn.execute(
                select(s.transcript_units).where(
                    s.transcript_units.c.track_id == track_id,
                    s.transcript_units.c.unit_index == grid_index,
                )
            )
            .mappings()
            .first()
        )
        if result["kind"] == "cached_transcript":
            if prior is None or prior["revision_id"] != result["raw_revision_id"]:
                raise AppError("ASR_RESULT_SCOPE_CONFLICT", "原字幕检查点已不可核对")
            cls._payload(prior)
            return
        if (
            result["kind"] != "asr"
            or not track["probe"]
            or grid_index not in track["probe"]["unit_indexes"]
        ):
            raise AppError("ASR_RESULT_SCOPE_CONFLICT", "字幕单元不在已核音轨范围")
        raw = result["raw"]
        audio = result["audio"]
        expected_start = grid_index * WINDOW_MS
        expected_end = min((grid_index + 1) * WINDOW_MS, track["probe"]["audio_end_ms"])
        if (
            audio["requested_start_ms"] != expected_start
            or audio["requested_end_ms"] != expected_end
            or audio["source"]["asset_id"] != lease["asset_id"]
            or audio["source"]["source_version"] != lease["source_version"]
            or audio["source"]["sha256"] != track['parameters'].get('audio_source_sha256', lease["source_digest"])
            or audio["image_frames_requested"] != 0
            or audio["selected_stream_type"] != "audio"
            or raw["audio_sha256"] != audio["wav_sha256"]
            or raw["start_ms"] != audio["start_ms"]
            or raw["duration_ms"] != audio["duration_ms"]
        ):
            raise AppError("ASR_RESULT_SCOPE_CONFLICT", "字幕与本次有限音轨输入不对应")
        first = max(
            expected_start * 16, round(Fraction(track["probe"]["audio_start_seconds"]) * 16000)
        )
        last = min(expected_end * 16, round(Fraction(track["probe"]["audio_end_seconds"]) * 16000))
        if (
            abs(audio["effective_start_sample"] - first) > 1
            or abs(audio["effective_end_sample"] - last) > 1
            or audio["sample_count"]
            != audio["effective_end_sample"] - audio["effective_start_sample"]
        ):
            raise AppError("ASR_AUDIO_COVERAGE_GAP", "已解码样本未覆盖完整有效时间片，保留缺口")
        if raw.get("parameters_version") != track["parameters"]["parameters_version"]:
            raise AppError("ASR_RESOURCE_VERSION_CONFLICT", "字幕参数与冻结版本不同")
        for item in raw["segments"]:
            if (
                not raw["start_ms"]
                <= item["start_ms"]
                < item["end_ms"]
                <= raw["start_ms"] + raw["duration_ms"]
            ):
                raise AppError("ASR_SEGMENT_TIME_INVALID", "原始字幕时间不在音轨范围")
        digest = _hash(result)
        if prior:
            if prior["payload_hash"] != digest:
                raise AppError("ASR_RESULT_CONFLICT", "同一参数的原始字幕出现不同结果，旧版未覆盖")
        else:
            conn.execute(
                insert(s.transcript_units).values(
                    track_id=track_id,
                    unit_index=grid_index,
                    revision_id="trraw_" + _hash({"track": track_id, "payload": digest}),
                    payload=result,
                    payload_hash=digest,
                    created_at=now(),
                )
            )

    @staticmethod
    def _track(conn, course_id, track_id):
        row = (
            conn.execute(
                select(s.transcript_tracks).where(
                    s.transcript_tracks.c.track_id == track_id,
                    s.transcript_tracks.c.course_id == course_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("TRANSCRIPT_NOT_FOUND", "指定课程的字幕版本不存在", 404)
        return row

    @staticmethod
    def _payload(row):
        if _hash(row["payload"]) != row["payload_hash"]:
            raise AppError("TRANSCRIPT_INTEGRITY", "已保存字幕的完整性校验失败")
        return row["payload"]

    @classmethod
    def _raw_segments(cls, row):
        payload = cls._payload(row)
        return [
            SubtitleSegment(
                segment_id="segment_" + _hash({"revision": row["revision_id"], "index": index}),
                revision_id=row["revision_id"],
                raw_revision_id=row["revision_id"],
                start_ms=item["start_ms"],
                end_ms=item["end_ms"],
                text=item["text"],
                raw_text=item["text"],
                origin="original",
            )
            for index, item in enumerate(payload["raw"]["segments"])
        ]

    @classmethod
    def _edit(cls, conn, track_id, revision_id):
        if revision_id is None:
            return {}
        row = (
            conn.execute(
                select(s.transcript_edits).where(
                    s.transcript_edits.c.track_id == track_id,
                    s.transcript_edits.c.revision_id == revision_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("TRANSCRIPT_REVISION_NOT_FOUND", "指定字幕修订不存在", 404)
        return cls._payload(row)["changes"]

    def list(self, course_id, asset_id, version) -> list[str]:
        AudioSources(self.repository).associated(course_id, asset_id, version)
        with self.repository.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(s.transcript_tracks)
                    .where(
                        s.transcript_tracks.c.course_id == course_id,
                        s.transcript_tracks.c.asset_id == asset_id,
                        s.transcript_tracks.c.source_version == version,
                    )
                    .order_by(
                        s.transcript_tracks.c.created_at.desc(), s.transcript_tracks.c.track_id
                    )
                ).mappings()
            )
        def preference(row):
            if row['edit_head_id']:
                return 0
            parameters = row['parameters']
            origin = parameters.get('origin', 'local_asr')
            chinese = parameters.get('language') in {'zh', 'zh-CN', 'zh-Hans', 'ai-zh'}
            return (1 if origin == 'platform_cc' and chinese else
                    2 if origin == 'platform_ai' and chinese else
                    3 if origin == 'local_asr' else 4 if origin == 'platform_cc' else 5)
        return [row['track_id'] for row in sorted(rows, key=preference)]

    def view(self, course_id, track_id, *, first_unit=0, limit=8) -> TranscriptView:
        if first_unit < 0 or not 1 <= limit <= 16:
            raise AppError("TRANSCRIPT_RANGE_INVALID", "字幕分页范围不符", 422)
        with self.repository.engine.connect() as conn:
            track = self._track(conn, course_id, track_id)
            ready = list(
                conn.execute(
                    select(s.transcript_units.c.unit_index)
                    .where(
                        s.transcript_units.c.track_id == track_id,
                    )
                    .order_by(s.transcript_units.c.unit_index)
                ).scalars()
            )
            edits = self._edit(conn, track_id, track["edit_head_id"])
            segments = []
            for row in conn.execute(
                select(s.transcript_units)
                .where(
                    s.transcript_units.c.track_id == track_id,
                    s.transcript_units.c.unit_index >= first_unit,
                    s.transcript_units.c.unit_index < first_unit + limit,
                )
                .order_by(s.transcript_units.c.unit_index)
            ).mappings():
                for segment in self._raw_segments(row):
                    patch = edits.get(segment.segment_id)
                    if patch:
                        segment = segment.model_copy(
                            update={
                                "text": patch["text"],
                                "start_ms": patch["start_ms"],
                                "end_ms": patch["end_ms"],
                                "revision_id": track["edit_head_id"],
                                "origin": "user_note",
                            }
                        )
                    segments.append(segment)
            probe = track["probe"]
            indexes = probe["unit_indexes"] if probe else None
            total = len(indexes) if indexes is not None else None
            missing = sorted(set(indexes) - set(ready)) if indexes is not None else []
            return TranscriptView(
                **{
                    key: track[key]
                    for key in (
                        "track_id",
                        "course_id",
                        "asset_id",
                        "source_version",
                        "source_digest",
                        "edit_head_id",
                    )
                },
                audio_start_ms=probe["audio_start_ms"] if probe else None,
                source_type=track["parameters"].get("origin", "local_asr"),
                audio_end_ms=probe["audio_end_ms"] if probe else None,
                units_total=total,
                units_ready=ready,
                missing_units=missing,
                segments=segments,
            )

    def segment(self, course_id, track_id, value: SubtitleCapture) -> SubtitleSegment:
        with self.repository.engine.connect() as conn:
            self._track(conn, course_id, track_id)
            if value.revision_id.startswith("trraw_"):
                raw_id, patch = value.revision_id, None
            else:
                patch = self._edit(conn, track_id, value.revision_id).get(value.segment_id)
                if patch is None:
                    raise AppError("SUBTITLE_SEGMENT_NOT_FOUND", "该修订不包含指定字幕", 404)
                raw_id = patch["raw_revision_id"]
            row = (
                conn.execute(
                    select(s.transcript_units).where(
                        s.transcript_units.c.track_id == track_id,
                        s.transcript_units.c.revision_id == raw_id,
                    )
                )
                .mappings()
                .first()
            )
            segment = (
                next(
                    (
                        item
                        for item in self._raw_segments(row)
                        if item.segment_id == value.segment_id
                    ),
                    None,
                )
                if row
                else None
            )
            if segment is None:
                raise AppError("SUBTITLE_SEGMENT_NOT_FOUND", "指定原始字幕片段不存在", 404)
            return (
                segment.model_copy(
                    update={
                        "text": patch["text"],
                        "start_ms": patch["start_ms"],
                        "end_ms": patch["end_ms"],
                        "revision_id": value.revision_id,
                        "origin": "user_note",
                    }
                )
                if patch
                else segment
            )

    def edit(self, course_id, track_id, value: TranscriptEdit) -> TranscriptEditReceipt:
        request_hash = _hash(value.model_dump(mode="json"))
        with self.repository._lock, self.repository.engine.begin() as conn:
            track = self._track(conn, course_id, track_id)
            previous = (
                conn.execute(
                    select(s.transcript_edits).where(
                        s.transcript_edits.c.track_id == track_id,
                        s.transcript_edits.c.operation_id == value.operation_id,
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["request_hash"] != request_hash:
                    raise AppError("TRANSCRIPT_OPERATION_CONFLICT", "同一字幕操作身份不能更改内容")
                return TranscriptEditReceipt.model_validate(self._payload(previous)["receipt"])
            base = dict(self._edit(conn, track_id, value.expected_edit_head_id))
            for change in value.changes:
                row = (
                    conn.execute(
                        select(s.transcript_units).where(
                            s.transcript_units.c.track_id == track_id,
                            s.transcript_units.c.revision_id == change.raw_revision_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None or not any(
                    seg.segment_id == change.segment_id for seg in self._raw_segments(row)
                ):
                    raise AppError(
                        "SUBTITLE_SEGMENT_NOT_FOUND", "修订的字幕来源不属于当前版本", 404
                    )
                if not track["probe"] or change.end_ms > track["probe"]["audio_end_ms"]:
                    raise AppError("TRANSCRIPT_RANGE_INVALID", "字幕修订时间不在已核音轨内", 422)
                base[change.segment_id] = change.model_dump(mode="json")
            revision = new_id("tredit")
            conflict = track["edit_head_id"] != value.expected_edit_head_id
            receipt = TranscriptEditReceipt(
                revision_id=revision,
                head_revision_id=track["edit_head_id"] if conflict else revision,
                disposition="conflict" if conflict else "published",
            )
            payload = {"changes": base, "receipt": receipt.model_dump(mode="json")}
            conn.execute(
                insert(s.transcript_edits).values(
                    revision_id=revision,
                    track_id=track_id,
                    parent_revision_id=value.expected_edit_head_id,
                    operation_id=value.operation_id,
                    request_hash=request_hash,
                    payload=payload,
                    payload_hash=_hash(payload),
                    disposition=receipt.disposition,
                    created_at=now(),
                )
            )
            if not conflict:
                conn.execute(
                    update(s.transcript_tracks)
                    .where(s.transcript_tracks.c.track_id == track_id)
                    .values(edit_head_id=revision)
                )
            self.repository._hook(
                "transcript_edit_before_commit", track_id=track_id, revision_id=revision
            )
            return receipt

    def operation(self, course_id, track_id, operation_id) -> TranscriptEditReceipt:
        with self.repository.engine.connect() as conn:
            self._track(conn, course_id, track_id)
            row = (
                conn.execute(
                    select(s.transcript_edits).where(
                        s.transcript_edits.c.track_id == track_id,
                        s.transcript_edits.c.operation_id == operation_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AppError(
                    "TRANSCRIPT_OPERATION_NOT_FOUND", "尚未找到该字幕操作回执，未重新发送", 404
                )
            return TranscriptEditReceipt.model_validate(self._payload(row)["receipt"])

    def capture(self, course_id, track_id, value: SubtitleCapture) -> SourceSnapshot:
        segment = self.segment(course_id, track_id, value)
        with self.repository.engine.connect() as conn:
            track = self._track(conn, course_id, track_id)
        reference = SubtitleRef(
            asset_id=track["asset_id"],
            source_version=track["source_version"],
            source_digest=track["source_digest"],
            transcript_revision_id=segment.revision_id,
            segment_id=segment.segment_id,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
        )
        return SourceStore(self.repository)._save_verified(
            course_id, reference, segment.text, segment.origin
        )

    def validate_snapshot(self, snapshot: SourceSnapshot):
        reference = snapshot.reference
        if not isinstance(reference, SubtitleRef):
            raise AppError("SUBTITLE_SOURCE_DENIED", "该快照不是字幕来源", 403)
        host = getattr(self.repository, "integrations", None)
        if host and host.source(reference.asset_id, reference.source_version, snapshot.course_id):
            source = self.repository.resolve_source(reference.asset_id, reference.source_version)
            if source["status"] != "available":
                raise AppError("SOURCE_VERSION_UNAVAILABLE", "原平台来源版本不可用")
        else:
            source = AudioSources(self.repository).current(
                snapshot.course_id, reference.asset_id, reference.source_version
            )
        if source["content_hash"] != reference.source_digest:
            raise AppError("SOURCE_VERSION_UNAVAILABLE", "音轨摘要与字幕来源不同")
        with self.repository.engine.connect() as conn:
            version_table = (
                s.transcript_units
                if reference.transcript_revision_id.startswith("trraw_")
                else s.transcript_edits
            )
            track_id = conn.execute(
                select(s.transcript_tracks.c.track_id)
                .join(version_table, s.transcript_tracks.c.track_id == version_table.c.track_id)
                .where(
                    s.transcript_tracks.c.course_id == snapshot.course_id,
                    s.transcript_tracks.c.asset_id == reference.asset_id,
                    s.transcript_tracks.c.source_version == reference.source_version,
                    version_table.c.revision_id == reference.transcript_revision_id,
                )
            ).scalar_one_or_none()
        if track_id is None:
            raise AppError("SUBTITLE_SOURCE_DENIED", "字幕修订不属于该课程音轨", 403)
        segment = self.segment(
            snapshot.course_id,
            track_id,
            SubtitleCapture(
                segment_id=reference.segment_id, revision_id=reference.transcript_revision_id
            ),
        )
        if (
            segment.text != snapshot.text
            or segment.start_ms != reference.start_ms
            or segment.end_ms != reference.end_ms
            or segment.origin != snapshot.content_role
        ):
            raise AppError("SUBTITLE_SOURCE_DENIED", "字幕来源与不可变快照不对应", 403)
