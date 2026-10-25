"""Local scheduling fences, without provider, decoder or user media."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update
from test_native_business import prepare_chat
from test_question_context import video_course

from learning_app.config import PROJECT_ROOT
from learning_app.errors import AppError
from learning_app.storage import schema as business
from learning_app.workbench import schema as s
from learning_app.workbench.common import now
from learning_app.workbench.focus import PreparationFocus
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.processing import PreparationService
from learning_app.workbench.subtitle_waits import SubtitleWaits


def owned_window(tmp_path):
    repo, course, _, source, selected = video_course(tmp_path)
    repo.create_chat(course, "native_chat")
    _, binding = prepare_chat(
        repo,
        course,
        selected,
        "native_window",
        source_locators=[
            {
                "origin": "video_question",
                "asset_id": source["asset_id"],
                "source_version": source["source_version"],
                "time_ms": 75000,
            }
        ],
    )
    port = NativeBusiness(repo)
    port.ensure_chat_scope(binding.generation_id, binding)
    with repo.engine.connect() as conn:
        scope = port._scope(conn, binding.generation_id)
    return repo, binding, scope["grants"]["subtitle_anchors"][0]


def local_job(
    repo, binding, anchor, *, job="native_job", grid=2, scope="unit", state="queued", owned=True
):
    params = {"requested_unit_index": grid, "scope_indexes": [0], "track_id": "fixture_no_decoder"}
    if owned:
        params.update(
            native_read_run_id=binding.generation_id, native_anchor_id=anchor["anchor_id"]
        )
    course = repo.get_chat("native_chat")["course_id"]
    with repo.engine.begin() as conn:
        conn.execute(
            insert(s.preparations).values(
                job_id=job,
                course_id=course,
                kind="asr",
                asset_id=anchor["asset_id"],
                source_version=anchor["source_version"],
                source_digest=anchor["source_digest"],
                scope=scope,
                parameters=params,
                state=state,
                revision=0,
                epoch=1,
                created_at=now(),
                updated_at=now(),
            )
        )
        conn.execute(
            insert(s.preparation_units).values(
                job_id=job, unit_index=0, state="pending", priority=0, local_attempts=0
            )
        )
    return job


def deadline(seconds=20):
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def test_question_owned_audio_job_is_the_only_foreground_wait_claim_and_pause_is_untouched(
    tmp_path,
):
    repo, binding, anchor = owned_window(tmp_path)
    native = local_job(repo, binding, anchor)
    batch = local_job(
        repo, binding, anchor, job="manual_batch", scope="full", state="paused", owned=False
    )
    store, waits = PreparationStore(repo), SubtitleWaits(repo)
    assert store.claim(native_wait_only=True) is None
    waits.register(binding.generation_id, anchor["anchor_id"], native, deadline())
    assert waits.has_active()
    lease = store.claim(native_wait_only=True)
    assert lease["job_id"] == native and waits.job_is_active(native)
    waits.close(binding.generation_id, anchor["anchor_id"])
    assert not waits.has_active() and not store.release(lease)
    with repo.engine.connect() as conn:
        assert (
            conn.execute(
                select(s.preparations.c.state).where(s.preparations.c.job_id == batch)
            ).scalar_one()
            == "paused"
        )
        assert (
            conn.execute(
                select(s.preparations.c.revision).where(s.preparations.c.job_id == batch)
            ).scalar_one()
            == 0
        )
        row = (
            conn.execute(select(s.preparations).where(s.preparations.c.job_id == native))
            .mappings()
            .one()
        )
        assert row["state"] == "cancelled" and row["epoch"] > lease["lease_epoch"]
        assert (
            conn.execute(
                select(s.preparation_events.c.action)
                .where(s.preparation_events.c.job_id == native)
                .order_by(s.preparation_events.c.revision.desc())
            )
            .scalars()
            .first()
            == "native_read_wait_closed"
        )
    repo.close()


@pytest.mark.parametrize("grid,scope", [(0, "unit"), (4, "unit"), (2, "full")])
def test_no_entire_track_or_outside_grid_can_enter_question_wait(tmp_path, grid, scope):
    repo, binding, anchor = owned_window(tmp_path)
    job = local_job(repo, binding, anchor, grid=grid, scope=scope)
    with pytest.raises(AppError) as denied:
        SubtitleWaits(repo).register(binding.generation_id, anchor["anchor_id"], job, deadline())
    assert denied.value.code == "NATIVE_LOCAL_READ_SCOPE_DENIED"
    assert PreparationStore(repo).claim() is None
    repo.close()


def test_expired_or_cancelled_model_owner_cannot_schedule_audio_after_restart(tmp_path):
    repo, binding, anchor = owned_window(tmp_path)
    job = local_job(repo, binding, anchor)
    waits = SubtitleWaits(repo)
    waits.register(binding.generation_id, anchor["anchor_id"], job, deadline(-1))
    assert not waits.has_active() and PreparationStore(repo).claim() is None
    assert not PreparationStore(repo).has_urgent()
    # An unrelated explicit local batch can still run normally.
    local_job(repo, binding, anchor, job="ordinary", scope="full", owned=False)
    assert PreparationStore(repo).claim()["job_id"] == "ordinary"
    repo.close()


def test_later_reader_focus_cannot_demote_or_starve_a_waiting_question(tmp_path):
    repo, binding, anchor = owned_window(tmp_path)
    native = local_job(repo, binding, anchor)
    waits, store = SubtitleWaits(repo), PreparationStore(repo)
    waits.register(binding.generation_id, anchor["anchor_id"], native, deadline())
    lease = store.claim(native_wait_only=True)
    ordinary = local_job(repo, binding, anchor, job="new_playhead", owned=False)
    with repo.engine.begin() as conn:
        PreparationFocus.clear(conn)
        conn.execute(
            update(s.preparation_units)
            .where(s.preparation_units.c.job_id == ordinary)
            .values(priority=0)
        )
        assert (
            conn.execute(
                select(s.preparation_units.c.priority).where(s.preparation_units.c.job_id == native)
            ).scalar_one()
            == 0
        )
    assert not store.should_yield(lease, native_wait_only=True)
    waits.close(binding.generation_id, anchor["anchor_id"])
    repo.close()


async def test_native_audio_wait_can_run_during_qa_but_yields_to_interaction_and_owner_cancel(
    tmp_path, monkeypatch
):
    repo, binding, anchor = owned_window(tmp_path)
    job = local_job(repo, binding, anchor)
    waits = SubtitleWaits(repo)
    waits.register(binding.generation_id, anchor["anchor_id"], job, deadline(30))
    calls, interrupted, interacting = [], threading.Event(), {"value": False}

    class LocalFixture:
        def __init__(self, *_, resource_policy=None):
            assert resource_policy is None

        def run(self, lease, cancelled):
            calls.append(lease["lease_id"])
            while not cancelled.wait(0.01):
                pass
            interrupted.set()
            raise AppError("PREPARATION_INTERRUPTED", "controlled owned worker yielded")

    monkeypatch.setattr("learning_app.workbench.processing.LocalTranscriber", LocalFixture)
    service = PreparationService(
        repo, PROJECT_ROOT, is_busy=lambda: True, is_interacting=lambda: interacting["value"]
    )
    await service.open()
    try:
        async with asyncio.timeout(5):
            while len(calls) < 1:
                await asyncio.sleep(0.01)
        interacting["value"] = True
        async with asyncio.timeout(5):
            while not interrupted.is_set():
                await asyncio.sleep(0.01)
        count = len(calls)
        await asyncio.sleep(0.2)
        assert len(calls) == count == 1
        interrupted.clear()
        interacting["value"] = False
        async with asyncio.timeout(5):
            while len(calls) < 2:
                await asyncio.sleep(0.01)
        with repo.engine.begin() as conn:
            conn.execute(
                update(business.generations)
                .where(business.generations.c.generation_id == binding.generation_id)
                .values(cancel_requested=True)
            )
        async with asyncio.timeout(5):
            while not interrupted.is_set():
                await asyncio.sleep(0.01)
        waits.close(binding.generation_id, anchor["anchor_id"])
        assert not waits.has_active()
        assert len(set(calls)) == 2
    finally:
        await service.close()
        repo.close()
