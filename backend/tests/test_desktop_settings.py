"""Native controls retain their UI without opening another execution path."""

import pytest
from test_desktop import app_at, native_envelope

from learning_app.errors import AppError
from learning_app.storage import StorageError


async def test_settings_writes_keep_execution_configuration_managed(tmp_path):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        gateway = app.state.desktop_gateway
        for namespace in ("locale", "ui-chat", "agent-preset-registry"):
            assert await gateway.rpc("settings/update", native_envelope("settings/update", {
                "ns": namespace, "patch": {},
            })) is None
        for namespace in ("llm-deepseek", "tools", "learning-guard"):
            with pytest.raises(AppError) as denied:
                await gateway.rpc("settings/update", native_envelope("settings/update", {
                    "ns": namespace, "patch": {},
                }))
            assert denied.value.code == "DESKTOP_SETTINGS_MANAGED"
        assert app.state.service.execution.starts == []


async def test_native_command_menu_requires_owned_conversation(tmp_path):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        gateway = app.state.desktop_gateway
        app.state.repository.create_course("合成课程", "course")
        opened = await gateway.open_course("course")
        envelope = {"payload": {"scope": {"agentId": opened["session_id"]}, "args": {}}}
        assert await gateway.rpc("commands/list", envelope) is None
        envelope["payload"]["scope"]["agentId"] = "another-session"
        with pytest.raises((AppError, StorageError)):
            await gateway.rpc("commands/list", envelope)
        envelope["payload"]["scope"]["agentId"] = opened["session_id"]
        envelope["payload"]["args"] = {"line": "/export"}
        assert await gateway.rpc("commands/execute", envelope) is None
        envelope["payload"]["args"] = {"line": "/run arbitrary"}
        with pytest.raises(AppError) as denied:
            await gateway.rpc("commands/execute", envelope)
        assert denied.value.code == "DESKTOP_METHOD_DENIED"
        assert app.state.service.execution.starts == []
