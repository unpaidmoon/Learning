"""Conversation management remains durable and separate from model execution."""
import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest
from test_desktop import app_at, native_envelope

from learning_app.errors import AppError
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.boundary import MessagesBoundary
from learning_app.execution.native_tools import NativeCallRegistry


@pytest.mark.host
async def test_desktop_submission_and_auxiliary_title_use_separate_actual_attempts(tmp_path):
    from learning_app.app import create_app
    from learning_app.config import AppConfig
    from learning_app.execution.native_tools import NativeToolTransport
    from learning_app.workbench.native_business import NativeBusiness

    root = Path(__file__).resolve().parents[2]
    def factory(repo, config):
        return ExecutionAdapter(root, tmp_path / "execution", mode="fixture", desktop_enabled=True,
            native_tools=NativeToolTransport(NativeCallRegistry(tmp_path / "calls.jsonl"), NativeBusiness(repo)))
    app = create_app(AppConfig(mode="test", desktop_enabled=True, harness_enabled=True,
        data_dir=tmp_path / "data", runtime_dir=tmp_path / "runtime"), execution_factory=factory)
    async with app.router.lifespan_context(app):
        repo, gateway = app.state.repository, app.state.desktop_gateway
        repo.create_course("测试课程", "course")
        opened = await gateway.open_course("course")
        await gateway.prompt({"sessionId": opened["session_id"], "requestId": "native-title-question",
            "content": [{"type": "text", "text": "解释条件独立"}]})
        for _ in range(100):
            generations = repo.get_chat(opened["chat_id"])["generations"]
            if generations and generations[0]["state"] in {"completed", "failed", "unknown"}:
                break
            await asyncio.sleep(.1)
        assert generations[0]["state"] == "completed"
        boundary = app.state.service.execution.boundary
        for _ in range(50):
            rows = [r for r in boundary.records if r.get("purpose") == "session-title"]
            if rows and rows[0].get("finished_ns"):
                break
            await asyncio.sleep(.1)
        assert len(rows) == 1 and rows[0]["status"] == "completed"
        assert len([r for r in boundary.records if r.get("purpose") == "agent-loop"]) == 1
        assert rows[0]["generation_id"] != generations[0]["generation_id"]


async def test_rename_archive_and_restore_keep_business_history_after_restart(tmp_path):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        repo, gateway = app.state.repository, app.state.desktop_gateway
        repo.create_course("课程", "course")
        opened = await gateway.open_course("course")
        generation = repo.publish_submission(opened["chat_id"], "解释条件独立", "first-question")
        repo.fail_pending(generation["generation_id"], "FIXTURE_FINISHED")
        await gateway.rpc("session/rename", native_envelope("session/rename", {
            "sessionId": opened["session_id"], "title": "我的条件独立笔记"}))
        for _ in range(2):
            result = await gateway.rpc("workspace/archiveSession", native_envelope("workspace/archiveSession", {
                "sessionId": opened["session_id"]}))
            assert result["archivedSessionIds"] == [opened["session_id"]]
        assert gateway.project_view({"sessionId": opened["session_id"], "title": "workspace"})["title"] == "我的条件独立笔记"
        assert repo.get_chat(opened["chat_id"])["messages"][0]["text"] == "解释条件独立"
    restarted = app_at(tmp_path)
    async with restarted.router.lifespan_context(restarted):
        gateway = restarted.state.desktop_gateway
        assert gateway.workspaces()["archivedSessionIds"] == [opened["session_id"]]
        assert gateway.presentation.all()[opened["chat_id"]]["title"] == "我的条件独立笔记"
        await gateway.rpc("workspace/unarchiveSession", native_envelope("workspace/unarchiveSession", {"sessionId": opened["session_id"]}))
        assert not gateway.workspaces()["archivedSessionIds"]
        assert not restarted.state.service.execution.starts


async def test_archive_requires_explicit_stop_for_active_answer(tmp_path):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        repo, gateway = app.state.repository, app.state.desktop_gateway
        repo.create_course("课程", "course")
        opened = await gateway.open_course("course")
        repo.publish_submission(opened["chat_id"], "尚在准备的回答", "pending")
        with pytest.raises(AppError) as error:
            await gateway.rpc("workspace/archiveSession", native_envelope("workspace/archiveSession", {"sessionId": opened["session_id"]}))
        assert error.value.code == "workspace/session-active"
        assert not gateway.workspaces()["archivedSessionIds"]
        result = await gateway.rpc("workspace/archiveSession", native_envelope("workspace/archiveSession", {"sessionId": opened["session_id"], "stopActivity": True}))
        assert result["archivedSessionIds"] == [opened["session_id"]]


