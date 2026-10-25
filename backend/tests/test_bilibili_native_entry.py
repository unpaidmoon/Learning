"""Native client command directory projection without admitting Host execution."""

import pytest
from test_desktop import app_at, native_envelope

from learning_app.errors import AppError
from learning_app.storage import StorageError


async def test_current_business_session_can_enumerate_admitted_host_commands_locally(tmp_path):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        repo = app.state.repository
        repo.create_course("合成课程", "course")
        gateway = app.state.desktop_gateway
        opened = await gateway.open_course("course")
        before = list(app.state.service.execution.commands)
        result = await gateway.rpc(
            "commands/list", native_envelope("commands/list", {"agentId": opened["session_id"]})
        )
        assert (
            result == []
        )  # This carrier admits no Host execute; client contributions are separate.
        assert app.state.service.execution.commands == before
        assert app.state.integrations._provider is None
        assert not repo.get_chat(opened["chat_id"])["messages"]
        for arguments in (
            {},
            {"agentId": opened["session_id"], "extra": "other-agent"},
            {"agentId": {"sessionId": opened["session_id"]}},
        ):
            with pytest.raises(AppError):
                await gateway.rpc("commands/list", native_envelope("commands/list", arguments))
        with pytest.raises((AppError, StorageError)):
            await gateway.rpc(
                "commands/list", native_envelope("commands/list", {"agentId": "unknown-session"})
            )


@pytest.mark.parametrize(
    "method", ["commands/execute", "terminal/execute", "permissionPresets/select"]
)
async def test_native_client_entry_does_not_open_host_execution_or_permissions(tmp_path, method):
    app = app_at(tmp_path)
    async with app.router.lifespan_context(app):
        gateway = app.state.desktop_gateway
        before = list(app.state.service.execution.commands)
        with pytest.raises(AppError) as denied:
            await gateway.rpc(
                method, native_envelope(method, {"agentId": "untrusted", "line": "/bilibili"})
            )
        assert denied.value.code == "DESKTOP_METHOD_DENIED"
        assert app.state.service.execution.commands == before
