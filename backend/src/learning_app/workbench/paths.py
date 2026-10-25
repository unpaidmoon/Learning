"""Project-owned route actions, using the fixed DeepTutor pure transaction adapter."""

from datetime import UTC, datetime

from deeptutor_reuse.book_models import Progress
from deeptutor_reuse.learning_actions import apply_learning_action
from deeptutor_reuse.learning_transactions import (
    InteractionStatus,
    MasteryEvent,
    MasteryInteraction,
    PendingQuestion,
)
from sqlalchemy import insert, select, update

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from . import path_schema as p
from . import practice_schema as practice
from .artifacts import ArtifactStore
from .common import new_id, now, prior_operation, save_operation
from .models import ArtifactRef, PathStepBlock
from .path_models import (
    PathAction,
    PathEvent,
    PathEventPage,
    PathQuestionEvidence,
    PathReceipt,
    PathStepView,
    PathView,
)
from .practice import PracticeStore
from .reviews import ReviewStore
from .sources import SourceStore


class PathStore:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def validate_links(conn, course_id, body, *, allowed_snapshots=None):
        """A model may link only a concrete question it actually received."""
        allowed = set()
        if allowed_snapshots is not None:
            for identity in allowed_snapshots:
                source = SourceStore.in_transaction(conn, course_id, identity)
                for _ in range(16):
                    if source.reference.kind != "snapshot_slice":
                        break
                    source = SourceStore.in_transaction(
                        conn, course_id, source.reference.parent_snapshot_id
                    )
                ref = source.reference
                if isinstance(ref, ArtifactRef):
                    allowed.add((ref.artifact_id, ref.revision_id, ref.block_id))
        for block in body.blocks:
            if not isinstance(block, PathStepBlock):
                continue
            for link in block.step.questions:
                key = (link.artifact_id, link.revision_id, link.block_id)
                if allowed_snapshots is not None and key not in allowed:
                    raise AppError("PATH_QUESTION_SCOPE_DENIED", "路线引用了本次未读取的题目", 403)
                PracticeStore._question(conn, course_id, *key)

    @staticmethod
    def _artifact(conn, course_id, artifact_id):
        artifact = ArtifactStore._view(conn, course_id, artifact_id)
        if artifact.kind != "learning_path":
            raise AppError("LEARNING_PATH_REQUIRED", "此成果不是学习路线", 422)
        return artifact

    @staticmethod
    def _state(conn, artifact_id):
        row = (
            conn.execute(select(p.path_states).where(p.path_states.c.artifact_id == artifact_id))
            .mappings()
            .first()
        )
        if row is not None:
            return dict(row)
        return dict(
            artifact_id=artifact_id,
            revision=0,
            progress=Progress(book_id=artifact_id).model_dump(mode="json"),
            interactions={},
            self_assessments={},
            updated_at=now(),
        )

    @classmethod
    def _view(cls, conn, course_id, artifact_id):
        artifact = cls._artifact(conn, course_id, artifact_id)
        state = cls._state(conn, artifact_id)
        progress = Progress.model_validate(state["progress"])
        steps = sorted(
            (b for b in artifact.revision.body.blocks if isinstance(b, PathStepBlock)),
            key=lambda b: (b.step.suggested_order, b.block_id),
        )
        reviews = {
            row["block_id"]: ReviewStore.public(conn, dict(row))
            for row in conn.execute(
                select(practice.review_items).where(
                    practice.review_items.c.artifact_id == artifact_id,
                    practice.review_items.c.kind == "knowledge",
                )
            ).mappings()
        }
        result = []
        for block in steps:
            answers = []
            for link in block.step.questions:
                row = (
                    conn.execute(
                        select(practice.answers)
                        .where(
                            practice.answers.c.course_id == course_id,
                            practice.answers.c.artifact_id == link.artifact_id,
                            practice.answers.c.question_revision_id == link.revision_id,
                            practice.answers.c.block_id == link.block_id,
                        )
                        .order_by(practice.answers.c.sequence.desc())
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                answers.append(
                    PathQuestionEvidence(
                        question=link,
                        latest_answer=PracticeStore._view(conn, dict(row)) if row else None,
                    )
                )
            raw = state["interactions"].get(block.block_id)
            transaction = MasteryInteraction.model_validate(raw) if raw else None
            block_hash = _hash(block.model_dump(mode="json"))
            self_report = state["self_assessments"].get(block.block_id)
            result.append(
                PathStepView(
                    block=block,
                    visited=block.block_id in progress.visited_page_ids,
                    skipped=bool(transaction and transaction.status == InteractionStatus.ABANDONED),
                    explanation_seen=bool(transaction and transaction.question.assistance_used),
                    activity_current=bool(
                        transaction
                        and transaction.question.source_evidence.get("block_hash") == block_hash
                    ),
                    self_assessment=self_report["value"] if self_report else None,
                    self_assessment_current=bool(
                        self_report and self_report["block_hash"] == block_hash
                    ),
                    answers=answers,
                    review=reviews.get(block.block_id),
                )
            )
        return PathView(
            artifact_id=artifact_id,
            content_revision_id=artifact.head_revision_id,
            revision=state["revision"],
            current_block_id=progress.current_page_id
            if any(b.block_id == progress.current_page_id for b in steps)
            else None,
            steps=result,
        )

    def get(self, course_id, artifact_id):
        with self.repository.engine.connect() as conn:
            return self._view(conn, course_id, artifact_id)

    def act(self, course_id: str, artifact_id: str, value: PathAction) -> PathReceipt:
        payload = {"artifact_id": artifact_id, "input": value.model_dump(mode="json")}
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "path_action", value.operation_id, payload)
            if prior is not None:
                return PathReceipt.model_validate(prior)
            artifact = self._artifact(conn, course_id, artifact_id)
            if artifact.head_revision_id != value.content_revision_id:
                raise AppError("PATH_CONTENT_CHANGED", "路线内容已修改，请核对新版本后继续")
            block = next(
                (b for b in artifact.revision.body.blocks if b.block_id == value.block_id), None
            )
            if not isinstance(block, PathStepBlock):
                raise AppError("PATH_STEP_NOT_FOUND", "路线中没有此知识点", 404)
            state = self._state(conn, artifact_id)
            if state["revision"] != value.expected_revision:
                raise AppError("PATH_VERSION_CONFLICT", "另一处已更新路线学习记录，请刷新")
            epoch = datetime.now(UTC).timestamp()
            raw = state["interactions"].get(block.block_id)
            block_hash = _hash(block.model_dump(mode="json"))
            if raw and raw["question"]["source_evidence"].get("block_hash") != block_hash:
                raw = None  # Older content actions remain in the immutable event journal.
            transaction = (
                MasteryInteraction.model_validate(raw)
                if raw
                else MasteryInteraction(
                    interaction_id=new_id("interaction"),
                    path_id=artifact_id,
                    question=PendingQuestion(
                        question_id=block.block_id,
                        knowledge_point_id=block.block_id,
                        prompt=block.step.recall_prompt,
                        source_evidence={
                            "content_revision_id": artifact.head_revision_id,
                            "block_hash": block_hash,
                        },
                        created_at=epoch,
                    ),
                    created_at=epoch,
                    updated_at=epoch,
                )
            )
            sequence = state["revision"] + 1
            if value.action == "self_assess":
                event = MasteryEvent(
                    id=sequence,
                    path_id=artifact_id,
                    revision=sequence,
                    event_type="learning_self_assess",
                    payload={
                        "self_assessment": value.self_assessment,
                        "grants_mastery": False,
                        "adds_quiz_attempt": False,
                    },
                    created_at=epoch,
                )
                state["self_assessments"] = {
                    **state["self_assessments"],
                    block.block_id: {
                        "value": value.self_assessment,
                        "block_hash": block_hash,
                        "content_revision_id": artifact.head_revision_id,
                    },
                }
            else:
                outcome = apply_learning_action(
                    Progress.model_validate(state["progress"]),
                    transaction,
                    action=value.action,
                    event_id=sequence,
                    revision=sequence,
                    now=epoch,
                    target_page_id=block.block_id,
                )
                state["progress"] = outcome.progress.model_dump(mode="json")
                transaction, event = outcome.interaction, outcome.event
                if value.action == "view_explanation":
                    # The upstream active-interaction guard omits exposure after a skip.
                    # Actual reveal remains evidence even for an abandoned interaction.
                    transaction.question.assistance_used = True
            state["interactions"] = {
                **state["interactions"],
                block.block_id: transaction.model_dump(mode="json"),
            }
            state.update(revision=sequence, updated_at=now())
            if sequence == 1:
                conn.execute(insert(p.path_states).values(**state))
            else:
                changed = conn.execute(
                    update(p.path_states)
                    .where(
                        p.path_states.c.artifact_id == artifact_id,
                        p.path_states.c.revision == value.expected_revision,
                    )
                    .values(**state)
                )
                if changed.rowcount != 1:
                    raise AppError("PATH_VERSION_CONFLICT", "路线学习记录已变化，未重复保存")
            receipt_event = PathEvent(
                event_id=new_id("pathevent"),
                artifact_id=artifact_id,
                content_revision_id=value.content_revision_id,
                block_id=block.block_id,
                revision=sequence,
                action=value.action,
                detail=event.model_dump(mode="json"),
                created_at=now(),
            )
            conn.execute(insert(p.path_events).values(**receipt_event.model_dump(mode="json")))
            result = PathReceipt(event=receipt_event, path=self._view(conn, course_id, artifact_id))
            save_operation(conn, course_id, "path_action", value.operation_id, payload, result)
            self.repository._hook("learning_path_action_before_commit", artifact_id=artifact_id)
            return result

    def events(self, course_id, artifact_id, *, before=None, limit=50):
        with self.repository.engine.connect() as conn:
            self._artifact(conn, course_id, artifact_id)
            query = select(p.path_events).where(p.path_events.c.artifact_id == artifact_id)
            if before is not None:
                query = query.where(p.path_events.c.revision < before)
            rows = list(
                conn.execute(
                    query.order_by(p.path_events.c.revision.desc()).limit(limit + 1)
                ).mappings()
            )
            return PathEventPage(
                events=[PathEvent.model_validate(dict(row)) for row in rows[:limit]],
                next_before=rows[limit - 1]["revision"] if len(rows) > limit else None,
            )
