"""Immutable essay rounds, exact annotation proposals and independent user evaluation."""

from __future__ import annotations

import hashlib

from sqlalchemy import func, insert, select, update

from learning_app.errors import AppError
from learning_app.storage import Repository

from . import essay_schema as e
from . import schema as s
from .artifacts import ArtifactStore
from .common import new_id, now, prior_operation, save_operation
from .essay_models import (
    EssayAssessment,
    EssayAssessmentInput,
    EssayRound,
    EssayRoundPage,
    EssayRoundSummary,
    EssaySuggestionInput,
)
from .essay_processing import EssayProcessor
from .models import ArtifactUpdate, BlockChange, EssayDraftBlock, EssayFeedbackBlock
from .sources import SourceStore


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EssayStore:
    def __init__(self, repository: Repository, *, processor=None):
        self.repository = repository
        self.processor = processor

    @staticmethod
    def draft(conn, course_id, artifact_id, revision_id, block_id):
        ArtifactStore._view(conn, course_id, artifact_id)
        revision = ArtifactStore._revision(conn, artifact_id, revision_id)
        if revision.disposition != "published":
            raise AppError("ESSAY_DRAFT_NOT_PUBLISHED", "并发候选尚未成为正式作文稿", 409)
        block = next((block for block in revision.body.blocks if block.block_id == block_id), None)
        if not isinstance(block, EssayDraftBlock):
            raise AppError("ESSAY_DRAFT_NOT_FOUND", "该修订中没有指定作文稿", 404)
        return block, revision.content_role

    def review_input(self, course_id, value):
        (self.processor or EssayProcessor()).verified()
        with self.repository.engine.connect() as conn:
            block, _ = self.draft(
                conn, course_id, value.artifact_id, value.revision_id, value.block_id
            )
        snapshot = SourceStore(self.repository).capture_artifact(
            course_id, value.artifact_id, value.revision_id, value.block_id
        )
        return block, snapshot

    @staticmethod
    def bind(conn, course_id, task_id, scope, block, source_snapshot_id):
        number = (
            conn.execute(
                select(func.max(e.essay_rounds.c.round_number)).where(
                    e.essay_rounds.c.artifact_id == scope.artifact_id,
                )
            ).scalar_one()
            or 0
        )
        if scope.expected_round_number != number:
            raise AppError(
                "ESSAY_ROUND_VERSION_CONFLICT", "已有新的批改轮次，请核对原任务而不重复提交"
            )
        EssayStore.draft(conn, course_id, scope.artifact_id, scope.revision_id, scope.block_id)
        conn.execute(
            insert(e.essay_rounds).values(
                round_id=new_id("essayround"),
                course_id=course_id,
                artifact_id=scope.artifact_id,
                block_id=scope.block_id,
                draft_revision_id=scope.revision_id,
                draft_content_hash=text_digest(block.markdown),
                round_number=number + 1,
                task_id=task_id,
                source_snapshot_id=source_snapshot_id,
                assessment_revision=0,
                created_at=now(),
            )
        )

    @staticmethod
    def _row(conn, course_id, round_id):
        row = (
            conn.execute(
                select(e.essay_rounds).where(
                    e.essay_rounds.c.course_id == course_id, e.essay_rounds.c.round_id == round_id
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("ESSAY_ROUND_NOT_FOUND", "指定课程的作文轮次不存在", 404)
        return dict(row)

    @staticmethod
    def _summary(conn, row):
        state = conn.execute(
            select(s.tasks.c.state).where(s.tasks.c.task_id == row["task_id"])
        ).scalar_one()
        return EssayRoundSummary(
            **{key: row[key] for key in EssayRoundSummary.model_fields if key != "state"},
            state=state,
        )

    @classmethod
    def _view(cls, conn, row):
        block, role = cls.draft(
            conn, row["course_id"], row["artifact_id"], row["draft_revision_id"], row["block_id"]
        )
        assessments = [
            EssayAssessment.model_validate(dict(item))
            for item in conn.execute(
                select(e.essay_assessments)
                .where(
                    e.essay_assessments.c.round_id == row["round_id"],
                )
                .order_by(e.essay_assessments.c.revision.desc())
                .limit(100)
            ).mappings()
        ]
        return EssayRound(
            **cls._summary(conn, row).model_dump(),
            draft=block,
            content_role=role,
            result=row["result"],
            assessments=list(reversed(assessments)),
        )

    def get(self, course_id, round_id):
        with self.repository.engine.connect() as conn:
            return self._view(conn, self._row(conn, course_id, round_id))

    def list(self, course_id, artifact_id, *, before=None, limit=30):
        with self.repository.engine.connect() as conn:
            ArtifactStore._view(conn, course_id, artifact_id)
            query = select(e.essay_rounds).where(
                e.essay_rounds.c.course_id == course_id, e.essay_rounds.c.artifact_id == artifact_id
            )
            if before is not None:
                query = query.where(e.essay_rounds.c.round_number < before)
            rows = list(
                conn.execute(
                    query.order_by(e.essay_rounds.c.round_number.desc()).limit(limit + 1)
                ).mappings()
            )
            return EssayRoundPage(
                items=[self._summary(conn, dict(row)) for row in rows[:limit]],
                next_before=rows[limit - 1]["round_number"] if len(rows) > limit else None,
            )

    @staticmethod
    def validate_feedback(conn, task, body):
        blocks = [block for block in body.blocks if isinstance(block, EssayFeedbackBlock)]
        if task["kind"] != "essay_review":
            if blocks:
                raise AppError(
                    "TASK_ESSAY_FEEDBACK_DENIED", "此任务未获指定作文轮次的评价权限", 403
                )
            return None
        row = (
            conn.execute(select(e.essay_rounds).where(e.essay_rounds.c.task_id == task["task_id"]))
            .mappings()
            .first()
        )
        if row is None or len(blocks) != 1 or len(body.blocks) != 1:
            raise AppError("TASK_ESSAY_TARGET_INVALID", "作文评价须对应本次唯一稿件及轮次", 403)
        value = blocks[0].feedback
        if (
            value.round_id != row["round_id"]
            or value.draft_revision_id != row["draft_revision_id"]
            or value.draft_content_hash != row["draft_content_hash"]
            or row["source_snapshot_id"] not in blocks[0].source_snapshot_ids
        ):
            raise AppError(
                "TASK_ESSAY_TARGET_INVALID", "作文评价的稿件、摘要或来源与原提交不符", 403
            )
        return dict(row)

    def publish_feedback(self, conn, task, run, body, artifact_id):
        row = self.validate_feedback(conn, task, body)
        if row is None:
            return
        if row["feedback_artifact_id"]:
            raise AppError("ESSAY_FEEDBACK_ALREADY_PUBLISHED", "该轮参考结果已发布，未追加重复评价")
        block, role = self.draft(
            conn, row["course_id"], row["artifact_id"], row["draft_revision_id"], row["block_id"]
        )
        feedback = body.blocks[0]
        rubric = block.essay.rubric
        result = (self.processor or EssayProcessor()).analyze(
            run["run_id"],
            {
                "draft_id": row["artifact_id"],
                "revision_id": row["draft_revision_id"],
                "content_hash": row["draft_content_hash"],
                "content_role": role,
                "text": block.markdown,
            },
            {
                "draft_revision_id": feedback.feedback.draft_revision_id,
                "draft_content_hash": feedback.feedback.draft_content_hash,
                "body": feedback.markdown,
                "annotated_text": feedback.feedback.annotated_text,
                "host_terminal": run["state"],
                "cancel_requested": run["cancel_requested"],
                "origin": "ai_reference",
            },
            {
                "max_total": rubric.max_total,
                "aggregation": rubric.aggregation,
                "dimensions": [
                    {"name": dim.name, "max_score": dim.max_score} for dim in rubric.dimensions
                ],
            },
        )
        evaluation = result.get("reference_evaluation")
        assessment = EssayAssessment(
            assessment_id=new_id("essayassessment"),
            round_id=row["round_id"],
            revision=row["assessment_revision"] + 1,
            origin="ai_reference",
            total=evaluation["total"] if evaluation else None,
            max_total=rubric.max_total,
            reason="AI参考评价"
            if evaluation
            else "参考评分或批注尚不能可靠使用，保留待判断与原始输出。",
            supersedes=row["reference_assessment_id"],
            run_id=run["run_id"],
            payload=result,
            created_at=now(),
        )
        conn.execute(insert(e.essay_assessments).values(**assessment.model_dump(mode="json")))
        conn.execute(
            update(e.essay_rounds)
            .where(e.essay_rounds.c.round_id == row["round_id"])
            .values(
                feedback_artifact_id=artifact_id,
                result=result,
                assessment_revision=assessment.revision,
                reference_assessment_id=assessment.assessment_id,
            )
        )
        self.repository._hook(
            "learning_essay_feedback_before_commit", round_id=row["round_id"], run_id=run["run_id"]
        )

    def assess(self, course_id, round_id, value: EssayAssessmentInput):
        payload = {"round_id": round_id, "input": value.model_dump(mode="json")}
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(
                conn, course_id, "essay_assessment", value.operation_id, payload
            )
            if prior is not None:
                return EssayRound.model_validate(prior)
            row = self._row(conn, course_id, round_id)
            if row["assessment_revision"] != value.expected_revision:
                raise AppError(
                    "ESSAY_ASSESSMENT_CONFLICT", "本轮评价已变化，用户评价未覆盖其他新记录"
                )
            block, _ = self.draft(
                conn, course_id, row["artifact_id"], row["draft_revision_id"], row["block_id"]
            )
            if value.total is not None and value.total > block.essay.rubric.max_total:
                raise AppError("ESSAY_SCORE_OUT_OF_RANGE", "用户评分超过本稿评分标准的满分", 422)
            if value.origin == "user_override" and not row["reference_assessment_id"]:
                raise AppError("ESSAY_REFERENCE_REQUIRED", "本轮尚无参考评价，可先保存独立自评")
            assessment = EssayAssessment(
                assessment_id=new_id("essayassessment"),
                round_id=round_id,
                revision=value.expected_revision + 1,
                origin=value.origin,
                total=value.total,
                max_total=block.essay.rubric.max_total,
                reason=value.reason,
                supersedes=row["reference_assessment_id"]
                if value.origin == "user_override"
                else row["user_assessment_id"],
                run_id=None,
                payload={"user_operation_id": value.operation_id},
                created_at=now(),
            )
            conn.execute(insert(e.essay_assessments).values(**assessment.model_dump(mode="json")))
            changed = conn.execute(
                update(e.essay_rounds)
                .where(
                    e.essay_rounds.c.round_id == round_id,
                    e.essay_rounds.c.assessment_revision == value.expected_revision,
                )
                .values(
                    assessment_revision=assessment.revision,
                    user_assessment_id=assessment.assessment_id,
                )
            )
            if changed.rowcount != 1:
                raise AppError("ESSAY_ASSESSMENT_CONFLICT", "本轮评价版本已变化")
            result = self._view(conn, self._row(conn, course_id, round_id))
            self.repository._hook("learning_essay_assessment_before_commit", round_id=round_id)
            save_operation(conn, course_id, "essay_assessment", value.operation_id, payload, result)
            return result

    def apply_suggestions(self, course_id, round_id, value: EssaySuggestionInput):
        with self.repository.engine.connect() as conn:
            row = self._row(conn, course_id, round_id)
            block, _ = self.draft(
                conn, course_id, row["artifact_id"], row["draft_revision_id"], row["block_id"]
            )
        if value.expected_revision_id != row["draft_revision_id"] or not row["result"]:
            raise AppError(
                "ESSAY_SUGGESTION_VERSION_CONFLICT", "修改建议必须作用于它实际绑定的稿件版本"
            )
        indices = value.annotation_indices
        annotations = row["result"].get("annotations", [])
        if len(set(indices)) != len(indices) or any(
            index < 0 or index >= len(annotations) for index in indices
        ):
            raise AppError("ESSAY_SUGGESTION_INVALID", "请选择本轮实际定位的修改建议", 422)
        selected = sorted(
            [annotations[index] for index in indices], key=lambda item: (item["start"], item["end"])
        )
        previous_end = -1
        for item in selected:
            if (
                item["start"] < previous_end
                or item["draft_revision_id"] != row["draft_revision_id"]
                or item["draft_content_hash"] != row["draft_content_hash"]
                or block.markdown[item["start"] : item["end"]] != item["quote"]
            ):
                raise AppError(
                    "ESSAY_SUGGESTION_ANCHOR_CONFLICT", "批注重叠或与实际稿件原文不符，未自动改稿"
                )
            previous_end = item["end"]
        text = block.markdown
        for item in reversed(selected):
            text = text[: item["start"]] + item["replacement"] + text[item["end"] :]
        sources = list(block.source_snapshot_ids)
        if row["feedback_artifact_id"]:
            feedback = ArtifactStore(self.repository).get(course_id, row["feedback_artifact_id"])
            for feedback_block in feedback.revision.body.blocks:
                snapshot = SourceStore(self.repository).capture_artifact(
                    course_id,
                    feedback.artifact_id,
                    feedback.head_revision_id,
                    feedback_block.block_id,
                )
                if snapshot.snapshot_id not in sources:
                    sources.append(snapshot.snapshot_id)
        replacement = EssayDraftBlock.model_validate(
            {
                **block.model_dump(mode="json"),
                "markdown": text,
                "attribution": "user_authored",
                "source_snapshot_ids": sources,
            }
        )
        # A concurrent draft change becomes the existing retained artifact conflict.
        return ArtifactStore(self.repository).update(
            course_id,
            row["artifact_id"],
            ArtifactUpdate(
                operation_id=value.operation_id,
                expected_revision_id=value.expected_revision_id,
                changes=[
                    BlockChange(
                        block_id=block.block_id,
                        replacement=replacement,
                    )
                ],
            ),
            assistance={
                "kind": "essay_feedback",
                "round_id": round_id,
                "annotation_indices": indices,
            },
        )
