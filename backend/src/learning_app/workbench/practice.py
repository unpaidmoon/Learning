"""Question revisions, actual user answers and effective append-only verdicts."""

from __future__ import annotations

from sqlalchemy import func, insert, select, update

from learning_app.errors import AppError
from learning_app.storage import Repository

from . import practice_schema as p
from .artifacts import ArtifactStore
from .common import new_id, now, prior_operation, save_operation
from .models import AnswerRef, AssessmentBlock, QuestionBlock
from .practice_models import (
    AnswerHistory,
    AnswerInput,
    AnswerView,
    AssessmentHistory,
    AssessmentView,
    PracticeQuestion,
    PracticeQuestionPage,
    UserVerdict,
)
from .reviews import ReviewStore
from .sources import SourceStore


class PracticeStore:
    def __init__(self, repository: Repository, *, grader=None, reviews=None):
        self.repository = repository
        self.grader = grader
        self.reviews = reviews or ReviewStore(repository)

    @staticmethod
    def _question(conn, course_id, artifact_id, revision_id, block_id) -> QuestionBlock:
        ArtifactStore._view(conn, course_id, artifact_id)
        revision = ArtifactStore._revision(conn, artifact_id, revision_id)
        if revision.disposition != "published":
            raise AppError("QUESTION_REVISION_NOT_PUBLISHED", "并发候选尚未成为正式题目版本", 409)
        block = next((item for item in revision.body.blocks if item.block_id == block_id), None)
        if not isinstance(block, QuestionBlock):
            raise AppError("QUESTION_NOT_FOUND", "该修订中没有此结构化题目", 404)
        return block

    @staticmethod
    def _answer(conn, course_id, answer_id) -> dict:
        row = (
            conn.execute(
                select(p.answers).where(
                    p.answers.c.answer_id == answer_id,
                    p.answers.c.course_id == course_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("ANSWER_NOT_FOUND", "指定课程的作答不存在", 404)
        return dict(row)

    @staticmethod
    def _view(conn, row: dict) -> AnswerView:
        assessment = (
            conn.execute(
                select(p.assessments).where(
                    p.assessments.c.assessment_id == row["effective_assessment_id"],
                )
            )
            .mappings()
            .one()
        )
        return AnswerView(
            **{key: row[key] for key in AnswerView.model_fields if key != "effective_assessment"},
            effective_assessment=AssessmentView.model_validate(dict(assessment)),
        )

    def get(self, course_id: str, answer_id: str) -> AnswerView:
        with self.repository.engine.connect() as conn:
            return self._view(conn, self._answer(conn, course_id, answer_id))

    def review_inputs(self, course_id: str, answer_id: str):
        with self.repository.engine.connect() as conn:
            value = self._answer(conn, course_id, answer_id)
        sources = SourceStore(self.repository)
        question = sources.capture_artifact(
            course_id, value["artifact_id"], value["question_revision_id"], value["block_id"]
        )
        actual = sources._save_verified(
            course_id,
            AnswerRef(
                answer_id=answer_id,
                question_revision_id=value["question_revision_id"],
                block_id=value["block_id"],
            ),
            value["text"],
            "user_note",
        )
        return value, question, actual

    @staticmethod
    def validate_feedback(conn, task: dict, body):
        blocks = [block for block in body.blocks if isinstance(block, AssessmentBlock)]
        if task["kind"] != "answer_review":
            if blocks:
                raise AppError("TASK_ASSESSMENT_DENIED", "普通成果不能写入用户作答的评价", 403)
            return None
        binding = (
            conn.execute(
                select(p.answer_reviews).where(p.answer_reviews.c.task_id == task["task_id"])
            )
            .mappings()
            .first()
        )
        if binding is None or len(blocks) != 1 or len(body.blocks) != 1:
            raise AppError("TASK_ASSESSMENT_TARGET_INVALID", "参考评价须对应本次唯一作答范围", 403)
        block = blocks[0]
        if (
            block.assessment.answer_id != binding["answer_id"]
            or block.assessment.expected_revision != binding["expected_revision"]
            or not {binding["question_snapshot_id"], binding["answer_snapshot_id"]}.issubset(
                set(block.source_snapshot_ids)
            )
        ):
            raise AppError(
                "TASK_ASSESSMENT_TARGET_INVALID", "参考评价的作答、版本或依据与原提交不符", 403
            )
        return binding

    def publish_feedback(self, conn, task: dict, run: dict, body):
        binding = self.validate_feedback(conn, task, body)
        if binding is None:
            return
        block = body.blocks[0]
        row = self._answer(conn, task["course_id"], binding["answer_id"])
        effective = row["assessment_revision"] == binding["expected_revision"]
        assessment = AssessmentView(
            assessment_id=new_id("assessment"),
            answer_id=row["answer_id"],
            revision=row["assessment_revision"] + 1,
            parent_assessment_id=binding["parent_assessment_id"],
            verdict=block.assessment.verdict,
            author="ai_reference",
            reason=block.markdown,
            score=block.assessment.score,
            run_id=run["run_id"],
            evidence={
                "expected_revision": binding["expected_revision"],
                "disposition": "effective" if effective else "stale",
                "model_settings": run["settings"],
                "raw_result": body.model_dump(mode="json"),
                "input_hash": run["input_hash"],
            },
            created_at=now(),
        )
        self._append(conn, row, assessment, make_effective=effective)
        self.repository._hook(
            "learning_model_assessment_before_commit",
            answer_id=row["answer_id"],
            run_id=run["run_id"],
        )

    def questions(
        self, course_id: str, artifact_id: str, revision_id: str | None = None
    ) -> PracticeQuestionPage:
        with self.repository.engine.connect() as conn:
            artifact = ArtifactStore._view(conn, course_id, artifact_id)
            revision = ArtifactStore._revision(
                conn, artifact_id, revision_id or artifact.head_revision_id
            )
            items = []
            for block in revision.body.blocks:
                if not isinstance(block, QuestionBlock):
                    continue
                latest = (
                    conn.execute(
                        select(p.answers)
                        .where(
                            p.answers.c.artifact_id == artifact_id,
                            p.answers.c.block_id == block.block_id,
                            p.answers.c.question_revision_id == revision.revision_id,
                        )
                        .order_by(p.answers.c.sequence.desc())
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                items.append(
                    PracticeQuestion(
                        artifact_id=artifact_id,
                        revision_id=revision.revision_id,
                        block=block,
                        latest_answer=self._view(conn, dict(latest)) if latest else None,
                    )
                )
            return PracticeQuestionPage(items=items)

    def submit(self, course_id: str, artifact_id: str, value: AnswerInput) -> AnswerView:
        payload = {
            "artifact_id": artifact_id,
            "input": value.model_dump(
                mode="json",
                exclude={"review_session_id", "expected_review_revision"}
                if value.review_session_id is None
                else set(),
            ),
        }
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "answer_submit", value.operation_id, payload)
            if prior is not None:
                return AnswerView.model_validate(prior)
            block = self._question(
                conn, course_id, artifact_id, value.question_revision_id, value.block_id
            )
            if value.review_session_id:
                review_row = conn.execute(
                    select(p.review_sessions.c.review_id).where(
                        p.review_sessions.c.session_id == value.review_session_id,
                    )
                ).scalar_one_or_none()
                if review_row is None:
                    raise AppError("REVIEW_SESSION_NOT_FOUND", "原错题复习会话不存在", 404)
                item, session = self.reviews._session(
                    conn, course_id, review_row, value.review_session_id
                )
                self.reviews._active(item)
                self.reviews._expected(item, value.expected_review_revision)
                if (
                    item["kind"] != "wrong_question"
                    or session["state"] not in {"opened", "revealed"}
                    or session["schedule_revision"] != item["schedule_revision"]
                    or item["artifact_id"] != artifact_id
                    or item["block_id"] != value.block_id
                    or item["content_revision_id"] != value.question_revision_id
                ):
                    raise AppError("REVIEW_SESSION_STALE", "本次作答不属于当前错题会话和题目版本")
                if session["logical_day"] != self.reviews._logical_day(
                    self.reviews.clock(), self.reviews._preferences(conn)
                ):
                    raise AppError("REVIEW_DAY_CHANGED", "复习日已切换，请重新打开今日入口")
            verdict, reason, score, evidence = self._grade(
                block, value.text, value.operation_id, value.question_revision_id
            )
            if value.review_session_id:
                evidence["review_session_id"] = value.review_session_id
                evidence["review_id"] = review_row
            answer_id, assessment_id = new_id("answer"), new_id("assessment")
            sequence = (
                conn.execute(
                    select(func.max(p.answers.c.sequence)).where(
                        p.answers.c.artifact_id == artifact_id,
                        p.answers.c.block_id == value.block_id,
                    )
                ).scalar_one()
                or 0
            ) + 1
            row = dict(
                answer_id=answer_id,
                course_id=course_id,
                artifact_id=artifact_id,
                question_revision_id=value.question_revision_id,
                block_id=value.block_id,
                text=value.text,
                sequence=sequence,
                submitted_at=now(),
                assessment_revision=1,
                effective_assessment_id=assessment_id,
            )
            conn.execute(insert(p.answers).values(**row))
            assessment = AssessmentView(
                assessment_id=assessment_id,
                answer_id=answer_id,
                revision=1,
                parent_assessment_id=None,
                verdict=verdict,
                author="deterministic",
                reason=reason,
                score=score,
                run_id=None,
                evidence=evidence,
                created_at=now(),
            )
            conn.execute(insert(p.assessments).values(**assessment.model_dump(mode="json")))
            self.reviews.reconcile_answer(conn, row, assessment)
            if value.review_session_id:
                conn.execute(
                    update(p.review_sessions)
                    .where(p.review_sessions.c.session_id == value.review_session_id)
                    .values(state="answered")
                )
            result = self._view(conn, row)
            self.repository._hook("learning_answer_before_commit", answer_id=answer_id)
            save_operation(conn, course_id, "answer_submit", value.operation_id, payload, result)
            return result

    def _grade(self, block: QuestionBlock, actual: str, request_id: str, revision_id: str):
        question = block.question
        evidence = {
            "answer_authority": question.answer_authority,
            "reference_answer": question.reference_answer,
            "question_type": question.question_type,
        }
        if self.grader is None:
            from .question_grading import QuestionGrader

            grader = QuestionGrader()
        else:
            grader = self.grader
        try:
            result = grader.grade(block, actual, request_id, revision_id)
        except AppError as exc:
            evidence["calculation_error"] = exc.code
            return (
                "pending",
                "本地比较未完成，原作答已保存；可请求参考评价或由用户判断。",
                None,
                evidence,
            )
        evidence["calculation"] = result
        if result["needs_manual_grading"] or result["is_correct"] is None:
            return "pending", "该题需要参考评价或用户判断，暂不计对错。", None, evidence
        correct = result["is_correct"]
        return (
            "correct" if correct else "wrong",
            "按此题已确认答案进行确定性比较。",
            1.0 if correct else 0.0,
            evidence,
        )

    def override(self, course_id: str, answer_id: str, value: UserVerdict) -> AnswerView:
        payload = {"answer_id": answer_id, "input": value.model_dump(mode="json")}
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "answer_override", value.operation_id, payload)
            if prior is not None:
                return AnswerView.model_validate(prior)
            row = self._answer(conn, course_id, answer_id)
            if row["assessment_revision"] != value.expected_revision:
                raise AppError("ASSESSMENT_VERSION_CONFLICT", "评价已变化，更正未覆盖其他新判定")
            assessment = AssessmentView(
                assessment_id=new_id("assessment"),
                answer_id=answer_id,
                revision=value.expected_revision + 1,
                parent_assessment_id=row["effective_assessment_id"],
                verdict=value.verdict,
                author="user_override",
                reason=value.reason,
                score=value.score,
                run_id=None,
                evidence={"previous_assessment_id": row["effective_assessment_id"]},
                created_at=now(),
            )
            self._append(conn, row, assessment)
            result = self._view(conn, row)
            self.repository._hook("learning_assessment_before_commit", answer_id=answer_id)
            save_operation(conn, course_id, "answer_override", value.operation_id, payload, result)
            return result

    def _append(self, conn, row: dict, assessment: AssessmentView, *, make_effective=True):
        conn.execute(insert(p.assessments).values(**assessment.model_dump(mode="json")))
        changed = conn.execute(
            update(p.answers)
            .where(
                p.answers.c.answer_id == row["answer_id"],
                p.answers.c.assessment_revision == row["assessment_revision"],
            )
            .values(
                effective_assessment_id=assessment.assessment_id
                if make_effective
                else row["effective_assessment_id"],
                assessment_revision=assessment.revision,
            )
        )
        if changed.rowcount != 1:
            raise AppError("ASSESSMENT_VERSION_CONFLICT", "评价版本已变化，原判和新结果均保留")
        row.update(
            effective_assessment_id=assessment.assessment_id
            if make_effective
            else row["effective_assessment_id"],
            assessment_revision=assessment.revision,
        )
        if make_effective:
            self.reviews.reconcile_answer(conn, row, assessment)

    def history(
        self,
        course_id: str,
        artifact_id: str,
        block_id: str,
        *,
        limit=50,
        before_sequence: int | None = None,
    ) -> AnswerHistory:
        with self.repository.engine.connect() as conn:
            ArtifactStore._view(conn, course_id, artifact_id)
            query = select(p.answers).where(
                p.answers.c.artifact_id == artifact_id, p.answers.c.block_id == block_id
            )
            if before_sequence is not None:
                query = query.where(p.answers.c.sequence < before_sequence)
            return AnswerHistory(
                answers=[
                    self._view(conn, dict(row))
                    for row in conn.execute(
                        query.order_by(p.answers.c.sequence.desc()).limit(limit)
                    ).mappings()
                ]
            )

    def assessments(self, course_id: str, answer_id: str) -> AssessmentHistory:
        with self.repository.engine.connect() as conn:
            self._answer(conn, course_id, answer_id)
            return AssessmentHistory(
                assessments=[
                    AssessmentView.model_validate(dict(row))
                    for row in conn.execute(
                        select(p.assessments)
                        .where(p.assessments.c.answer_id == answer_id)
                        .order_by(p.assessments.c.revision)
                    ).mappings()
                ]
            )
