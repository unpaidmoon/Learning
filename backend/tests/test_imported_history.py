"""Immutable native history cuts without execution events or writable identity."""

from types import SimpleNamespace

import pytest
from test_native_business import prepare_chat
from workbench_support import learning_course

from learning_app.config import AppConfig
from learning_app.desktop import DesktopGateway
from learning_app.errors import AppError
from learning_app.imported_executions import ImportedExecutions
from learning_app.imported_history import history_page
from learning_app.service import GenerationService


@pytest.mark.asyncio
async def test_imported_unknown_history_is_visible_in_native_page_but_every_write_is_denied(
    tmp_path,
):
    repo, course, _, source = learning_course(tmp_path)
    _, binding = prepare_chat(repo, course, source, "old")
    repo.ingest_event(
        binding.generation_id,
        binding_id=repo.current_binding(binding.conversation_id)["binding_id"],
        epoch=binding.epoch,
        event_id="saved",
        seq=1,
        kind="body",
        text="saved unknown prefix $x^2$",
    )
    repo.mark_unknown(binding.generation_id, "fixture_unknown")
    ImportedExecutions.install(repo, "package", "f" * 64)
    old = repo.get_generation(binding.generation_id)
    native = SimpleNamespace(host=SimpleNamespace(workspace=tmp_path / "workspace"))
    config = AppConfig(mode="test", data_dir=repo.root, desktop_enabled=True)
    service = GenerationService(repo, config, execution=native)
    service.execution_ready = True
    gateway = DesktopGateway(
        SimpleNamespace(state=SimpleNamespace(config=config, service=service, ready=True))
    )
    opened = await gateway.open_chat(binding.conversation_id)
    assert opened["read_only"] and opened["has_unresolved_history"]
    sid = opened["session_id"]
    page = await gateway.rpc(
        "session/page",
        {
            "payload": {
                "request": {
                    "address": {"kind": "session", "sessionId": sid},
                    "throughSeq": 1,
                    "maxMessages": 10,
                }
            }
        },
    )
    assert [row["event"]["type"] for row in page["records"]] == [
        "user/message",
        "assistant/message",
    ]
    assert (
        page["records"][1]["event"]["data"]["message"]["content"][0]["text"]
        == "saved unknown prefix $x^2$"
    )
    assert page["records"][1]["event"]["data"]["stream"] == []
    assert gateway.read_mapping(sid) == opened
    assert "tokenUsage" not in gateway.history.projections(sid)["values"]
    for method in (
        "session/prompt",
        "session/cancel",
        "session/selectModel",
        "session/rename",
        "session/updateQueue",
        "session/initializeDefaultModel",
        "fileUploads/upload",
        "session/delete",
    ):
        with pytest.raises(AppError) as error:
            await gateway.rpc(method, {"payload": {"request": {"sessionId": sid}}})
        assert error.value.code == "IMPORTED_EXECUTION_READ_ONLY"
    assert repo.get_generation(binding.generation_id) == old
    assert (
        repo.current_binding(binding.conversation_id)["active_generation_id"]
        == binding.generation_id
    )
    assert repo.counts()["outbound_attempts"] == 0
    repo.close()


def test_history_paging_obeys_the_original_cut_and_exclusive_before_boundary():
    records = [{"n": n} for n in range(106)]
    result = history_page(records, 105, max_messages=50)
    assert result["records"][0]["n"] == 56 and result["hasMore"]
    second = history_page(records, 105, before_seq=56, max_messages=50)
    third = history_page(records, 105, before_seq=6, max_messages=50)
    assert [r["n"] for r in [*third["records"], *second["records"], *result["records"]]] == list(
        range(106)
    )
    assert not third["hasMore"]
    assert history_page(records, 10, max_messages=50)["records"] == records[:11]
    for arguments in [(106, None, 50), (105, -1, 50), (105, None, 0), (True, None, 1)]:
        with pytest.raises(AppError):
            history_page(records, *arguments)
