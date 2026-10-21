"""V2-14 adjacent regression: buffered reasoning must not starve real HTTP cancel.

This starts the pinned, real Host and the formal ASGI application on loopback.
The provider is local and cannot forward to the cloud. Its explicit gate holds
the active execution, while 1,000 actual SSE reasoning deltas create a backlog.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import time
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.host import free_port
from learning_app.storage import Repository

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.host
async def test_reasoning_backlog_keeps_real_http_cancel_responsive() -> None:
    directory = ROOT / ".runtime/validation" / f"v2-http-backlog-{uuid.uuid4().hex[:12]}"
    directory.mkdir(parents=True, exist_ok=False)
    service_path = ROOT / "backend/src/learning_app/service.py"
    source_before = hashlib.sha256(service_path.read_bytes()).hexdigest()
    repository = Repository(directory / "business")
    repository.create_course("合成高频推理课程", course_id="course_backlog")
    repository.create_chat("course_backlog", chat_id="chat_backlog")
    port = free_port()
    adapter = ExecutionAdapter(ROOT, directory / "execution", mode="fixture", timeout=90)
    config = AppConfig(
        data_dir=directory / "business",
        runtime_dir=directory / "server",
        backend_port=port,
        mode="test",
        harness_enabled=False,
        max_output_tokens=16384,
        model_timeout_seconds=90,
    )
    app = create_app(config, repository=repository, execution=adapter)
    first_reasoning = asyncio.Event()
    terminal = asyncio.Event()
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    committed_reasoning_events = 0

    def observe(name: str, details: dict):
        nonlocal committed_reasoning_events
        if name == "event_after_commit" and details.get("kind") == "reasoning":
            committed_reasoning_events += 1
            loop.call_soon_threadsafe(first_reasoning.set)
        if name == "event_after_commit" and details.get("kind") == "terminal":
            loop.call_soon_threadsafe(terminal.set)

    repository.fault_hook = observe

    class ReadyServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            if self.started:
                ready.set()

    server = ReadyServer(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    server_task = None
    evidence = {
        "id": "V2-14-http-reasoning-backlog",
        "status": "running",
        "scope": "actual fixed Host, formal HTTP/ASGI service, local provider fixture; no cloud",
        "service_sha256": source_before,
        "reasoning_chunks": 1000,
    }
    try:
        await adapter.open()
        chunks = [f"推理片段{i:04d}；" for i in range(1000)]
        full_reasoning = "".join(chunks)
        adapter.boundary.set_scenario(
            {
                "thinking_chunks": chunks,
                "text_chunks": ["等待取消的正文。"],
                "gate_before_terminal": "cancel-before-terminal",
            }
        )
        server_task = asyncio.create_task(server.serve())
        await asyncio.wait_for(ready.wait(), 30)
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=30) as client:
            session = await client.get("/api/v1/session")
            session.raise_for_status()
            client.headers.update({"Origin": base, "X-Learning-CSRF": session.json()["csrf_token"]})
            response = await client.post(
                "/api/v1/submissions",
                json={
                    "chat_id": "chat_backlog",
                    "submission_id": "sub_http_backlog",
                    "text": "完整解释这个合成问题。",
                    "model": "deepseek-flash",
                    "thinking": "high",
                },
            )
            response.raise_for_status()
            gid = response.json()["generation_id"]
            await asyncio.wait_for(first_reasoning.wait(), 30)
            # A real provider event confirms all chunks reached the native
            # transport while the terminal is held. This is not a guessed delay.
            gate = await asyncio.to_thread(
                adapter.boundary.wait_for_event, "gate_waiting", 30, generation_id=gid
            )
            entered = {}
            original_cancel = app.state.service.cancel

            async def record_cancel(generation_id: str):
                current = repository.get_generation(generation_id)
                entered.update(
                    at_ns=time.time_ns(),
                    persisted_reasoning_characters=len(current["reasoning"]),
                    committed_reasoning_events=committed_reasoning_events,
                    state=current["state"],
                )
                await original_cancel(generation_id)

            app.state.service.cancel = record_cancel
            started = time.monotonic()
            cancelled = await client.post(f"/api/v1/generations/{gid}/cancel")
            elapsed = time.monotonic() - started
            cancelled.raise_for_status()
            await asyncio.wait_for(terminal.wait(), 30)
            generation = repository.get_generation(gid)
            await asyncio.to_thread(
                adapter.boundary.wait_for_event, "request_finished", 15, generation_id=gid
            )
            row = adapter.boundary.records[0]
            evidence.update(
                generation_id=gid,
                HTTP_status=cancelled.status_code,
                HTTP_cancel_seconds=elapsed,
                cancel_handler=entered,
                fault_event=gate,
                body_characters=len(generation["body"]),
                reasoning_characters=len(generation["reasoning"]),
                full_reasoning_characters=len(full_reasoning),
                state=generation["state"],
                business=repository.evidence(gid),
                boundary_records=adapter.boundary.records,
                actual_outbound_attempts=adapter.boundary.actual_outbound_attempts,
            )
            assert 0 < entered["persisted_reasoning_characters"] < len(full_reasoning), (
                "HTTP cancel handler was starved until the reasoning backlog fully drained"
            )
            assert generation["state"] == "cancelled"
            assert generation["reasoning"]
            assert row.get("cancel_observed") is True
            assert len(adapter.boundary.records) == 1
            assert adapter.boundary.actual_outbound_attempts == 0
            evidence["status"] = "passed"
    finally:
        if server_task is not None:
            server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(server_task), 15)
            except TimeoutError:
                evidence["server_shutdown_error"] = "OWNED_ASGI_SHUTDOWN_TIMEOUT"
                evidence["status"] = "failed"
                server.force_exit = True
                server_task.cancel()
                await asyncio.gather(server_task, return_exceptions=True)
        evidence["cleanup"] = await adapter.close()
        with socket.socket() as probe:
            evidence["HTTP_port_closed"] = probe.connect_ex(("127.0.0.1", port)) != 0
        evidence["source_changed_during_run"] = (
            source_before != hashlib.sha256(service_path.read_bytes()).hexdigest()
        )
        if evidence["status"] == "running":
            evidence["status"] = "failed"
        (directory / "result.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
