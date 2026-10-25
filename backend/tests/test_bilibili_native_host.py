"""Actual pinned Harness tool registration → private read port → platform fixture."""

import asyncio
import json

import pytest
from sqlalchemy import select
from test_bilibili_native_query import URL, query_host  # noqa: F401
from test_integrations import FixtureProvider

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.boundary import MessagesBoundary
from learning_app.execution.native_tools import NativeCall, NativeCallRegistry, NativeToolTransport
from learning_app.integrations import schema as s
from learning_app.integrations.models import ImportSelection, ResolveInput
from learning_app.service import GenerationService
from learning_app.workbench.native_business import NativeBusiness


class BilibiliQueryFixture(MessagesBoundary):
    def __init__(self, directory, repo, registry, *, query_kind="links"):
        super().__init__(directory, native_registry=registry)
        self.repo, self.requests = repo, []
        self.exercise = True
        self.query_kind = query_kind

    def _fixture(self, handler, request, scenario, row, observer):
        self.requests.append(request)
        step = len(self.requests)
        calls = []
        if self.exercise and step == 1:
            calls = [
                {"id": "load_platform_skill", "name": "skill", "input": {"name": "bilibili-read"}}
            ]
        elif self.exercise and step == 2:
            calls = [
                {
                    "id": "list_links",
                    "name": "query_bilibili",
                    "input": {
                        "operation": "links" if self.query_kind == "links" else "tasks",
                        "handle_id": None,
                        "page": 1,
                    },
                }
            ]
        elif self.exercise and step == 3 and self.query_kind == "links":
            with self.repo.engine.connect() as conn:
                receipt = conn.execute(
                    select(s.operations.c.receipt).where(s.operations.c.kind == "native_query")
                ).scalar_one()
            handle = receipt["result"]["items"][0]["handle_id"]
            calls = [
                {
                    "id": "inspect_link",
                    "name": "query_bilibili",
                    "input": {"operation": "inspect", "handle_id": handle, "page": 1},
                }
            ]
        return super()._fixture(
            handler,
            request,
            {"text_chunks": [] if calls else ["已只读查询目录，尚未导入。"], "tool_calls": calls},
            row,
            observer,
        )


@pytest.mark.host
@pytest.mark.parametrize("mode", ["links", "tasks"])
async def test_actual_native_platform_skill_is_per_request_and_removed_after_release(
    query_host,  # noqa: F811 - imported pytest fixture
    tmp_path,
    mode,
):
    repo, integration, _ = query_host
    if mode == "tasks":
        account = integration.access.modules()[0].connection_id
        plan = integration.resolve("a", ResolveInput(connection_id=account, url=URL))
        item = integration.import_selected(
            "a",
            ImportSelection(
                operation_id="before_query",
                plan_id=plan.plan_id,
                object_ids=[plan.parts[0].object_id],
            ),
        ).items[0]
        integration.queue("a", item.asset_id, item.source_version, "platform_download")
    registry = NativeCallRegistry(tmp_path / "native-facts.jsonl")
    boundary = BilibiliQueryFixture(tmp_path / "boundary", repo, registry, query_kind=mode)
    adapter = ExecutionAdapter(
        PROJECT_ROOT,
        tmp_path / "host",
        boundary=boundary,
        native_tools=NativeToolTransport(registry, NativeBusiness(repo)),
    )
    repo.create_chat("a", "native_bili_chat")
    async with adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=adapter
        )
        await service.open()
        try:
            result = await service.submit(
                {
                    "chat_id": "native_bili_chat",
                    "submission_id": "explicit_link",
                    "text": ("请查询这个B站链接的分P目录，不下载也不导入：" + URL)
                    if mode == "links"
                    else "请查看当前课程B站下载任务状态，不启动或恢复处理",
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[result["generation_id"]], 60)
            run = repo.get_generation(result["generation_id"])
            assert run["state"] == "completed", run
            assert len(boundary.requests) == (4 if mode == "links" else 3)
            assert all(
                "query_bilibili" in {t["name"] for t in request.get("tools", [])}
                for request in boundary.requests
            )
            assert "BILIBILI_READ_SKILL_BODY" in json.dumps(boundary.requests[1:])
            assert '"type": "image"' not in json.dumps(boundary.requests)
            assert [c[0] for c in FixtureProvider.calls] == ["resolve"]
            assert result["generation_id"] in registry.released
            for key, native_call in registry.bindings.items():
                if native_call["tool_name"] == "query_bilibili":
                    receipt = await asyncio.wait_for(
                        asyncio.to_thread(
                            adapter.native_tools.port.reconcile_effect, NativeCall(**native_call)
                        ),
                        5,
                    )
                    assert receipt == registry.operations[key]["result"]
            state = await adapter.host.audit_state()
            assert "query_bilibili" not in state["tools"]
            assert all("query_bilibili" not in row["tools"] for row in state["businessScopes"])
            integration.access.change("bilibili", False)
            boundary.exercise = False
            second = await service.submit(
                {
                    "chat_id": "native_bili_chat",
                    "submission_id": "plain_question",
                    "text": "请解释矩形面积公式",
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[second["generation_id"]], 40)
            assert repo.get_generation(second["generation_id"])["state"] == "completed"
            assert "query_bilibili" not in {
                t["name"] for t in boundary.requests[-1].get("tools", [])
            }
            assert "bilibili-read" not in json.dumps(boundary.requests[-1].get("system"))
            assert [c[0] for c in FixtureProvider.calls] == ["resolve"]
            assert boundary.actual_outbound_attempts == 0
            integration.access.change("bilibili", True)
            third = await service.submit(
                {
                    "chat_id": "native_bili_chat",
                    "submission_id": "enabled_but_unrelated",
                    "text": "请解释长方形周长公式",
                    "model": "deepseek-flash",
                    "thinking": "high",
                }
            )
            await asyncio.wait_for(service.tasks[third["generation_id"]], 40)
            assert repo.get_generation(third["generation_id"])["state"] == "completed"
            assert "query_bilibili" not in {
                t["name"] for t in boundary.requests[-1].get("tools", [])
            }
            assert "bilibili-read" not in json.dumps(boundary.requests[-1].get("system"))
            assert [c[0] for c in FixtureProvider.calls] == ["resolve"]
        finally:
            await service.close()
    assert adapter.host.cleanup["remaining_pids"] == []
