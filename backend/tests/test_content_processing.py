"""Original text-only cuts retain Unicode, source versions and existing task plans."""

import asyncio
import json
import wave

import pytest
from sqlalchemy import insert, select, update
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.service import GenerationService
from learning_app.storage.repository import _hash
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.common import now
from learning_app.workbench.content_processing import CHUNKER_VERSION, ContentProcessor
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import MAX_WORK_CHARACTERS, TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore
from learning_app.workbench.transcript_models import SubtitleCapture
from learning_app.workbench.transcripts import TranscriptStore


def stored_subtitle(tmp_path, text):
    repo, course, root, _ = learning_course(tmp_path)
    with wave.open(str(root / "synthetic.wav"), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 16000)
    opened = CourseFileStore(repo).open(course, "synthetic.wav")
    digest = repo.resolve_source(opened["asset_id"], opened["source_version"])["content_hash"]
    payload = {"raw": {"segments": [{"text": text, "start_ms": 123, "end_ms": 987}]}}
    with repo.engine.begin() as conn:
        conn.execute(
            insert(s.transcript_tracks).values(
                track_id="content_track",
                course_id=course,
                asset_id=opened["asset_id"],
                source_version=opened["source_version"],
                source_digest=digest,
                parameters={"fixture": "stored synthetic captions, no decoder/ASR invocation"},
                probe={"unit_indexes": [0], "audio_start_ms": 0, "audio_end_ms": 1000},
                created_at=now(),
            )
        )
        conn.execute(
            insert(s.transcript_units).values(
                track_id="content_track",
                unit_index=0,
                revision_id="trraw_content_fixture",
                payload=payload,
                payload_hash=_hash(payload),
                created_at=now(),
            )
        )
    view = TranscriptStore(repo).view(course, "content_track")
    source = TranscriptStore(repo).capture(
        course,
        "content_track",
        SubtitleCapture(
            segment_id=view.segments[0].segment_id, revision_id=view.segments[0].revision_id
        ),
    )
    return repo, course, source


@pytest.mark.parametrize(
    "text",
    ["第一行\n第二行\n" * 6000, "a" * 23998 + "🙂" + "b" * 30_000, "\r\n\n🙂数学" * 7000],
    ids=["multiline", "unicode_boundary", "blank_lines_unicode"],
)
def test_actual_fixed_helper_proposes_lossless_unicode_ranges(text):
    ranges = ContentProcessor().ranges(text, MAX_WORK_CHARACTERS)
    assert ranges and len(ranges) >= 2
    assert "".join(text[start:end] for start, end in ranges) == text
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert all(0 < end - start <= MAX_WORK_CHARACTERS for start, end in ranges)
    assert all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))


@pytest.mark.parametrize(
    "text,method",
    [
        ("a" * 23998 + "🙂" + "b" * 30_000, "deepstudent_line_hints"),
        ("\n" * 25000 + "tail", "exact_codepoint_fallback"),
    ],
    ids=["long_line_hint", "blank_line_fallback"],
)
def test_formal_planning_uses_original_hints_or_explicit_exact_fallback_without_repeated_reads(
    tmp_path, monkeypatch, text, method
):
    repo, course, source = stored_subtitle(tmp_path, text)
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="subtitle_handout",
            kind="handout",
            instruction="完整整理所选字幕，不补画面或其他段落。",
            settings=settings(),
            coverage_mode="complete",
            sources_only=True,
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "长字幕讲义"},
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id, budget=100)
    assert TaskPlanner(repo).plan(course, task.task_id)
    with repo.engine.connect() as conn:
        row = conn.execute(
            select(s.tasks.c.scope_spec).where(s.tasks.c.task_id == task.task_id)
        ).scalar_one()
        specs = list(
            conn.execute(
                select(s.task_units.c.work_spec)
                .where(s.task_units.c.task_id == task.task_id)
                .order_by(s.task_units.c.ordinal)
            ).scalars()
        )
    assert row["source_chunker"] == CHUNKER_VERSION
    items = [item for spec in specs for item in spec["inputs"]]
    assert {item["source_chunker"] for item in items} == {method}
    selected = [SourceStore(repo).snapshot(course, item["snapshot_id"]) for item in items]
    assert "".join(snapshot.text for snapshot in selected) == source.text
    assert all(
        snapshot.reference.kind == "snapshot_slice"
        and snapshot.reference.parent_hash == source.content_hash
        for snapshot in selected
    )
    for snapshot in selected:
        SourceStore(repo).require_current(snapshot)
    assert source.reference.start_ms == 123 and source.reference.end_ms == 987
    assert source.reference.transcript_revision_id == "trraw_content_fixture"
    assert all(snapshot.content_role == "original" for snapshot in selected)

    def no_repeated_helper(*_args):
        pytest.fail("an already fully assigned source must not rerun the helper")

    monkeypatch.setattr(ContentProcessor, "ranges", no_repeated_helper)
    assert not TaskPlanner(repo).plan(course, task.task_id)
    assert repo.counts()["outbound_attempts"] == 0
    repo.close()


