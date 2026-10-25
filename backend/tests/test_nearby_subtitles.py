"""Synthetic stored transcript fixtures; no speech recognition or provider."""

import pytest
from sqlalchemy import insert
from test_native_business import call
from test_subtitle_waits import owned_window

from learning_app.execution.native_tools import ToolRejected
from learning_app.storage.repository import _hash
from learning_app.workbench import schema as s
from learning_app.workbench.common import now
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.transcript_models import SubtitleChange, TranscriptEdit
from learning_app.workbench.transcripts import TranscriptStore


def cached_track(repo, binding, anchor, *, course=None):
    with repo.engine.begin() as conn:
        if course is None:
            scope = NativeBusiness._scope(conn, binding.generation_id)
            course = scope["course_id"]
        conn.execute(
            insert(s.transcript_tracks).values(
                track_id="frozen_track",
                course_id=course,
                asset_id=anchor["asset_id"],
                source_version=anchor["source_version"],
                source_digest=anchor["source_digest"],
                parameters={"parameters_version": "synthetic"},
                probe={"unit_indexes": list(range(8)), "audio_start_ms": 0, "audio_end_ms": 240000},
                created_at=now(),
            )
        )
        for i in range(8):
            payload = {
                "raw": {
                    "segments": [
                        {
                            "start_ms": i * 30000 + 1000,
                            "end_ms": i * 30000 + 2000,
                            "text": f"SEGMENT_{i}_SENTINEL",
                        }
                    ]
                }
            }
            conn.execute(
                insert(s.transcript_units).values(
                    track_id="frozen_track",
                    unit_index=i,
                    revision_id=f"trraw_fixture_{i}",
                    payload=payload,
                    payload_hash=_hash(payload),
                    created_at=now(),
                )
            )
    return course


def test_native_read_is_bound_to_three_grids_and_returns_reconcilable_exact_sources(
    tmp_path, monkeypatch
):
    repo, binding, anchor = owned_window(tmp_path)
    cached_track(repo, binding, anchor)

    def no_asr(*_, **__):
        pytest.fail("ready subtitle read must not invoke ASR")

    monkeypatch.setattr(PreparationStore, "define_asr_track", no_asr)
    port = NativeBusiness(repo)
    args = {"anchor_id": None}
    native_call = call(binding, "read_nearby_subtitles", args)
    result = port.invoke(native_call, args)
    assert result["status"] == "matched" and len(result["results"]) == 3
    assert result["start_ms"] == 30000 and result["end_ms"] == 120000
    assert result["image_frames_requested"] == 0 and result["external_fallback"] is False
    assert [item["source"]["text"] for item in result["results"]] == [
        f"SEGMENT_{i}_SENTINEL" for i in (1, 2, 3)
    ]
    assert "SEGMENT_0_SENTINEL" not in str(result) and "SEGMENT_4_SENTINEL" not in str(result)
    assert port.reconcile_effect(native_call) == result
    for invalid in (
        {"anchor_id": "ungranted"},
        {"anchor_id": None, "start_ms": 0},
        {"anchor_id": None, "path": "video.mp4"},
    ):
        with pytest.raises(ToolRejected):
            port.invoke(call(binding, "read_nearby_subtitles", invalid), invalid)
    repo.close()


def test_manual_subtitle_crossing_window_is_a_gap_without_outside_text(tmp_path):
    repo, binding, anchor = owned_window(tmp_path)
    course = cached_track(repo, binding, anchor)
    store = TranscriptStore(repo)
    segment = next(
        item for item in store.view(course, "frozen_track", first_unit=2, limit=1).segments
    )
    store.edit(
        course,
        "frozen_track",
        TranscriptEdit(
            operation_id="retime",
            expected_edit_head_id=None,
            changes=[
                SubtitleChange(
                    segment_id=segment.segment_id,
                    raw_revision_id=segment.raw_revision_id,
                    text="OUTSIDE_MANUAL_TEXT_SENTINEL",
                    start_ms=0,
                    end_ms=150000,
                )
            ],
        ),
    )
    args = {"anchor_id": anchor["anchor_id"]}
    result = NativeBusiness(repo).invoke(call(binding, "read_nearby_subtitles", args), args)
    assert result["status"] == "partial"
    assert result["gaps"][0]["code"] == "SUBTITLE_SEGMENT_CROSSES_SCOPE"
    assert "OUTSIDE_MANUAL_TEXT_SENTINEL" not in str(result)
    repo.close()
