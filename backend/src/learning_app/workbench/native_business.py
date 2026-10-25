"""Repository authority behind the private native tools; no native SDK objects."""

from __future__ import annotations

import re
from dataclasses import asdict

from pydantic import ValidationError
from sqlalchemy import func, insert, select, update

from learning_app.drafts import message_selection_ids
from learning_app.errors import AppError
from learning_app.execution.native_tools import KnownToolFailure, NativeCall, ToolRejected
from learning_app.execution.types import ExecutionBinding
from learning_app.imported_executions import imported_executions
from learning_app.storage import schema as business
from learning_app.storage.repository import _hash
from learning_app.workspaces import explicit_subtitles, read_workspaces

from . import corpus_schema as corpus_s
from . import schema as s
from .artifacts import ArtifactStore
from .common import new_id, now, run_still_in_scope
from .corpus import CourseCorpus, concept_argument
from .models import ArtifactBody
from .native_document_images import TOOL as DOCUMENT_IMAGE_TOOL
from .native_document_images import NativeDocumentImages
from .nearby_subtitles import NearbySubtitles, select_anchor
from .on_demand_sources import search as search_documents
from .question_context import freeze_subtitle_anchors
from .requested_learning import TOOL as ADMISSION_TOOL
from .requested_learning import RequestedLearning
from .research_external import EXTERNAL_TOOLS, ResearchExternal
from .source_grants import course_scope, declare_snapshot
from .sources import SourceStore
from .task_materials import material

MAX_STAGE_BODY_BYTES = 20 * 1024 * 1024
_ACTIVE_RUNS = frozenset({"dispatching", "accepted", "running"})
_FINISHED_RUNS = frozenset({"completed", "failed", "cancelled", "truncated"})
_BINDING_KEYS = (
    "conversation_id",
    "submission_id",
    "generation_id",
    "epoch",
    "session_id",
    "request_id",
    "native_floor",
    "native_turn",
)


def _binding_identity(binding: ExecutionBinding | dict) -> dict:
    value = binding.to_dict() if isinstance(binding, ExecutionBinding) else binding
    return {key: value.get(key) for key in _BINDING_KEYS}


