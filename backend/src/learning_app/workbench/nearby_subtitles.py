"""Read at most three audio grids around one immutable question anchor."""

import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from learning_app.config import PROJECT_ROOT
from learning_app.errors import AppError

from . import schema as s
from .preparations import PreparationStore
from .processing_models import PreparationCreate
from .subtitle_waits import SubtitleWaits
from .transcript_models import SubtitleCapture
from .transcripts import WINDOW_MS, TranscriptStore


def select_anchor(scope, arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"anchor_id"}:
        raise AppError("NEARBY_SUBTITLE_ARGUMENTS", "只允许选择问题绑定的字幕时间", 422)
    identity = arguments["anchor_id"]
    anchors = scope["grants"].get("subtitle_anchors", [])
    if identity is None:
        return anchors[0] if len(anchors) == 1 else None
    if not isinstance(identity, str):
        raise AppError("NEARBY_SUBTITLE_ARGUMENTS", "字幕时间身份无效", 422)
    match = next((item for item in anchors if item["anchor_id"] == identity), None)
    if match is None:
        raise AppError("NEARBY_SUBTITLE_ANCHOR_DENIED", "该时间未绑定本次问题", 403)
    return match


class NearbySubtitles:
    def __init__(self, repository, project_root=PROJECT_ROOT, *, wait_seconds=30):
        self.repository, self.project_root, self.wait_seconds = (
            repository,
            project_root,
            wait_seconds,
        )
        self.tracks = TranscriptStore(repository)

    def _collect(self, course, track_id, head, first, last):
        begin, end = first * WINDOW_MS, (last + 1) * WINDOW_MS
        with self.repository.engine.connect() as conn:
            track = dict(self.tracks._track(conn, course, track_id))
            edits = self.tracks._edit(conn, track_id, head)
            moved = [
                patch["raw_revision_id"]
                for patch in edits.values()
                if patch["start_ms"] < end and patch["end_ms"] > begin
            ]
            rows = (
                conn.execute(
                    select(s.transcript_units).where(
                        s.transcript_units.c.track_id == track_id,
                        or_(
                            (s.transcript_units.c.unit_index >= first)
                            & (s.transcript_units.c.unit_index <= last),
                            s.transcript_units.c.revision_id.in_(moved),
                        ),
                    )
                )
                .mappings()
                .all()
            )
        ready = {row["unit_index"] for row in rows}
        expected = set(range(first, last + 1))
        if track["probe"]:
            expected &= set(track["probe"]["unit_indexes"])
        segments, gaps = [], []
        for row in rows:
            for segment in self.tracks._raw_segments(row):
                patch = edits.get(segment.segment_id)
                if patch:
                    segment = segment.model_copy(
                        update={
                            "text": patch["text"],
                            "start_ms": patch["start_ms"],
                            "end_ms": patch["end_ms"],
                            "revision_id": head,
                            "origin": "user_note",
                        }
                    )
                if segment.start_ms >= end or segment.end_ms <= begin:
                    continue
                if not begin <= segment.start_ms < segment.end_ms <= end:
                    gaps.append(
                        {"code": "SUBTITLE_SEGMENT_CROSSES_SCOPE", "segment_id": segment.segment_id}
                    )
                    continue
                segments.append(segment)
        segments.sort(key=lambda item: (item.start_ms, item.segment_id))
        return track, sorted(expected - ready), segments, gaps

    def read(self, scope, anchor, check_active):
        course, run = anchor.get("source_course_id") or scope["course_id"], scope["run_id"]
        grid = anchor["time_ms"] // WINDOW_MS
        first, last = max(0, grid - 1), grid + 1
        tracks = self.tracks.list(course, anchor["asset_id"], anchor["source_version"])
        track_id = tracks[0] if tracks else None
        with self.repository.engine.connect() as conn:
            head = self.tracks._track(conn, course, track_id)["edit_head_id"] if track_id else None
        store, waits = PreparationStore(self.repository), SubtitleWaits(self.repository)
        problems, jobs = [], []
        deadline = (datetime.now(UTC) + timedelta(seconds=self.wait_seconds)).isoformat(
            timespec="milliseconds"
        )
        stop_at = time.monotonic() + self.wait_seconds
        try:
            missing = list(range(first, last + 1))
            if track_id:
                _, missing, _, _ = self._collect(course, track_id, head, first, last)
            if missing:
                try:
                    check_active()
                    missing.sort(key=lambda unit: (abs(unit - grid), unit))
                    definition = (
                        store.define_asr_track(course, track_id, self.project_root, explicit=True)
                        if track_id
                        else store.define_asr(
                            course,
                            PreparationCreate(
                                kind="asr",
                                asset_id=anchor["asset_id"],
                                source_version=anchor["source_version"],
                                scope="unit",
                                unit_index=missing[0],
                            ),
                            self.project_root,
                            explicit=True,
                        )
                    )
                    track_id = definition["payload"]["parameters"]["track_id"]
                    if definition["payload"]["source_digest"] != anchor["source_digest"]:
                        raise AppError("SOURCE_VERSION_UNAVAILABLE", "问题原音轨版本已改变")
                    for unit in missing:
                        check_active()
                        payload = {
                            **definition["payload"],
                            "scope": "unit",
                            "parameters": {
                                **definition["payload"]["parameters"],
                                "scope_indexes": [0],
                                "requested_unit_index": unit,
                                "native_read_run_id": run,
                                "native_anchor_id": anchor["anchor_id"],
                            },
                        }
                        job = store.publish_pdf(
                            {"payload": payload, "priority_unit": unit + 1}, summary=True
                        )
                        jobs.append(job.job_id)
                        waits.register(run, anchor["anchor_id"], job.job_id, deadline)
                    while time.monotonic() < stop_at:
                        check_active()
                        _, missing, _, _ = self._collect(course, track_id, head, first, last)
                        if not missing or all(
                            store.summary(course, job).state
                            in {"failed", "cancelled", "needs_source", "paused"}
                            for job in jobs
                        ):
                            break
                        time.sleep(0.1)
                except AppError as error:
                    if error.code.startswith("NATIVE_"):
                        raise
                    problems.append({"code": error.code})
            check_active()
            result = {
                "anchor_id": anchor["anchor_id"],
                "start_ms": first * WINDOW_MS,
                "end_ms": (last + 1) * WINDOW_MS,
                "track_id": track_id,
                "edit_head_id": head,
                "external_fallback": False,
                "image_frames_requested": 0,
                "missing_units": missing,
                "gaps": problems,
                "snapshots": [],
            }
            if not track_id:
                return {**result, "status": "not_ready"}
            track, missing, segments, gaps = self._collect(course, track_id, head, first, last)
            result.update(missing_units=missing, gaps=[*problems, *gaps])
            expected_units = set(range(first, last + 1))
            if track["probe"]:
                expected_units &= set(track["probe"]["unit_indexes"])
            result.update(
                grid_ms=WINDOW_MS,
                ready_units=sorted(expected_units - set(missing)),
                coverage_kind="nearby_audio_grid_readiness",
            )
            if track["source_digest"] != anchor["source_digest"]:
                raise AppError("NEARBY_SUBTITLE_VERSION_DENIED", "字幕不属于问题原音轨", 403)
            if (
                track["probe"]
                and not track["probe"]["audio_start_ms"]
                <= anchor["time_ms"]
                < track["probe"]["audio_end_ms"]
            ):
                return {**result, "status": "outside_audio_range", "snapshots": []}
            if sum(len(item.text) for item in segments) > 24_000 or len(segments) > 96:
                return {
                    **result,
                    "status": "input_limit",
                    "gaps": [*result["gaps"], {"code": "NEARBY_SUBTITLE_SIZE_LIMIT"}],
                }
            snapshots = [
                self.tracks.capture(
                    course,
                    track_id,
                    SubtitleCapture(segment_id=item.segment_id, revision_id=item.revision_id),
                )
                for item in segments
            ]
            status = (
                "partial"
                if snapshots and (missing or result["gaps"])
                else "matched"
                if snapshots
                else "not_ready"
                if missing or result["gaps"]
                else "no_speech"
            )
            return {**result, "status": status, "snapshots": snapshots}
        finally:
            waits.close(run, anchor["anchor_id"])
