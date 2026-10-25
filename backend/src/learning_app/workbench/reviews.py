"""One durable review budget, user-owned controls and transactional FSRS events."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, insert, select, update

from learning_app.errors import AppError
from learning_app.storage import Repository

from . import practice_schema as p
from . import schema as s
from .common import new_id, now, prior_operation, save_operation
from .models import CardBlock, PathStepBlock
from .practice_models import (
    ReviewControl,
    ReviewItem,
    ReviewPreferenceInput,
    ReviewPreferences,
    ReviewRatingInput,
    ReviewRatingReceipt,
    ReviewSession,
    ReviewSessionInput,
    ReviewUndo,
    TodayReview,
)
from .scheduler import FSRS_CONFIG, FsrsScheduler


def utc_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


class ReviewStore:
    def __init__(self, repository: Repository, *, clock=utc_ms, scheduler=None):
        self.repository = repository
        self.clock = clock
        self.scheduler = scheduler or FsrsScheduler()

    @staticmethod
    def _item(conn, course_id, review_id) -> dict:
        row = (
            conn.execute(
                select(p.review_items).where(
                    p.review_items.c.review_id == review_id,
                    p.review_items.c.course_id == course_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("REVIEW_NOT_FOUND", "指定课程的复习项不存在", 404)
        return dict(row)

    def get(self, course_id: str, review_id: str) -> ReviewItem:
        with self.repository.engine.connect() as conn:
            return self.public(conn, self._item(conn, course_id, review_id))

    @staticmethod
    def public(conn, row: dict) -> ReviewItem:
        parts = func.json_each(s.revisions.c.body, "$.blocks").table_valued(
            "value", joins_implicitly=True
        )
        label = conn.execute(
            select(func.substr(func.json_extract(parts.c.value, "$.markdown"), 1, 160))
            .select_from(s.revisions)
            .select_from(parts)
            .where(
                s.revisions.c.revision_id == row["content_revision_id"],
                func.json_extract(parts.c.value, "$.block_id") == row["block_id"],
            )
            .limit(1)
        ).scalar_one_or_none()
        return ReviewItem.model_validate({**row, "label": label or "复习内容"})

    @staticmethod
    def _preferences(conn) -> dict:
        row = conn.execute(select(p.review_preferences)).mappings().first()
        if row is None:
            value = dict(
                preference_id="global",
                revision=0,
                daily_limit=20,
                timezone="Asia/Shanghai",
                day_rollover_hour=4,
            )
            conn.execute(insert(p.review_preferences).values(**value))
            return value
        return dict(row)

    def preferences(self) -> ReviewPreferences:
        with self.repository._lock, self.repository.engine.begin() as conn:
            value = self._preferences(conn)
            return ReviewPreferences(**{key: value[key] for key in ReviewPreferences.model_fields})

    def set_preferences(self, course_id: str, value: ReviewPreferenceInput) -> ReviewPreferences:
        payload = value.model_dump(mode="json")
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(
                conn, course_id, "review_preferences", value.operation_id, payload
            )
            if prior is not None:
                return ReviewPreferences.model_validate(prior)
            self._require_course(conn, course_id)
            current = self._preferences(conn)
            if current["revision"] != value.expected_revision:
                raise AppError("REVIEW_PREFERENCES_CONFLICT", "复习设置已变化，请核对当前版本")
            conn.execute(
                update(p.review_preferences)
                .where(
                    p.review_preferences.c.preference_id == "global",
                    p.review_preferences.c.revision == value.expected_revision,
                )
                .values(daily_limit=value.daily_limit, revision=value.expected_revision + 1)
            )
            result = ReviewPreferences(
                revision=value.expected_revision + 1,
                daily_limit=value.daily_limit,
                timezone=current["timezone"],
                day_rollover_hour=current["day_rollover_hour"],
            )
            save_operation(
                conn, course_id, "review_preferences", value.operation_id, payload, result
            )
            return result

    @staticmethod
    def _require_course(conn, course_id):
        from learning_app.storage import schema as business

        if not conn.execute(
            select(business.courses.c.course_id).where(
                business.courses.c.course_id == course_id,
            )
        ).first():
            raise AppError("COURSE_NOT_FOUND", "课程不存在", 404)

    def _logical_day(self, current_ms: int, preferences: dict) -> str:
        return self.scheduler.logical_day(
            {
                "now_ms": current_ms,
                "timezone": preferences["timezone"],
                "day_rollover_hour": preferences["day_rollover_hour"],
            }
        )["logical_day"]

    @staticmethod
    def _change(
        conn, before: dict | None, changes: dict, kind: str, detail: dict
    ) -> tuple[dict, str]:
        after = {
            **(before or {}),
            **changes,
            "schedule_revision": before["schedule_revision"] + 1 if before else 1,
            "updated_at": now(),
        }
        if before:
            result = conn.execute(
                update(p.review_items)
                .where(
                    p.review_items.c.review_id == before["review_id"],
                    p.review_items.c.schedule_revision == before["schedule_revision"],
                )
                .values(**after)
            )
            if result.rowcount != 1:
                raise AppError("REVIEW_VERSION_CONFLICT", "复习排期已变化，未重复提交")
        else:
            conn.execute(insert(p.review_items).values(**after))
        event_id = new_id("reviewevent")
        conn.execute(
            insert(p.review_events).values(
                event_id=event_id,
                review_id=after["review_id"],
                revision=after["schedule_revision"],
                kind=kind,
                before=before,
                after=after,
                detail=detail,
                created_at=now(),
            )
        )
        return after, event_id

    def sync_cards(self, conn, artifact) -> None:
        """Content save and default enqueue are one transaction; manual flags are sticky."""
        if artifact.kind not in {"card_set", "learning_path"}:
            return
        kind = "card" if artifact.kind == "card_set" else "knowledge"
        existing = {
            row["block_id"]: dict(row)
            for row in conn.execute(
                select(p.review_items).where(
                    p.review_items.c.artifact_id == artifact.artifact_id,
                    p.review_items.c.kind == kind,
                )
            ).mappings()
        }
        cards = {
            block.block_id: block
            for block in artifact.revision.body.blocks
            if isinstance(block, CardBlock if kind == "card" else PathStepBlock)
        }
        for block_id in cards:
            before = existing.get(block_id)
            if (
                before
                and before["content_revision_id"] == artifact.head_revision_id
                and before["eligible"]
            ):
                continue
            changes = {
                "content_revision_id": artifact.head_revision_id,
                "eligible": True,
                "inactive_reason": None,
            }
            if before is None:
                changes.update(
                    review_id=new_id("review"),
                    course_id=artifact.course_id,
                    kind=kind,
                    artifact_id=artifact.artifact_id,
                    block_id=block_id,
                    source_answer_id=None,
                    assessment_id=None,
                    queue_state="active",
                    due_ms=self.clock() + (86_400_000 if kind == "knowledge" else 0),
                    scheduler="deepstudent-fsrs-6.6.2-v1",
                    scheduler_state={
                        "phase": "new",
                        "memory": None,
                        "learning_step": 0,
                        "last_review_ms": None,
                        "reps": 0,
                        "lapses": 0,
                    },
                    scheduler_config=FSRS_CONFIG,
                    created_at=now(),
                )
            self._change(
                conn,
                before,
                changes,
                "created" if before is None else "content_updated",
                {"content_revision_id": artifact.head_revision_id},
            )
        for block_id, before in existing.items():
            if block_id not in cards and before["eligible"]:
                self._change(
                    conn,
                    before,
                    {"eligible": False, "inactive_reason": "content_removed"},
                    "content_updated",
                    {"content_revision_id": artifact.head_revision_id},
                )

    def reconcile_answer(self, conn, answer: dict, assessment) -> None:
        latest = conn.execute(
            select(p.answers.c.answer_id)
            .where(
                p.answers.c.artifact_id == answer["artifact_id"],
                p.answers.c.block_id == answer["block_id"],
            )
            .order_by(p.answers.c.sequence.desc())
            .limit(1)
        ).scalar_one()
        if latest != answer["answer_id"]:
            return  # An old-answer correction remains history, never replaces a newer answer.
        before = (
            conn.execute(
                select(p.review_items).where(
                    p.review_items.c.artifact_id == answer["artifact_id"],
                    p.review_items.c.block_id == answer["block_id"],
                    p.review_items.c.kind == "wrong_question",
                )
            )
            .mappings()
            .first()
        )
        before = dict(before) if before else None
        wrong = assessment.verdict == "wrong"
        if before is None and not wrong:
            return
        changes = dict(
            content_revision_id=answer["question_revision_id"],
            source_answer_id=answer["answer_id"],
            assessment_id=assessment.assessment_id,
            eligible=wrong,
            inactive_reason=None if wrong else assessment.verdict,
        )
        if before is None:
            changes.update(
                review_id=new_id("review"),
                course_id=answer["course_id"],
                kind="wrong_question",
                artifact_id=answer["artifact_id"],
                block_id=answer["block_id"],
                queue_state="active",
                due_ms=self.clock() + 86_400_000,
                scheduler="wrong-question-next-day-v1",
                scheduler_state={},
                scheduler_config={"delay_days": 1},
                created_at=now(),
            )
        elif wrong and (
            before["source_answer_id"] != answer["answer_id"] or not before["eligible"]
        ):
            changes["due_ms"] = self.clock() + 86_400_000
        self._change(
            conn,
            before,
            changes,
            "assessment_changed",
            {
                "answer_id": answer["answer_id"],
                "assessment_id": assessment.assessment_id,
                "verdict": assessment.verdict,
            },
        )

    def today(self, *, course_id: str | None = None, kind: str | None = None) -> TodayReview:
        with self.repository._lock, self.repository.engine.begin() as conn:
            if course_id:
                self._require_course(conn, course_id)
            preferences, current_ms = self._preferences(conn), self.clock()
            day = self._logical_day(current_ms, preferences)
            frozen = (
                conn.execute(select(p.review_days).where(p.review_days.c.logical_day == day))
                .mappings()
                .first()
            )
            if frozen is None:
                frozen = dict(
                    logical_day=day,
                    daily_limit=preferences["daily_limit"],
                    timezone=preferences["timezone"],
                    day_rollover_hour=preferences["day_rollover_hour"],
                    created_at=now(),
                )
                conn.execute(insert(p.review_days).values(**frozen))
            admitted = set(
                conn.execute(
                    select(p.review_admissions.c.review_id).where(
                        p.review_admissions.c.logical_day == day,
                    )
                ).scalars()
            )
            remaining = max(0, frozen["daily_limit"] - len(admitted))
            if remaining:
                query = select(p.review_items.c.review_id).where(
                    p.review_items.c.queue_state == "active",
                    p.review_items.c.eligible.is_(True),
                    p.review_items.c.due_ms <= current_ms,
                )
                if admitted:
                    query = query.where(p.review_items.c.review_id.not_in(admitted))
                selected = (
                    conn.execute(
                        query.order_by(
                            p.review_items.c.due_ms,
                            p.review_items.c.created_at,
                            p.review_items.c.review_id,
                        ).limit(remaining)
                    )
                    .scalars()
                    .all()
                )
                for identity in selected:
                    conn.execute(
                        insert(p.review_admissions).values(
                            logical_day=day, review_id=identity, admitted_at=now()
                        )
                    )
                admitted.update(selected)
            completed = set(
                conn.execute(
                    select(p.review_sessions.c.review_id).where(
                        p.review_sessions.c.logical_day == day,
                        p.review_sessions.c.state.in_(["rated", "answered"]),
                    )
                ).scalars()
            )
            query = select(p.review_items).where(
                p.review_items.c.review_id.in_(admitted),
                p.review_items.c.queue_state == "active",
                p.review_items.c.eligible.is_(True),
                p.review_items.c.due_ms <= current_ms,
            )
            if course_id:
                query = query.where(p.review_items.c.course_id == course_id)
            if kind:
                query = query.where(p.review_items.c.kind == kind)
            items = [
                self.public(conn, dict(row))
                for row in conn.execute(
                    query.order_by(p.review_items.c.due_ms, p.review_items.c.review_id)
                ).mappings()
            ]
            return TodayReview(
                logical_day=day,
                daily_limit=frozen["daily_limit"],
                admitted_count=len(admitted),
                completed_count=len(completed),
                items=items,
            )

    def control(self, course_id: str, review_id: str, value: ReviewControl) -> ReviewItem:
        payload = {"review_id": review_id, "input": value.model_dump(mode="json")}
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "review_control", value.operation_id, payload)
            if prior is not None:
                return ReviewItem.model_validate(prior)
            before = self._item(conn, course_id, review_id)
            self._expected(before, value.expected_revision)
            state = {"pause": "paused", "remove": "removed", "resume": "active"}[value.action]
            after, _ = self._change(conn, before, {"queue_state": state}, value.action, {})
            result = self.public(conn, after)
            save_operation(conn, course_id, "review_control", value.operation_id, payload, result)
            return result

    @staticmethod
    def _expected(item, expected):
        if item["schedule_revision"] != expected:
            raise AppError("REVIEW_VERSION_CONFLICT", "复习内容或排期已变化，原操作未覆盖新版本")

    def _session(self, conn, course_id, review_id, session_id):
        item = self._item(conn, course_id, review_id)
        session = (
            conn.execute(
                select(p.review_sessions).where(
                    p.review_sessions.c.session_id == session_id,
                    p.review_sessions.c.review_id == review_id,
                )
            )
            .mappings()
            .first()
        )
        if session is None:
            raise AppError("REVIEW_SESSION_NOT_FOUND", "该复习会话不存在", 404)
        return item, dict(session)

    def open_session(
        self, course_id: str, review_id: str, value: ReviewSessionInput
    ) -> ReviewSession:
        self.today()  # Global arbitration always precedes any course/type filter.
        payload = {"review_id": review_id, "input": value.model_dump(mode="json")}
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "review_open", value.operation_id, payload)
            if prior is not None:
                return ReviewSession.model_validate(prior)
            item = self._item(conn, course_id, review_id)
            self._expected(item, value.expected_revision)
            self._active(item)
            day = self._logical_day(self.clock(), self._preferences(conn))
            if (
                not conn.execute(
                    select(p.review_admissions).where(
                        p.review_admissions.c.logical_day == day,
                        p.review_admissions.c.review_id == review_id,
                    )
                ).first()
                or item["due_ms"] > self.clock()
            ):
                raise AppError("REVIEW_NOT_ADMITTED", "此项尚未进入今日总量或尚未到期")
            result = ReviewSession(
                session_id=new_id("reviewsession"),
                review_id=review_id,
                logical_day=day,
                schedule_revision=item["schedule_revision"],
                content_revision_id=item["content_revision_id"],
                state="opened",
                opened_at=now(),
                revealed_at=None,
                rating_event_id=None,
            )
            conn.execute(insert(p.review_sessions).values(**result.model_dump(mode="json")))
            save_operation(conn, course_id, "review_open", value.operation_id, payload, result)
            return result

    @staticmethod
    def _active(item):
        if item["queue_state"] != "active" or not item["eligible"]:
            raise AppError("REVIEW_INACTIVE", "复习项已暂停、移出或不再符合当前有效评价")

    def reveal(
        self, course_id: str, review_id: str, session_id: str, value: ReviewSessionInput
    ) -> ReviewSession:
        payload = {
            "review_id": review_id,
            "session_id": session_id,
            "input": value.model_dump(mode="json"),
        }
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "review_reveal", value.operation_id, payload)
            if prior is not None:
                return ReviewSession.model_validate(prior)
            item, session = self._session(conn, course_id, review_id, session_id)
            self._active(item)
            self._expected(item, value.expected_revision)
            if (
                session["schedule_revision"] != item["schedule_revision"]
                or session["state"] != "opened"
            ):
                raise AppError("REVIEW_SESSION_STALE", "该复习会话已变化或已评分")
            session.update(state="revealed", revealed_at=now())
            conn.execute(
                update(p.review_sessions)
                .where(p.review_sessions.c.session_id == session_id)
                .values(**session)
            )
            result = ReviewSession.model_validate(session)
            save_operation(conn, course_id, "review_reveal", value.operation_id, payload, result)
            return result

    def rate(self, course_id: str, review_id: str, value: ReviewRatingInput) -> ReviewRatingReceipt:
        payload = {"review_id": review_id, "input": value.model_dump(mode="json")}
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "review_rating", value.operation_id, payload)
            if prior is not None:
                return ReviewRatingReceipt.model_validate(prior)
            before, session = self._session(conn, course_id, review_id, value.session_id)
            self._active(before)
            self._expected(before, value.expected_revision)
            if before["kind"] not in {"card", "knowledge"}:
                raise AppError(
                    "REVIEW_RATING_KIND_DENIED", "错题须提交实际作答，不能用翻面自评代替"
                )
            if (
                session["state"] != "revealed"
                or session["schedule_revision"] != before["schedule_revision"]
                or session["content_revision_id"] != before["content_revision_id"]
            ):
                raise AppError("REVIEW_SESSION_STALE", "请先在当前内容版本完成回忆与翻面")
            preferences, current_ms = self._preferences(conn), self.clock()
            if session["logical_day"] != self._logical_day(current_ms, preferences):
                raise AppError("REVIEW_DAY_CHANGED", "复习日已切换，请从今日入口重新打开")
            calculation = self.scheduler.calculate(
                before,
                {
                    "now_ms": current_ms,
                    "timezone": preferences["timezone"],
                    "day_rollover_hour": preferences["day_rollover_hour"],
                },
                value.operation_id,
                value.rating,
            )
            outcome, audit = calculation["outcome"], calculation["audit"]
            state = {
                key: outcome[key] for key in ("phase", "memory", "learning_step", "reps", "lapses")
            }
            state["last_review_ms"] = current_ms
            after, event_id = self._change(
                conn,
                before,
                {"due_ms": outcome["due_ms"], "scheduler_state": state},
                "reviewed",
                {"rating": value.rating, "session_id": value.session_id, "audit": audit},
            )
            session.update(state="rated", rating_event_id=event_id)
            conn.execute(
                update(p.review_sessions)
                .where(p.review_sessions.c.session_id == value.session_id)
                .values(**session)
            )
            result = ReviewRatingReceipt(
                event_id=event_id,
                item=self.public(conn, after),
                session=ReviewSession.model_validate(session),
                audit=audit,
            )
            self.repository._hook("learning_review_rating_before_commit", review_id=review_id)
            save_operation(conn, course_id, "review_rating", value.operation_id, payload, result)
            return result

    def undo(self, course_id: str, review_id: str, value: ReviewUndo) -> ReviewItem:
        payload = {"review_id": review_id, "input": value.model_dump(mode="json")}
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "review_undo", value.operation_id, payload)
            if prior is not None:
                return ReviewItem.model_validate(prior)
            current = self._item(conn, course_id, review_id)
            self._expected(current, value.expected_revision)
            event = (
                conn.execute(
                    select(p.review_events).where(
                        p.review_events.c.event_id == value.event_id,
                        p.review_events.c.review_id == review_id,
                        p.review_events.c.revision == current["schedule_revision"],
                        p.review_events.c.kind == "reviewed",
                    )
                )
                .mappings()
                .first()
            )
            if event is None or event["before"] is None:
                raise AppError("REVIEW_UNDO_STALE", "只能撤销最新一次且其后尚无其他更改的评分")
            restored, _ = self._change(
                conn, current, event["before"], "undone", {"event_id": value.event_id}
            )
            conn.execute(
                update(p.review_sessions)
                .where(p.review_sessions.c.rating_event_id == value.event_id)
                .values(
                    state="revealed",
                    rating_event_id=None,
                    schedule_revision=restored["schedule_revision"],
                )
            )
            result = self.public(conn, restored)
            save_operation(conn, course_id, "review_undo", value.operation_id, payload, result)
            return result
