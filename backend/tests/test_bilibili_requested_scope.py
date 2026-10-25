"""Platform reads cannot turn an external link into a learning-task grant."""

import pytest
from test_native_business import call, count
from test_requested_learning import chat_scope
from workbench_support import learning_course

from learning_app.config import PROJECT_ROOT
from learning_app.execution.native_tools import ToolRejected
from learning_app.integrations.host import IntegrationHost
from learning_app.workbench import schema as s
from learning_app.workbench.requested_learning import TOOL

URL = "https://www.bilibili.com/video/BV1234567890"


@pytest.mark.parametrize("external", [False, True])
def test_task_admission_and_platform_read_keep_their_own_frozen_authority(tmp_path, external):
    repo, course, _, source = learning_course(tmp_path)
    try:
        host = IntegrationHost(repo, PROJECT_ROOT)
        repo.integrations = host
        host.access.change("bilibili", True)
        host.access.connect()
        text = "请生成学习笔记" + (" " + URL if external else "")
        binding, port, scope = chat_scope(repo, course, source, text)
        assert ("query_bilibili" in scope["allowed_tools"]) is external
        assert (TOOL in scope["allowed_tools"]) is not external
        assert "search_research_web" not in scope["allowed_tools"]
        assert "stage_result" not in scope["allowed_tools"]
        assert port.ensure_chat_scope(binding.generation_id, binding) == scope
        if external:
            with pytest.raises(ToolRejected, match="NATIVE_TOOL_NOT_ALLOWED"):
                port.invoke(call(binding, TOOL, {}), {})
            args = {"operation": "links", "handle_id": None, "page": 1}
            result = port.invoke(call(binding, "query_bilibili", args), args)
            assert result["items"][0]["url"] == URL
            assert count(repo, s.tasks) == 0
        else:
            admitted = port.invoke(call(binding, TOOL, {}), {})
            assert admitted["task_id"] and count(repo, s.tasks) == 1
        assert host._provider is None  # Neither frozen authority performs platform HTTP.
    finally:
        repo.close()
