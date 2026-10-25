"""Explicit gap and generation recovery; unknown execution is never replayed."""

from sqlalchemy import select, update

from learning_app.errors import AppError
from learning_app.imported_executions import imported_executions
from learning_app.storage.repository import _hash

from . import schema as s
from .common import now, prior_operation, save_operation
from .models import TaskAction, TaskView
from .runs import TaskRunStore
from .task_scope_models import SourceGapDecision
from .task_source_courses import source_course
from .tasks import FINAL_TASK_STATES, UNSETTLED_UNIT_STATES, TaskStore


class TaskRecovery:
    def __init__(self, repository):
        self.repository = repository

    def source_gap(self, course_id, task_id, value: SourceGapDecision) -> TaskView:
        imported_executions(self.repository).require_fresh("task", task_id)
        payload = {"task_id": task_id, "source_gap": value.model_dump(mode="json")}
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "task_gap", value.operation_id, payload)
            if prior is not None:
                return TaskView.model_validate(prior)
            task = TaskStore._row(conn, course_id, task_id)
            TaskStore._check_revision(task, value.expected_revision)
            if task["layout_version"] != 2 or task["state"] in FINAL_TASK_STATES:
                raise AppError("TASK_GAP_STATE_INVALID", "此任务当前不能作来源缺口决定")
            units = list(
                conn.execute(
                    select(s.task_units).where(
                        s.task_units.c.task_id == task_id,
                        s.task_units.c.excluded_at_scope_revision.is_(None),
                    )
                ).mappings()
            )
            if (
                task["state"] == "unknown"
                or any(unit["state"] in UNSETTLED_UNIT_STATES for unit in units)
                or any(
                    unit["run_id"]
                    and unit["state"] == "completed"
                    and not TaskRunStore.input_proofs_settled(conn, unit["run_id"])
                    for unit in units
                )
            ):
                raise AppError(
                    "TASK_RECONCILIATION_REQUIRED", "先核对正在执行或结果未知的请求，再处理缺口"
                )
            if value.decision == "accept_partial":
                if any(unit["state"] in {"pending", "read"} for unit in units):
                    raise AppError("TASK_UNITS_PENDING", "仍有未处理生成工作，请先完成或明确排除")
                TaskStore._event(
                    conn,
                    task,
                    "partial_accepted",
                    payload,
                    state="partial",
                    scope_revision=task["scope_revision"] + 1,
                )
            else:
                sources = list(
                    conn.execute(
                        select(s.task_sources).where(
                            s.task_sources.c.task_id == task_id,
                            s.task_sources.c.source_id.in_(value.source_ids),
                        )
                    ).mappings()
                )
                leaves = list(
                    conn.execute(
                        select(s.task_source_leaves).where(
                            s.task_source_leaves.c.task_id == task_id,
                            s.task_source_leaves.c.leaf_id.in_(value.leaf_ids),
                        )
                    ).mappings()
                )
                if len(sources) != len(set(value.source_ids)) or len(leaves) != len(
                    set(value.leaf_ids)
                ):
                    raise AppError("TASK_SOURCE_SCOPE_DENIED", "部分来源或单位不属于此任务", 403)
                if any(
                    row["state"] != "blocked" or row["excluded_at_scope_revision"] is not None
                    for row in sources + leaves
                ):
                    raise AppError("TASK_GAP_DECISION_INVALID", "只对已知来源缺口明确重试或排除")
                leaf_ids = {row["leaf_id"] for row in leaves}
                leaf_ids.update(
                    conn.execute(
                        select(s.task_source_leaves.c.leaf_id).where(
                            s.task_source_leaves.c.source_id.in_(value.source_ids)
                        )
                    ).scalars()
                )
                revision = task["scope_revision"] + 1
                exclude = value.decision == "exclude"
                for source in sources:
                    conn.execute(
                        update(s.task_sources)
                        .where(s.task_sources.c.source_id == source["source_id"])
                        .values(
                            **(
                                {"excluded_at_scope_revision": revision}
                                if exclude
                                else {
                                    "state": "declared"
                                    if source["expected_leaf_count"] is None
                                    else "preparing",
                                    "error_code": None,
                                }
                            ),
                            **(
                                {"frozen": {**(source["frozen"] or {}), "external_epoch": revision}}
                                if not exclude
                                and source["declaration"]["kind"] == "external_research"
                                else {}
                            ),
                            updated_at=now(),
                        )
                    )
                if not exclude:
                    owner_ids = set(value.source_ids) | {leaf["source_id"] for leaf in leaves}
                    for source in conn.execute(
                        select(s.task_sources).where(
                            s.task_sources.c.task_id == task_id,
                            s.task_sources.c.source_id.in_(owner_ids),
                        )
                    ).mappings():
                        if not source["preparation_job_id"]:
                            continue
                        frozen = source["frozen"] or {}
                        preparation = (
                            conn.execute(
                                select(s.preparations).where(
                                    s.preparations.c.job_id == source["preparation_job_id"],
                                    s.preparations.c.course_id == source_course(course_id, source),
                                    s.preparations.c.asset_id == frozen.get("asset_id"),
                                    s.preparations.c.source_version == frozen.get("source_version"),
                                )
                            )
                            .mappings()
                            .one_or_none()
                        )
                        if preparation is None:
                            raise AppError(
                                "TASK_PREPARATION_SCOPE_DENIED", "准备任务与明确来源版本不符", 403
                            )
                        if preparation["state"] in {"failed", "needs_source"}:
                            request = {
                                "operation_id": "task_prep_retry_"
                                + _hash(
                                    {
                                        "task_id": task_id,
                                        "operation_id": value.operation_id,
                                        "job_id": preparation["job_id"],
                                    }
                                )[:40],
                                "expected_revision": preparation["revision"],
                                "state": "pending",
                            }
                            conn.execute(
                                update(s.task_sources)
                                .where(s.task_sources.c.source_id == source["source_id"])
                                .values(
                                    frozen={
                                        **(source["frozen"] or {}),
                                        "preparation_retry": request,
                                    }
                                )
                            )
                conn.execute(
                    update(s.task_source_leaves)
                    .where(
                        s.task_source_leaves.c.leaf_id.in_(leaf_ids),
                        s.task_source_leaves.c.state == "blocked",
                    )
                    .values(
                        **(
                            {"excluded_at_scope_revision": revision}
                            if exclude
                            else {"state": "pending", "error_code": None}
                        ),
                        updated_at=now(),
                    )
                )
                for unit in units:
                    external_gap = (unit["work_spec"] or {}).get("research", {}).get(
                        "external", {}
                    ).get("source_id") in {
                        source["source_id"]
                        for source in sources
                        if source["declaration"]["kind"] == "external_research"
                    }
                    affected = external_gap or bool(
                        leaf_ids & {item["leaf_id"] for item in unit["work_spec"]["inputs"]}
                    )
                    research = (unit["work_spec"] or {}).get("research", {})
                    retire_summary = (
                        task["kind"] == "research" and research.get("phase") == "synthesis"
                    )
                    retire_evidence = task["kind"] == "research" and exclude and affected
                    if retire_summary or (
                        affected and (unit["state"] != "completed" or retire_evidence)
                    ):
                        # Preserve the prior plan/run; a new plan can cover the
                        # remaining/restored input only after this user action.
                        conn.execute(
                            update(s.task_units)
                            .where(
                                s.task_units.c.task_id == task_id,
                                s.task_units.c.unit_id == unit["unit_id"],
                            )
                            .values(excluded_at_scope_revision=revision)
                        )
                TaskStore._event(
                    conn,
                    task,
                    "source_gap_decision",
                    payload,
                    state="queued",
                    scope_revision=revision,
                )
            result = TaskStore._view(conn, course_id, task_id)
            save_operation(conn, course_id, "task_gap", value.operation_id, payload, result)
            return result

    def retry_work(self, course_id, task_id, unit_id, value: TaskAction) -> TaskView:
        imported_executions(self.repository).require_fresh("task", task_id)
        payload = {
            "task_id": task_id,
            "unit_id": unit_id,
            "retry_work": value.model_dump(mode="json"),
        }
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(conn, course_id, "task_control", value.operation_id, payload)
            if prior is not None:
                return TaskView.model_validate(prior)
            task = TaskStore._row(conn, course_id, task_id)
            TaskStore._check_revision(task, value.expected_revision)
            unit = (
                conn.execute(
                    select(s.task_units).where(
                        s.task_units.c.task_id == task_id,
                        s.task_units.c.unit_id == unit_id,
                        s.task_units.c.excluded_at_scope_revision.is_(None),
                    )
                )
                .mappings()
                .first()
            )
            if (
                unit is None
                or task["layout_version"] != 2
                or unit["state"] != "failed"
                or unit["error_code"] == "TASK_TARGET_CONFLICT"
                or task["state"] in FINAL_TASK_STATES
            ):
                raise AppError(
                    "TASK_WORK_RETRY_DENIED", "此工作不能直接重试；来源和编辑冲突须分别处理"
                )
            reprocess = False
            if unit["run_id"]:
                run = (
                    conn.execute(select(s.task_runs).where(s.task_runs.c.run_id == unit["run_id"]))
                    .mappings()
                    .one()
                )
                if run["state"] not in {"failed", "cancelled", "truncated", "completed"}:
                    raise AppError(
                        "TASK_RECONCILIATION_REQUIRED", "结果未知须先核对原执行，不重新发送"
                    )
                reprocess = (
                    run["state"] == "completed"
                    and unit["error_code"] == "TASK_RESULT_LOCAL_RECOVERY_REQUIRED"
                )
                if reprocess and not TaskRunStore.input_proofs_settled(conn, run["run_id"]):
                    raise AppError(
                        "TASK_RECONCILIATION_REQUIRED", "先对账原执行的出站与用量事实，不重新生成"
                    )
            conn.execute(
                update(s.task_units)
                .where(s.task_units.c.task_id == task_id, s.task_units.c.unit_id == unit_id)
                .values(
                    state="read" if reprocess else "pending",
                    run_id=unit["run_id"] if reprocess else None,
                    error_code=None,
                )
            )
            TaskStore._event(
                conn,
                task,
                "local_result_reprocess_requested" if reprocess else "work_retry_requested",
                payload,
                state="queued",
            )
            result = TaskStore._view(conn, course_id, task_id)
            save_operation(conn, course_id, "task_control", value.operation_id, payload, result)
            return result
