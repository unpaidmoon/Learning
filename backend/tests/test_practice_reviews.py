"""Synthetic product transactions using the actual fixed Rust calculation helpers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from workbench_support import learning_course

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.workbench import practice_schema as p
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.models import (
    ArtifactBody,
    ArtifactCreate,
    ArtifactUpdate,
    BlockChange,
    CardBlock,
    CardContent,
    QuestionBlock,
    QuestionContent,
    QuestionOption,
)
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


def question(*, authority="user_confirmed", question_type="single_choice", reference="A"):
    return QuestionBlock(
        block_id="q1",
        markdown="Which sentence was selected?",
        attribution="user_authored",
        question=QuestionContent(
            question_type=question_type,
            options=[
                QuestionOption(option_id="A", text="Only this selected sentence."),
                QuestionOption(option_id="B", text="A different sentence."),
            ],
            reference_answer=reference,
            explanation="Compare the actual selected words.",
            answer_authority=authority,
        ),
    )


def cards(repo, course_id, *, operation="cards", count=1):
    return (
        ArtifactStore(repo)
        .create(
            course_id,
            ArtifactCreate(
                operation_id=operation,
                kind="card_set",
                title="Recall",
                body=ArtifactBody(
                    blocks=[
                        CardBlock(
                            block_id=f"c{i}",
                            markdown=f"Recall {i}?",
                            attribution="user_authored",
                            card=CardContent(back=f"Answer {i}."),
                        )
                        for i in range(count)
                    ]
                ),
            ),
        )
        .artifact
    )


def paper(repo, course_id, block=None):
    return (
        ArtifactStore(repo)
        .create(
            course_id,
            ArtifactCreate(
                operation_id="paper",
                kind="question_set",
                title="Practice",
                body=ArtifactBody(blocks=[block or question()]),
            ),
        )
        .artifact
    )


def answer(store, course_id, artifact, text="B", operation="submit"):
    return store.submit(
        course_id,
        artifact.artifact_id,
        AnswerInput(
            operation_id=operation,
            question_revision_id=artifact.head_revision_id,
            block_id="q1",
            text=text,
        ),
    )


def review_rows(repo, kind=None):
    with repo.engine.connect() as conn:
        query = select(p.review_items)
        if kind:
            query = query.where(p.review_items.c.kind == kind)
        return [dict(row) for row in conn.execute(query).mappings()]


def test_actual_objective_answers_use_fixed_code_and_preserve_source_revision(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    artifact = paper(repo, course_id)
    practice = PracticeStore(repo)
    correct = answer(practice, course_id, artifact, "Ａ", "answer_fullwidth")
    assert correct.effective_assessment.verdict == "correct"
    raw = correct.effective_assessment.evidence["calculation"]
    assert raw["source_commit"] == "635216503c0b34bdf812e17942e0f09f51872e6f"
    assert raw["question_revision_id"] == artifact.head_revision_id
    assert raw["is_correct"] is True and not raw["needs_manual_grading"]
    wrong = answer(practice, course_id, artifact)
    assert wrong.text == "B" and wrong.sequence == 2
    assert wrong.effective_assessment.verdict == "wrong"
    assert len(review_rows(repo, "wrong_question")) == 1
    assert review_rows(repo)[0]["source_answer_id"] == wrong.answer_id
    assert repo.counts()["outbound_attempts"] == 0


@pytest.mark.parametrize(
    "authority,kind",
    [("ai_reference", "single_choice"), ("user_confirmed", "short_answer"), ("missing", "proof")],
)
def test_uncertain_and_subjective_are_pending_never_wrong(tmp_path, authority, kind):
    repo, course_id, _, _ = learning_course(tmp_path)
    artifact = paper(
        repo,
        course_id,
        question(
            authority=authority, question_type=kind, reference="" if authority == "missing" else "A"
        ),
    )
    result = answer(PracticeStore(repo), course_id, artifact, "A")
    assert (
        result.effective_assessment.verdict == "pending"
        and result.effective_assessment.score is None
    )
    assert result.effective_assessment.evidence["calculation"]["is_correct"] is None
    assert review_rows(repo) == []


def test_answer_operation_payload_cas_history_and_sticky_pause(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    artifact, practice, reviews = paper(repo, course_id), PracticeStore(repo), ReviewStore(repo)
    first = answer(practice, course_id, artifact)
    assert answer(practice, course_id, artifact).answer_id == first.answer_id
    with pytest.raises(AppError) as error:
        answer(practice, course_id, artifact, "A")
    assert error.value.code == "LEARNING_OPERATION_CONFLICT"
    row = review_rows(repo)[0]
    paused = reviews.control(
        course_id,
        row["review_id"],
        ReviewControl(
            operation_id="pause", expected_revision=row["schedule_revision"], action="pause"
        ),
    )
    corrected = practice.override(
        course_id,
        first.answer_id,
        UserVerdict(
            operation_id="correct",
            expected_revision=1,
            verdict="correct",
            reason="Original option was wrong.",
        ),
    )
    assert corrected.assessment_revision == 2
    assert reviews.get(course_id, row["review_id"]).queue_state == "paused"
    assert not reviews.get(course_id, row["review_id"]).eligible
    with pytest.raises(AppError) as error:
        practice.override(
            course_id,
            first.answer_id,
            UserVerdict(
                operation_id="stale", expected_revision=1, verdict="wrong", reason="Late other tab"
            ),
        )
    assert error.value.code == "ASSESSMENT_VERSION_CONFLICT"
    practice.override(
        course_id,
        first.answer_id,
        UserVerdict(
            operation_id="again",
            expected_revision=2,
            verdict="wrong",
            reason="Retain correction history",
        ),
    )
    current = reviews.get(course_id, row["review_id"])
    assert current.eligible and current.queue_state == paused.queue_state
    history = practice.assessments(course_id, first.answer_id).assessments
    assert [item.verdict for item in history] == ["wrong", "correct", "wrong"]
    assert history[1].parent_assessment_id == history[0].assessment_id


def test_old_question_and_old_answer_corrections_do_not_replace_newer_work(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    artifact, practice = paper(repo, course_id), PracticeStore(repo)
    old = answer(practice, course_id, artifact)
    updated = (
        ArtifactStore(repo)
        .update(
            course_id,
            artifact.artifact_id,
            ArtifactUpdate(
                operation_id="edit_question",
                expected_revision_id=artifact.head_revision_id,
                changes=[BlockChange(block_id="q1", replacement=question(reference="B"))],
            ),
        )
        .artifact
    )
    latest = answer(practice, course_id, updated, "A", "redo")
    assert latest.question_revision_id != old.question_revision_id
    assert (
        old.text == "B"
        and practice.get(course_id, old.answer_id).question_revision_id == artifact.head_revision_id
    )
    practice.override(
        course_id,
        old.answer_id,
        UserVerdict(
            operation_id="old_override",
            expected_revision=1,
            verdict="correct",
            reason="Old answer correction",
        ),
    )
    row = review_rows(repo)[0]
    assert row["source_answer_id"] == latest.answer_id and row["eligible"]
    assert [
        item.sequence for item in practice.history(course_id, artifact.artifact_id, "q1").answers
    ] == [2, 1]


def test_fault_rolls_back_answer_assessment_and_enqueue_as_one_transaction(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    artifact = paper(repo, course_id)

    def fail(event, detail):
        if event == "learning_answer_before_commit":
            raise RuntimeError("controlled transaction fault")

    original = repo.fault_hook
    repo.fault_hook = fail
    with pytest.raises(RuntimeError):
        answer(PracticeStore(repo), course_id, artifact)
    with repo.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(p.answers)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(p.assessments)).scalar_one() == 0
    assert review_rows(repo) == []
    repo.fault_hook = original
    assert answer(PracticeStore(repo), course_id, artifact).effective_assessment.verdict == "wrong"


def test_card_save_enqueue_conflicts_and_sticky_controls(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    artifact = cards(repo, course_id)
    row = review_rows(repo)[0]
    reviews = ReviewStore(repo)
    removed = reviews.control(
        course_id,
        row["review_id"],
        ReviewControl(operation_id="remove", expected_revision=1, action="remove"),
    )
    block = artifact.revision.body.blocks[0].model_copy(
        update={"card": CardContent(back="Revised answer")}
    )
    ArtifactStore(repo).update(
        course_id,
        artifact.artifact_id,
        ArtifactUpdate(
            operation_id="edit_card",
            expected_revision_id=artifact.head_revision_id,
            changes=[BlockChange(block_id=block.block_id, replacement=block)],
        ),
    )
    current = reviews.get(course_id, row["review_id"])
    assert current.queue_state == removed.queue_state == "removed"
    assert (
        current.content_revision_id != artifact.head_revision_id
        and current.scheduler_state["reps"] == 0
    )
    assert len(review_rows(repo)) == 1
    # An ordinary handout's inline card is content, not an implicitly requested deck.
    ArtifactStore(repo).create(
        course_id,
        ArtifactCreate(
            operation_id="handout",
            kind="handout",
            title="Example",
            body=ArtifactBody(blocks=[block]),
        ),
    )
    assert len(review_rows(repo)) == 1


def test_card_enqueue_fault_keeps_no_partial_asset_or_queue(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    original = repo._hook

    def fail(event, **detail):
        if event == "learning_review_enqueue_before_commit":
            raise RuntimeError("controlled enqueue fault")

    repo._hook = fail
    with pytest.raises(RuntimeError):
        cards(repo, course_id)
    assert review_rows(repo) == [] and ArtifactStore(repo).list(course_id) == []
    repo._hook = original
    cards(repo, course_id)
    assert len(review_rows(repo)) == 1


def test_actual_fsrs_rating_is_revealed_versioned_idempotent_and_undoable(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    cards(repo, course_id)
    clock = [utc_ms() + 1000]
    reviews = ReviewStore(repo, clock=lambda: clock[0])
    item = reviews.today(course_id=course_id).items[0]
    session = reviews.open_session(
        course_id, item.review_id, ReviewSessionInput(operation_id="open", expected_revision=1)
    )
    value = ReviewRatingInput(
        operation_id="rating", expected_revision=1, session_id=session.session_id, rating=3
    )
    with pytest.raises(AppError) as error:
        reviews.rate(course_id, item.review_id, value)
    assert error.value.code == "REVIEW_SESSION_STALE"
    reviews.reveal(
        course_id,
        item.review_id,
        session.session_id,
        ReviewSessionInput(operation_id="reveal", expected_revision=1),
    )
    receipt = reviews.rate(course_id, item.review_id, value)
    assert receipt.item.scheduler_state["reps"] == 1 and receipt.item.due_ms > clock[0]
    assert receipt.audit["engine"]["fsrs_version"] == "6.6.2"
    assert reviews.rate(course_id, item.review_id, value) == receipt
    with pytest.raises(AppError):
        reviews.rate(
            course_id, item.review_id, value.model_copy(update={"operation_id": "duplicate_new_id"})
        )
    today = reviews.today()
    assert today.admitted_count == today.completed_count == 1
    undone = reviews.undo(
        course_id,
        item.review_id,
        ReviewUndo(operation_id="undo", expected_revision=2, event_id=receipt.event_id),
    )
    assert undone.scheduler_state == item.scheduler_state and undone.due_ms == item.due_ms
    assert reviews.today().admitted_count == 1 and reviews.today().completed_count == 0


def test_one_daily_budget_across_courses_types_filters_and_restart(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    root = tmp_path / "other-course"
    root.mkdir()
    other = CourseFileStore(repo).bind(None, str(root))["course_id"]
    cards(repo, course_id, count=2)
    cards(repo, other, operation="other_cards", count=2)
    clock = [utc_ms() + 2 * 86_400_000]
    reviews = ReviewStore(repo, clock=lambda: clock[0])
    artifact = paper(repo, other)
    answer(
        PracticeStore(repo, reviews=ReviewStore(repo, clock=lambda: clock[0] - 2 * 86_400_000)),
        other,
        artifact,
    )
    reviews.set_preferences(
        course_id, ReviewPreferenceInput(operation_id="limit", expected_revision=0, daily_limit=2)
    )
    first = reviews.today(course_id=course_id, kind="card")
    second = ReviewStore(repo, clock=lambda: clock[0]).today(course_id=other, kind="wrong_question")
    assert first.admitted_count == second.admitted_count == 2
    assert len(reviews.today().items) == 2
    with repo.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(p.review_admissions)).scalar_one() == 2
    reviews.set_preferences(
        other, ReviewPreferenceInput(operation_id="new_limit", expected_revision=1, daily_limit=10)
    )
    assert reviews.today().daily_limit == 2
    clock[0] += 86_400_000
    assert reviews.today().daily_limit == 10


def test_logical_day_rollover_is_server_owned(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    cards(repo, course_id)
    stamp = datetime(2026, 10, 24, 19, 59, 59, tzinfo=UTC).timestamp() * 1000
    reviews = ReviewStore(repo, clock=lambda: int(stamp))
    assert reviews.today().logical_day == "2026-10-24"
    reviews.clock = lambda: int(stamp + 1000)
    assert reviews.today().logical_day == "2026-10-25"


def test_parallel_rating_only_one_commit_and_local_failure_does_not_apply(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    cards(repo, course_id)
    reviews = ReviewStore(repo, clock=lambda: utc_ms() + 1000)
    item = reviews.today().items[0]
    session = reviews.open_session(
        course_id, item.review_id, ReviewSessionInput(operation_id="open", expected_revision=1)
    )
    reviews.reveal(
        course_id,
        item.review_id,
        session.session_id,
        ReviewSessionInput(operation_id="reveal", expected_revision=1),
    )

    def submit(index):
        try:
            return reviews.rate(
                course_id,
                item.review_id,
                ReviewRatingInput(
                    operation_id=f"rate_{index}",
                    expected_revision=1,
                    session_id=session.session_id,
                    rating=3,
                ),
            ).event_id
        except AppError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(submit, [1, 2]))
    assert result.count("REVIEW_VERSION_CONFLICT") == 1
    with repo.engine.connect() as conn:
        assert (
            conn.execute(
                select(func.count())
                .select_from(p.review_events)
                .where(p.review_events.c.kind == "reviewed")
            ).scalar_one()
            == 1
        )


def test_http_scope_csrf_and_model_cannot_submit_user_evidence(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    artifact = paper(repo, course_id)
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "runtime")
    prefix = f"/api/v1/courses/{course_id}/learning"
    with TestClient(
        create_app(config, repository=repo), base_url="http://127.0.0.1:8765"
    ) as client:
        payload = AnswerInput(
            operation_id="http_answer",
            question_revision_id=artifact.head_revision_id,
            block_id="q1",
            text="A",
        ).model_dump(mode="json")
        assert (
            client.post(
                prefix + f"/artifacts/{artifact.artifact_id}/answers", json=payload
            ).status_code
            == 403
        )
        headers = {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}
        response = client.post(
            prefix + f"/artifacts/{artifact.artifact_id}/answers", headers=headers, json=payload
        )
        assert (
            response.status_code == 200
            and response.json()["effective_assessment"]["verdict"] == "correct"
        )
        receipt = client.get(prefix + "/practice-operations/http_answer")
        assert (
            receipt.status_code == 200
            and receipt.json()["result"]["answer_id"] == response.json()["answer_id"]
        )
        assert (
            client.get(
                f"/api/v1/courses/other/learning/answers/{response.json()['answer_id']}"
            ).status_code
            == 404
        )
        assert repo.counts()["outbound_attempts"] == 0


def test_wrong_question_review_requires_actual_answer_and_counts_same_daily_budget(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    clock = [utc_ms()]
    reviews = ReviewStore(repo, clock=lambda: clock[0])
    practice = PracticeStore(repo, reviews=reviews)
    artifact = paper(repo, course_id)
    answer(practice, course_id, artifact)
    clock[0] += 86_400_001
    today = reviews.today()
    item = today.items[0]
    assert item.kind == "wrong_question" and "Which sentence" in item.label
    session = reviews.open_session(
        course_id,
        item.review_id,
        ReviewSessionInput(operation_id="wrong_open", expected_revision=item.schedule_revision),
    )
    with pytest.raises(AppError) as error:
        reviews.rate(
            course_id,
            item.review_id,
            ReviewRatingInput(
                operation_id="fake_card_rating",
                expected_revision=item.schedule_revision,
                session_id=session.session_id,
                rating=4,
            ),
        )
    assert error.value.code == "REVIEW_RATING_KIND_DENIED"
    submitted = practice.submit(
        course_id,
        artifact.artifact_id,
        AnswerInput(
            operation_id="actual_redo",
            question_revision_id=item.content_revision_id,
            block_id=item.block_id,
            text="A",
            review_session_id=session.session_id,
            expected_review_revision=item.schedule_revision,
        ),
    )
    assert submitted.effective_assessment.verdict == "correct"
    assert submitted.effective_assessment.evidence["review_session_id"] == session.session_id
    assert reviews.today().completed_count == reviews.today().admitted_count == 1
    assert not reviews.get(course_id, item.review_id).eligible


def test_rating_commit_fault_preserves_original_schedule_and_receipt_absence(tmp_path):
    repo, course_id, _, _ = learning_course(tmp_path)
    cards(repo, course_id)
    reviews = ReviewStore(repo, clock=lambda: utc_ms() + 1000)
    item = reviews.today().items[0]
    session = reviews.open_session(
        course_id, item.review_id, ReviewSessionInput(operation_id="open", expected_revision=1)
    )
    reviews.reveal(
        course_id,
        item.review_id,
        session.session_id,
        ReviewSessionInput(operation_id="reveal", expected_revision=1),
    )

    def fail(event, detail):
        if event == "learning_review_rating_before_commit":
            raise RuntimeError("controlled after-calculation before-commit fault")

    repo.fault_hook = fail
    with pytest.raises(RuntimeError):
        reviews.rate(
            course_id,
            item.review_id,
            ReviewRatingInput(
                operation_id="rating_failure",
                expected_revision=1,
                session_id=session.session_id,
                rating=3,
            ),
        )
    repo.fault_hook = None
    assert reviews.get(course_id, item.review_id).scheduler_state == item.scheduler_state
    assert reviews.get(course_id, item.review_id).schedule_revision == 1
    with repo.engine.connect() as conn:
        assert (
            conn.execute(
                select(s.operations).where(s.operations.c.operation_id == "rating_failure")
            ).first()
            is None
        )
        assert (
            conn.execute(
                select(p.review_events).where(p.review_events.c.kind == "reviewed")
            ).first()
            is None
        )
