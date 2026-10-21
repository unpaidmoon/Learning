"""Application execution seam; one submit never implicitly becomes another prompt."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import AsyncIterator

import httpx

from .boundary import MessagesBoundary
from .host import HarnessHost
from .remote import RemotePeer
from .selection import native_thinking, supports_images, validate_selection
from .types import ApprovedAttachment, ExecutionBinding, ExecutionError, NormalizedEvent

TERMINAL_STATUS = {
    "completed": "completed",
    "end-turn": "completed",
    "stop": "completed",
    "aborted": "cancelled",
    "max-tokens": "truncated",
    "error": "failed",
}


def _events(snapshot: dict) -> list[dict]:
    return [
        record["event"] for record in snapshot.get("records", []) if record.get("type") == "event"
    ]


def _content(value) -> tuple[str, str]:
    """Extract detached accumulator deltas without counting usage/finish as text."""
    text, reasoning = [], []

    def visit(item):
        if isinstance(item, dict):
            kind = item.get("type")
            if kind in {"text-chunks", "reasoning-chunks"}:
                target = text if kind == "text-chunks" else reasoning
                target.extend(piece for piece in item.get("texts", []) if isinstance(piece, str))
                return
            if kind in {"text-delta", "text"} and isinstance(item.get("text"), str):
                text.append(item["text"])
                return
            if kind in {"reasoning-delta", "reasoning"} and isinstance(item.get("text"), str):
                reasoning.append(item["text"])
                return
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return "".join(text), "".join(reasoning)


class ExecutionAdapter:
    def __init__(
        self,
        repository: Path,
        run_dir: Path,
        *,
        mode: str = "fixture",
        api_key: str | None = None,
        model: str = "deepseek-flash",
        thinking: str = "high",
        max_tokens: int = 16384,
        timeout: float = 240,
        boundary: MessagesBoundary | None = None,
        credential_route: str = "api_key",
        account_home: Path | None = None,
    ) -> None:
        self.repository, self.run_dir = Path(repository), Path(run_dir)
        self.model, self.thinking = model, thinking
        self.timeout = timeout
        self.mode, self.credential_route = mode, credential_route
        self._account_models: list[dict] = []
        self._account_signed_in = False
        self.boundary = boundary or MessagesBoundary(
            self.run_dir / "boundary",
            mode=mode,
            api_key=api_key,
            model=model,
            thinking=thinking,
            max_tokens=max_tokens,
            timeout=timeout,
            credential_route=credential_route,
            account_enabled=mode == "official",
        )
        self.host = HarnessHost(
            self.repository,
            self.run_dir / "host",
            "",
            "",
            model=model,
            thinking=thinking,
            max_tokens=max_tokens,
            timeout=timeout,
            account_enabled=mode == "official",
            home_directory=account_home,
        )
        self.peer: RemotePeer | None = None
        self._streams: dict[str, str] = {}
        self._sequences: dict[str, int] = {}
        self._started: set[str] = set()
        self._attempts: dict[str, str] = {}
        self._chunk_floors: dict[str, int] = {}
        self._open = False
        self._image_sessions: set[str] = set()

    async def open(self) -> ExecutionAdapter:
        self.boundary.start()
        self.host.boundary_url, self.host.boundary_token = (
            self.boundary.url,
            self.boundary.local_token,
        )
        try:
            await self.host.start()
            self.peer = RemotePeer(self.host)
            await self.peer.connect()
            self._open = True
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self) -> dict:
        if self.peer is not None:
            await self.peer.close()
        host_cleanup = await self.host.stop()
        await asyncio.to_thread(self.boundary.stop)
        self._open = False
        return {"host": host_cleanup}

    async def __aenter__(self):
        return await self.open()

    async def __aexit__(self, *_args):
        await self.close()

    async def prepare(
        self,
        conversation_id: str,
        submission_id: str,
        generation_id: str,
        epoch: int = 1,
    ) -> ExecutionBinding:
        """Prepare identity and take a native cut, before business marks dispatching."""
        if not self.peer:
            raise ExecutionError("EXECUTION_NOT_OPEN")
        sid = "learning-" + hashlib.sha256(f"{conversation_id}:{epoch}".encode()).hexdigest()[:32]
        try:
            stream, snapshot = await self.peer.follow(sid)
        except ExecutionError as error:
            if error.code != "HARNESS_FOLLOW_NOT_FOUND":
                raise
            await self.peer.rpc(
                "session/create", {"cwd": str(self.host.workspace), "sessionId": sid}
            )
            stream, snapshot = await self.peer.follow(sid)
        if snapshot.get("assistantStream", {}).get("activeAttempt"):
            await self.peer.unfollow(stream)
            raise ExecutionError("CONVERSATION_ALREADY_ACTIVE")
        native_turn = max([e.get("data", {}).get("turn", 0) for e in _events(snapshot)] or [0]) + 1
        binding = ExecutionBinding(
            conversation_id,
            submission_id,
            generation_id,
            epoch,
            sid,
            submission_id,
            native_floor=snapshot["cursor"],
            native_turn=native_turn,
        )
        self._streams[generation_id] = stream
        return binding

    async def start(
        self,
        binding: ExecutionBinding | dict,
        prompt: str,
        attachments: list[ApprovedAttachment | dict] | None = None,
        *,
        model: str | None = None,
        thinking: str | None = None,
        history_context: str | None = None,
    ) -> ExecutionBinding:
        binding = self._binding(binding)
        if not self.peer:
            raise ExecutionError("EXECUTION_NOT_OPEN")
        if binding.generation_id in self._started or binding.status != "prepared":
            raise ExecutionError("GENERATION_ALREADY_DISPATCHED")
        selected_model, selected_thinking = model or self.model, thinking or self.thinking
        await self.validate_choice(selected_model, selected_thinking)
        if not self.supports_model_images(selected_model) and (
            attachments or binding.session_id in self._image_sessions
        ):
            raise ExecutionError("MODEL_IMAGE_UNSUPPORTED")
        # Durable routing marker is part of the submitted text, not an extra
        # network/header protocol. Boundary resolves the latest user marker,
        # including tool continuations, instead of guessing by arrival order.
        marker = f"[[learning-generation:{binding.generation_id}]]\n"
        content = [{"type": "text", "text": marker}]
        if history_context:
            content.append(
                {
                    "type": "text",
                    "text": "已完成业务历史，仅作上下文，不重答旧问题：\n" + history_context,
                }
            )
        content.append({"type": "text", "text": prompt})
        for attachment in attachments or []:
            if isinstance(attachment, dict):
                attachment = ApprovedAttachment(**attachment)
            path = Path(attachment.path)
            if not path.is_file():
                raise ExecutionError("CONFIRMED_ATTACHMENT_MISSING")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != attachment.sha256:
                raise ExecutionError("CONFIRMED_ATTACHMENT_CHANGED")
            if attachment.media_type not in {"image/png", "image/jpeg", "image/webp"}:
                raise ExecutionError("ATTACHMENT_MEDIA_UNSUPPORTED")
            content.append(
                {
                    "type": "image",
                    "mediaType": attachment.media_type,
                    "data": base64.b64encode(data).decode("ascii"),
                }
            )
        await self.peer.rpc(
            "session/selectModel",
            {
                "sessionId": binding.session_id,
                "provider": "deepseek-account" if self.mode == "official" and self.credential_route == "account" else "deepseek-official",
                "model": selected_model,
                "reasoningEffort": native_thinking(selected_thinking),
            },
        )
        self.boundary.bind(
            binding.generation_id, model=selected_model, thinking=selected_thinking
        )
        if attachments:
            self._image_sessions.add(binding.session_id)
        self._started.add(binding.generation_id)
        binding.status = "dispatching"
        try:
            receipt = await self.peer.rpc(
                "session/prompt",
                {
                    "requestId": binding.request_id,
                    "sessionId": binding.session_id,
                    "mode": "queue",
                    "content": content,
                    "clientTimeZone": "Asia/Shanghai",
                },
            )
            binding.accepted = receipt.get("accepted") is True
            binding.status = "accepted" if binding.accepted else "unknown"
        except (httpx.TransportError, asyncio.TimeoutError):
            binding.status = "unknown"
        return binding

    async def account_state(self) -> dict:
        if not self.peer or self.mode != "official":
            raise ExecutionError("ACCOUNT_NOT_AVAILABLE")
        try:
            state = await self.peer.rpc_args("account/getState", {})
        except (ExecutionError, httpx.HTTPError, OSError, TimeoutError):
            self._account_signed_in = False
            self._account_models = []
            raise
        self._account_signed_in = state.get("status") == "credential-stored"
        if not self._account_signed_in:
            self._account_models = []
        return state

    @property
    def api_key_configured(self) -> bool:
        return self.boundary.api_key_configured

    @property
    def question_answering_ready(self) -> bool:
        """Known route capability only; this property never performs IO."""
        if not self._open:
            return False
        if self.mode == "fixture":
            return True
        if self.credential_route == "api_key":
            return self.api_key_configured
        return (
            self._account_signed_in
            and bool(self._account_models)
            and not self.boundary.account_token_rejected
        )

    async def refresh_account_models(self) -> list[dict]:
        if not self.peer:
            raise ExecutionError("EXECUTION_NOT_OPEN")
        # A failed refresh cannot leave an old success advertised as the result
        # of the newly requested capability check.
        self._account_models = []
        view = await self.account_state()
        if view.get("status") != "credential-stored":
            self._account_models = []
            raise ExecutionError("ACCOUNT_SIGN_IN_REQUIRED")
        catalog = await self.peer.rpc_args("session/modelCatalog", {})
        group = next((item for item in catalog.get("groups", []) if item.get("id") == "deepseek-account"), None)
        if not self.boundary.account_catalog_observed or group is None:
            self._account_models = []
            raise ExecutionError("ACCOUNT_CATALOG_UNAVAILABLE")
        choices = []
        for model in group.get("models", []):
            model_id = model.get("id")
            if model_id not in self.boundary.account_model_modalities:
                continue
            efforts = model.get("reasoning", {}).get("efforts", [])
            levels = ["none" if effort.get("id") == "off" else effort.get("id") for effort in efforts]
            choices.append({
                "id": model_id,
                "name": model.get("name", model_id),
                "supports_images": "image" in self.boundary.account_model_modalities[model_id],
                "thinking_levels": [level for level in levels if level in {"none", "low", "high", "max"}],
            })
        self._account_models = choices
        return choices

    def supports_model_images(self, model: str) -> bool:
        if self.mode == "official" and self.credential_route == "account":
            return any(choice["id"] == model and choice["supports_images"] for choice in self._account_models)
        return supports_images(model)

    async def validate_choice(self, model: str, thinking: str) -> None:
        if self.mode != "official" or self.credential_route != "account":
            if self.mode == "official" and not self.api_key_configured:
                raise ExecutionError("MODEL_KEY_NOT_CONFIGURED")
            validate_selection(self.model, model, thinking)
            return
        state = await self.account_state()
        if state.get("status") != "credential-stored":
            raise ExecutionError("ACCOUNT_SIGN_IN_REQUIRED")
        if not self._account_models:
            await self.refresh_account_models()
        choice = next((choice for choice in self._account_models if choice["id"] == model), None)
        if choice is None:
            raise ExecutionError("MODEL_SELECTION_UNAVAILABLE")
        if thinking not in choice["thinking_levels"]:
            raise ExecutionError("THINKING_SELECTION_UNAVAILABLE")

    def set_credential_route(self, route: str) -> None:
        if route not in {"account", "api_key"}:
            raise ExecutionError("CREDENTIAL_ROUTE_INVALID")
        if route == "api_key" and self.mode == "official" and not self.api_key_configured:
            raise ExecutionError("MODEL_KEY_NOT_CONFIGURED")
        self.credential_route = route
        self.boundary.credential_route = route

    def _event(self, binding, event_id, kind, **kwargs) -> NormalizedEvent:
        number = self._sequences.get(binding.generation_id, 0) + 1
        self._sequences[binding.generation_id] = number
        return NormalizedEvent(
            event_id, number, binding.generation_id, binding.epoch, kind, **kwargs
        )

    async def observe(
        self,
        binding: ExecutionBinding | dict,
        after: int = 0,
    ) -> AsyncIterator[NormalizedEvent]:
        binding = self._binding(binding)
        self._sequences[binding.generation_id] = max(
            self._sequences.get(binding.generation_id, 0), after
        )
        if not self.peer:
            raise ExecutionError("EXECUTION_NOT_OPEN")
        if binding.generation_id not in self._streams:
            result = await self.reconcile(binding, after)
            for event in result["events"]:
                yield event
            if result["status"] not in {"active", "accepted"}:
                return
        stream = self._streams[binding.generation_id]
        queue = self.peer.streams[stream]
        if binding.accepted:
            yield self._event(
                binding, f"{binding.request_id}:accepted", "accepted", status="accepted"
            )
        async with asyncio.timeout(self.timeout + 15):
            while True:
                wire = await queue.get()
                if wire.get("type") == "disconnected":
                    yield self._event(
                        binding,
                        f"{binding.request_id}:observation-disconnected",
                        "unknown",
                        status="unknown",
                    )
                    return
                value = wire.get("value", {})
                if value.get("type") == "assistant-stream":
                    frame = value["frame"]
                    attempt = frame.get("attemptId")
                    if frame.get("type") == "start":
                        if frame.get("turn") != binding.native_turn:
                            continue
                        self._attempts[binding.generation_id] = attempt
                        self._chunk_floors[binding.generation_id] = 0
                        yield self._event(
                            binding,
                            f"{binding.session_id}:{attempt}:start",
                            "activity",
                            status="active",
                            attempt_id=attempt,
                        )
                    elif frame.get("type") == "chunk":
                        if attempt != self._attempts.get(binding.generation_id):
                            continue
                        floor = self._chunk_floors.get(binding.generation_id, 0)
                        if frame.get("index", -1) < floor:
                            continue
                        self._chunk_floors[binding.generation_id] = frame["index"] + 1
                        chunk = frame.get("chunk", {})
                        kind = {
                            "text-delta": "text_delta",
                            "reasoning-delta": "reasoning_delta",
                        }.get(chunk.get("type"))
                        if kind and chunk.get("text"):
                            yield self._event(
                                binding,
                                f"{binding.session_id}:{attempt}:chunk:{frame['index']}",
                                kind,
                                text=chunk["text"],
                                status="active",
                                attempt_id=attempt,
                            )
                elif value.get("type") == "event":
                    native = value["event"]
                    if native.get("seq", -1) <= binding.native_floor:
                        continue
                    if (
                        native.get("type") == "turn/end"
                        and native.get("data", {}).get("turn") == binding.native_turn
                    ):
                        reason = native["data"].get("reason", {})
                        status = TERMINAL_STATUS.get(
                            reason.get("kind") if isinstance(reason, dict) else reason, "unknown"
                        )
                        binding.status = status
                        yield self._event(
                            binding,
                            f"{binding.session_id}:event:{native['seq']}",
                            "terminal",
                            status=status,
                            native_seq=native["seq"],
                            data={"reason": reason},
                        )
                        await self.peer.unfollow(stream)
                        self._streams.pop(binding.generation_id, None)
                        for event in await self._usage(binding):
                            yield event
                        return

    async def _usage(self, binding) -> list[NormalizedEvent]:
        # Provider terminal may reach durable Host just before boundary handler finalizes.
        for _ in range(200):
            records = [
                r for r in self.boundary.records if r.get("generation_id") == binding.generation_id
            ]
            if not records or all(r.get("finished_ns") for r in records):
                break
            await asyncio.sleep(0.01)
        result = []
        for record in records:
            usage = record.get("usage", {})
            observation = hashlib.sha256(
                json.dumps(usage, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:24]
            settlement = f"provider:{record['request_id']}:usage"
            result.append(
                self._event(
                    binding,
                    f"{settlement}:observation:{observation}",
                    "usage",
                    raw_usage=usage,
                    usage_presence=usage.get("field_presence", {}),
                    data={
                        "attempt": record.get("actual_outbound_attempt"),
                        "request_id": record["request_id"],
                        "settlement_id": settlement,
                    },
                )
            )
        return result

    async def cancel(self, binding: ExecutionBinding | dict) -> dict:
        binding = self._binding(binding)
        if not self.peer:
            raise ExecutionError("EXECUTION_NOT_OPEN")
        # This is the single-session execution cancel, never observation cancellation.
        return await self.peer.rpc("session/cancel", {"sessionId": binding.session_id})

    def attempts_for(self, generation_id: str) -> list[dict]:
        """Return durable counted transport facts without exposing provider payloads."""
        return [
            {
                "attempt_id": record["request_id"],
                "request_id": record["request_id"],
                "generation_id": generation_id,
                "model": record.get("model"),
                "thinking": "none"
                if (record.get("thinking") or {}).get("type") == "disabled"
                else (record.get("output_config") or {}).get("effort"),
                "kind": "model",
                "actual_outbound": record.get("actual_outbound_attempt") is not None,
                "actual_outbound_attempt": record.get("actual_outbound_attempt"),
                "status": record.get("status"),
                "received_ns": record.get("received_ns"),
                "finished_ns": record.get("finished_ns"),
                "request_sha256": record.get("body_sha256"),
            }
            for record in self.boundary.records
            if record.get("generation_id") == generation_id
        ]

    async def reconcile(self, binding: ExecutionBinding | dict, after: int = 0) -> dict:
        binding = self._binding(binding)
        self._sequences[binding.generation_id] = max(
            self._sequences.get(binding.generation_id, 0), after
        )
        if not self.peer:
            raise ExecutionError("EXECUTION_NOT_OPEN")
        if self.peer.ws is None or self.peer.reader is None or self.peer.reader.done():
            await self.peer.disconnect()
            await self.peer.connect()
        try:
            stream, snapshot = await self.peer.follow(binding.session_id)
            page = await self.peer.page(binding.session_id, snapshot)
        except ExecutionError:
            return {
                "status": "unknown",
                "binding": binding,
                "events": [],
                "watermark": binding.native_floor,
            }
        old_stream = self._streams.get(binding.generation_id)
        if old_stream:
            await self.peer.unfollow(old_stream)
        self._streams[binding.generation_id] = stream
        native = [e for e in _events(page) if e.get("seq", -1) > binding.native_floor]
        terminals = [
            e
            for e in native
            if e["type"] == "turn/end" and e.get("data", {}).get("turn") == binding.native_turn
        ]
        accepted = any(
            e.get("type") == "user/message"
            and e.get("data", {}).get("source", {}).get("rpcId") == binding.request_id
            for e in native
        )
        # Also inspect admission inbox: prompt may be accepted but not yet consumed.
        for event in native:
            for item in event.get("data", {}).get("inserted", []):
                accepted |= item.get("source", {}).get("rpcId") == binding.request_id
        active = snapshot.get("assistantStream", {}).get("activeAttempt", {})
        active_here = active.get("turn") == binding.native_turn
        if active_here:
            self._attempts[binding.generation_id] = active["attemptId"]
            self._chunk_floors[binding.generation_id] = active.get("nextIndex", 0)
        status = "active" if active_here else ("accepted" if accepted else "unknown")
        result_events = []
        body, reasoning = [], []
        for event in native:
            if (
                event["type"] == "assistant/message"
                and event.get("data", {}).get("turn") == binding.native_turn
            ):
                text, thought = _content(event.get("data", {}).get("message", {}))
                body.append(text)
                reasoning.append(thought)
        if active_here:
            text, thought = _content(active.get("stream", []))
            body.append(text)
            reasoning.append(thought)
        if body or reasoning:
            result_events.append(
                self._event(
                    binding,
                    f"{binding.request_id}:snapshot:{snapshot['cursor']}:{active.get('nextIndex', 0)}",
                    "snapshot",
                    text="".join(body),
                    data={"reasoning": "".join(reasoning)},
                    native_seq=snapshot["cursor"],
                )
            )
        if terminals:
            terminal = terminals[-1]
            reason = terminal["data"].get("reason", {})
            status = TERMINAL_STATUS.get(
                reason.get("kind") if isinstance(reason, dict) else reason, "unknown"
            )
            result_events.append(
                self._event(
                    binding,
                    f"{binding.session_id}:event:{terminal['seq']}",
                    "terminal",
                    status=status,
                    native_seq=terminal["seq"],
                    data={"reason": reason},
                )
            )
            await self.peer.unfollow(stream)
            self._streams.pop(binding.generation_id, None)
            result_events.extend(await self._usage(binding))
        binding.accepted |= accepted
        binding.status = status
        return {
            "status": status,
            "binding": binding,
            "events": result_events,
            "watermark": snapshot["cursor"],
            "accepted": accepted,
            "active_attempt": active.get("attemptId"),
        }

    @staticmethod
    def _binding(value) -> ExecutionBinding:
        return ExecutionBinding.from_dict(value) if isinstance(value, dict) else value
