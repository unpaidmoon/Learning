"""The same native read interface with an explicit deterministic platform Adapter."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from test_bilibili_catalogue import FixtureCatalogueProvider
from test_integrations import FixtureProvider

from learning_app.config import PROJECT_ROOT
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.execution.native_tools import ToolRejected
from learning_app.integrations import schema as s
from learning_app.integrations.access import Grant
from learning_app.integrations.common import content_hash as digest
from learning_app.integrations.host import IntegrationHost
from learning_app.integrations.models import ImportSelection, ResolveInput
from learning_app.integrations.native import input_links
from learning_app.storage import Repository

URL = "https://www.bilibili.com/video/BV1234567890"


@pytest.fixture
def query_host(tmp_path):
    repo = Repository(tmp_path / "data")
    host = IntegrationHost(repo, PROJECT_ROOT, provider_factory=FixtureCatalogueProvider)
    repo.integrations = host
    for identity in ("a", "b"):
        repo.create_course(identity, identity)
        root = tmp_path / identity
        root.mkdir()
        CourseFileStore(repo).bind(identity, str(root))
    FixtureProvider.calls = []
    host.access.change("bilibili", True)
    connection = host.access.connect().connection_id
    yield repo, host, connection
    repo.close()


def call(frozen, args, name="first"):
    return {"run_id": frozen["run_id"], "args_sha256": digest(args), "call_id": name}


def test_scope_capture_is_local_and_does_not_prefetch_or_load_disabled_sdk(query_host):
    _, host, _ = query_host
    assert host.native.freeze("a", "r0", "请解释矩形面积") is None
    assert host._provider is None
    host.access.change("bilibili", False)
    assert host.native.freeze("a", "r1", "查看B站下载任务状态 " + URL) is None
    assert host._provider is None and FixtureProvider.calls == []


def test_only_explicit_supported_links_are_canonicalized_without_foreign_requests():
    assert input_links(f"[{URL}]({URL}?p=2&tracking=private)。") == [URL, URL + "?p=2"]
    assert (
        input_links(
            "https://user@www.bilibili.com/video/BV1234567890 https://www.bilibili.com.invalid/video/BV1234567890 https://www.bilibili.com:444/video/BV1234567890 https://b23.tv/not-expanded"
        )
        == []
    )
    assert input_links(URL + "?p=1&p=2") == []


def test_query_uses_frozen_handles_and_reuses_logical_receipt_without_import(query_host):
    repo, host, _ = query_host
    frozen = host.native.freeze("a", "r1", "请列出此链接的分P " + URL)
    listing_args = {"operation": "links", "handle_id": None, "page": 1}
    listed = host.native.query(frozen, listing_args, call(frozen, listing_args), lambda: None)
    assert listed["items"][0]["url"] == URL and FixtureProvider.calls == []
    args = {"operation": "inspect", "handle_id": listed["items"][0]["handle_id"], "page": 1}
    result = host.native.query(frozen, args, call(frozen, args), lambda: None)
    assert len(result["parts"]) == 2 and not result["media_opened"] and not result["imported"]
    assert host.native.query(frozen, args, call(frozen, args, "second"), lambda: None) == result
    assert host.native.receipt(frozen, digest(args)) == result
    assert [v[0] for v in FixtureProvider.calls] == ["resolve"]
    with repo.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(s.sources)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(s.plans)).scalar_one() == 0
    assert "connection_id" not in json.dumps(result) and "cover_url" not in json.dumps(result)


@pytest.mark.parametrize(
    "change",
    [
        {"handle_id": "other"},
        {"operation": "tasks", "handle_id": None},
        {"page": True},
        {"page": "1"},
        {"url": URL},
    ],
)
def test_unknown_handles_operations_and_extra_inputs_are_denied_before_network(query_host, change):
    _, host, _ = query_host
    frozen = host.native.freeze("a", "r1", URL)
    args = {
        "operation": "inspect",
        "handle_id": frozen["links"][0]["handle_id"],
        "page": 1,
        **change,
    }
    with pytest.raises(AppError):
        host.native.query(frozen, args, call(frozen, args), lambda: None)
    assert FixtureProvider.calls == []


def test_catalogue_paging_reads_only_requested_metadata_and_rechecks_epoch(query_host):
    _, host, _ = query_host
    frozen = host.native.freeze("a", "r1", "https://space.bilibili.com/10/lists/20?type=season")
    args = {"operation": "inspect", "handle_id": frozen["links"][0]["handle_id"], "page": 2}
    result = host.native.query(frozen, args, call(frozen, args), lambda: None)
    assert len(result["videos"]) == 1 and result["videos"][0]["ordinal"] == 31
    assert "group_id" not in result and [v[0] for v in FixtureProvider.calls] == ["catalogue"]
    host.access.change("bilibili", False)
    host.access.change("bilibili", True)
    with pytest.raises(AppError) as denied:
        host.native.query(frozen, args, call(frozen, args), lambda: None)
    assert denied.value.code == "PLATFORM_GRANT_REVOKED"
    assert len(FixtureProvider.calls) == 1


def test_new_scopes_and_courses_cannot_reuse_other_link_handles(query_host):
    _, host, _ = query_host
    first = host.native.freeze("a", "r1", URL)
    other = host.native.freeze("b", "r2", URL)
    args = {"operation": "inspect", "handle_id": first["links"][0]["handle_id"], "page": 1}
    with pytest.raises(AppError) as denied:
        host.native.query(other, args, call(other, args), lambda: None)
    assert denied.value.code == "BILI_NATIVE_HANDLE_DENIED" and FixtureProvider.calls == []


def test_task_status_is_frozen_to_course_and_original_connection(query_host):
    _, host, account = query_host

    def queue(course, connection, operation):
        plan = host.resolve(course, ResolveInput(connection_id=connection, url=URL))
        item = host.import_selected(
            course,
            ImportSelection(
                operation_id=operation, plan_id=plan.plan_id, object_ids=[plan.parts[0].object_id]
            ),
        ).items[0]
        return host.queue(course, item.asset_id, item.source_version, "platform_download")

    first = queue("a", account, "first")
    queue("b", account, "other_course")
    frozen = host.native.freeze("a", "r1", "查看B站下载任务状态")
    second_account = host.access.connect().connection_id
    second = queue("a", second_account, "second")
    new_scope = host.native.freeze("a", "r2", "查看B站下载任务状态")
    assert (
        frozen["tasks"][0]["job_id"] == first.job_id
        and new_scope["tasks"][0]["job_id"] == second.job_id
    )
    args = {"operation": "tasks", "handle_id": None, "page": 1}
    before = len(FixtureProvider.calls)
    result = host.native.query(frozen, args, call(frozen, args), lambda: None)
    assert result["total"] == 1 and result["items"][0]["state"] == "queued"
    assert len(FixtureProvider.calls) == before
    assert all(key not in json.dumps(result) for key in ("local_path", "connection_id", "job_id"))


def test_network_unknown_is_receipted_and_same_run_does_not_retry(query_host, monkeypatch):
    _, host, _ = query_host
    frozen = host.native.freeze("a", "r1", URL)
    attempts = []

    def fail(*_):
        attempts.append(True)
        raise AppError("BILI_NETWORK_FAILED", "synthetic unknown", 502)

    monkeypatch.setattr(host.provider, "resolve", fail)
    args = {"operation": "inspect", "handle_id": frozen["links"][0]["handle_id"], "page": 1}
    result = host.native.query(frozen, args, call(frozen, args), lambda: None)
    assert result["status"] == "unknown" and result["retried"] is False
    assert host.native.query(frozen, args, call(frozen, args, "new-call"), lambda: None) == result
    assert attempts == [True]


def test_run_stop_blocks_later_requests_and_unsettled_read_is_never_replayed(
    query_host, monkeypatch
):
    _, host, _ = query_host
    frozen = host.native.freeze("a", "r1", "https://space.bilibili.com/10/lists/20?type=series")
    stopped = False
    original = host.provider.json

    def json_call(*args):
        nonlocal stopped
        value = original(*args)
        stopped = True
        return value

    monkeypatch.setattr(host.provider, "json", json_call)

    def check():
        if stopped:
            raise ToolRejected("NATIVE_CHAT_NOT_ACTIVE", 409)

    args = {"operation": "inspect", "handle_id": frozen["links"][0]["handle_id"], "page": 1}
    with pytest.raises(ToolRejected):
        host.native.query(frozen, args, call(frozen, args), check)
    assert len(FixtureProvider.calls) == 1
    stopped = False
    with pytest.raises(AppError) as unknown:
        host.native.query(frozen, args, call(frozen, args, "another"), check)
    assert unknown.value.code == "BILI_NATIVE_QUERY_UNKNOWN" and len(FixtureProvider.calls) == 1


def test_scope_callback_and_operation_are_checked_on_each_platform_dispatch(query_host):
    _, host, account = query_host
    checked = []
    grant = replace(
        host.access.grant(account),
        scope_check=lambda: checked.append(True),
        operation_id="owned-query",
    )
    identity = host.access.request(grant, "metadata")
    assert checked == [True]
    with host.repository.engine.connect() as conn:
        assert (
            conn.execute(
                select(s.requests.c.purpose).where(s.requests.c.request_id == identity)
            ).scalar_one()
            == "metadata:owned-query"
        )
    assert grant == Grant(account, grant.module_epoch, grant.connection_epoch)
