"""Serialize application submissions and reconcile execution without resending unknown work."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from dotenv import dotenv_values

from learning_app.config import AppConfig
from learning_app.drafts import submission_source_locators
from learning_app.errors import AppError
from learning_app.execution.selection import supports_images, validate_selection
from learning_app.execution.types import (
    ApprovedAttachment,
    ExecutionBinding,
    ExecutionError,
    NormalizedEvent,
)
from learning_app.faults import Faults
from learning_app.storage import StorageError

TERMINAL = {"completed", "cancelled", "failed", "truncated"}
logger = logging.getLogger(__name__)


class GenerationService:
    def __init__(self, repository, config: AppConfig, *, execution=None, fault_hook=None):
        self.repository = repository
        self.config = config
        self.execution = execution
        self.owns_execution = execution is None
        self.execution_ready = False
        self.faults = Faults(fault_hook)
        self.tasks: dict[str, asyncio.Task] = {}
        self.locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.execution_error: str | None = None
        self.cleanup: dict = {}
        self._binding_ids: dict[str, str] = {}
        self._binding_snapshots: dict[str, str] = {}
        self._attempt_snapshots: dict[str, tuple] = {}

    async def open(self):
        if self.execution is None and self.config.harness_enabled:
            from learning_app.execution.adapter import ExecutionAdapter

            key = dotenv_values(self.config.secret_file).get("DEEPSEEK_API_KEY")
            if not key:
                self.execution_error = "MODEL_KEY_NOT_CONFIGURED"
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
            )
        if self.execution is None:
            return
        try:
            if self.owns_execution:
                await self.execution.open()
            self.execution_ready = True
        except (ExecutionError, OSError, TimeoutError) as exc:
            self.execution_error = (
                exc.code if isinstance(exc, ExecutionError) else "EXECUTION_START_FAILED"
            )
            logger.error("Harness initialization failed: %s", self.execution_error)
            return
        await self.recover()

    async def recover(self):
        for generation in self.repository.recoverable_generations():
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

    async def close(self):
        # Stop only this service's readers; unknown work is never converted back to pending.
        for generation_id, task in self.tasks.items():
            if not task.done():
                state = self.repository.get_generation(generation_id)["state"]
                if state not in TERMINAL and state != "pending":
                    self.repository.mark_unknown(generation_id, "SERVICE_SHUTDOWN")
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if self.execution is not None and self.owns_execution:
            self.cleanup = await self.execution.close()
        self.execution_ready = False

    async def submit(self, value: dict) -> dict:
        if not self.execution_ready:
            raise AppError("EXECUTION_UNAVAILABLE", "Harness尚未连接，未提交问题", 503)
        try:
            validate_selection(self.config.model, value["model"], value["thinking"])
        except ExecutionError as exc:
            raise AppError(exc.code, "当前执行层不支持所选模型或思考强度，请更改选择", 422) from exc
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
            # Capability changes in later turns cannot invalidate an earlier
            # receipt. publish_submission still checks its full immutable hash.
            if not existing_submission and not supports_images(value["model"]):
                history = self.repository.get_chat(value["chat_id"])["messages"]
                if value.get("attachment_ids") or any(m.get("attachment_ids") for m in history):
                    raise AppError(
                        "MODEL_IMAGE_UNSUPPORTED",
                        "所选模型仅支持文字；当前图片及题图历史已保留，请选择支持图片的模型或新建文字对话",
                        422,
                    )
            source_locators = submission_source_locators(
                self.repository, value["chat_id"], value.get("attachment_ids", ())
            )
            result = self.repository.publish_submission(
                value["chat_id"],
                value["text"],
                value["submission_id"],
                attachment_ids=value.get("attachment_ids", ()),
                conditions=value.get("conditions", ()),
                corrections=value.get("corrections", ()),
                source_locators=source_locators,
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
        generation = self.repository.get_generation(generation_id)
        chat_id = generation["chat_id"]
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
                )
                db_binding = self.repository.bind_execution(
                    chat_id,
                    binding.session_id,
                    adapter_metadata={"history_revision": revision},
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
                attachments = [
                    ApprovedAttachment(
                        attachment_id=item["attachment_id"],
                        path=item["path"],
                        sha256=item.get("sha256", item.get("content_hash", "")),
                        media_type=item["media_type"],
                    )
                    for item in context.get("attachments", [])
                ]
                prompt = context.get("prompt", generation.get("text", ""))
                history = context.get("history_context") if rebuild else None
                if isinstance(history, (list, dict)):
                    history = json.dumps(history, ensure_ascii=False)
                binding = await self.execution.start(
                    binding,
                    prompt,
                    attachments,
                    model=generation["model"],
                    thinking=generation["thinking"],
                    history_context=history,
                )
                self.faults.hit("after_host_accept_before_bind_ack", binding.to_dict())
                self._persist_binding(binding)
                if not binding.accepted:
                    self.repository.mark_unknown(generation_id, "ACCEPTANCE_UNKNOWN")
                    return
                self.repository.record_receipt(generation_id, binding.request_id)
            except (ExecutionError, StorageError, OSError, TimeoutError) as exc:
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
        except (ExecutionError, StorageError, OSError, TimeoutError):
            self.repository.mark_unknown(binding.generation_id, "OBSERVATION_DISCONNECTED")
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

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
                "text_delta": "body", "reasoning_delta": "reasoning", "accepted": "receipt",
                "activity": "activity", "snapshot": "snapshot", "terminal": "terminal",
                "usage": "usage", "unknown": "unknown",
            }[event.kind]
            rows.append({
                "generation_id": binding.generation_id, "binding_id": binding_id,
                "epoch": event.epoch, "event_id": event.event_id, "seq": event.seq,
                "kind": kind, "text": event.text, "status": event.status,
                "raw_usage": event.raw_usage, "usage_source": "provider_boundary",
                "usage_fields": event.usage_presence,
                "settlement_id": event.data.get("settlement_id", event.event_id),
                "data": {
                    **event.data, **({"body": event.text} if kind == "snapshot" else {}),
                    "native_seq": event.native_seq, "attempt_id": event.attempt_id,
                },
            })
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
            generation = indexed.get(message.get("generation_id"))
            if generation:
                message["reasoning"] = generation.get("reasoning", "")
        return value
