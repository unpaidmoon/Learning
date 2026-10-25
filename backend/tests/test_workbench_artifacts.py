from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from workbench_support import learning_course, note

from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.models import (
    ArtifactRestore,
    ArtifactUpdate,
    BlockChange,
    MarkdownBlock,
    PageRegion,
)
from learning_app.workbench.sources import SourceStore


@pytest.fixture
def course(tmp_path):
    value = learning_course(tmp_path)
    yield value
    value[0].close()


def test_saved_selection_exact_and_identity_includes_locator_and_course(course):
    repo, course_id, _, first = course
    sources = SourceStore(repo)
    assert first.text == "Only this selected sentence"
    assert (
        sources.capture_text(
            course_id, first.reference.asset_id, first.reference.source_version, 0, 27
        )
        == first
    )
    second = sources.capture_text(
        course_id, first.reference.asset_id, first.reference.source_version, 1, 27
    )
    assert first.snapshot_id != second.snapshot_id
    assert sources.snapshot(course_id, first.snapshot_id) == first
    other = repo.create_course("另一课")
    with pytest.raises(AppError, match="选区") as error:
        sources.snapshot(other["course_id"], first.snapshot_id)
    assert error.value.code == "SOURCE_SELECTION_DENIED"
    assert repo.counts()["outbound_attempts"] == 0


def test_old_snapshot_retained_when_source_changes_without_redirect(course):
    repo, course_id, root, snapshot = course
    (root / "lesson.txt").write_text("完全不同的新版", encoding="utf-8")
    assert SourceStore(repo).snapshot(course_id, snapshot.snapshot_id).text == snapshot.text
    with pytest.raises(AppError) as error:
        SourceStore(repo).require_current(snapshot)
    assert error.value.code == "SOURCE_VERSION_UNAVAILABLE"


def test_selection_denies_cross_course_and_video_even_if_registered(course):
    repo, course_id, root, snapshot = course
    other = repo.create_course("其他课")
    with pytest.raises(AppError) as error:
        SourceStore(repo).capture_text(
            other["course_id"], snapshot.reference.asset_id, snapshot.reference.source_version, 0, 1
        )
    assert error.value.code == "SOURCE_COURSE_DENIED"
    fake = root / "video.mp4"
    fake.write_bytes(b"fixture: not decoded")
    source = repo.register_source(course_id, fake)
    with pytest.raises(AppError) as error:
        SourceStore(repo).capture_text(
            course_id, source["asset_id"], source["source_version"], 0, 1
        )
    assert error.value.code == "SOURCE_KIND_DENIED"


def test_asset_create_default_index_idempotent_and_restart(course):
    repo, course_id, _, snapshot = course
    value = note(snapshot_id=snapshot.snapshot_id)
    store = ArtifactStore(repo)
    created = store.create(course_id, value)
    assert store.create(course_id, value) == created
    assert created.artifact.indexed and created.artifact.content_role == "user_note"
    assert store.search(course_id, "学习内容")[0].revision_id == created.artifact.head_revision_id
    reopened = Repository(repo.root)
    try:
        assert (
            ArtifactStore(reopened).get(course_id, created.artifact.artifact_id) == created.artifact
        )
    finally:
        reopened.close()


def test_parallel_repeat_creates_single_product(course):
    repo, course_id, _, _ = course
    store = ArtifactStore(repo)
    with ThreadPoolExecutor(max_workers=4) as workers:
        values = list(workers.map(lambda _: store.create(course_id, note()), range(8)))
    assert len({item.artifact.artifact_id for item in values}) == 1
    with repo.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(s.artifacts)) == 1
        assert conn.scalar(select(func.count()).select_from(s.revisions)) == 1


def test_reused_operation_cannot_overwrite_or_cross_course(course):
    repo, course_id, _, _ = course
    store = ArtifactStore(repo)
    created = store.create(course_id, note())
    with pytest.raises(AppError) as error:
        store.create(course_id, note(text="不同内容"))
    assert error.value.code == "LEARNING_OPERATION_CONFLICT"
    other = repo.create_course("其他")
    with pytest.raises(AppError):
        store.create(other["course_id"], note())
    assert (
        store.get(course_id, created.artifact.artifact_id).revision.body.blocks[0].markdown
        == "学习内容"
    )