class NativeBusiness:
    """Freeze authority once, then recheck live business state on every call.

    A native epoch is private ownership of one run in a reused session. It is
    deliberately independent of the chat binding epoch used by event storage.
    Result stages are durable drafts; only the completed-run publisher creates
    or revises the reserved learning artifact.
    """

    def __init__(self, repository):
        self.repository = repository
        self.imported = imported_executions(repository)
        self.research_external = ResearchExternal(repository)
        self.task_admitted = None
        self.document_images = NativeDocumentImages(repository)

    @staticmethod
    def _scope(conn, run_id):
        row = (
            conn.execute(select(s.native_scopes).where(s.native_scopes.c.run_id == run_id))
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("NATIVE_SCOPE_NOT_FOUND", "原执行权限记录不存在", 404)
        return dict(row)

    @staticmethod
    def _task(conn, run_id):
        run = (
            conn.execute(select(s.task_runs).where(s.task_runs.c.run_id == run_id))
            .mappings()
            .first()
        )
        if run is None:
            raise AppError("TASK_RUN_NOT_FOUND", "任务执行不存在", 404)
        task = (
            conn.execute(select(s.tasks).where(s.tasks.c.task_id == run["task_id"]))
            .mappings()
            .one()
        )
        unit = (
            conn.execute(
                select(s.task_units).where(
                    s.task_units.c.task_id == run["task_id"],
                    s.task_units.c.unit_id == run["unit_id"],
                )
            )
            .mappings()
            .one()
        )
        return dict(run), dict(task), dict(unit)

    @staticmethod
    def _chat(conn, run_id):
        row = (
            conn.execute(
                select(business.generations).where(business.generations.c.generation_id == run_id)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("GENERATION_NOT_FOUND", "对话执行不存在", 404)
        submission = (
            conn.execute(
                select(business.submissions).where(
                    business.submissions.c.submission_id == row["submission_id"]
                )
            )
            .mappings()
            .one()
        )
        chat = (
            conn.execute(
                select(business.chats).where(business.chats.c.chat_id == submission["chat_id"])
            )
            .mappings()
            .one()
        )
        context = (
            conn.execute(
                select(business.context_snapshots).where(
                    business.context_snapshots.c.snapshot_id == row["snapshot_id"]
                )
            )
            .mappings()
            .one()
        )
        return (
            dict(row),
            dict(chat),
            dict(submission),
            {**context["payload"], "context_hash": context["context_hash"]},
        )

    def ensure_task_scope(self, run_id: str, binding: ExecutionBinding) -> dict:
        self.imported.require_fresh("run", run_id)
        self.imported.require_fresh("session", binding.session_id)
        with self.repository._lock, self.repository.engine.begin() as conn:
            run, task, unit = self._task(conn, run_id)
            self.imported.require_fresh("task", task["task_id"])
            if not run_still_in_scope(run, task, unit) or not run["binding"]:
                raise AppError("NATIVE_TASK_SCOPE_STALE", "原任务输入或执行绑定已失效", 409)
            if _binding_identity(run["binding"]) != _binding_identity(binding):
                raise AppError("NATIVE_BINDING_MISMATCH", "工具权限与实际任务绑定不符", 403)
            contract = run["input_contract"]
            sources = (
                [item["snapshot_id"] for item in contract["inputs"]]
                if contract
                else ([unit["source_snapshot_id"]] if unit["source_snapshot_id"] else [])
            )
            purpose = (
                "translation" if task["kind"] in {"translation", "aligned_translation"} else "task"
            )
            planning = task["kind"] == "book" and (contract or {}).get("phase") == "outline"
            tools = [] if purpose == "translation" or planning else ["skill", "read_source_handle"]
            if (
                contract
                and "structure_revision" in contract["target"]
                and purpose == "task"
                and not planning
            ):
                tools.append("stage_result")
            research = (contract or {}).get("research") if task["kind"] == "research" else None
            if research and research.get("external"):
                if ResearchExternal.scope(conn, task) != research["external"]:
                    raise AppError("RESEARCH_EXTERNAL_SCOPE_CHANGED", "原外部研究范围已变化", 403)
                tools.extend(
                    [
                        "read_research_page",
                        *([] if research["external"]["urls"] else ["search_research_web"]),
                    ]
                )
            scope = self._ensure(
                conn,
                binding,
                task["course_id"],
                "task",
                purpose,
                run["settings"],
                tools,
                sources,
                {
                    "task_id": task["task_id"],
                    "unit_id": unit["unit_id"],
                    "input_hash": run["input_hash"],
                    "target": contract["target"] if contract else None,
                    **({"research": research} if research else {}),
                },
                task["instruction"],
            )
            value = {**run["binding"], "native_lease_epoch": scope["epoch"]}
            conn.execute(
                update(s.task_runs).where(s.task_runs.c.run_id == run_id).values(binding=value)
            )
            binding.native_lease_epoch = scope["epoch"]
            return scope

    def ensure_chat_scope(self, run_id: str, binding: ExecutionBinding) -> dict:
        self.imported.require_fresh("generation", run_id)
        self.imported.require_fresh("run", run_id)
        self.imported.require_fresh("session", binding.session_id)
        with self.repository._lock, self.repository.engine.begin() as conn:
            run, chat, submission, context = self._chat(conn, run_id)
            if _binding_identity(run["adapter_metadata"]) != _binding_identity(binding):
                raise AppError("NATIVE_BINDING_MISMATCH", "工具权限与实际对话绑定不符", 403)
            current = next(
                (
                    m
                    for m in context["messages"]
                    if m["message_id"] == submission["user_message_id"]
                ),
                None,
            )
            if current is None:
                raise AppError("NATIVE_CURRENT_INPUT_MISSING", "冻结对话中缺少本次输入", 409)
            # Only selections actually included in this immutable submission,
            # including its legitimate submitted history. No current UI lookup.
            sources = list(
                dict.fromkeys(
                    identity
                    for message in context["messages"]
                    for identity in message_selection_ids(message.get("conditions", []))
                )
            )
            settings = {
                key: run[key]
                for key in ("model", "thinking", "max_output_tokens", "timeout_seconds")
            }
            previous = conn.execute(
                select(s.native_scopes.c.grants).where(s.native_scopes.c.run_id == run_id)
            ).scalar_one_or_none()
            requested = (
                previous.get("learning_request")
                if previous is not None
                else RequestedLearning.freeze(conn, self.repository, run, chat, current, context)
            )
            image_enabled = (
                "document_image_history" in previous
                if previous is not None
                else self.document_images.prepare is not None
            )
            image_history = (
                previous.get("document_image_history", [])
                if previous is not None
                else self.document_images.freeze_history(conn, run, chat, context)
                if image_enabled
                else []
            )
            query_courses = (
                previous.get("corpus_course_ids")
                if previous is not None
                else list(current["read_course_ids"])
                if "read_course_ids" in current
                else read_workspaces(conn, chat)
            )
            if requested:
                sources = list(dict.fromkeys([*sources, *requested["source_snapshot_ids"]]))
            scope = self._ensure(
                conn,
                binding,
                chat["course_id"],
                "chat",
                "qa",
                settings,
                [
                    "skill",
                    "read_source_handle",
                    *(["search_course_concept"] if query_courses else []),
                    *([DOCUMENT_IMAGE_TOOL] if image_enabled else []),
                    *([ADMISSION_TOOL] if requested else []),
                    *(
                        ["read_nearby_subtitles"]
                        if explicit_subtitles(current["text"]) and freeze_subtitle_anchors(conn, chat["course_id"], context, current)
                        else []
                    ),
                ],
                sources,
                {
                    "task_id": None,
                    "context_snapshot_id": run["snapshot_id"],
                    "context_hash": context.get("context_hash"),
                    **({"corpus_course_ids": query_courses} if query_courses is not None else {}),
                    **({"document_image_history": image_history} if image_enabled else {}),
                    **({"learning_request": requested} if requested else {}),
                    "subtitle_anchors": freeze_subtitle_anchors(
                        conn, chat["course_id"], context, current
                    ),
                },
                current["text"],
            )
            value = {**run["adapter_metadata"], "native_lease_epoch": scope["epoch"]}
            conn.execute(
                update(business.generations)
                .where(business.generations.c.generation_id == run_id)
                .values(adapter_metadata=value)
            )
            conn.execute(
                update(business.execution_bindings)
                .where(
                    business.execution_bindings.c.binding_id == run["binding_id"],
                    business.execution_bindings.c.active_generation_id == run_id,
                )
                .values(adapter_metadata=value)
            )
            binding.native_lease_epoch = scope["epoch"]
            return scope

    def _ensure(
        self,
        conn,
        binding,
        course_id,
        owner_kind,
        purpose,
        settings,
        tools,
        sources,
        grants,
        raw_user_text,
        *,
        recovery_only=False,
    ):
        existing = (
            conn.execute(
                select(s.native_scopes).where(s.native_scopes.c.run_id == binding.generation_id)
            )
            .mappings()
            .first()
        )
        authority = {"source_snapshot_ids": list(dict.fromkeys(sources)), **grants}
        identity = _binding_identity(binding)
        if existing:
            if any(
                existing[key] != value
                for key, value in {
                    "course_id": course_id,
                    "owner_kind": owner_kind,
                    "purpose": purpose,
                    "business_binding": identity,
                    "settings": settings,
                    "allowed_tools": tools,
                    "grants": authority,
                    "raw_user_text": raw_user_text,
                }.items()
            ):
                raise AppError("NATIVE_SCOPE_IMMUTABLE", "原执行的权限范围不能替换", 409)
            return self._public(existing)
        if conn.execute(
            select(s.native_scopes.c.run_id).where(
                s.native_scopes.c.session_id == binding.session_id,
                s.native_scopes.c.state != "released",
            )
        ).first():
            raise AppError("NATIVE_PREVIOUS_NOT_RELEASED", "原生对话上次执行尚未完成对账", 409)
        if owner_kind == "task":
            run, task, _ = self._task(conn, binding.generation_id)
            if not recovery_only and (
                run["state"] != "prepared" or task["state"] not in {"queued", "running"}
            ):
                raise AppError(
                    "NATIVE_SCOPE_NEW_DISPATCH_DENIED", "原任务不能创建新的工具租约", 409
                )
        else:
            run, _, _, _ = self._chat(conn, binding.generation_id)
            if not recovery_only and run["state"] != "dispatching":
                raise AppError(
                    "NATIVE_SCOPE_NEW_DISPATCH_DENIED", "原对话不能创建新的工具租约", 409
                )
        epoch = (
            conn.execute(
                select(func.max(s.native_scopes.c.native_epoch)).where(
                    s.native_scopes.c.session_id == binding.session_id
                )
            ).scalar_one()
            or 0
        ) + 1
        row = {
            "run_id": binding.generation_id,
            "course_id": course_id,
            "owner_kind": owner_kind,
            "purpose": purpose,
            "session_id": binding.session_id,
            "request_id": binding.request_id,
            "native_epoch": epoch,
            "business_binding": identity,
            "settings": settings,
            "allowed_tools": tools,
            "grants": authority,
            "raw_user_text": raw_user_text,
            "state": "active",
            "native_terminal": None,
            "created_at": now(),
            "updated_at": now(),
        }
        conn.execute(insert(s.native_scopes).values(**row))
        for source_id in authority["source_snapshot_ids"]:
            snapshot = SourceStore.in_transaction(conn, course_id, source_id)
            conn.execute(
                insert(s.native_source_handles).values(
                    handle_id=source_id,
                    run_id=binding.generation_id,
                    kind="snapshot",
                    source_version=snapshot.content_hash,
                    locator={"snapshot_id": source_id},
                    created_at=now(),
                )
            )
        return self._public(row)

    @staticmethod
    def _public(row):
        return {
            "run_id": row["run_id"],
            "session_id": row["session_id"],
            "request_id": row["request_id"],
            "epoch": row["native_epoch"],
            "course_id": row["course_id"],
            "purpose": row["purpose"],
            "task_id": row["grants"].get("task_id") if row["purpose"] == "task" else None,
            "allowed_tools": row["allowed_tools"],
        }

    def restore_scope(self, run_id: str, binding: ExecutionBinding) -> dict:
        self.imported.require_fresh("run", run_id)
        self.imported.require_fresh("generation", run_id)
        """Restore the original mapping, never allocate a replacement lease."""
        with self.repository._lock, self.repository.engine.begin() as conn:
            existing = (
                conn.execute(select(s.native_scopes).where(s.native_scopes.c.run_id == run_id))
                .mappings()
                .first()
            )
            if existing is None:
                return self._adopt_legacy_recovery(conn, run_id, binding)
            row = dict(existing)
            if row["business_binding"] != _binding_identity(binding):
                raise AppError("NATIVE_BINDING_MISMATCH", "恢复绑定与原权限记录不符", 409)
            binding.native_lease_epoch = row["native_epoch"]
            return self._public(row)

    def _adopt_legacy_recovery(self, conn, run_id, binding):
        # Pre-0016 executions had no native tools. Preserve them as read-only
        # recovery owners, rather than granting today's tools to an old run.
        if binding.native_lease_epoch is not None or binding.generation_id != run_id:
            raise AppError("NATIVE_SCOPE_NOT_FOUND", "已有租约身份的执行缺少原权限记录", 409)
        if conn.execute(select(s.task_runs.c.run_id).where(s.task_runs.c.run_id == run_id)).first():
            run, task, unit = self._task(conn, run_id)
            saved = run["binding"]
            settings, course_id = run["settings"], task["course_id"]
            owner, purpose = (
                "task",
                "translation" if task["kind"] in {"translation", "aligned_translation"} else "task",
            )
            grants = {
                "task_id": task["task_id"],
                "input_hash": run["input_hash"],
                "target": None,
                "legacy_recovery": True,
            }
            raw_text = task["instruction"]
            if run["state"] == "prepared":
                if not saved or _binding_identity(saved) != _binding_identity(binding):
                    raise AppError("NATIVE_BINDING_MISMATCH", "未派发任务恢复身份不符", 409)
                contract = run["input_contract"]
                sources = (
                    [item["snapshot_id"] for item in contract["inputs"]]
                    if contract
                    else ([unit["source_snapshot_id"]] if unit["source_snapshot_id"] else [])
                )
                tools = [] if purpose == "translation" else ["skill", "read_source_handle"]
                if contract and "structure_revision" in contract["target"] and purpose == "task":
                    tools.append("stage_result")
                result = self._ensure(
                    conn,
                    binding,
                    course_id,
                    owner,
                    purpose,
                    settings,
                    tools,
                    sources,
                    {
                        "task_id": task["task_id"],
                        "unit_id": unit["unit_id"],
                        "input_hash": run["input_hash"],
                        "target": contract["target"] if contract else None,
                    },
                    raw_text,
                    recovery_only=True,
                )
                binding.native_lease_epoch = result["epoch"]
                return result
        else:
            run, chat, submission, context = self._chat(conn, run_id)
            saved = run["adapter_metadata"]
            settings = {
                key: run[key]
                for key in ("model", "thinking", "max_output_tokens", "timeout_seconds")
            }
            course_id, owner, purpose = chat["course_id"], "chat", "qa"
            grants = {
                "task_id": None,
                "context_snapshot_id": run["snapshot_id"],
                "legacy_recovery": True,
            }
            raw_text = next(
                m["text"]
                for m in context["messages"]
                if m["message_id"] == submission["user_message_id"]
            )
        if not saved or _binding_identity(saved) != _binding_identity(binding):
            raise AppError("NATIVE_BINDING_MISMATCH", "旧执行恢复身份不符", 409)
        result = self._ensure(
            conn,
            binding,
            course_id,
            owner,
            purpose,
            settings,
            [],
            [],
            grants,
            raw_text,
            recovery_only=True,
        )
        binding.native_lease_epoch = result["epoch"]
        return result

    def unreleased_scopes(self, owner_kind: str | None = None) -> list[dict]:
        with self.repository.engine.connect() as conn:
            statement = select(s.native_scopes).where(s.native_scopes.c.state == "active")
            if owner_kind is not None:
                statement = statement.where(s.native_scopes.c.owner_kind == owner_kind)
            return [
                dict(row)
                for row in conn.execute(statement).mappings()
                if not self.imported.contains("run", row["run_id"])
                and not self.imported.contains("generation", row["run_id"])
            ]

    def retire_unstarted(self, lease: dict) -> dict | None:
        """Close a recorded pre-dispatch failure without inventing native end."""
        with self.repository._lock, self.repository.engine.begin() as conn:
            scope = self._scope(conn, lease["run_id"])
            self._check_owner(scope, lease)
            if scope["owner_kind"] != "task":
                return None
            run, _, _ = self._task(conn, scope["run_id"])
            proofs = (
                conn.execute(
                    select(s.task_events.c.detail).where(
                        s.task_events.c.task_id == run["task_id"],
                        s.task_events.c.kind == "run_not_dispatched",
                    )
                )
                .scalars()
                .all()
            )
            attempted = conn.execute(
                select(s.run_attempts.c.attempt_id).where(s.run_attempts.c.run_id == run["run_id"])
            ).first()
            if (
                run["state"] not in {"failed", "cancelled"}
                or attempted is not None
                or _binding_identity(run["binding"]) != scope["business_binding"]
                or not any(proof.get("run_id") == run["run_id"] for proof in proofs)
            ):
                return None
            conn.execute(
                update(s.native_scopes)
                .where(s.native_scopes.c.run_id == run["run_id"])
                .values(state="not_dispatched", updated_at=now())
            )
            return {
                **{key: lease[key] for key in ("session_id", "run_id", "request_id", "epoch")},
                "not_dispatched": True,
                "proof": "business_pre_dispatch_transition",
            }

    @staticmethod
    def _check_owner(scope, value):
        if any(
            value.get(key) != scope[column]
            for key, column in (
                ("run_id", "run_id"),
                ("session_id", "session_id"),
                ("request_id", "request_id"),
                ("epoch", "native_epoch"),
            )
        ):
            raise AppError("NATIVE_OWNER_MISMATCH", "工具调用与业务执行归属不符", 403)

    def _authorize(self, conn, call, arguments):
        self.imported.require_fresh("run", call.run_id)
        self.imported.require_fresh("generation", call.run_id)
        self.imported.require_fresh("session", call.session_id)
        scope = self._scope(conn, call.run_id)
        self._check_owner(scope, asdict(call))
        if (
            call.native_turn != scope["business_binding"]["native_turn"]
            or call.native_call_seq <= scope["business_binding"]["native_floor"]
        ):
            raise AppError("NATIVE_CALL_POSITION_DENIED", "工具调用不属于原生执行轮次", 403)
        if scope["state"] != "active" or call.tool_name not in scope["allowed_tools"]:
            raise AppError("NATIVE_TOOL_NOT_ALLOWED", "本次执行未获该工具权限", 403)
        if scope["owner_kind"] == "task":
            run, task, unit = self._task(conn, call.run_id)
            if (
                not run_still_in_scope(run, task, unit)
                or scope["business_binding"] != _binding_identity(run["binding"])
                or run["input_hash"] != scope["grants"]["input_hash"]
            ):
                raise AppError("NATIVE_TASK_NOT_ACTIVE", "原任务已暂停、取消或范围变化", 409)
            if run["cancel_requested"] or task["state"] in {"paused", "cancelled"}:
                raise AppError("NATIVE_RUN_CANCEL_REQUESTED", "原执行已请求停止", 409)
            if run["state"] not in _ACTIVE_RUNS or task["state"] != "running":
                raise AppError("NATIVE_TASK_NOT_ACTIVE", "原任务已结束或不再活动", 409)
        else:
            run, _, _, _ = self._chat(conn, call.run_id)
            active = (
                conn.execute(
                    select(business.execution_bindings).where(
                        business.execution_bindings.c.binding_id == run["binding_id"]
                    )
                )
                .mappings()
                .first()
            )
            if (
                active is None
                or active["active_generation_id"] != call.run_id
                or scope["business_binding"] != _binding_identity(run["adapter_metadata"])
            ):
                raise AppError("NATIVE_CHAT_NOT_ACTIVE", "原对话执行已结束或不再活动", 409)
            if run["cancel_requested"]:
                raise AppError("NATIVE_RUN_CANCEL_REQUESTED", "原执行已请求停止", 409)
            if run["state"] not in _ACTIVE_RUNS:
                raise AppError("NATIVE_CHAT_NOT_ACTIVE", "原对话执行已结束或不再活动", 409)
        if call.tool_name == DOCUMENT_IMAGE_TOOL:
            self.document_images.authorize(self, conn, scope, arguments)
        elif call.tool_name == "read_source_handle":
            self._source(conn, scope, arguments)
        elif call.tool_name == "stage_result":
            from .stage_attempts import rejected

            known = rejected(conn, call)
            if known:
                raise AppError(
                    known["code"],
                    "原草稿校验失败回执保持",
                    known["status"],
                    details=known["details"],
                )
            try:
                self._stage_value(conn, scope, arguments)
            except AppError as error:
                error.stage_actor_verified = True
                raise
        elif call.tool_name == ADMISSION_TOOL:
            RequestedLearning.authorize(conn, scope, arguments)
        elif call.tool_name == "search_course_concept":
            if scope["owner_kind"] != "chat" or scope["purpose"] != "qa":
                raise AppError("COURSE_QUERY_SCOPE_DENIED", "只有本次课程问答可按需检索", 403)
            concept_argument(arguments)
        elif call.tool_name == "read_nearby_subtitles":
            if not explicit_subtitles(scope["raw_user_text"]):
                raise AppError("SUBTITLE_REQUEST_REQUIRED", "本次没有明确的字幕请求", 403)
            if scope["owner_kind"] != "chat" or scope["purpose"] != "qa":
                raise AppError("NEARBY_SUBTITLE_SCOPE_DENIED", "仅允许本次问题附近字幕", 403)
            select_anchor(scope, arguments)
        elif call.tool_name in EXTERNAL_TOOLS:
            if scope["owner_kind"] != "task":
                raise AppError(
                    "RESEARCH_EXTERNAL_SCOPE_DENIED", "仅明确外部研究任务可读取公开网络", 403
                )
            ResearchExternal.authorize(conn, task, scope, arguments, call.tool_name)
        else:
            # Native skill is scoped inside Harness, never a business invoke.
            raise AppError("NATIVE_TOOL_NOT_ALLOWED", "该工具不属于业务接口", 403)
        return scope

    @staticmethod
    def _source(conn, scope, arguments):
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"handle_id", "source_version"}
            or not all(isinstance(v, str) for v in arguments.values())
        ):
            raise AppError("NATIVE_SOURCE_ARGUMENTS", "来源读取参数无效", 422)
        handle = (
            conn.execute(
                select(s.native_source_handles).where(
                    s.native_source_handles.c.run_id == scope["run_id"],
                    s.native_source_handles.c.handle_id == arguments["handle_id"],
                    s.native_source_handles.c.source_version == arguments["source_version"],
                )
            )
            .mappings()
            .first()
        )
        if handle is None or handle["kind"] != "snapshot":
            raise AppError("NATIVE_SOURCE_DENIED", "来源句柄或固定版本不属于本次授权", 403)
        if handle["locator"].get("corpus_document_id"):
            CourseCorpus.document_allowed(
                conn,
                handle["locator"].get("source_course_id", scope["course_id"]),
                handle["locator"]["corpus_document_id"],
            )
        if handle["locator"].get("retrieved_artifact_id"):
            artifact = conn.execute(
                select(s.artifacts).where(
                    s.artifacts.c.artifact_id == handle["locator"]["retrieved_artifact_id"],
                    s.artifacts.c.course_id
                    == handle["locator"].get("source_course_id", scope["course_id"]),
                    s.artifacts.c.indexed.is_(True),
                    s.artifacts.c.head_revision_id == handle["locator"]["retrieved_revision_id"],
                )
            ).first()
            if artifact is None:
                raise AppError("COURSE_CORPUS_SOURCE_DENIED", "成果已取消索引或发生修订", 403)
        snapshot = SourceStore.in_transaction(
            conn, scope["course_id"], handle["locator"]["snapshot_id"]
        )
        if snapshot.content_hash != handle["source_version"]:
            raise AppError("NATIVE_SOURCE_VERSION_MISMATCH", "来源快照完整性不符", 409)
        if len(snapshot.text) > 96_000:
            raise AppError("NATIVE_SOURCE_SIZE_LIMIT", "选区超过单次工具读取上限，未截断", 422)
        return snapshot

    @staticmethod
    def _stage_value(conn, scope, arguments):
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"task_id", "object_id", "expected_version", "stage", "body"}
            or type(arguments["expected_version"]) is not int
            or not isinstance(arguments["body"], str)
            or len(arguments["body"].encode("utf-8")) > MAX_STAGE_BODY_BYTES
        ):
            raise AppError("NATIVE_STAGE_ARGUMENTS", "成果草稿参数或大小无效", 422)
        target = scope["grants"].get("target")
        if (
            scope["owner_kind"] != "task"
            or not target
            or arguments["task_id"] != scope["grants"]["task_id"]
            or arguments["object_id"] != target["reserved_artifact_id"]
            or arguments["expected_version"] != target["structure_revision"]
            or arguments["stage"] != "content"
        ):
            raise AppError(
                "NATIVE_STAGE_TARGET_DENIED",
                "成果对象、版本或阶段不在本次写入范围",
                403,
                details={
                    "object_id": target.get("reserved_artifact_id") if target else None,
                    "expected_version": target.get("structure_revision") if target else None,
                    "stage": "content",
                    "hint": "expected_version is the fixed target version, not stage_revision",
                },
            )
        run, task, _ = NativeBusiness._task(conn, scope["run_id"])
        current = (
            conn.execute(select(s.task_targets).where(s.task_targets.c.task_id == task["task_id"]))
            .mappings()
            .one()
        )
        if current["state"] in {"conflict", "blocked"} or (
            current["structure_revision"] != target["structure_revision"]
            and not (run["input_contract"] or {}).get("edit_range")
        ):
            raise AppError("NATIVE_STAGE_TARGET_STALE", "成果写入范围已变化，已有草稿保留", 409)
        try:
            body = ArtifactBody.model_validate_json(arguments["body"])
        except ValidationError as error:
            from .stage_contract import validation_details

            raise AppError(
                "NATIVE_STAGE_BODY_INVALID",
                "成果草稿不符合内容契约",
                422,
                details=validation_details(error),
            ) from error
        ArtifactStore.validate_run_body(conn, task, run, body)
        if target["mode"] == "edit" and not {block.block_id for block in body.blocks}.issubset(
            set(target["write_scope"]["block_ids"])
        ):
            raise AppError("TASK_TARGET_BLOCK_DENIED", "草稿改动了指定内容块以外的部分", 403)
        return body

    def _remember_stage_failure(self, call, error):
        if call.tool_name != "stage_result" or not getattr(error, "stage_actor_verified", False):
            return
        from .stage_attempts import record

        with self.repository._lock, self.repository.engine.begin() as conn:
            try:
                scope = self._scope(conn, call.run_id)
                self._check_owner(scope, asdict(call))
            except AppError:
                return
            if scope["owner_kind"] == "task" and "stage_result" in scope["allowed_tools"]:
                record(conn, call, error)
                self.repository._hook("native_stage_rejection_before_commit", run_id=call.run_id)
        self.repository._hook("native_stage_rejection_after_commit", run_id=call.run_id)

    def authorize(self, call: NativeCall, arguments: dict) -> None:
        try:
            with self.repository._lock, self.repository.engine.begin() as conn:
                self._authorize(conn, call, arguments)
        except AppError as error:
            self._remember_stage_failure(call, error)
            raise ToolRejected(error.code, error.status, details=error.details) from None

    def validate_model_images(self, run_id, request, summarize):
        try:
            self.document_images.validate_request(run_id, request, summarize)
        except AppError as error:
            raise ToolRejected(error.code, error.status) from None

    def invoke(self, call: NativeCall, arguments: dict) -> dict:
        try:
            if call.tool_name == DOCUMENT_IMAGE_TOOL:
                return self.document_images.invoke(self, call, arguments)
            if call.tool_name == ADMISSION_TOOL:
                with self.repository._lock, self.repository.engine.begin() as conn:
                    scope = self._authorize(conn, call, arguments)
                    result = RequestedLearning.admit(conn, self.repository, call, scope, arguments)
                self.repository._hook("native_admission_after_commit", run_id=call.run_id)
                if self.task_admitted:
                    self.task_admitted(scope["course_id"], result["task_id"])
                return result
            if call.tool_name in EXTERNAL_TOOLS:
                return self.research_external.invoke(self, call, arguments)
            if call.tool_name == "read_source_handle":
                return self._invoke_read(call, arguments)
            if call.tool_name == "search_course_concept":
                return self._invoke_query(call, arguments)
            if call.tool_name == "read_nearby_subtitles":
                return self._invoke_nearby(call, arguments)
            with self.repository._lock, self.repository.engine.begin() as conn:
                scope = self._authorize(conn, call, arguments)
                body = self._stage_value(conn, scope, arguments)
                digest = _hash(body.model_dump(mode="json"))
                operation = "native_stage_" + _hash(
                    {
                        "run_id": call.run_id,
                        "target": scope["grants"]["target"],
                        "stage": arguments["stage"],
                        "body": digest,
                    }
                )
                prior = (
                    conn.execute(
                        select(s.model_stages).where(s.model_stages.c.operation_id == operation)
                    )
                    .mappings()
                    .first()
                )
                if prior is None:
                    ordinal = (
                        conn.execute(
                            select(func.max(s.model_stages.c.stage_revision)).where(
                                s.model_stages.c.run_id == call.run_id
                            )
                        ).scalar_one()
                        or 0
                    ) + 1
                    prior = {
                        "stage_id": new_id("stage"),
                        "run_id": call.run_id,
                        "operation_id": operation,
                        "stage": arguments["stage"],
                        "stage_revision": ordinal,
                        "artifact_id": arguments["object_id"],
                        "object_version": arguments["expected_version"],
                        "expected_revision_id": scope["grants"]["target"]["expected_revision_id"],
                        "input_hash": scope["grants"]["input_hash"],
                        "body": body.model_dump(mode="json"),
                        "body_hash": digest,
                        "native_call": asdict(call),
                        "created_at": now(),
                    }
                    conn.execute(insert(s.model_stages).values(**prior))
                    self.repository._hook("native_stage_before_commit", run_id=call.run_id)
                result = self._stage_receipt(prior)
                effect_id = _hash(asdict(call))
                recorded = (
                    conn.execute(
                        select(s.native_effects).where(s.native_effects.c.effect_id == effect_id)
                    )
                    .mappings()
                    .first()
                )
                if recorded is None:
                    conn.execute(
                        insert(s.native_effects).values(
                            effect_id=effect_id,
                            run_id=call.run_id,
                            stage_id=prior["stage_id"],
                            native_call=asdict(call),
                            receipt=result,
                            created_at=now(),
                        )
                    )
            self.repository._hook("native_stage_after_commit", run_id=call.run_id)
            return result
        except AppError as error:
            self._remember_stage_failure(call, error)
            raise ToolRejected(error.code, error.status, details=error.details) from None

    def _invoke_read(self, call, arguments):
        # Dynamic document grants must still match the real file version. This
        # filesystem check stays outside a SQLite write transaction.
        with self.repository.engine.connect() as conn:
            scope = self._authorize(conn, call, arguments)
            handle = (
                conn.execute(
                    select(s.native_source_handles).where(
                        s.native_source_handles.c.run_id == call.run_id,
                        s.native_source_handles.c.handle_id == arguments["handle_id"],
                    )
                )
                .mappings()
                .one()
            )
            snapshot = self._source(conn, scope, arguments)
        if handle["locator"].get("retrieval"):
            SourceStore(self.repository).require_current(snapshot)
        with self.repository._lock, self.repository.engine.begin() as conn:
            scope = self._authorize(conn, call, arguments)
            snapshot = self._source(conn, scope, arguments)
            identity = _hash(asdict(call))
            known = (
                conn.execute(
                    select(s.native_read_receipts).where(
                        s.native_read_receipts.c.effect_id == identity
                    )
                )
                .mappings()
                .first()
            )
            if known is not None:
                return known["result"]
            result = {
                "handle_id": snapshot.snapshot_id,
                "source_version": snapshot.content_hash,
                "source": material(
                    {"representation": "snapshot", "leaf_id": None, "label": "已授权来源"}, snapshot
                ),
            }
            conn.execute(
                insert(s.native_read_receipts).values(
                    effect_id=identity,
                    run_id=call.run_id,
                    snapshot_id=snapshot.snapshot_id,
                    native_call=asdict(call),
                    result=result,
                    result_hash=_hash(result),
                    created_at=now(),
                )
            )
            self.repository._hook("native_read_before_commit", run_id=call.run_id)
        self.repository._hook("native_read_after_commit", run_id=call.run_id)
        return result

    def _invoke_query(self, call, arguments):
        identity = _hash(asdict(call))
        with self.repository.engine.connect() as conn:
            scope = self._authorize(conn, call, arguments)
            known = (
                conn.execute(
                    select(corpus_s.query_receipts).where(
                        corpus_s.query_receipts.c.effect_id == identity
                    )
                )
                .mappings()
                .first()
            )
            if known is not None:
                return known["result"]
            course = scope["course_id"]
            courses = scope["grants"].get("corpus_course_ids", [course])
            if not courses:
                raise AppError("WORKSPACE_SCOPE_REQUIRED", "请先为对话添加资料工作区", 403)
            course_scope(conn, course, courses)
            titles = dict(
                conn.execute(
                    select(business.courses.c.course_id, business.courses.c.title).where(
                        business.courses.c.course_id.in_(courses)
                    )
                ).all()
            )
        groups = [
            search_documents(self.repository, origin, concept_argument(arguments))
            for origin in courses
        ]
        if courses == [course]:
            found = groups[0]
        else:
            found = []
            for documents in (True, False):
                per_course = [
                    [item for item in items if bool(item.get("document_id")) == documents]
                    for items in groups
                ]
                count = 0
                for offset in range(4):
                    for items in per_course:
                        if offset < len(items) and count < 4:
                            found.append(items[offset])
                            count += 1
        with self.repository._lock, self.repository.engine.begin() as conn:
            scope = self._authorize(conn, call, arguments)
            results = []
            for item in found:
                snapshot = item["snapshot"]
                origin = snapshot.course_id
                if origin not in courses:
                    raise AppError("SOURCE_COURSE_SCOPE_DENIED", "检索结果超出本轮明确课程", 403)
                declare_snapshot(
                    conn,
                    course,
                    snapshot.snapshot_id,
                    authority={
                        "kind": "native_query",
                        "run_id": call.run_id,
                        "effect_id": identity,
                    },
                )
                locator = {"snapshot_id": snapshot.snapshot_id, "retrieval": True}
                if origin != course:
                    locator["source_course_id"] = origin
                if item.get("live_document"):
                    locator["live_document"] = True
                elif item.get("document_id"):
                    CourseCorpus.document_allowed(conn, origin, item["document_id"])
                    locator["corpus_document_id"] = item["document_id"]
                else:
                    current = conn.execute(
                        select(s.artifacts.c.head_revision_id).where(
                            s.artifacts.c.artifact_id == item["artifact_id"],
                            s.artifacts.c.course_id == origin,
                            s.artifacts.c.indexed.is_(True),
                        )
                    ).scalar_one_or_none()
                    if current != item["revision_id"]:
                        continue
                    locator.update(
                        retrieved_artifact_id=item["artifact_id"],
                        retrieved_revision_id=item["revision_id"],
                    )
                handle_id = "retrieved_" + _hash(
                    {"snapshot": snapshot.content_hash, "locator": locator}
                )
                if (
                    conn.execute(
                        select(s.native_source_handles.c.handle_id).where(
                            s.native_source_handles.c.run_id == call.run_id,
                            s.native_source_handles.c.handle_id == handle_id,
                        )
                    ).first()
                    is None
                ):
                    conn.execute(
                        insert(s.native_source_handles).values(
                            handle_id=handle_id,
                            run_id=call.run_id,
                            kind="snapshot",
                            source_version=snapshot.content_hash,
                            locator=locator,
                            created_at=now(),
                        )
                    )
                results.append(
                    {
                        "handle_id": handle_id,
                        "source_version": snapshot.content_hash,
                        "source": material(
                            {
                                "representation": "snapshot",
                                "leaf_id": None,
                                "label": item["label"]
                                if courses == [course]
                                else titles[origin] + " · " + item["label"],
                            },
                            snapshot,
                        ),
                    }
                )
            availability = {"incomplete": True, "mode": "on_demand", "bounded": True}
            result = {
                "course_id": course,
                **({"read_course_ids": courses} if courses != [course] else {}),
                "concept": concept_argument(arguments),
                "results": results,
                "status": "matched"
                if results
                else "no_match_in_checked_range",
                "index_status": availability,
                "external_fallback": False,
                "scope": "explicit_workspaces_on_demand_documents",
                "coverage": "retrieval_only",
            }
            conn.execute(
                insert(corpus_s.query_receipts).values(
                    effect_id=identity,
                    run_id=call.run_id,
                    native_call=asdict(call),
                    result=result,
                    result_hash=_hash(result),
                    created_at=now(),
                )
            )
            self.repository._hook("native_query_before_commit", run_id=call.run_id)
        self.repository._hook("native_query_after_commit", run_id=call.run_id)
        return result

    def _invoke_nearby(self, call, arguments):
        identity = _hash(asdict(call))
        with self.repository.engine.connect() as conn:
            scope = self._authorize(conn, call, arguments)
            known = (
                conn.execute(
                    select(corpus_s.query_receipts).where(
                        corpus_s.query_receipts.c.effect_id == identity
                    )
                )
                .mappings()
                .first()
            )
            if known:
                return known["result"]
            scope = dict(scope)
            anchor = select_anchor(scope, arguments)
        if anchor is None:
            value = {
                "status": "choose_anchor",
                "anchors": scope["grants"].get("subtitle_anchors", []),
                "snapshots": [],
            }
        else:
            if re.search(r"生成|转写|制作|转录", scope["raw_user_text"]) and not re.search(r"附近|这一段|这段|片段", scope["raw_user_text"]):
                from learning_app.config import PROJECT_ROOT

                from .preparations import PreparationStore
                from .processing_models import PreparationCreate

                self.authorize(call, arguments)
                store = PreparationStore(self.repository)
                origin = anchor.get("source_course_id") or scope["course_id"]
                definition = store.define_asr(origin, PreparationCreate(
                    kind="asr", asset_id=anchor["asset_id"], source_version=anchor["source_version"],
                ), PROJECT_ROOT, explicit=True)
                job = store.publish_pdf(definition, summary=True)
                value = {"status": job.state, "preparation_id": job.job_id, "course_id": origin,
                         "track_id": definition["payload"]["parameters"]["track_id"],
                         "scope": "explicit_current_video", "snapshots": []}
            else:
                value = NearbySubtitles(self.repository).read(
                    scope, anchor, lambda: self.authorize(call, arguments)
                )
        snapshots = value.pop("snapshots")
        with self.repository._lock, self.repository.engine.begin() as conn:
            self._authorize(conn, call, arguments)
            results = []
            for snapshot in snapshots:
                reference = snapshot.reference
                if (
                    reference.kind != "subtitle"
                    or reference.asset_id != anchor["asset_id"]
                    or reference.source_version != anchor["source_version"]
                    or not value["start_ms"]
                    <= reference.start_ms
                    < reference.end_ms
                    <= value["end_ms"]
                ):
                    raise AppError("NEARBY_SUBTITLE_SCOPE_DENIED", "字幕超出冻结问题范围", 403)
                declare_snapshot(conn, scope["course_id"], snapshot.snapshot_id,
                                 authority={"kind": "explicit_subtitles", "run_id": call.run_id})
                handle_id = "nearby_" + _hash(
                    {"anchor": anchor["anchor_id"], "snapshot": snapshot.content_hash}
                )
                if (
                    conn.execute(
                        select(s.native_source_handles.c.handle_id).where(
                            s.native_source_handles.c.run_id == call.run_id,
                            s.native_source_handles.c.handle_id == handle_id,
                        )
                    ).first()
                    is None
                ):
                    conn.execute(
                        insert(s.native_source_handles).values(
                            handle_id=handle_id,
                            run_id=call.run_id,
                            kind="snapshot",
                            source_version=snapshot.content_hash,
                            locator={
                                "snapshot_id": snapshot.snapshot_id,
                                "subtitle_anchor_id": anchor["anchor_id"],
                            },
                            created_at=now(),
                        )
                    )
                results.append(
                    {
                        "handle_id": handle_id,
                        "source_version": snapshot.content_hash,
                        "source": material(
                            {
                                "representation": "snapshot",
                                "leaf_id": None,
                                "label": "问题附近字幕",
                            },
                            snapshot,
                        ),
                    }
                )
            result = {**value, "results": results, "external_fallback": False}
            conn.execute(
                insert(corpus_s.query_receipts).values(
                    effect_id=identity,
                    run_id=call.run_id,
                    native_call=asdict(call),
                    result=result,
                    result_hash=_hash(result),
                    created_at=now(),
                )
            )
            self.repository._hook("native_query_before_commit", run_id=call.run_id)
        self.repository._hook("native_query_after_commit", run_id=call.run_id)
        return result

    @staticmethod
    def _stage_receipt(row):
        return {
            "stage_id": row["stage_id"],
            "operation_id": row["operation_id"],
            "stage_revision": row["stage_revision"],
            "expected_version": row["object_version"],
            "artifact_id": row["artifact_id"],
            "body_hash": row["body_hash"],
            "disposition": "staged",
        }

    def staged_body(self, run_id: str) -> ArtifactBody | None:
        with self.repository.engine.connect() as conn:
            run, task, unit = self._task(conn, run_id)
            if run["state"] != "completed" or not run_still_in_scope(run, task, unit):
                return None
            from .stage_attempts import final_stage

            stage_id = final_stage(conn, run_id)
            if stage_id is None:
                return None
            row = (
                conn.execute(
                    select(s.model_stages)
                    .where(
                        s.model_stages.c.run_id == run_id,
                        s.model_stages.c.input_hash == run["input_hash"],
                        s.model_stages.c.stage_id == stage_id,
                    )
                    .order_by(s.model_stages.c.stage_revision.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None or _hash(row["body"]) != row["body_hash"]:
                raise AppError("NATIVE_STAGE_RECEIPT_CONFLICT", "成果草稿与原回执不符，未发布")
            return ArtifactBody.model_validate(row["body"])

    def reconcile_effect(self, call: NativeCall) -> dict | None:
        """Read an exact committed effect, without invoking a tool again."""
        with self.repository._lock, self.repository.engine.begin() as conn:
            scope = self._scope(conn, call.run_id)
            self._check_owner(scope, asdict(call))
            if call.tool_name == "stage_result":
                from .stage_attempts import rejected

                known = rejected(conn, call)
                if known:
                    raise KnownToolFailure(known["code"], known["status"], details=known["details"])
            if call.tool_name == ADMISSION_TOOL:
                receipt = RequestedLearning.receipt(conn, call, scope)
                if receipt is not None and self.task_admitted:
                    self.task_admitted(scope["course_id"], receipt["task_id"])
                return receipt
            if call.tool_name in EXTERNAL_TOOLS:
                return ResearchExternal.receipt(conn, call)
            if call.tool_name == DOCUMENT_IMAGE_TOOL:
                return self.document_images.receipt(conn, call)
            if call.tool_name in {"search_course_concept", "read_nearby_subtitles"}:
                query = (
                    conn.execute(
                        select(corpus_s.query_receipts).where(
                            corpus_s.query_receipts.c.effect_id == _hash(asdict(call)),
                            corpus_s.query_receipts.c.run_id == call.run_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                return (
                    query["result"]
                    if query
                    and query["native_call"] == asdict(call)
                    and _hash(query["result"]) == query["result_hash"]
                    else None
                )
            if call.tool_name == "read_source_handle":
                row = (
                    conn.execute(
                        select(s.native_read_receipts).where(
                            s.native_read_receipts.c.effect_id == _hash(asdict(call)),
                            s.native_read_receipts.c.run_id == call.run_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                return (
                    row["result"]
                    if row
                    and row["native_call"] == asdict(call)
                    and _hash(row["result"]) == row["result_hash"]
                    else None
                )
            row = (
                conn.execute(
                    select(s.native_effects).where(
                        s.native_effects.c.effect_id == _hash(asdict(call)),
                        s.native_effects.c.run_id == call.run_id,
                    )
                )
                .mappings()
                .first()
            )
            return row["receipt"] if row and row["native_call"] == asdict(call) else None

    def remember_terminal(self, run_id: str, terminal: dict) -> None:
        """Called only with the adapter's actual native turn/end projection."""
        with self.repository._lock, self.repository.engine.begin() as conn:
            scope = self._scope(conn, run_id)
            self._terminal(conn, scope, terminal)
            if scope["native_terminal"] is not None and scope["native_terminal"] != terminal:
                raise AppError("NATIVE_TERMINAL_IMMUTABLE", "原生终态证据不能替换", 409)
            conn.execute(
                update(s.native_scopes)
                .where(s.native_scopes.c.run_id == run_id)
                .values(native_terminal=terminal, updated_at=now())
            )

    def _terminal(self, conn, scope, terminal):
        if (
            not isinstance(terminal, dict)
            or set(terminal) != {"turn", "seq", "reason"}
            or terminal["turn"] != scope["business_binding"]["native_turn"]
            or type(terminal["seq"]) is not int
            or terminal["seq"] <= scope["business_binding"]["native_floor"]
            or not isinstance(terminal["reason"], dict)
        ):
            raise AppError("NATIVE_TERMINAL_OWNER", "原生终态与固定执行轮次不符", 409)
        if scope["owner_kind"] == "task":
            run, _, _ = self._task(conn, scope["run_id"])
            events = (
                conn.execute(
                    select(s.run_events.c.payload).where(s.run_events.c.run_id == scope["run_id"])
                )
                .scalars()
                .all()
            )
            events = [item for item in events if item.get("kind") == "terminal"]
            matching = [
                item
                for item in events
                if item.get("native_seq") == terminal["seq"]
                and item.get("data", {}).get("reason") == terminal["reason"]
            ]
        else:
            run, _, _, _ = self._chat(conn, scope["run_id"])
            events = (
                conn.execute(
                    select(business.execution_events.c.payload).where(
                        business.execution_events.c.generation_id == scope["run_id"],
                        business.execution_events.c.kind == "terminal",
                    )
                )
                .scalars()
                .all()
            )
            matching = [
                item
                for item in events
                if item.get("data", {}).get("native_seq") == terminal["seq"]
                and item.get("data", {}).get("reason") == terminal["reason"]
            ]
        reason = terminal["reason"]
        expected = {
            "completed": {"completed", "truncated"},
            "max-tokens": {"truncated"},
            "aborted": {"cancelled"},
            "error": {"failed"},
        }.get(reason.get("kind"), set())
        if (
            run["state"] not in _FINISHED_RUNS
            or run["state"] not in expected
            or not matching
            or (
                reason.get("kind") == "aborted"
                and (not run["cancel_requested"] or reason.get("reason", {}).get("kind") != "user")
            )
        ):
            raise AppError("NATIVE_TERMINAL_NOT_SETTLED", "业务终态尚不能核实原生执行已结束", 409)

    def assert_releasable(self, lease: dict) -> None:
        try:
            with self.repository._lock, self.repository.engine.begin() as conn:
                scope = self._scope(conn, lease.get("run_id"))
                self._check_owner(scope, lease)
                terminal = scope["native_terminal"]
                if terminal is None or (
                    "native_terminal" in lease and lease["native_terminal"] != terminal
                ):
                    raise AppError("NATIVE_TERMINAL_NOT_SETTLED", "尚无可核实的原生终态", 409)
                self._terminal(conn, scope, terminal)
        except AppError as error:
            raise ToolRejected(error.code, error.status) from None

    def mark_released(self, receipt: dict) -> None:
        with self.repository._lock, self.repository.engine.begin() as conn:
            scope = self._scope(conn, receipt.get("run_id"))
            self._check_owner(scope, receipt)
            terminal = scope["native_terminal"]
            if (
                receipt.get("released") is not True
                or receipt.get("durable") is not True
                or terminal is None
                or receipt.get("terminal_seq") != terminal["seq"]
                or receipt.get("native_turn") != terminal["turn"]
                or receipt.get("native_terminal_kind") != terminal["reason"]["kind"]
            ):
                raise AppError("NATIVE_RELEASE_RECEIPT", "原生执行释放回执尚不能核实", 409)
            self._terminal(conn, scope, terminal)
            conn.execute(
                update(s.native_scopes)
                .where(s.native_scopes.c.run_id == scope["run_id"])
                .values(state="released", updated_at=now())
            )