def test_existing_task_keeps_its_old_codepoint_cuts_when_new_helper_is_available(
    tmp_path, monkeypatch
):
    repo, course, source = stored_subtitle(tmp_path, "a" * 23998 + "🙂" + "b" * 30_000)
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="legacy_source_plan",
            kind="handout",
            instruction="旧任务保留原切块",
            settings=settings(),
            coverage_mode="complete",
            sources_only=True,
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "旧计划"},
        ),
    )
    with repo.engine.begin() as conn:
        row = dict(
            conn.execute(
                select(s.tasks.c.scope_spec).where(s.tasks.c.task_id == task.task_id)
            ).scalar_one()
        )
        row.pop("source_chunker")
        conn.execute(
            update(s.tasks).where(s.tasks.c.task_id == task.task_id).values(scope_spec=row)
        )

    def new_helper_must_not_change_old_plan(*_args):
        pytest.fail("old task was not admitted with the new chunker")

    monkeypatch.setattr(ContentProcessor, "ranges", new_helper_must_not_change_old_plan)
    TaskCoverage(repo).refresh(course, task.task_id, budget=100)
    assert TaskPlanner(repo).plan(course, task.task_id)
    with repo.engine.connect() as conn:
        specs = list(
            conn.execute(
                select(s.task_units.c.work_spec)
                .where(s.task_units.c.task_id == task.task_id)
                .order_by(s.task_units.c.ordinal)
            ).scalars()
        )
    items = [item for spec in specs for item in spec["inputs"]]
    assert items[0]["char_start"] == 0 and items[0]["char_end"] == 24000
    assert all("source_chunker" not in item for item in items)
    repo.close()


def test_helper_has_no_media_window_or_arbitrary_path_operation(tmp_path):
    processor = ContentProcessor()
    with pytest.raises(AppError) as error:
        processor.invoke(
            "forbidden", fields={"operation": "sectionTranscriptWindow", "path": str(tmp_path)}
        )
    assert error.value.code == "CONTENT_HELPER_INPUT_INVALID"
    processor.directory = tmp_path / "missing_resource"
    with pytest.raises(AppError) as error:
        processor.ranges("text", 100)
    assert error.value.code == "CONTENT_RESOURCE_NOT_PREPARED"


@pytest.mark.host
async def test_actual_host_receives_exact_frozen_long_caption_parts_without_any_images_or_asr(
    tmp_path,
):
    from test_native_business_host import runtime

    text = "first\n" * 4100 + "a" * 10000 + "🙂tail"
    repo, course, source = stored_subtitle(tmp_path, text)
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="long_caption_host",
            kind="handout",
            instruction="整理明确选中的整段字幕文字，不读取画面或其他字幕。",
            settings=settings(),
            coverage_mode="complete",
            sources_only=True,
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "长字幕讲义"},
        ),
    )
    adapter, _, registry, boundary = runtime(tmp_path, repo, source, bare=True)
    original = boundary._fixture
    observed = []

    def fixture(handler, request, scenario, row, observer):
        run = TaskRunStore(repo).internal(row["generation_id"])
        assert not run["request_image_proofs"] and run["input_contract"]["image_hashes"] == []
        inputs = run["input_contract"]["inputs"]
        observed.extend(
            SourceStore(repo).snapshot(course, item["snapshot_id"]).text for item in inputs
        )
        body = {
            "blocks": [
                {
                    "block_id": "summary",
                    "markdown": "只整理本次实际字幕文字。",
                    "attribution": "source_paraphrase",
                    "source_snapshot_ids": [item["snapshot_id"] for item in inputs],
                }
            ]
        }
        return original(
            handler, request, {"text_chunks": [json.dumps(body, ensure_ascii=False)]}, row, observer
        )

    boundary._fixture = fixture
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), 35)
            result = TaskStore(repo).get(course, task.task_id)
            assert result.state == "completed" and len(observed) == len(result.units) == 2
            assert "".join(observed) == text
            coverage = TaskCoverage(repo).get(course, task.task_id)
            assert len(coverage.leaves) == 1 and coverage.sources[0].processed_leaf_count == 1
            assert len(ArtifactStore(repo).list(course)) == 1
            assert all(identity in registry.released for identity in registry.scopes)
            assert boundary.actual_outbound_attempts == 0
        finally:
            await executor.close()
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
    repo.close()