def test_update_changes_requested_block_and_conflict_keeps_both_versions(course):
    repo, course_id, _, _ = course
    store = ArtifactStore(repo)
    original = store.create(course_id, note()).artifact

    def edit(op, text):
        return ArtifactUpdate(
            operation_id=op,
            expected_revision_id=original.head_revision_id,
            changes=[
                BlockChange(
                    block_id="paragraph_1",
                    replacement=MarkdownBlock(
                        block_id="paragraph_1",
                        markdown=text,
                        attribution="user_authored",
                    ),
                )
            ],
        )

    manual = store.update(course_id, original.artifact_id, edit("manual_edit", "手动新稿"))
    stale = store.update(course_id, original.artifact_id, edit("stale_edit", "并发候选"))
    assert manual.outcome == "applied" and stale.outcome == "conflict"
    assert stale.conflict.expected_revision_id == original.head_revision_id
    assert store.get(course_id, original.artifact_id) == manual.artifact
    history = store.history(course_id, original.artifact_id)
    assert {item.body.blocks[0].markdown for item in history} == {
        "学习内容",
        "手动新稿",
        "并发候选",
    }
    assert store.update(course_id, original.artifact_id, edit("stale_edit", "并发候选")) == stale
    assert len(store.history(course_id, original.artifact_id)) == 3


def test_restore_creates_new_revision_and_updates_index_without_deleting_history(course):
    repo, course_id, _, _ = course
    store = ArtifactStore(repo)
    original = store.create(course_id, note()).artifact
    changed = store.update(
        course_id,
        original.artifact_id,
        ArtifactUpdate(
            operation_id="edit_title",
            expected_revision_id=original.head_revision_id,
            title="新标题",
        ),
    ).artifact
    restored = store.restore(
        course_id,
        original.artifact_id,
        ArtifactRestore(
            operation_id="undo",
            expected_revision_id=changed.head_revision_id,
            restore_revision_id=original.head_revision_id,
        ),
    ).artifact
    assert restored.title == original.title
    assert restored.head_revision_id not in {original.head_revision_id, changed.head_revision_id}
    assert len(store.history(course_id, original.artifact_id)) == 3
    assert store.search(course_id, "新标题") == []


def test_source_attribution_and_cross_course_snapshot_are_validated(course):
    repo, course_id, _, snapshot = course
    value = note(text="伪造原话", snapshot_id=snapshot.snapshot_id)
    value.body.blocks[0].attribution = "source_quote"
    with pytest.raises(AppError) as error:
        ArtifactStore(repo).create(course_id, value)
    assert error.value.code == "SOURCE_QUOTE_MISMATCH"
    other = repo.create_course("其他")
    with pytest.raises(AppError) as error:
        ArtifactStore(repo).create(other["course_id"], note(snapshot_id=snapshot.snapshot_id))
    assert error.value.code == "SOURCE_SELECTION_DENIED"


def test_generated_or_user_note_keeps_role_when_selected_as_source(course):
    repo, course_id, _, _ = course
    artifact = ArtifactStore(repo).create(course_id, note()).artifact
    snapshot = SourceStore(repo).capture_artifact(
        course_id,
        artifact.artifact_id,
        artifact.head_revision_id,
        "paragraph_1",
    )
    assert snapshot.content_role == "user_note"
    assert snapshot.reference.kind == "artifact"
    assert "original" not in ArtifactStore(repo).search(course_id, "学习内容")[0].content_role


@pytest.mark.parametrize(
    "point", ["learning_artifact_before_publish", "learning_revision_before_publish"]
)
def test_fault_rolls_back_product_revision_index_and_receipt_together(course, point):
    repo, course_id, _, _ = course
    store = ArtifactStore(repo)
    original = store.create(course_id, note()).artifact

    def hook(name, _detail):
        if name == point:
            raise RuntimeError("injected durable boundary")

    repo.fault_hook = hook
    with pytest.raises(RuntimeError, match="injected"):
        if point == "learning_artifact_before_publish":
            store.create(course_id, note("faulted_creation"))
        else:
            store.update(
                course_id,
                original.artifact_id,
                ArtifactUpdate(
                    operation_id="faulted_update",
                    expected_revision_id=original.head_revision_id,
                    title="不可发布",
                ),
            )
    assert len(store.list(course_id)) == 1
    assert len(store.history(course_id, original.artifact_id)) == 1
    assert store.get(course_id, original.artifact_id) == original
    with repo.engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(s.operations)) == 1


def test_invalid_regions_not_widened_or_reordered():
    for values in [
        dict(x0=0.8, y0=0, x1=0.2, y1=1),
        dict(x0=0, y0=0, x1=1.1, y1=1),
        dict(x0=0, y0=0, x1=float("nan"), y1=1),
    ]:
        with pytest.raises(ValidationError):
            PageRegion(**values)