async def test_archived_blank_is_not_reused_for_new_conversation(tmp_path):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        repo, gateway = app.state.repository, app.state.desktop_gateway
        repo.create_course("课程", "course")
        opened = await gateway.open_course("course")
        await gateway.rpc("workspace/archiveSession", native_envelope("workspace/archiveSession", {"sessionId": opened["session_id"]}))
        new = await gateway.create_chat("course", None, opened["session_id"])
        assert new["chat_id"] != opened["chat_id"]
        assert gateway.workspaces()["archivedSessionIds"] == [opened["session_id"]]


def test_title_capability_rejects_other_text_tools_and_duplicate_dispatch(tmp_path):
    registry = NativeCallRegistry(tmp_path / "native-calls.jsonl")
    boundary = MessagesBoundary(tmp_path / "boundary", mode="fixture", native_registry=registry).start()
    try:
        identity = "title_" + uuid.uuid4().hex
        scope = boundary.register_title(identity, "learning-" + "a" * 32, "解释条件独立", "deepseek-flash", "high")
        payload = {"stream": True, "model": "deepseek-flash", "max_tokens": 4096,
            "thinking": {"type": "enabled"}, "output_config": {"effort": "high"},
            "messages": [{"role": "user", "content": [{"type": "text", "text": scope["input"]}]}]}
        headers = {"x-api-key": boundary.local_token, "x-learning-title-id": identity,
            "x-learning-title-token": scope["token"], "x-deepseek-harness-session-id": scope["session_id"]}
        with httpx.Client(trust_env=False, timeout=5) as client:
            changed = {**payload, "messages": [{"role": "user", "content": [{"type": "text", "text": "forged material"}]}]}
            assert client.post(boundary.url + "/messages", headers=headers, json=changed).status_code == 403
            assert client.post(boundary.url + "/messages", headers=headers, json={**payload,"tools":[{"name":"read_source_handle"}]}).status_code == 403
            assert client.post(boundary.url + "/messages", headers=headers, json=payload).status_code == 200
            assert client.post(boundary.url + "/messages", headers=headers, json=payload).status_code == 403
        assert len(boundary.records) == 1 and boundary.records[0]["purpose"] == "session-title"
        assert boundary.records[0]["usage"]["settled"] and not registry.attempts
    finally:
        boundary.stop()


@pytest.mark.host
async def test_native_summary_uses_only_approved_text_and_manual_title_stays_pinned(tmp_path):
    root = Path(__file__).resolve().parents[2]
    adapter = ExecutionAdapter(root, tmp_path / "execution", desktop_enabled=True)
    async with adapter:
        binding = await adapter.prepare("chat", "submission", "generation")
        identity = "title_" + uuid.uuid4().hex
        scope = adapter.boundary.register_title(identity, binding.session_id, "解释条件独立", "deepseek-flash", "high")
        await adapter.host.configure_title(scope)
        await adapter.start(binding, "PRIVATE_CONTEXT_SENTINEL: this expanded material must never enter a title request")
        events = [event async for event in adapter.observe(binding)]
        assert any(event.status == "completed" for event in events)
        for _ in range(50):
            title_rows = [r for r in adapter.boundary.records if r.get("purpose") == "session-title"]
            if title_rows and title_rows[-1].get("finished_ns"):
                break
            await asyncio.sleep(.1)
        assert len(title_rows) == 1 and title_rows[0]["status"] == "completed"
        title = title_rows[0]
        payload = json.loads((adapter.boundary.private / f"{title['request_id']}.request.json").read_text("utf-8"))
        assert "解释条件独立" in json.dumps(payload, ensure_ascii=False)
        assert "PRIVATE_CONTEXT_SENTINEL" not in json.dumps(payload)
        assert not title["images"] and not title["tools"]
        assert payload["output_config"]["effort"] == "high"
        assert title["usage"]["field_presence"]["reasoning_tokens"] is False
        assert title["usage"]["normalized"]["reasoning_tokens"] is None
        stream, snapshot = await adapter.peer.follow(binding.session_id)
        assert snapshot["projections"]["values"]["title"] == "条件独立的理解"
        await adapter.peer.unfollow(stream)
        await adapter.peer.rpc("session/rename", {"sessionId": binding.session_id, "title": "我的笔记"})
        stream, snapshot = await adapter.peer.follow(binding.session_id)
        assert snapshot["projections"]["values"]["title"] == "我的笔记"
        await adapter.peer.unfollow(stream)
        assert adapter.boundary.actual_outbound_attempts == 0
