"""Task-owned execution facts, sharing the chat transport and usage projection."""

from __future__ import annotations

from sqlalchemy import exists, insert, or_, select, update
from sqlalchemy.engine import Connection

from learning_app.errors import AppError
from learning_app.execution.types import ExecutionBinding, NormalizedEvent
from learning_app.execution.usage import usage_is_settled, usage_projection
from learning_app.storage import Repository
from learning_app.storage.repository import _hash

from . import schema as s
from .common import new_id, now, run_input_hash, run_still_in_scope
from .models import (
    ModelSettings,
    TaskAttemptView,
    TaskRunView,
    TaskUsageView,
    UsageCounters,
)
from .sources import SourceStore
from .tasks import TaskStore

RUN_TERMINAL = frozenset({"completed", "failed", "truncated", "cancelled"})


class TaskRunStore:
    def __init__(self, repository: Repository):
        self.repository = repository

    @staticmethod
    def input_proofs_settled(conn: Connection, run_id: str) -> bool:
        run = TaskRunStore._row(conn, run_id)
        if run["input_contract"] is None:
            return True
        entries = list(
            conn.execute(select(s.run_inputs).where(s.run_inputs.c.run_id == run_id)).mappings()
        )
        return len(entries) == len(run["input_contract"]["inputs"]) and all(
            (entry["proof"] or {}).get("state") in {"actual_outbound", "fixture_dispatch"}
            for entry in entries
        )

    @staticmethod
    def _row(conn: Connection, run_id: str) -> dict:
        row = (
            conn.execute(select(s.task_runs).where(s.task_runs.c.run_id == run_id))
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("TASK_RUN_NOT_FOUND", "任务执行不存在", 404)
        return dict(row)

    @staticmethod
    def _view(conn: Connection, run_id: str) -> TaskRunView:
        row = TaskRunStore._row(conn, run_id)
        attempts = [
            TaskAttemptView.model_validate({key: item[key] for key in TaskAttemptView.model_fields})
            for item in conn.execute(
                select(s.run_attempts)
                .where(
                    s.run_attempts.c.run_id == run_id,
                )
                .order_by(s.run_attempts.c.received_ns)
            ).mappings()
        ]
        usage = [
            TaskUsageView.model_validate({key: item[key] for key in TaskUsageView.model_fields})
            for item in conn.execute(
                select(s.run_usage).where(
                    s.run_usage.c.run_id == run_id,
                )
            ).mappings()
        ]
        covered = bool(attempts) and {item.request_id for item in attempts} == {
            item.request_id for item in usage
        }
        counters = {}
        for key in UsageCounters.model_fields:
            values = [getattr(item.normalized, key) for item in usage]
            counters[key] = (
                sum(values)
                if covered and values and all(type(value) is int for value in values)
                else None
            )
        totals = UsageCounters(**counters)
        return TaskRunView(
            **{
                key: row[key]
                for key in (
                    "run_id",
                    "task_id",
                    "unit_id",
                    "scope_revision",
                    "state",
                    "settings",
                    "body",
                    "reasoning",
                    "cancel_requested",
                    "error_code",
                    "event_watermark",
                    "created_at",
                    "updated_at",
                )
            },
            attempts=attempts,
            usage=usage,
            totals=totals,
            usage_status="known"
            if totals.input_tokens is not None and totals.output_tokens is not None
            else "unknown",
            actual_outbound_attempts=sum(item.actual_outbound for item in attempts),
        )

    def get(self, course_id: str, task_id: str, run_id: str) -> TaskRunView:
        with self.repository.engine.connect() as conn:
            TaskStore._row(conn, course_id, task_id)
            row = self._row(conn, run_id)
            if row["task_id"] != task_id:
                raise AppError("TASK_RUN_DENIED", "执行不属于指定任务", 403)
            return self._view(conn, run_id)

    def internal(self, run_id: str) -> dict:
        with self.repository.engine.connect() as conn:
            row = self._row(conn, run_id)
            allowed = conn.execute(
                select(s.native_scopes.c.allowed_tools).where(s.native_scopes.c.run_id == run_id)
            ).scalar_one_or_none()
            row["native_allowed_tools"] = allowed or []
            row["request_image_proofs"] = [
                entry["proof"]["native_image"]
                for entry in conn.execute(
                    select(s.run_inputs)
                    .where(
                        s.run_inputs.c.run_id == run_id, s.run_inputs.c.representation == "image"
                    )
                    .order_by(s.run_inputs.c.ordinal)
                ).mappings()
                if entry["proof"] and entry["proof"].get("native_image")
            ]
            return row

    def save_document_images(self, run_id: str, proofs: list[dict]) -> None:
        with self.repository._lock, self.repository.engine.begin() as conn:
            run = self._row(conn, run_id)
            binding = ExecutionBinding.from_dict(run["binding"]) if run["binding"] else None
            if run["state"] != "prepared" or binding is None or run["cancel_requested"]:
                raise AppError("TASK_IMAGE_PREPARATION_STALE", "当前执行不再接受图像准备回执")
            task = TaskStore._row(
                conn,
                conn.execute(
                    select(s.tasks.c.course_id).where(s.tasks.c.task_id == run["task_id"])
                ).scalar_one(),
                run["task_id"],
            )
            entries = list(
                conn.execute(
                    select(s.run_inputs)
                    .where(
                        s.run_inputs.c.run_id == run_id, s.run_inputs.c.representation == "image"
                    )
                    .order_by(s.run_inputs.c.ordinal)
                ).mappings()
            )
            if len(entries) != len(proofs):
                raise AppError("TASK_IMAGE_PREPARATION_SCOPE", "图像派生回执数量与冻结输入不符")
            for entry, proof in zip(entries, proofs, strict=True):
                snapshot = SourceStore.in_transaction(conn, task["course_id"], entry["snapshot_id"])
                from .source_images import image_grant

                expected = {
                    **image_grant(snapshot),
                    "run_id": run_id,
                    "session_id": binding.session_id,
                    "request_id": binding.request_id,
                    "epoch": binding.native_lease_epoch or binding.epoch,
                    "model": run["settings"]["model"],
                    "runtime_sha256": "f9ed01f241df37c50c702d81b6c226d576210b8f3947699735ca90285ea13365",
                }
                if any(proof.get(key) != value for key, value in expected.items()) or proof.get(
                    "provider"
                ) not in {"deepseek-account", "deepseek-official"}:
                    raise AppError(
                        "TASK_IMAGE_PREPARATION_SCOPE", "图像派生回执不属于原始授权文档页及执行"
                    )
                hashes = [proof.get("normalized_sha256"), proof.get("encoded_sha256")]
                if any(
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value)
                    for value in hashes
                ):
                    raise AppError("TASK_IMAGE_PREPARATION_INVALID", "图像派生摘要无效")
                if (
                    proof.get("normalized_ref", {}).get("attachmentId")
                    != "sha256:" + proof["normalized_sha256"]
                    or proof.get("depth") != "uchar"
                    or proof.get("space") != "srgb"
                    or proof.get("encoded_media_type")
                    not in {"image/png", "image/jpeg", "image/webp"}
                    or any(
                        type(proof.get(key)) is not int or proof[key] <= 0
                        for key in ("encoded_bytes", "encoded_width", "encoded_height")
                    )
                ):
                    raise AppError("TASK_IMAGE_PREPARATION_INVALID", "原生图像编码身份或尺寸无效")
                previous = (entry["proof"] or {}).get("native_image")
                if previous is not None and previous != proof:
                    raise AppError(
                        "TASK_IMAGE_PREPARATION_CONFLICT", "同一冻结图像的原生派生回执不能替换"
                    )
                conn.execute(
                    update(s.run_inputs)
                    .where(
                        s.run_inputs.c.run_id == run_id, s.run_inputs.c.ordinal == entry["ordinal"]
                    )
                    .values(proof={**(entry["proof"] or {}), "native_image": proof})
                )

    def claim(self, course_id: str, task_id: str, unit_id: str, prompt: str) -> TaskRunView:
        with self.repository._lock, self.repository.engine.begin() as conn:
            task = TaskStore._row(conn, course_id, task_id)
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
            if unit is None:
                raise AppError("TASK_UNIT_NOT_FOUND", "任务单元不存在或已排除", 404)
            if (
                task["kind"] == "book"
                and not task["outline_confirmed"]
                and (unit["work_spec"] or {}).get("phase") != "outline"
            ):
                raise AppError("TASK_OUTLINE_REQUIRED", "交互书正文生成须先确认一次目录")
            input_contract = None
            if task["layout_version"] == 2:
                book_outline = (
                    task["kind"] == "book" and unit["work_spec"].get("phase") == "outline"
                )
                book_empty = False
                research_synthesis = False
                if task["kind"] == "book":
                    from .books import BookStore

                    if book_outline:
                        BookStore.validate_planning_work(conn, task, unit["work_spec"])
                    elif unit["work_spec"].get("book"):
                        book_empty = BookStore.validate_page_work(conn, task, unit["work_spec"])
                if task["kind"] == "research" and not unit["work_spec"].get("edit_range"):
                    from .research import ResearchStore
                    from .research_external import ResearchExternal
                    from .research_synthesis import ResearchSynthesis

                    context = ResearchStore.validate_work(task, unit["work_spec"])
                    if context["phase"] == "synthesis":
                        ResearchSynthesis.validate(
                            conn,
                            task,
                            context,
                            before_ordinal=unit["ordinal"],
                            require_current_scope=True,
                        )
                        research_synthesis = True
                    else:
                        external = ResearchExternal.scope(conn, task)
                        if context.get("external") != external:
                            raise AppError(
                                "RESEARCH_EXTERNAL_SCOPE_CHANGED", "研究工作与原外部范围不符", 403
                            )
                        research_synthesis = external is not None
                if (
                    not unit["work_spec"]["inputs"]
                    and (task["sources_only"] or task["coverage_mode"] == "complete")
                    and not (book_outline or book_empty or research_synthesis)
                ):
                    raise AppError(
                        "TASK_SOURCES_REQUIRED", "此任务没有可生成的授权材料，未创建空材料执行"
                    )
                target = dict(
                    conn.execute(select(s.task_targets).where(s.task_targets.c.task_id == task_id))
                    .mappings()
                    .one()
                )
                input_contract = {
                    "version": 2,
                    "work_spec_hash": _hash(unit["work_spec"]),
                    "inputs": unit["work_spec"]["inputs"],
                    "target": {
                        key: target[key]
                        for key in (
                            "reserved_artifact_id",
                            "mode",
                            "artifact_kind",
                            "expected_revision_id",
                            "structure_revision",
                            "write_scope",
                        )
                    },
                    "image_hashes": [],
                }
                from .range_edits import RangeEdits

                edit_range = RangeEdits.validate_work(conn, task, unit["work_spec"], target)
                if edit_range:
                    input_contract["edit_range"] = edit_range
                if task["kind"] == "book":
                    input_contract["phase"] = unit["work_spec"]["phase"]
                    if book_outline:
                        input_contract["book_context"] = unit["work_spec"]["book_context"]
                    elif unit["work_spec"].get("book"):
                        input_contract["book"] = unit["work_spec"]["book"]
                if task["kind"] == "research" and not edit_range:
                    from .research import ResearchStore

                    input_contract["research"] = ResearchStore.validate_work(
                        task, unit["work_spec"]
                    )
                for item in unit["work_spec"]["inputs"]:
                    snapshot = SourceStore.in_transaction(conn, course_id, item["snapshot_id"])
                    if snapshot.reference.kind in {
                        "document_image",
                        "document_region_image",
                        "submitted_image",
                    }:
                        input_contract["image_hashes"].append(snapshot.reference.image_sha256)
            input_hash = run_input_hash(
                task_id,
                unit_id,
                task["scope_revision"],
                unit["source_snapshot_id"],
                task["settings"],
                prompt,
                input_contract,
            )
            if unit["run_id"]:
                existing = self._row(conn, unit["run_id"])
                expected = run_input_hash(
                    task_id,
                    unit_id,
                    existing["scope_revision"],
                    unit["source_snapshot_id"],
                    task["settings"],
                    prompt,
                    existing["input_contract"],
                )
                if existing["input_hash"] != expected or (
                    existing["input_contract"]
                    and existing["input_contract"]["work_spec_hash"] != _hash(unit["work_spec"])
                ):
                    raise AppError("TASK_RUN_INPUT_CONFLICT", "该执行的冻结输入不能改变")
                if existing["state"] == "prepared" and task["state"] == "queued":
                    TaskStore._event(
                        conn,
                        task,
                        "prepared_run_resumed",
                        {"run_id": existing["run_id"]},
                        state="running",
                    )
                return self._view(conn, existing["run_id"])
            if task["state"] not in {"queued", "running"} or unit["state"] != "pending":
                raise AppError("TASK_RUN_CLAIM_DENIED", "当前不能创建执行，未知请求须先对账")
            identity, timestamp = new_id("taskrun"), now()
            conn.execute(
                insert(s.task_runs).values(
                    run_id=identity,
                    task_id=task_id,
                    unit_id=unit_id,
                    scope_revision=task["scope_revision"],
                    state="prepared",
                    settings=task["settings"],
                    prompt=prompt,
                    input_hash=input_hash,
                    input_contract=input_contract,
                    body="",
                    reasoning="",
                    cancel_requested=False,
                    event_watermark=0,
                    native_watermark=-1,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            if input_contract is not None:
                for ordinal, item in enumerate(input_contract["inputs"]):
                    snapshot = SourceStore.in_transaction(conn, course_id, item["snapshot_id"])
                    if (
                        snapshot.content_hash != item["snapshot_hash"]
                        or snapshot.content_role != item["content_role"]
                    ):
                        raise AppError("TASK_RUN_INPUT_CONFLICT", "冻结执行与实际来源快照不符")
                    leaf = (
                        conn.execute(
                            select(s.task_source_leaves).where(
                                s.task_source_leaves.c.task_id == task_id,
                                s.task_source_leaves.c.leaf_id == item["leaf_id"],
                                s.task_source_leaves.c.excluded_at_scope_revision.is_(None),
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if (
                        leaf is None
                        or leaf["state"] != "ready"
                        or item["representation_snapshot_id"]
                        not in {rep["snapshot_id"] for rep in leaf["representations"]}
                    ):
                        raise AppError(
                            "TASK_RUN_SOURCE_SCOPE_DENIED", "执行输入不属于本任务已冻结范围", 403
                        )
                    conn.execute(
                        insert(s.run_inputs).values(
                            run_id=identity,
                            ordinal=ordinal,
                            leaf_id=item["leaf_id"],
                            snapshot_id=snapshot.snapshot_id,
                            snapshot_hash=snapshot.content_hash,
                            content_role=snapshot.content_role,
                            representation=item["representation"],
                            char_start=item["char_start"],
                            char_end=item["char_end"],
                            proof={
                                "representation_snapshot_id": item["representation_snapshot_id"],
                                "state": "prepared",
                            },
                        )
                    )
            conn.execute(
                update(s.task_units)
                .where(
                    s.task_units.c.task_id == task_id,
                    s.task_units.c.unit_id == unit_id,
                )
                .values(run_id=identity, state="read")
            )
            TaskStore._event(
                conn, task, "run_claimed", {"unit_id": unit_id, "run_id": identity}, state="running"
            )
            self.repository._hook("learning_run_claim_before_commit", run_id=identity)
            return self._view(conn, identity)

    def persist_binding(
        self, run_id: str, binding: ExecutionBinding, host_instance_id: str
    ) -> None:
        if binding.generation_id != run_id:
            raise AppError("TASK_RUN_BINDING_DENIED", "执行绑定身份不符", 403)
        with self.repository._lock, self.repository.engine.begin() as conn:
            row = self._row(conn, run_id)
            if row["binding"]:
                original = ExecutionBinding.from_dict(row["binding"])
                if any(
                    getattr(original, key) != getattr(binding, key)
                    for key in (
                        "session_id",
                        "request_id",
                        "epoch",
                        "generation_id",
                        "conversation_id",
                        "native_turn",
                        "native_floor",
                    )
                ):
                    raise AppError("TASK_RUN_BINDING_CONFLICT", "原执行绑定不可替换")
            elif row["state"] != "prepared":
                raise AppError("TASK_RUN_BINDING_UNKNOWN", "缺少原绑定的未知执行不能重新准备")
            conn.execute(
                update(s.task_runs)
                .where(s.task_runs.c.run_id == run_id)
                .values(
                    binding=binding.to_dict(),
                    host_instance_id=host_instance_id,
                    updated_at=now(),
                )
            )

    def mark_dispatching(self, run_id: str) -> bool:
        with self.repository._lock, self.repository.engine.begin() as conn:
            row = self._row(conn, run_id)
            task = (
                conn.execute(select(s.tasks).where(s.tasks.c.task_id == row["task_id"]))
                .mappings()
                .one()
            )
            if row["state"] != "prepared":
                return False
            unit = (
                conn.execute(
                    select(s.task_units).where(
                        s.task_units.c.task_id == row["task_id"],
                        s.task_units.c.unit_id == row["unit_id"],
                    )
                )
                .mappings()
                .one()
            )
            if (
                task["state"] != "running"
                or not run_still_in_scope(row, task, unit)
                or not row["binding"]
                or row["cancel_requested"]
            ):
                raise AppError("TASK_RUN_DISPATCH_DENIED", "任务已暂停、范围改变或尚无执行绑定")
            conn.execute(
                update(s.task_runs)
                .where(s.task_runs.c.run_id == run_id)
                .values(state="dispatching", updated_at=now())
            )
            conn.execute(
                update(s.task_units)
                .where(
                    s.task_units.c.task_id == row["task_id"],
                    s.task_units.c.unit_id == row["unit_id"],
                    s.task_units.c.run_id == run_id,
                )
                .values(state="dispatching")
            )
            self.repository._hook("learning_run_dispatch_before_commit", run_id=run_id)
            return True

    def record_attempts(
        self, run_id: str, attempts: list[dict], *, source: str = "provider"
    ) -> None:
        if source not in {"fixture", "provider"}:
            raise AppError("TASK_ATTEMPT_SOURCE_INVALID", "出站事实来源无效")
        with self.repository._lock, self.repository.engine.begin() as conn:
            row = self._row(conn, run_id)
            settings = ModelSettings.model_validate(row["settings"])
            for item in attempts:
                attempt = TaskAttemptView.model_validate(
                    {key: item[key] for key in TaskAttemptView.model_fields}
                )
                if (
                    item.get("generation_id") != run_id
                    or attempt.model != settings.model
                    or attempt.thinking != settings.thinking
                ):
                    raise AppError("TASK_ATTEMPT_OWNER_CONFLICT", "出站事实的执行或模型归属不符")
                existing = (
                    conn.execute(
                        select(s.run_attempts).where(
                            s.run_attempts.c.attempt_id == attempt.attempt_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                values = attempt.model_dump(mode="json")
                if existing:
                    if (
                        existing["run_id"] != run_id
                        or existing["request_id"] != attempt.request_id
                        or existing["request_sha256"] != attempt.request_sha256
                    ):
                        raise AppError("TASK_ATTEMPT_CONFLICT", "同一出站身份事实冲突")
                    conn.execute(
                        update(s.run_attempts)
                        .where(s.run_attempts.c.attempt_id == attempt.attempt_id)
                        .values(**values)
                    )
                else:
                    conn.execute(insert(s.run_attempts).values(run_id=run_id, **values))
            outbound = list(
                conn.execute(
                    select(s.run_attempts.c.attempt_id).where(
                        s.run_attempts.c.run_id == run_id,
                        *(
                            []
                            if source == "fixture"
                            else [s.run_attempts.c.actual_outbound.is_(True)]
                        ),
                    )
                ).scalars()
            )
            if outbound:
                for entry in conn.execute(
                    select(s.run_inputs).where(s.run_inputs.c.run_id == run_id)
                ).mappings():
                    conn.execute(
                        update(s.run_inputs)
                        .where(
                            s.run_inputs.c.run_id == run_id,
                            s.run_inputs.c.ordinal == entry["ordinal"],
                        )
                        .values(
                            proof={
                                **(entry["proof"] or {}),
                                "state": "fixture_dispatch"
                                if source == "fixture"
                                else "actual_outbound",
                                "source": source,
                                "attempt_ids": outbound,
                            }
                        )
                    )

    @staticmethod
    def _save_usage(conn: Connection, run_id: str, event: NormalizedEvent, source: str) -> None:
        settlement_id = event.data.get("settlement_id")
        request_id = event.data.get("request_id")
        presence, normalized = usage_projection(event.raw_usage, event.usage_presence)
        usage = TaskUsageView(
            settlement_id=settlement_id,
            request_id=request_id,
            source=source,
            raw_usage=event.raw_usage,
            field_presence=presence,
            normalized=UsageCounters(**normalized),
        )
        existing = (
            conn.execute(
                select(s.run_usage).where(
                    s.run_usage.c.run_id == run_id,
                    s.run_usage.c.settlement_id == settlement_id,
                )
            )
            .mappings()
            .first()
        )
        if existing:
            if existing["request_id"] != request_id or existing["source"] != source:
                raise AppError("TASK_USAGE_CONFLICT", "用量结算身份冲突")
            if existing["raw_usage"] == event.raw_usage and existing["field_presence"] == presence:
                return
            if usage_is_settled(existing["raw_usage"]):
                if usage_is_settled(event.raw_usage):
                    raise AppError("TASK_USAGE_CONFLICT", "已结算用量出现冲突")
                return
            conn.execute(
                update(s.run_usage)
                .where(
                    s.run_usage.c.run_id == run_id,
                    s.run_usage.c.settlement_id == settlement_id,
                )
                .values(**usage.model_dump(mode="json"))
            )
        else:
            conn.execute(insert(s.run_usage).values(run_id=run_id, **usage.model_dump(mode="json")))

    def ingest(self, run_id: str, event: NormalizedEvent, *, source: str) -> bool:
        if event.generation_id != run_id:
            raise AppError("TASK_RUN_EVENT_DENIED", "执行事件归属不符", 403)
        payload = event.to_dict()
        with self.repository._lock, self.repository.engine.begin() as conn:
            row = self._row(conn, run_id)
            binding = ExecutionBinding.from_dict(row["binding"]) if row["binding"] else None
            if binding is None or event.epoch != binding.epoch:
                raise AppError("TASK_RUN_EPOCH_STALE", "事件绑定或代次已失效", 403)
            prior = (
                conn.execute(
                    select(s.run_events).where(
                        s.run_events.c.run_id == run_id,
                        s.run_events.c.event_id == event.event_id,
                    )
                )
                .mappings()
                .first()
            )
            if prior:
                old = {key: value for key, value in prior["payload"].items() if key != "seq"}
                new = {key: value for key, value in payload.items() if key != "seq"}
                if _hash(old) != _hash(new):
                    raise AppError("TASK_RUN_EVENT_CONFLICT", "同一事件身份内容冲突")
                return False
            if event.seq <= row["event_watermark"]:
                raise AppError("TASK_RUN_EVENT_STALE", "事件水位已过期", 403)
            changes = {"event_watermark": event.seq, "updated_at": now()}
            closed = row["state"] in RUN_TERMINAL
            if event.kind == "usage":
                self._save_usage(conn, run_id, event, source)
            elif event.kind == "snapshot":
                if event.native_seq is None or event.native_seq < row["native_watermark"]:
                    raise AppError("TASK_RUN_SNAPSHOT_STALE", "执行快照水位落后", 403)
                changes.update(
                    body=event.text,
                    reasoning=event.data.get("reasoning", ""),
                    native_watermark=event.native_seq,
                )
            elif event.kind in {"text_delta", "reasoning_delta"}:
                if closed:
                    raise AppError("TASK_RUN_ALREADY_FINAL", "已结束执行拒绝迟到增量", 403)
                key = "body" if event.kind == "text_delta" else "reasoning"
                changes.update({key: row[key] + event.text, "state": "running"})
            elif event.kind == "terminal":
                state = event.status if event.status in RUN_TERMINAL else "unknown"
                if closed and state != row["state"]:
                    raise AppError("TASK_RUN_TERMINAL_CONFLICT", "原执行终态冲突")
                changes["state"] = state
                if event.native_seq is not None:
                    changes["native_watermark"] = max(row["native_watermark"], event.native_seq)
            elif event.kind in {"accepted", "activity"}:
                if not closed:
                    changes["state"] = "accepted" if event.kind == "accepted" else "running"
            elif event.kind == "unknown" and not closed:
                changes.update(state="unknown", error_code="TASK_EXECUTION_UNKNOWN")
            conn.execute(
                insert(s.run_events).values(
                    run_id=run_id,
                    event_id=event.event_id,
                    seq=event.seq,
                    payload=payload,
                    created_at=now(),
                )
            )
            conn.execute(
                update(s.task_runs).where(s.task_runs.c.run_id == run_id).values(**changes)
            )
            self.repository._hook(
                "learning_run_event_before_commit", run_id=run_id, kind=event.kind
            )
            return True

    def mark_unknown(self, run_id: str, code: str) -> None:
        with self.repository._lock, self.repository.engine.begin() as conn:
            row = self._row(conn, run_id)
            if row["state"] not in RUN_TERMINAL:
                conn.execute(
                    update(s.task_runs)
                    .where(s.task_runs.c.run_id == run_id)
                    .values(
                        state="unknown",
                        error_code=code,
                        updated_at=now(),
                    )
                )

    def fail_prepared(self, run_id: str, code: str) -> None:
        with self.repository._lock, self.repository.engine.begin() as conn:
            row = self._row(conn, run_id)
            if row["state"] != "prepared":
                raise AppError("TASK_RUN_NOT_PREPARED", "已派发执行不能当作未执行失败")
            conn.execute(
                update(s.task_runs)
                .where(s.task_runs.c.run_id == run_id)
                .values(
                    state="failed",
                    error_code=code,
                    updated_at=now(),
                )
            )
            self._not_dispatched(conn, row, code)

    @staticmethod
    def _not_dispatched(conn, run, code):
        task = dict(
            conn.execute(select(s.tasks).where(s.tasks.c.task_id == run["task_id"]))
            .mappings()
            .one()
        )
        TaskStore._event(conn, task, "run_not_dispatched", {"run_id": run["run_id"], "code": code})

    def request_cancel(self, run_id: str) -> dict:
        with self.repository._lock, self.repository.engine.begin() as conn:
            row = self._row(conn, run_id)
            if row["state"] not in RUN_TERMINAL:
                changes = {"cancel_requested": True, "updated_at": now()}
                if row["state"] == "prepared":
                    changes["state"] = "cancelled"
                    changes["error_code"] = "TASK_CANCELLED_BEFORE_DISPATCH"
                    self._not_dispatched(conn, row, changes["error_code"])
                conn.execute(
                    update(s.task_runs).where(s.task_runs.c.run_id == run_id).values(**changes)
                )
            return self._row(conn, run_id)

    def has_active_runs(self) -> bool:
        with self.repository.engine.connect() as conn:
            return bool(
                conn.execute(
                    select(s.task_runs.c.run_id)
                    .where(
                        s.task_runs.c.state.not_in(list(RUN_TERMINAL)),
                    )
                    .limit(1)
                ).first()
            )

    def active_tasks(self) -> list[dict]:
        with self.repository.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(
                        s.tasks.c.task_id,
                        s.tasks.c.course_id,
                        s.tasks.c.state,
                    ).where(
                        or_(
                            s.tasks.c.state.in_(["queued", "running", "unknown"]),
                            exists(
                                select(s.task_runs.c.run_id).where(
                                    s.task_runs.c.task_id == s.tasks.c.task_id,
                                    s.task_runs.c.state.not_in(list(RUN_TERMINAL)),
                                )
                            ),
                        )
                    )
                ).mappings()
            ]

    def recoverable(self) -> list[dict]:
        with self.repository.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    select(s.task_runs).where(
                        s.task_runs.c.state.not_in(list(RUN_TERMINAL)),
                    )
                ).mappings()
            ]
