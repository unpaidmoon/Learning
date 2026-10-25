"""Persistent explicit learning tasks. Recovery observes; it never redispatches."""

from __future__ import annotations

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection

from learning_app.errors import AppError
from learning_app.imported_executions import imported_executions
from learning_app.storage import Repository
from learning_app.storage import schema as business

from . import schema as s
from .common import new_id, now, prior_operation, save_operation
from .manifests import ManifestStore
from .models import TaskAction, TaskCreate, TaskGapDecision, TaskOutline, TaskUnitView, TaskView
from .sources import SourceStore

FINAL_TASK_STATES = frozenset({"completed", "partial", "cancelled"})
UNSETTLED_UNIT_STATES = frozenset({"dispatching", "unknown"})


class TaskStore:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.sources = SourceStore(repository)

    @staticmethod
    def _row(conn: Connection, course_id: str, task_id: str) -> dict:
        row = (
            conn.execute(
                select(s.tasks).where(
                    s.tasks.c.task_id == task_id,
                    s.tasks.c.course_id == course_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("LEARNING_TASK_NOT_FOUND", "指定课程的学习任务不存在", 404)
        return dict(row)

    @staticmethod
    def _view(conn: Connection, course_id: str, task_id: str) -> TaskView:
        row = TaskStore._row(conn, course_id, task_id)
        units = [
            TaskUnitView.model_validate({key: unit[key] for key in TaskUnitView.model_fields})
            for unit in conn.execute(
                select(s.task_units)
                .where(s.task_units.c.task_id == task_id)
                .order_by(s.task_units.c.ordinal),
            ).mappings()
        ]
        return TaskView(
            **{key: row[key] for key in TaskView.model_fields if key != "units" and key in row},
            units=units,
        )

    def get(self, course_id: str, task_id: str) -> TaskView:
        with self.repository.engine.connect() as conn:
            return self._view(conn, course_id, task_id)

    def list(self, course_id: str) -> list[TaskView]:
        with self.repository.engine.connect() as conn:
            identities = (
                conn.execute(
                    select(s.tasks.c.task_id)
                    .where(
                        s.tasks.c.course_id == course_id,
                    )
                    .order_by(s.tasks.c.created_at.desc())
                )
                .scalars()
                .all()
            )
            return [self._view(conn, course_id, identity) for identity in identities]

    @staticmethod
    def _event(conn: Connection, row: dict, kind: str, detail: dict, **changes) -> None:
        revision = row["revision"] + 1
        timestamp = now()
        conn.execute(
            update(s.tasks)
            .where(
                s.tasks.c.task_id == row["task_id"],
                s.tasks.c.revision == row["revision"],
            )
            .values(**changes, revision=revision, updated_at=timestamp)
        )
        conn.execute(
            insert(s.task_events).values(
                task_id=row["task_id"],
                revision=revision,
                kind=kind,
                detail=detail,
                created_at=timestamp,
            )
        )

    def create(self, course_id: str, value: TaskCreate) -> TaskView:
        if value.kind == "essay_review":
            raise AppError(
                "ESSAY_REVIEW_SCOPE_REQUIRED", "作文参考评价须绑定实际稿件修订及轮次", 422
            )
        if value.kind == "answer_review":
            raise AppError(
                "ANSWER_REVIEW_SCOPE_REQUIRED",
                "参考评价须通过绑定实际作答和版本的专用任务提交",
                422,
            )
        units = value.units
        if value.scope_manifest_id:
            units = ManifestStore(self.repository).get(course_id, value.scope_manifest_id).units
        snapshots = {
            unit.source_snapshot_id: self.sources.snapshot(course_id, unit.source_snapshot_id)
            for unit in units
        }
        unavailable = {}
        for identity, snapshot in snapshots.items():
            try:
                self.sources.require_current(snapshot)
            except AppError as exc:
                unavailable[identity] = exc.code
        payload = value.model_dump(mode="json")
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "task_create", value.operation_id, payload)
            if prior is not None:
                return TaskView.model_validate(prior)
            if not conn.execute(
                select(business.courses).where(
                    business.courses.c.course_id == course_id,
                )
            ).first():
                raise AppError("COURSE_NOT_FOUND", "课程不存在", 404)
            if (
                value.chat_id
                and not conn.execute(
                    select(business.chats).where(
                        business.chats.c.chat_id == value.chat_id,
                        business.chats.c.course_id == course_id,
                    )
                ).first()
            ):
                raise AppError("TASK_CHAT_DENIED", "任务对话不属于指定课程", 403)
            if value.sources_only and not units:
                raise AppError("TASK_SOURCES_REQUIRED", "只依据资料的任务须先指定实际来源", 422)
            outline_confirmed = value.kind != "book" or value.outline_supplied
            state = (
                "awaiting_outline"
                if not outline_confirmed
                else ("needs_sources" if unavailable else "queued")
            )
            identity, timestamp = new_id("task"), now()
            conn.execute(
                insert(s.tasks).values(
                    task_id=identity,
                    course_id=course_id,
                    chat_id=value.chat_id,
                    kind=value.kind,
                    instruction=value.instruction,
                    coverage_mode=value.coverage_mode,
                    scope_manifest_id=value.scope_manifest_id,
                    revision=0,
                    scope_revision=0,
                    state=state,
                    settings=value.settings.model_dump(mode="json"),
                    sources_only=value.sources_only,
                    allow_external=value.allow_external,
                    outline=[item.model_dump(mode="json") for item in value.outline],
                    outline_confirmed=outline_confirmed,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            for ordinal, unit in enumerate(units):
                # Recheck ownership under the transaction as well as availability before it.
                self.sources.in_transaction(conn, course_id, unit.source_snapshot_id)
                error = unavailable.get(unit.source_snapshot_id)
                conn.execute(
                    insert(s.task_units).values(
                        task_id=identity,
                        unit_id=unit.unit_id,
                        ordinal=ordinal,
                        label=unit.label,
                        source_snapshot_id=unit.source_snapshot_id,
                        state="missing" if error else "pending",
                        error_code=error,
                    )
                )
            conn.execute(
                insert(s.task_events).values(
                    task_id=identity,
                    revision=0,
                    kind="created",
                    detail=payload,
                    created_at=timestamp,
                )
            )
            self.repository._hook("learning_task_before_commit", task_id=identity)
            result = self._view(conn, course_id, identity)
            save_operation(conn, course_id, "task_create", value.operation_id, payload, result)
            return result

    @staticmethod
    def _check_revision(row: dict, expected: int) -> None:
        if row["revision"] != expected:
            raise AppError("TASK_REVISION_CONFLICT", "任务已变化，请读取最新进度后再操作")

    def control(
        self,
        course_id: str,
        task_id: str,
        action: str,
        value: TaskAction,
    ) -> TaskView:
        imported_executions(self.repository).require_fresh("task", task_id)
        if action not in {"pause", "resume", "cancel"}:
            raise AppError("TASK_ACTION_DENIED", "该任务操作不被支持", 422)
        payload = {"task_id": task_id, "action": action, "input": value.model_dump(mode="json")}
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "task_control", value.operation_id, payload)
            if prior is not None:
                return TaskView.model_validate(prior)
            row = self._row(conn, course_id, task_id)
            self._check_revision(row, value.expected_revision)
            if row["state"] in FINAL_TASK_STATES:
                raise AppError("TASK_ALREADY_FINAL", "任务已结束；已有内容和历史仍保留")
            current = self._view(conn, course_id, task_id)
            if action == "cancel":
                state = "cancelled"
            elif action == "pause":
                state = "paused"
            elif not row["outline_confirmed"]:
                state = "awaiting_outline"
            elif any(unit.state in UNSETTLED_UNIT_STATES for unit in current.units):
                state = "unknown"
            elif row["layout_version"] == 2 and any(
                unit.state == "failed"
                for unit in current.units
                if unit.excluded_at_scope_revision is None
            ):
                state = "failed"
            elif any(
                unit.state in {"missing", "failed"}
                for unit in current.units
                if unit.excluded_at_scope_revision is None
            ):
                state = "needs_sources"
            else:
                state = "queued"
            self._event(conn, row, action, {}, state=state)
            result = self._view(conn, course_id, task_id)
            save_operation(conn, course_id, "task_control", value.operation_id, payload, result)
            return result

    def confirm_outline(self, course_id: str, task_id: str, value: TaskOutline) -> TaskView:
        imported_executions(self.repository).require_fresh("task", task_id)
        payload = {"task_id": task_id, "input": value.model_dump(mode="json")}
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "task_outline", value.operation_id, payload)
            if prior is not None:
                return TaskView.model_validate(prior)
            row = self._row(conn, course_id, task_id)
            self._check_revision(row, value.expected_revision)
            if row["kind"] != "book" or row["state"] != "awaiting_outline":
                raise AppError("TASK_OUTLINE_STATE_INVALID", "当前任务没有待确认的目录")
            current = self._view(conn, course_id, task_id)
            state = (
                "needs_sources"
                if any(unit.state == "missing" for unit in current.units)
                else "queued"
            )
            self._event(
                conn,
                row,
                "outline_confirmed",
                {},
                state=state,
                outline_confirmed=True,
                outline=[item.model_dump(mode="json") for item in value.outline],
            )
            result = self._view(conn, course_id, task_id)
            save_operation(conn, course_id, "task_outline", value.operation_id, payload, result)
            return result

    def decide_gap(self, course_id: str, task_id: str, value: TaskGapDecision) -> TaskView:
        imported_executions(self.repository).require_fresh("task", task_id)
        payload = {"task_id": task_id, "input": value.model_dump(mode="json")}
        with self.repository.engine.connect() as conn:
            prior = prior_operation(conn, course_id, "task_gap", value.operation_id, payload)
            if prior is not None:
                return TaskView.model_validate(prior)
        before = self.get(course_id, task_id)
        selected = [unit for unit in before.units if unit.unit_id in value.unit_ids]
        if len(selected) != len(set(value.unit_ids)):
            raise AppError("TASK_UNIT_NOT_FOUND", "部分单元不属于指定任务", 404)
        if not selected or any(unit.state not in {"missing", "failed"} for unit in selected):
            raise AppError("TASK_GAP_DECISION_INVALID", "只对已知缺失或失败的单元作缺口决定")
        refreshed_sources = {}
        if value.decision == "retry":
            for unit in selected:
                snapshot = self.sources.snapshot(course_id, unit.source_snapshot_id)
                refreshed = self.sources.refresh_gap(snapshot)
                if refreshed.snapshot_id != snapshot.snapshot_id:
                    refreshed_sources[unit.unit_id] = refreshed.snapshot_id
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "task_gap", value.operation_id, payload)
            if prior is not None:
                return TaskView.model_validate(prior)
            row = self._row(conn, course_id, task_id)
            self._check_revision(row, value.expected_revision)
            if row["state"] != "needs_sources":
                raise AppError("TASK_GAP_STATE_INVALID", "任务当前未等待缺口决定")
            if value.decision == "accept_partial":
                current = self._view(conn, course_id, task_id)
                if any(unit.state in UNSETTLED_UNIT_STATES for unit in current.units):
                    raise AppError(
                        "TASK_RECONCILIATION_REQUIRED", "先核对结果未知的执行，再接受部分成果"
                    )
                if any(unit.state in {"pending", "read"} for unit in current.units):
                    raise AppError("TASK_UNITS_PENDING", "仍有未处理单元，请先完成或明确排除")
                state = "partial"
            else:
                excluded = value.decision == "exclude"
                conn.execute(
                    update(s.task_units)
                    .where(
                        s.task_units.c.task_id == task_id,
                        s.task_units.c.unit_id.in_(value.unit_ids),
                    )
                    .values(
                        **(
                            {"excluded_at_scope_revision": row["scope_revision"] + 1}
                            if excluded
                            else {"state": "pending", "error_code": None, "run_id": None}
                        )
                    )
                )
                for unit_id, identity in refreshed_sources.items():
                    from .source_grants import declare_snapshot

                    declare_snapshot(
                        conn,
                        course_id,
                        identity,
                        authority={
                            "kind": "task_gap_retry",
                            "task_id": task_id,
                            "unit_id": unit_id,
                            "scope_revision": row["scope_revision"] + 1,
                        },
                    )
                    conn.execute(
                        update(s.task_units)
                        .where(
                            s.task_units.c.task_id == task_id,
                            s.task_units.c.unit_id == unit_id,
                        )
                        .values(source_snapshot_id=identity)
                    )
                current = self._view(conn, course_id, task_id)
                remaining = [
                    unit for unit in current.units if unit.excluded_at_scope_revision is None
                ]
                state = (
                    "needs_sources"
                    if any(unit.state in {"missing", "failed"} for unit in remaining)
                    else "queued"
                )
            self._event(
                conn,
                row,
                "gap_decision",
                {**value.model_dump(mode="json"), "refreshed_sources": refreshed_sources},
                state=state,
                scope_revision=row["scope_revision"]
                + (value.decision != "retry" or bool(refreshed_sources)),
            )
            result = self._view(conn, course_id, task_id)
            save_operation(conn, course_id, "task_gap", value.operation_id, payload, result)
            return result

    def reconcile(self, course_id: str, task_id: str) -> TaskView:
        if imported_executions(self.repository).contains("task", task_id):
            return self.get(course_id, task_id)
        """Use durable execution facts; completed products survive pause and restart."""
        with self.repository._lock, self.repository.engine.begin() as conn:
            row = self._row(conn, course_id, task_id)
            current = self._view(conn, course_id, task_id)
            changed = False
            for unit in current.units:
                if not unit.run_id or unit.state == "completed":
                    continue
                execution = conn.execute(
                    select(s.task_runs.c.state).where(
                        s.task_runs.c.run_id == unit.run_id,
                    )
                ).scalar_one()
                if unit.state == "failed" and execution == "completed":
                    # A known parse/publication failure is not an unknown provider result.
                    continue
                if execution == "completed" and unit.result_artifact_id:
                    state, error = "completed", None
                elif execution in {"failed", "cancelled", "truncated"}:
                    state, error = "failed", f"GENERATION_{execution.upper()}"
                elif execution == "prepared":
                    state, error = "read", None
                elif execution in {"dispatching", "accepted", "running"}:
                    state, error = "dispatching", None
                else:
                    # Completion without a product also needs publication reconciliation.
                    state, error = "unknown", "TASK_EXECUTION_RECONCILIATION_REQUIRED"
                if state != unit.state or error != unit.error_code:
                    conn.execute(
                        update(s.task_units)
                        .where(
                            s.task_units.c.task_id == task_id,
                            s.task_units.c.unit_id == unit.unit_id,
                        )
                        .values(state=state, error_code=error)
                    )
                    changed = True
            refreshed = self._view(conn, course_id, task_id)
            active = [unit for unit in refreshed.units if unit.excluded_at_scope_revision is None]
            state = row["state"]
            if state not in FINAL_TASK_STATES | {"paused", "awaiting_outline"}:
                if row["layout_version"] == 2:
                    from .task_planning import TaskPlanner

                    state = TaskPlanner.state(conn, row, refreshed.units)
                elif any(unit.state == "unknown" for unit in active):
                    state = "unknown"
                elif any(unit.state == "dispatching" for unit in active):
                    state = "running"
                elif any(unit.state in {"failed", "missing"} for unit in active):
                    state = "needs_sources"
                elif active and all(unit.state == "completed" for unit in active):
                    state = "completed"
                elif state == "running":
                    state = "queued"
            if changed or state != row["state"]:
                self._event(conn, row, "reconciled", {}, state=state)
            return self._view(conn, course_id, task_id)

    def mark_unit_failed(
        self,
        course_id: str,
        task_id: str,
        unit_id: str,
        code: str,
        *,
        missing: bool = False,
        cause: str | None = None,
        source_leaf_ids: tuple[str, ...] | None = None,
    ) -> None:
        with self.repository._lock, self.repository.engine.begin() as conn:
            row = self._row(conn, course_id, task_id)
            conn.execute(
                update(s.task_units)
                .where(
                    s.task_units.c.task_id == task_id,
                    s.task_units.c.unit_id == unit_id,
                    s.task_units.c.state != "completed",
                )
                .values(state="missing" if missing else "failed", error_code=code)
            )
            state = (
                row["state"]
                if row["state"] in FINAL_TASK_STATES | {"paused"}
                else ("needs_sources" if missing or row["layout_version"] == 1 else "failed")
            )
            if missing and row["layout_version"] == 2:
                spec = conn.execute(
                    select(s.task_units.c.work_spec).where(
                        s.task_units.c.task_id == task_id, s.task_units.c.unit_id == unit_id
                    )
                ).scalar_one()
                owned_leaf_ids = {item["leaf_id"] for item in spec["inputs"]}
                leaf_ids = set(source_leaf_ids) if source_leaf_ids is not None else owned_leaf_ids
                if not leaf_ids.issubset(owned_leaf_ids):
                    raise AppError("TASK_SOURCE_SCOPE_DENIED", "失效来源不属于本批任务", 403)
                conn.execute(
                    update(s.task_source_leaves)
                    .where(s.task_source_leaves.c.leaf_id.in_(leaf_ids))
                    .values(state="blocked", error_code=code, updated_at=now())
                )
                conn.execute(
                    update(s.task_sources)
                    .where(
                        s.task_sources.c.source_id.in_(
                            select(s.task_source_leaves.c.source_id).where(
                                s.task_source_leaves.c.leaf_id.in_(leaf_ids)
                            )
                        )
                    )
                    .values(state="blocked", error_code=code, updated_at=now())
                )
            self._event(
                conn,
                row,
                "unit_unavailable",
                {"unit_id": unit_id, "code": code, **({"cause_code": cause} if cause else {})},
                state=state,
            )

    def recover(self, *, is_read_only=None) -> list[TaskView]:
        with self.repository._lock, self.repository.engine.begin() as conn:
            # A persisted active label is not proof that the new Host accepted it.
            active = list(
                conn.execute(
                    select(s.task_runs.c.run_id, s.task_runs.c.task_id).where(
                        s.task_runs.c.state.in_(["dispatching", "accepted", "running"])
                    )
                )
            )
            for run_id, task_id in active:
                if is_read_only and is_read_only(task_id):
                    continue
                conn.execute(
                    update(s.task_runs)
                    .where(s.task_runs.c.run_id == run_id)
                    .values(
                        state="unknown",
                        error_code="TASK_EXECUTION_RECOVERY_REQUIRED",
                        updated_at=now(),
                    )
                )
            pending = conn.execute(
                select(s.tasks.c.course_id, s.tasks.c.task_id).where(
                    s.tasks.c.state.in_(["running", "unknown", "paused"]),
                )
            ).all()
        return [
            self.reconcile(course_id, task_id)
            for course_id, task_id in pending
            if not is_read_only or not is_read_only(task_id)
        ]
