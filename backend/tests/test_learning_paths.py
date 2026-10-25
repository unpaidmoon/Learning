"""Suggested paths retain real user evidence without gatekeeping or fake mastery."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from test_practice_reviews import cards, question
from workbench_support import learning_course

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.workbench import path_schema as p
from learning_app.workbench import practice_schema as practice
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.models import (
    ArtifactBody,
    ArtifactCreate,
    ArtifactRestore,
    ArtifactUpdate,
    BlockChange,
    PathQuestionLink,
    PathStepBlock,
    PathStepContent,
)
from learning_app.workbench.path_models import PathAction
from learning_app.workbench.paths import PathStore
from learning_app.workbench.practice import PracticeStore
from learning_app.workbench.practice_models import (
    AnswerInput,
    ReviewControl,
    ReviewPreferenceInput,
    ReviewRatingInput,
    ReviewSessionInput,
    ReviewUndo,
    UserVerdict,
)
from learning_app.workbench.reviews import ReviewStore, utc_ms
from learning_app.workbench.sources import SourceStore


def route(repo, course_id, *, links=(), count=2, operation="make_path"):
    return (
        ArtifactStore(repo)
        .create(
            course_id,
            ArtifactCreate(
                operation_id=operation,
                kind="learning_path",
                title="按目标学习原句",
                body=ArtifactBody(
                    blocks=[
                        PathStepBlock(
                            block_id=f"point_{i}",
                            markdown=f"先学习第{i}个概念，可以自由跳过。",
                            attribution="user_authored",
                            step=PathStepContent(
                                title=f"知识点{i}",
                                objective="辨别选中的原句与其他句子",
                                suggested_order=i,
                                recall_prompt="不看资料，复述所选的句子。",
                                explanation="核对原始选区，不能推测相邻内容。",
                                questions=list(links),
                            ),
                        )
                        for i in range(count)
                    ]
                ),
            ),
        )
        .artifact
    )


def action(
    artifact, operation="navigate", *, revision=0, block="point_0", kind="navigate", **extra
):
    return PathAction(
        operation_id=operation,
        expected_revision=revision,
        content_revision_id=artifact.head_revision_id,
        block_id=block,
        action=kind,
        **extra,
    )


def test_free_skip_reveal_self_report_never_create_answer_or_mastery(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    artifact = route(repo, course)
    store = PathStore(repo)
    initial = store.get(course, artifact.artifact_id)
    assert initial.revision == 0 and initial.current_block_id is None
    schedule = initial.steps[0].review.model_dump()
    skipped = store.act(course, artifact.artifact_id, action(artifact, "skip", kind="skip"))
    assert skipped.path.steps[0].skipped and skipped.path.current_block_id == "point_0"
    revealed = store.act(
        course,
        artifact.artifact_id,
        action(
            artifact,
            "reveal",
            revision=1,
            kind="view_explanation",
        ),
    )
    assert revealed.path.steps[0].explanation_seen
    confident = store.act(
        course,
        artifact.artifact_id,
        action(
            artifact,
            "self",
            revision=2,
            kind="self_assess",
            self_assessment="confident",
        ),
    )
    assert confident.path.steps[0].self_assessment == "confident"
    moved = store.act(
        course, artifact.artifact_id, action(artifact, "next", revision=3, block="point_1")
    )
    assert moved.path.current_block_id == "point_1"
    assert moved.path.steps[0].review.model_dump() == schedule
    events = store.events(course, artifact.artifact_id, limit=2)
    assert len(events.events) == 2 and events.next_before == 3
    assert [e.revision for e in store.events(course, artifact.artifact_id, before=3).events] == [
        2,
        1,
    ]
    assert all(e.detail["payload"]["grants_mastery"] is False for e in events.events)
    with repo.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(practice.answers)).scalar_one() == 0
        state = conn.execute(select(p.path_states)).mappings().one()
        assert state["progress"]["score"] == 0 and state["progress"]["quiz_attempts"] == []
    reopened = Repository(repo.root)
    assert PathStore(reopened).get(course, artifact.artifact_id) == moved.path


def test_assessment_evidence_tracks_actual_question_revision_and_user_override(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    quiz = (
        ArtifactStore(repo)
        .create(
            course,
            ArtifactCreate(
                operation_id="questions",
                kind="question_set",
                title="实际练习",
                body=ArtifactBody(blocks=[question()]),
            ),
        )
        .artifact
    )
    link = PathQuestionLink(
        artifact_id=quiz.artifact_id, revision_id=quiz.head_revision_id, block_id="q1"
    )
    artifact = route(repo, course, links=[link], count=1)
    store, answers = PathStore(repo), PracticeStore(repo)
    store.act(course, artifact.artifact_id, action(artifact, "revealed", kind="view_explanation"))
    assert store.get(course, artifact.artifact_id).steps[0].answers[0].latest_answer is None
    answer = answers.submit(
        course,
        quiz.artifact_id,
        AnswerInput(
            operation_id="actual_answer",
            question_revision_id=quiz.head_revision_id,
            block_id="q1",
            text="B",
        ),
    )
    assert (
        store.get(course, artifact.artifact_id)
        .steps[0]
        .answers[0]
        .latest_answer.effective_assessment.verdict
        == "wrong"
    )
    corrected = answers.override(
        course,
        answer.answer_id,
        UserVerdict(
            operation_id="correction",
            expected_revision=answer.assessment_revision,
            verdict="pending",
            reason="此项需要重新核对，先不计对错。",
        ),
    )
    evidence = store.get(course, artifact.artifact_id).steps[0].answers[0].latest_answer
    assert (
        evidence == corrected
        and evidence.text == "B"
        and evidence.effective_assessment.score is None
    )
    edited = (
        ArtifactStore(repo)
        .update(
            course,
            quiz.artifact_id,
            ArtifactUpdate(
                operation_id="change_question",
                expected_revision_id=quiz.head_revision_id,
                changes=[BlockChange(block_id="q1", replacement=question(reference="B"))],
            ),
        )
        .artifact
    )
    answers.submit(
        course,
        quiz.artifact_id,
        AnswerInput(
            operation_id="new_version_answer",
            question_revision_id=edited.head_revision_id,
            block_id="q1",
            text="B",
        ),
    )
    # An answer to a newer question never migrates backwards into the old link.
    assert (
        store.get(course, artifact.artifact_id).steps[0].answers[0].latest_answer.answer_id
        == answer.answer_id
    )


def test_content_edits_reorder_keep_old_activity_bound_and_undo_restores_identity(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    artifact = route(repo, course)
    store = PathStore(repo)
    store.act(
        course,
        artifact.artifact_id,
        action(artifact, "self", kind="self_assess", self_assessment="confident"),
    )
    blocks = artifact.revision.body.blocks
    modified = blocks[0].model_copy(
        update={
            "step": blocks[0].step.model_copy(
                update={"objective": "新的学习目标", "suggested_order": 3}
            )
        }
    )
    result = ArtifactStore(repo).update(
        course,
        artifact.artifact_id,
        ArtifactUpdate(
            operation_id="edit",
            expected_revision_id=artifact.head_revision_id,
            changes=[BlockChange(block_id=modified.block_id, replacement=modified)],
        ),
    )
    current = store.get(course, artifact.artifact_id)
    assert [s.block.block_id for s in current.steps] == ["point_1", "point_0"]
    assert (
        current.steps[1].self_assessment == "confident"
        and not current.steps[1].self_assessment_current
    )
    with pytest.raises(AppError, match="路线内容已修改"):
        store.act(course, artifact.artifact_id, action(artifact, "stale", revision=1))
    restored = (
        ArtifactStore(repo)
        .restore(
            course,
            artifact.artifact_id,
            ArtifactRestore(
                operation_id="undo",
                expected_revision_id=result.artifact.head_revision_id,
                restore_revision_id=artifact.head_revision_id,
            ),
        )
        .artifact
    )
    view = store.get(course, restored.artifact_id)
    assert (
        view.steps[0].self_assessment_current
        and len(store.events(course, artifact.artifact_id).events) == 1
    )


def test_action_idempotence_concurrent_cas_and_failure_rolls_back(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    artifact = route(repo, course)
    store = PathStore(repo)
    first = action(artifact)
    receipt = store.act(course, artifact.artifact_id, first)
    assert store.act(course, artifact.artifact_id, first) == receipt

    def compete(index):
        try:
            return store.act(
                course, artifact.artifact_id, action(artifact, f"move_{index}", revision=1)
            ).event.revision
        except AppError as e:
            return e.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(compete, range(2)))
    assert sorted(map(str, result)) == ["2", "PATH_VERSION_CONFLICT"]
    original_hook = repo._hook

    def fail(name, **kwargs):
        if name == "learning_path_action_before_commit":
            raise RuntimeError("local_commit_interrupted")
        original_hook(name, **kwargs)

    repo._hook = fail
    value = action(artifact, "recover", revision=2)
    with pytest.raises(RuntimeError, match="local_commit_interrupted"):
        store.act(course, artifact.artifact_id, value)
    repo._hook = original_hook
    assert store.get(course, artifact.artifact_id).revision == 2
    assert store.act(course, artifact.artifact_id, value).event.revision == 3


def test_knowledge_review_shares_global_daily_limit_and_keeps_pause_on_edit(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    cards(repo, course, count=5)
    artifact = route(repo, course, count=8)
    clock = utc_ms() + 2 * 86_400_000
    reviews = ReviewStore(repo, clock=lambda: clock)
    reviews.set_preferences(
        course, ReviewPreferenceInput(operation_id="limit", expected_revision=0, daily_limit=7)
    )
    today = reviews.today()
    assert today.admitted_count == 7
    assert sorted(item.kind for item in today.items) == ["card"] * 5 + ["knowledge"] * 2
    assert reviews.today(kind="knowledge").admitted_count == 7
    item = next(i for i in today.items if i.kind == "knowledge")
    session = reviews.open_session(
        course,
        item.review_id,
        ReviewSessionInput(operation_id="open", expected_revision=item.schedule_revision),
    )
    with pytest.raises(AppError, match="请先"):
        reviews.rate(
            course,
            item.review_id,
            ReviewRatingInput(
                operation_id="early_rate",
                expected_revision=item.schedule_revision,
                session_id=session.session_id,
                rating=4,
            ),
        )
    reviews.reveal(
        course,
        item.review_id,
        session.session_id,
        ReviewSessionInput(operation_id="reveal", expected_revision=item.schedule_revision),
    )
    rated = reviews.rate(
        course,
        item.review_id,
        ReviewRatingInput(
            operation_id="rate",
            expected_revision=item.schedule_revision,
            session_id=session.session_id,
            rating=3,
        ),
    )
    assert rated.item.kind == "knowledge" and rated.item.due_ms > clock
    undone = reviews.undo(
        course,
        item.review_id,
        ReviewUndo(
            operation_id="undo_rate",
            expected_revision=rated.item.schedule_revision,
            event_id=rated.event_id,
        ),
    )
    assert undone.scheduler_state == item.scheduler_state
    paused = reviews.control(
        course,
        item.review_id,
        ReviewControl(
            operation_id="pause", expected_revision=undone.schedule_revision, action="pause"
        ),
    )
    ArtifactStore(repo).update(
        course,
        artifact.artifact_id,
        ArtifactUpdate(
            operation_id="rename_path",
            expected_revision_id=artifact.head_revision_id,
            title="修改路线标题",
        ),
    )
    assert reviews.get(course, item.review_id).queue_state == paused.queue_state == "paused"
    assert not PathStore(repo).get(course, artifact.artifact_id).steps[0].answers


def test_link_permission_model_read_scope_and_strict_user_contract(tmp_path):
    repo, course, root, source = learning_course(tmp_path)
    quiz = (
        ArtifactStore(repo)
        .create(
            course,
            ArtifactCreate(
                operation_id="quiz",
                kind="question_set",
                title="题目",
                body=ArtifactBody(blocks=[question()]),
            ),
        )
        .artifact
    )
    link = PathQuestionLink(
        artifact_id=quiz.artifact_id, revision_id=quiz.head_revision_id, block_id="q1"
    )
    artifact = route(repo, course, links=[link], count=1)
    with repo.engine.connect() as conn:
        with pytest.raises(AppError, match="未读取"):
            PathStore.validate_links(
                conn, course, artifact.revision.body, allowed_snapshots=[source.snapshot_id]
            )
    actual = SourceStore(repo).capture_artifact(
        course, quiz.artifact_id, quiz.head_revision_id, "q1"
    )
    with repo.engine.connect() as conn:
        PathStore.validate_links(
            conn, course, artifact.revision.body, allowed_snapshots=[actual.snapshot_id]
        )
    from learning_app.course_files import CourseFileStore

    other_root = root.parent / "unrelated_course"
    other_root.mkdir()
    other = CourseFileStore(repo).bind(None, str(other_root))["course_id"]
    with pytest.raises(AppError):
        route(repo, other, links=[link], operation="cross_course")
    with pytest.raises(ValidationError):
        PathAction.model_validate({**action(artifact).model_dump(), "mastered": True})
    with pytest.raises(ValidationError):
        action(artifact, kind="navigate", self_assessment="confident")


def test_authenticated_http_route_and_lost_response_read_only_receipt(tmp_path):
    repo, course, _, _ = learning_course(tmp_path)
    artifact = route(repo, course)
    prefix = f"/api/v1/courses/{course}/learning"
    url = prefix + f"/artifacts/{artifact.artifact_id}/path"
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run")
    with TestClient(
        create_app(config, repository=repo), base_url="http://127.0.0.1:8765"
    ) as client:
        assert client.get(url).status_code == 403
        token = client.get("/api/v1/session").json()["csrf_token"]
        payload = action(artifact).model_dump(mode="json")
        assert client.post(url + "/actions", json=payload).status_code == 403
        result = client.post(url + "/actions", json=payload, headers={"X-Learning-CSRF": token})
        assert result.status_code == 200
        assert client.get(prefix + "/path-operations/navigate").json()["result"] == result.json()
        assert client.get(url + "/events").json()["events"][0]["action"] == "navigate"
        assert client.get(url).json()["revision"] == 1
