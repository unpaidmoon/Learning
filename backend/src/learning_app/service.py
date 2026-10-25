"""Serialize application submissions and reconcile execution without resending unknown work."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

import httpx
from dotenv import dotenv_values

from learning_app.config import AppConfig
from learning_app.documents import expand_document_parts, requires_images, validate_document_scope
from learning_app.drafts import (
    message_references,
    message_selection_ids,
    submission_reference_conditions,
    submission_selection_conditions,
    submission_source_locators,
)
from learning_app.errors import AppError
from learning_app.execution.native_tools import ToolRejected
from learning_app.execution.selection import supports_images, validate_selection
from learning_app.execution.types import (
    ApprovedAttachment,
    ExecutionBinding,
    ExecutionError,
    ExecutionPolicy,
    NormalizedEvent,
)
from learning_app.faults import Faults
from learning_app.imported_executions import imported_executions
from learning_app.storage import StorageError

TERMINAL = {"completed", "cancelled", "failed", "truncated"}
logger = logging.getLogger(__name__)


class GenerationService:
    def __init__(
        self,
        repository,
        config: AppConfig,
        *,
        execution=None,
        fault_hook=None,
        execution_factory=None,
    ):
        self.repository = repository
        self._imported = None
        self.config = config
        self.execution = execution
        self.owns_execution = execution is None
        self.execution_factory = execution_factory
        self.execution_ready = False
        self.faults = Faults(fault_hook)
        self.tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.execution_error: str | None = None
        self.cleanup: dict = {}
        self._binding_ids: dict[str, str] = {}
        self._binding_snapshots: dict[str, str] = {}
        self._attempt_snapshots: dict[str, tuple] = {}
        from learning_app.workbench.translation_reuse import TranslationReuse

        self.translation_reuse = TranslationReuse(repository, config, lambda: self.execution)

    @property
    def imported(self):
        if self._imported is None:
            if self.repository is None:
                raise AppError("PERSISTENCE_UNAVAILABLE", "本地学习数据尚未就绪，未启动执行", 503)
            self._imported = imported_executions(self.repository)
        return self._imported

    @property
    def question_answering_ready(self) -> bool:
        """Separate an open Host from the selected route's known capability."""
        if not self.execution_ready or self.execution is None:
            return False
        process = getattr(getattr(self.execution, "host", None), "process", None)
        if process is not None and process.poll() is not None:
            return False
        capability = getattr(self.execution, "question_answering_ready", None)
        if capability is not None:
            return bool(capability)
        # Injected local test doubles predate the route-capability seam. An
        # unknown official execution implementation does not gain readiness.
        return getattr(self.execution, "mode", "fixture") == "fixture"

    async def open(self):
        from learning_app.data_activation import require_active_data

        if self.repository is not None:
            require_active_data(self.repository.root)
        # Capability predicates remain free of I/O; real startup validates the
        # import fence before credential access, Host startup or recovery.
        if not self.imported.ready:
            raise AppError("IMPORTED_EXECUTION_NOT_READY", "导入执行隔离尚未完成，未启动执行", 503)
        if self.execution is None and self.execution_factory is not None:
            self.execution = self.execution_factory(self.repository, self.config)
        if self.execution is None and self.config.harness_enabled:
            from learning_app.execution.adapter import ExecutionAdapter
            from learning_app.execution.native_tools import NativeCallRegistry, NativeToolTransport
            from learning_app.workbench.native_business import NativeBusiness

            key = dotenv_values(self.config.secret_file).get("DEEPSEEK_API_KEY")
            route = self.config.credential_route
            route_path = self.config.data_dir / "credential-route.json"
            if route_path.is_file():
                try:
                    stored = json.loads(route_path.read_text("utf-8"))
                    if isinstance(stored, dict) and stored.get("route") in {"account", "api_key"}:
                        route = stored["route"]
                except (OSError, ValueError):
                    self.execution_error = "CREDENTIAL_ROUTE_STORAGE_INVALID"
                    return
            self.execution = ExecutionAdapter(
                self.config.project_root,
                self.config.data_dir / "execution",
                mode="official",
                api_key=key,
                model=self.config.model,
                thinking=self.config.thinking,
                max_tokens=self.config.max_output_tokens,
                timeout=self.config.model_timeout_seconds,
                credential_route=route,
                desktop_enabled=self.config.desktop_enabled,
                native_tools=NativeToolTransport(
                    NativeCallRegistry(self.config.data_dir / "execution/native-calls.jsonl"),
                    NativeBusiness(self.repository),
                ),
            )
        if self.execution is None:
            return
        try:
            if self.owns_execution:
                self._restore_execution_policies()
                await self.execution.open()
            self.execution_ready = True
            if (
                getattr(self.execution, "mode", "fixture") == "official"
                and getattr(self.execution, "credential_route", "api_key") == "account"
            ):
                try:
                    await self.execution.refresh_account_models()
                except (ExecutionError, httpx.HTTPError, OSError, TimeoutError):
                    # Account availability is independent of video, files and
                    # business persistence. Never fall back to another credential.
                    pass
        except (ExecutionError, OSError, TimeoutError) as exc:
            self.execution_error = (
                exc.code if isinstance(exc, ExecutionError) else "EXECUTION_START_FAILED"
            )
            logger.error("Harness initialization failed: %s", self.execution_error)
            return
        await self.recover()

    def _restore_execution_policies(self) -> None:
        restore = getattr(self.execution, "restore_execution_policy", None)
        if restore is None:
            return
        native = getattr(self.execution, "native_tools", None)
        generations = {
            row["generation_id"]: row
            for row in self.repository.recoverable_generations()
            if not self.imported.contains("generation", row["generation_id"])
        }
        pending_scopes = []
        if native is not None:
            for scope in native.port.unreleased_scopes():
                if self.imported.contains("run", scope["run_id"]) or self.imported.contains(
                    "generation", scope["run_id"]
                ):
                    continue
                lease = {
                    "session_id": scope["session_id"],
                    "run_id": scope["run_id"],
                    "request_id": scope["request_id"],
                    "epoch": scope["native_epoch"],
                }
                if native.registry.retire_unstarted(lease, native.port):
                    continue
                released = native.registry.release_receipts.get(scope["run_id"])
                if released is not None:
                    native.port.mark_released(released)
                else:
                    pending_scopes.append(scope)
                    if scope["owner_kind"] == "chat":
                        generations[scope["run_id"]] = self.repository.get_generation(
                            scope["run_id"]
                        )
        for generation in generations.values():
            if generation.get("adapter_metadata"):
                extra = {}
                if native is not None:
                    binding = ExecutionBinding.from_dict(generation["adapter_metadata"])
                    extra["scope"] = native.port.restore_scope(generation["generation_id"], binding)
                    extra["reconcile_only"] = True
                restore(
                    generation["adapter_metadata"],
                    ExecutionPolicy(
                        generation["model"],
                        generation["thinking"],
                        generation["max_output_tokens"],
                        generation["timeout_seconds"],
                        allowed_tool_names=tuple(extra.get("scope", {}).get("allowed_tools", ())),
                    ),
                    task_owned=False,
                    **extra,
                )
        from learning_app.workbench.executor import TaskExecutor
        from learning_app.workbench.runs import TaskRunStore

        task_runs = {row["run_id"]: row for row in TaskRunStore(self.repository).recoverable()}
        for scope in pending_scopes:
            if scope["owner_kind"] == "task":
                task_runs[scope["run_id"]] = TaskRunStore(self.repository).internal(scope["run_id"])
        for run in task_runs.values():
            if self.imported.contains("run", run["run_id"]) or self.imported.contains(
                "task", run["task_id"]
            ):
                continue
            if run["binding"]:
                extra = {}
                if native is not None:
                    binding = ExecutionBinding.from_dict(run["binding"])
                    extra["scope"] = native.port.restore_scope(run["run_id"], binding)
                    extra["reconcile_only"] = run["state"] != "prepared"
                restore(
                    run["binding"],
                    TaskExecutor._policy(TaskRunStore(self.repository).internal(run["run_id"])),
                    task_owned=True,
                    **extra,
                )

    async def recover(self):
        generations = {
            row["generation_id"]: row
            for row in self.repository.recoverable_generations()
            if not self.imported.contains("generation", row["generation_id"])
        }
        native = getattr(self.execution, "native_tools", None)
        if native is not None:
            for scope in native.port.unreleased_scopes("chat"):
                if self.imported.contains("run", scope["run_id"]) or self.imported.contains(
                    "generation", scope["run_id"]
                ):
                    continue
                generations[scope["run_id"]] = self.repository.get_generation(scope["run_id"])
        for generation in generations.values():
            gid = generation["generation_id"]
            if generation["state"] == "pending":
                self._schedule(gid, self._dispatch(gid))
            else:
                self._schedule(gid, self.reconcile(gid, resume_observer=True))

    def _schedule(self, generation_id: str, coroutine):
        previous = self.tasks.get(generation_id)
        if previous and not previous.done():
            coroutine.close()
            return
        task = asyncio.create_task(coroutine, name=f"generation:{generation_id}")
        self.tasks[generation_id] = task

    async def close(self, *, reason="SERVICE_SHUTDOWN"):
        # Stop only this service's readers; unknown work is never converted back to pending.
        for generation_id, task in self.tasks.items():
            if not task.done():
                state = self.repository.get_generation(generation_id)["state"]
                if state not in TERMINAL and state != "pending":
                    self.repository.mark_unknown(generation_id, reason)
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if self.execution is not None and self.owns_execution:
            self.cleanup = await self.execution.close()
        self.execution_ready = False

    async def submit(self, value: dict) -> dict:
        if not self.execution_ready:
            raise AppError("EXECUTION_UNAVAILABLE", "Harness尚未连接，未提交问题", 503)
        process = getattr(getattr(self.execution, "host", None), "process", None)
        if process is not None and process.poll() is not None:
            raise AppError(
                "EXECUTION_UNAVAILABLE", "执行层已退出，原请求未重发；可查看已有资料", 503
            )
        if not value["text"].strip() and not value.get("attachment_ids"):
            raise AppError("INPUT_EMPTY", "请输入问题或确认图片", 422)
        async with self.locks[value["chat_id"]]:
            try:
                self.repository.get_submission(value["submission_id"])
                existing_submission = True
            except StorageError as exc:
                if exc.code != "SUBMISSION_NOT_FOUND":
                    raise
                existing_submission = False
            if not existing_submission and (value.get("conditions") or value.get("corrections")):
                raise AppError("SEPARATE_CONTEXT_REMOVED", "请通过普通消息补充条件或更正内容", 422)
            if not existing_submission:
                try:
                    validator = getattr(self.execution, "validate_choice", None)
                    if validator is None:
                        validate_selection(self.config.model, value["model"], value["thinking"])
                    else:
                        await validator(value["model"], value["thinking"])
                except ExecutionError as exc:
                    message = {
                        "ACCOUNT_SIGN_IN_REQUIRED": "请先在账号与模型中登录 DeepSeek 官方账号",
                        "ACCOUNT_CATALOG_UNAVAILABLE": "账号模型目录暂不可用，请在账号与模型中刷新后重试",
                        "MODEL_KEY_NOT_CONFIGURED": "所选 API Key 方式尚未配置，请检查本机私有配置",
                    }.get(exc.code, "当前执行层不支持所选模型或思考强度，请更改选择")
                    raise AppError(exc.code, message, 422) from exc
            # Capability changes in later turns cannot invalidate an earlier
            # receipt. publish_submission still checks its full immutable hash.
            image_support = getattr(self.execution, "supports_model_images", supports_images)
            if not existing_submission and not image_support(value["model"]):
                history = self.repository.get_chat(value["chat_id"])["messages"]
                relevant_ids = list(value.get("attachment_ids", ())) + [
                    aid for message in history for aid in message.get("attachment_ids", ())
                ]
                if requires_images(self.repository, relevant_ids):
                    raise AppError(
                        "MODEL_IMAGE_UNSUPPORTED",
                        "所选模型仅支持文字；当前图片及题图历史已保留，请选择支持图片的模型或新建文字对话",
                        422,
                    )
            if not existing_submission:
                validate_document_scope(
                    self.repository, value["chat_id"], value.get("attachment_ids", ())
                )
                # Check the complete selected/current document set before publishing a
                # generation, so a size limit never creates a partial provider turn.
                document_ids = list(value.get("attachment_ids", ()))
                history = self.repository.get_chat(value["chat_id"])["messages"]
                document_ids.extend(
                    aid for message in history for aid in message.get("attachment_ids", ())
                )
                unique_documents = list(dict.fromkeys(document_ids))
                await asyncio.to_thread(
                    expand_document_parts,
                    self.repository,
                    value["chat_id"],
                    [self.repository.resolve_attachment(aid) for aid in unique_documents],
                )
            conditions = (
                list(value.get("conditions", ())) if existing_submission else []
            ) + submission_reference_conditions(
                self.repository, value["chat_id"], value.get("references", ())
            )
            conditions += submission_selection_conditions(
                self.repository, value["chat_id"], value.get("source_snapshot_ids", ())
            )
            source_locators = submission_source_locators(
                self.repository, value["chat_id"], value.get("attachment_ids", ())
            )
            if value.get("question_video") is not None:
                from learning_app.workbench.question_context import question_video_locator

                source_locators.append(
                    question_video_locator(
                        self.repository, value["chat_id"], value["question_video"]
                    )
                )
            result = self.repository.publish_submission(
                value["chat_id"],
                value["text"],
                value["submission_id"],
                attachment_ids=value.get("attachment_ids", ()),
                conditions=conditions,
                corrections=value.get("corrections", ()) if existing_submission else (),
                source_locators=source_locators,
                read_course_ids=value.get("read_course_ids", ()),
                model=value["model"],
                thinking=value["thinking"],
                max_output_tokens=self.config.max_output_tokens,
                timeout_seconds=self.config.model_timeout_seconds,
            )
            self.faults.hit("after_submission_committed_before_dispatch", result)
            if result["created"]:
                self._schedule(result["generation_id"], self._dispatch(result["generation_id"]))
            return result

    async def _dispatch(self, generation_id: str):
        self.imported.require_fresh("generation", generation_id)
        generation = self.repository.get_generation(generation_id)
        chat_id = generation["chat_id"]
        native = getattr(self.execution, "native_tools", None)
        if native is not None:
            for scope in native.port.unreleased_scopes("chat"):
                if self.imported.contains("run", scope["run_id"]) or self.imported.contains(
                    "generation", scope["run_id"]
                ):
                    continue
                if (
                    scope["business_binding"]["conversation_id"] != chat_id
                    or scope["run_id"] == generation_id
                ):
                    continue
                previous = self.tasks.get(scope["run_id"])
                if (
                    previous is not None
                    and previous is not asyncio.current_task()
                    and not previous.done()
                ):
                    await asyncio.shield(previous)
                if not await self.release_native_execution(
                    ExecutionBinding.from_dict(
                        {**scope["business_binding"], "native_lease_epoch": scope["native_epoch"]}
                    )
                ):
                    self.repository.fail_pending(generation_id, "NATIVE_PREVIOUS_NOT_RELEASED")
                    return
        async with self.locks[chat_id]:
            generation = self.repository.get_generation(generation_id)
            if generation["state"] != "pending":
                return
            try:
                context = self.repository.execution_context(generation_id)
                current = self.repository.current_binding(chat_id)
                revision = self.repository.get_chat(chat_id).get("history_revision", 0)
                rebuild = current is None or current["state"] == "stale"
                epoch = (current["epoch"] + 1 if current else 1) if rebuild else current["epoch"]
                binding = await self.execution.prepare(
                    chat_id,
                    generation["submission_id"],
                    generation_id,
                    epoch,
                    **(
                        {"session_id": current["session_id"]}
                        if native is not None and current and not rebuild
                        else {}
                    ),
                )
                db_binding = self.repository.bind_execution(
                    chat_id,
                    binding.session_id,
                    adapter_metadata={
                        "history_revision": revision,
                        **(
                            {
                                key: value
                                for key, value in current.get("adapter_metadata", {}).items()
                                if key.startswith("desktop_") and key != "desktop_pending_history"
                            }
                            if current
                            else {}
                        ),
                    },
                )
                binding.epoch = db_binding["epoch"]
                self.faults.hit("before_dispatch_claim", {"generation_id": generation_id})
                claimed = self.repository.claim_dispatch(
                    generation_id,
                    db_binding["binding_id"],
                    binding.epoch,
                    adapter_metadata=binding.to_dict(),
                    request_id=binding.request_id,
                    host_instance_id=self.config.instance_id,
                )
                if not claimed:
                    return
                self._binding_ids[generation_id] = db_binding["binding_id"]
                self._binding_snapshots[generation_id] = json.dumps(
                    binding.to_dict(), sort_keys=True
                )
                native = getattr(self.execution, "native_tools", None)
                execution_arguments = {}
                if native is not None:
                    scope = native.port.ensure_chat_scope(generation_id, binding)
                    self.execution.configure_business_scope(binding, scope)
                    execution_arguments["policy"] = ExecutionPolicy(
                        generation["model"],
                        generation["thinking"],
                        generation["max_output_tokens"],
                        generation["timeout_seconds"],
                        allowed_tool_names=tuple(scope["allowed_tools"]),
                    )
                prepared_images, document_text = await asyncio.to_thread(
                    expand_document_parts,
                    self.repository,
                    generation["chat_id"],
                    context.get("attachments", []),
                )
                attachments = [
                    ApprovedAttachment(
                        attachment_id=item["attachment_id"],
                        path=item["path"],
                        sha256=item.get("sha256", item.get("content_hash", "")),
                        media_type=item["media_type"],
                    )
                    for item in prepared_images
                ]
                prompt = context.get("prompt", generation.get("text", ""))
                if document_text:
                    prompt += "\n\n" + document_text
                needs_history = rebuild or bool(
                    current and current.get("adapter_metadata", {}).get("desktop_pending_history")
                )
                history = context.get("history_context") if needs_history else None
                if isinstance(history, (list, dict)):
                    history = json.dumps(history, ensure_ascii=False)
                binding = await self.execution.start(
                    binding,
                    prompt,
                    attachments,
                    model=generation["model"],
                    thinking=generation["thinking"],
                    history_context=history,
                    **execution_arguments,
                )
                self.faults.hit("after_host_accept_before_bind_ack", binding.to_dict())
                self._persist_binding(binding)
                if not binding.accepted:
                    self.repository.mark_unknown(generation_id, "ACCEPTANCE_UNKNOWN")
                    return
                self.repository.record_receipt(generation_id, binding.request_id)
            except (ExecutionError, StorageError, AppError, OSError, TimeoutError) as exc:
                if self.repository.get_generation(generation_id)["state"] == "pending":
                    self.repository.fail_pending(
                        generation_id, getattr(exc, "code", "DISPATCH_NOT_STARTED")
                    )
                else:
                    self._sync_attempts(generation_id)
                    self.repository.mark_unknown(
                        generation_id, getattr(exc, "code", "DISPATCH_UNCERTAIN")
                    )
                return
        await self._observe(binding)

    async def _observe(self, binding: ExecutionBinding):
        queue: asyncio.Queue[NormalizedEvent | Exception | None] = asyncio.Queue(maxsize=256)

        async def receive():
            try:
                watermark = self.repository.get_generation(binding.generation_id)["event_watermark"]
                async for item in self.execution.observe(binding, after=watermark):
                    await queue.put(item)
            except Exception as exc:
                # Transfer the reader failure to the sole state-writing consumer.
                await queue.put(exc)
            else:
                await queue.put(None)

        reader = asyncio.create_task(receive(), name=f"execution-reader:{binding.generation_id}")
        try:
            ended = False
            deferred_error = None
            while not ended:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                batch = [item]
                # Keep durable visible progress within 25ms while amortizing FULL
                # SQLite fsync over a bounded token burst. Every event stays intact.
                deadline = asyncio.get_running_loop().time() + 0.025
                if item.kind in {"text_delta", "reasoning_delta"}:
                    while len(batch) < 32:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        try:
                            following = await asyncio.wait_for(queue.get(), remaining)
                        except TimeoutError:
                            break
                        if following is None:
                            ended = True
                            break
                        if isinstance(following, Exception):
                            deferred_error = following
                            ended = True
                            break
                        batch.append(following)
                        if following.kind not in {"text_delta", "reasoning_delta"}:
                            break
                self._ingest_batch(binding, batch)
                await asyncio.sleep(0)
            if deferred_error is not None:
                raise deferred_error
        except (ExecutionError, StorageError, OSError, TimeoutError) as exc:
            logger.warning(
                "Execution observation remains unknown: %s",
                getattr(exc, "code", type(exc).__name__),
            )
            self.repository.mark_unknown(binding.generation_id, "OBSERVATION_DISCONNECTED")
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        await self.release_native_execution(binding)

    async def release_native_execution(self, binding: ExecutionBinding) -> bool:
        if getattr(self.execution, "native_tools", None) is None:
            return True
        try:
            await self.execution.release_execution(binding)
            return True
        except (
            ExecutionError,
            ToolRejected,
            AppError,
            httpx.HTTPError,
            OSError,
            TimeoutError,
        ) as exc:
            logger.warning(
                "Native execution remains reserved: %s",
                getattr(exc, "code", "NATIVE_RELEASE_UNCONFIRMED"),
            )
            return False

    def _ingest(self, binding, event):
        self._ingest_batch(binding, [event])

    def _ingest_batch(self, binding: ExecutionBinding, events: list[NormalizedEvent]):
        for event in events:
            if event.kind == "terminal":
                self.faults.hit("after_terminal_observed_before_ingest", event.to_dict())
        self._sync_attempts(binding.generation_id)
        binding_id = self._binding_ids.get(binding.generation_id)
        if binding_id is None:
            binding_id = self.repository.get_generation(binding.generation_id)["binding_id"]
            self._binding_ids[binding.generation_id] = binding_id
        rows = []
        for event in events:
            kind = {
                "text_delta": "body",
                "reasoning_delta": "reasoning",
                "accepted": "receipt",
                "activity": "activity",
                "snapshot": "snapshot",
                "terminal": "terminal",
                "usage": "usage",
                "unknown": "unknown",
            }[event.kind]
            rows.append(
                {
                    "generation_id": binding.generation_id,
                    "binding_id": binding_id,
                    "epoch": event.epoch,
                    "event_id": event.event_id,
                    "seq": event.seq,
                    "kind": kind,
                    "text": event.text,
                    "status": event.status,
                    "raw_usage": event.raw_usage,
                    "usage_source": "provider_boundary",
                    "usage_fields": event.usage_presence,
                    "settlement_id": event.data.get("settlement_id", event.event_id),
                    "data": {
                        **event.data,
                        **({"body": event.text} if kind == "snapshot" else {}),
                        "native_seq": event.native_seq,
                        "attempt_id": event.attempt_id,
                    },
                }
            )
        self.repository.ingest_events(rows)
        self._persist_binding(binding)

    def _persist_binding(self, binding):
        value = binding.to_dict()
        snapshot = json.dumps(value, sort_keys=True)
        if self._binding_snapshots.get(binding.generation_id) != snapshot:
            self.repository.save_adapter_metadata(binding.generation_id, value)
            self._binding_snapshots[binding.generation_id] = snapshot

    def _sync_attempts(self, generation_id: str):
        for attempt in self.execution.attempts_for(generation_id):
            identity = tuple(
                attempt.get(key)
                for key in ("actual_outbound", "request_id", "model", "thinking", "kind")
            )
            if self._attempt_snapshots.get(attempt["attempt_id"]) == identity:
                continue
            if attempt.get("actual_outbound") or attempt.get("status"):
                self.repository.record_outbound_attempt(
                    generation_id,
                    attempt["attempt_id"],
                    kind=attempt["kind"],
                    request_id=attempt["request_id"],
                    model=attempt["model"],
                    thinking=attempt["thinking"],
                    actual_outbound=attempt["actual_outbound"],
                )
                self._attempt_snapshots[attempt["attempt_id"]] = identity

    async def cancel(self, generation_id: str):
        self.imported.require_fresh("generation", generation_id)
        generation = self.repository.get_generation(generation_id)
        if generation["state"] in TERMINAL:
            return
        self.repository.request_cancel(generation_id)
        if not self.execution_ready:
            self.repository.mark_unknown(generation_id, "CANCEL_NOT_CONFIRMED")
            return
        binding = self._binding_for(generation_id)
        if binding is None:
            return
        try:
            await self.execution.cancel(binding)
        except (ExecutionError, OSError, TimeoutError) as exc:
            logger.warning(
                "Cancel not confirmed for %s: %s",
                generation_id,
                getattr(exc, "code", type(exc).__name__),
            )
            self.repository.mark_unknown(generation_id, "CANCEL_NOT_CONFIRMED")
            return
        # Acceptance of a cancel is an intent; the durable Host terminal decides outcome.
        task = self.tasks.get(generation_id)
        if task is None or task.done():
            await self.reconcile(generation_id, resume_observer=True)

    async def reconcile(self, generation_id: str, *, resume_observer: bool = True):
        self.imported.require_fresh("generation", generation_id)
        if not self.execution_ready:
            return
        binding = self._binding_for(generation_id)
        if binding is None:
            return
        observer = self.tasks.get(generation_id)
        if observer is not None and observer is not asyncio.current_task() and not observer.done():
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        async with self.locks[binding.conversation_id]:
            self._sync_attempts(generation_id)
            watermark = self.repository.get_generation(generation_id)["event_watermark"]
            try:
                result = await self.execution.reconcile(binding, after=watermark)
            except (ExecutionError, OSError, TimeoutError):
                self.repository.mark_unknown(generation_id, "RECONCILIATION_UNAVAILABLE")
                return
            for event in result["events"]:
                self._ingest(binding, event)
            self._persist_binding(result["binding"])
            if result["status"] == "unknown":
                self.repository.mark_unknown(generation_id, "RECONCILIATION_UNKNOWN")
        if result["status"] in TERMINAL:
            await self.release_native_execution(result["binding"])
        if resume_observer and result["status"] in {"active", "accepted"}:
            if asyncio.current_task() is self.tasks.get(generation_id):
                await self._observe(result["binding"])
            else:
                self._schedule(generation_id, self._observe(result["binding"]))

    def _binding_for(self, generation_id: str):
        generation = self.repository.get_generation(generation_id)
        value = generation.get("adapter_metadata")
        return ExecutionBinding.from_dict(value) if value else None

    def generation_view(self, generation_id: str) -> dict:
        value = self.repository.get_generation(generation_id)
        records = value.get("usage", [])
        if isinstance(records, list):
            # Each settlement is immutable. Missing counters stay unknown, even after text completes.
            raw = (
                records[0]["raw_usage"]
                if len(records) == 1
                else (
                    {
                        "settlements": [
                            {"settlement_id": item["settlement_id"], "raw": item["raw_usage"]}
                            for item in records
                        ]
                    }
                    if records
                    else None
                )
            )
            keys = {key for item in records for key in item["field_presence"]}
            fields = {
                key: all(item["field_presence"].get(key, False) for item in records) for key in keys
            }

            def total(key):
                values = [item.get("normalized", {}).get(key) for item in records]
                return (
                    sum(values) if values and all(type(value) is int for value in values) else None
                )

            input_tokens, output_tokens = total("input_tokens"), total("output_tokens")
            usage = {
                "status": "known"
                if input_tokens is not None and output_tokens is not None
                else "unknown",
                "raw": raw,
                "fields": fields,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        else:
            usage = records
        return {
            "generation_id": value["generation_id"],
            "submission_id": value["submission_id"],
            "chat_id": value["chat_id"],
            "state": value["state"],
            "body": value["body"],
            "reasoning": value["reasoning"],
            "cancel_requested": value["cancel_requested"],
            "event_watermark": value.get("event_watermark", value.get("last_event_watermark", 0)),
            "usage": usage,
            "error_code": value.get("unknown_reason"),
        }

    def chat_view(self, chat_id: str) -> dict:
        value = self.repository.get_chat(chat_id)
        generations = value.get("generations", [])
        value["generations"] = [self.generation_view(item["generation_id"]) for item in generations]
        indexed = {item["generation_id"]: item for item in value["generations"]}
        for message in value.get("messages", []):
            message["references"] = message_references(message.get("conditions", ()))
            message["source_snapshot_ids"] = message_selection_ids(message.get("conditions", ()))
            generation = indexed.get(message.get("generation_id"))
            if generation:
                message["reasoning"] = generation.get("reasoning", "")
        return value
