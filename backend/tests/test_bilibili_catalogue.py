"""Frozen directory pages and explicit selection, with no platform network access."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_integrations import FixtureProvider

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.integrations import schema as s
from learning_app.integrations.bilibili.catalogue import browse_catalogue, catalogue_link
from learning_app.integrations.host import IntegrationHost
from learning_app.integrations.models import CatalogueBrowse, ImportSelection, ResolveInput
from learning_app.storage import Repository

URL = "https://space.bilibili.com/10/lists/20?type=season"


class FixtureCatalogueProvider(FixtureProvider):
    def json(self, grant, url, params):
        self.access.check(grant)
        self.calls.append(("catalogue", url, dict(params)))
        if url.endswith("/x/series/series"):
            return {"meta": {"mid": 10, "name": "合成系列", "mtime": 123}}
        page = int(params.get("page_num", params.get("pn", 1)))
        first, last = (page - 1) * 30 + 1, min(page * 30, 31)
        size = (
            {"page_size": 30, "page_num": page}
            if "page_num" in params
            else {"size": 30, "num": page}
        )
        return {
            "meta": {"mid": 10, "name": "合成合集", "ptime": 123},
            "page": {"total": 31, **size},
            "archives": [
                {"bvid": f"BV{i:010d}", "title": f"目录视频{i}"} for i in range(first, last + 1)
            ],
        }

    def resolve(self, grant, value):
        result = super().resolve(grant, value)
        from learning_app.integrations.bilibili.provider import video_url

        bvid, _ = video_url(value)
        result["parts"] = [
            {**part, "bvid": bvid, "object_id": f"{bvid}:{part['cid']}"} for part in result["parts"]
        ]
        return result


@pytest.fixture
def catalogue_host(tmp_path):
    repo = Repository(tmp_path / "data")
    host = IntegrationHost(
        repo, Path(__file__).resolve().parents[2], provider_factory=FixtureCatalogueProvider
    )
    repo.integrations = host
    for identity in ["a", "b"]:
        repo.create_course(identity, identity)
        root = tmp_path / identity
        root.mkdir()
        CourseFileStore(repo).bind(identity, str(root))
    FixtureProvider.calls = []
    host.access.change("bilibili", True)
    connection = host.access.connect().connection_id
    yield repo, host, connection
    repo.close()


def inspect(host, connection, page=1, parent=None, url=URL, course="a"):
    return host.inspect(
        course, CatalogueBrowse(connection_id=connection, url=url, page=page, catalogue_id=parent)
    )


def test_paging_does_not_resolve_import_or_download_unselected_videos(catalogue_host):
    repo, host, connection = catalogue_host
    first = inspect(host, connection)
    assert len(first.videos) == 30 and first.has_more and first.total == 31
    assert [call[0] for call in FixtureProvider.calls] == ["catalogue"]
    with repo.engine.connect() as conn:
        assert conn.execute(select(s.sources)).first() is None
    with pytest.raises(AppError) as error:
        host.import_selected(
            "a",
            ImportSelection(
                operation_id="bad", plan_id=first.catalogue_id, object_ids=[first.videos[0].bvid]
            ),
        )
    assert error.value.code == "BILI_PART_SELECTION_REQUIRED"
    second = inspect(host, connection, 2, first.catalogue_id)
    assert second.videos[0].ordinal == 31 and not second.has_more
    assert not any(call[0] in {"resolve", "media", "subtitle"} for call in FixtureProvider.calls)


def test_selected_multi_part_has_collection_hierarchy_and_original_order(catalogue_host):
    repo, host, connection = catalogue_host
    page = inspect(host, connection, 2)
    plan = host.resolve(
        "a",
        ResolveInput(
            connection_id=connection,
            url="https://www.bilibili.com/video/" + page.videos[0].bvid,
            catalogue_id=page.catalogue_id,
        ),
    )
    receipt = host.import_selected(
        "a",
        ImportSelection(
            operation_id="selected", plan_id=plan.plan_id, object_ids=[plan.parts[1].object_id]
        ),
    )
    item = receipt.items[0]
    parts = Path(item.path).parts
    assert len(parts) == 3 and parts[0].startswith("合成合集-")
    assert parts[1].startswith("000031 ") and parts[2].startswith("002 ")
    row = host.source(item.asset_id)
    assert row["payload"]["collection"]["ordinal"] == 31
    assert row["payload"]["collection"]["group_id"] == "collection:10:20"
    assert [call[0] for call in FixtureProvider.calls].count("resolve") == 1
    assert not any(call[0] in {"media", "subtitle"} for call in FixtureProvider.calls)


def test_existing_item_keeps_version_path_and_adds_collection_provenance(catalogue_host):
    repo, host, connection = catalogue_host
    plain = host.resolve("a", ResolveInput(connection_id=connection, url="BV0000000001"))
    old = host.import_selected(
        "a",
        ImportSelection(
            operation_id="plain", plan_id=plain.plan_id, object_ids=[plain.parts[0].object_id]
        ),
    ).items[0]
    original = repo.resolve_source(old.asset_id, old.source_version)
    data = Path(original["local_path"]).read_bytes()
    page = inspect(host, connection)
    selected = host.resolve(
        "a",
        ResolveInput(connection_id=connection, url="BV0000000001", catalogue_id=page.catalogue_id),
    )
    value = ImportSelection(
        operation_id="linked", plan_id=selected.plan_id, object_ids=[selected.parts[0].object_id]
    )
    linked = host.import_selected("a", value).items[0]
    assert (
        linked.reused
        and linked.asset_id == old.asset_id
        and linked.source_version == old.source_version
    )
    assert linked.path == old.path and Path(original["local_path"]).read_bytes() == data
    relations = host.source(old.asset_id)["payload"]["collections"]
    assert len(relations) == 1 and relations[0]["group_id"] == "collection:10:20"
    host.import_selected("a", value)
    assert len(host.source(old.asset_id)["payload"]["collections"]) == 1
    with repo.engine.connect() as conn:
        assert len(conn.execute(select(s.sources)).all()) == 1


def test_frozen_directory_denies_other_video_course_connection_and_revoked_calls(catalogue_host):
    repo, host, connection = catalogue_host
    page = inspect(host, connection)
    with pytest.raises(AppError) as error:
        host.resolve(
            "a",
            ResolveInput(
                connection_id=connection, url="BV0000000031", catalogue_id=page.catalogue_id
            ),
        )
    assert error.value.code == "BILI_CATALOGUE_VIDEO_DENIED"
    other = host.access.connect().connection_id
    for course, account in [("b", connection), ("a", other)]:
        with pytest.raises(AppError) as error:
            host.resolve(
                course,
                ResolveInput(
                    connection_id=account, url="BV0000000001", catalogue_id=page.catalogue_id
                ),
            )
        assert error.value.code == "BILI_CATALOGUE_SCOPE_DENIED"
    calls = len(FixtureProvider.calls)
    with pytest.raises(AppError):
        inspect(host, connection, 2, page.catalogue_id, url=URL.replace("/20?", "/21?"))
    host.access.change("bilibili", False)
    host.access.change("bilibili", True)
    with pytest.raises(AppError) as error:
        inspect(host, connection, 2, page.catalogue_id)
    assert error.value.code == "PLATFORM_GRANT_REVOKED"
    assert len(FixtureProvider.calls) == calls


def test_series_uses_fixed_header_and_page_endpoints(catalogue_host):
    repo, host, connection = catalogue_host
    page = inspect(host, connection, url=URL.replace("type=season", "type=series"))
    assert page.catalogue_kind == "series" and page.title == "合成系列"
    assert [call[1].split("/")[-1] for call in FixtureProvider.calls] == ["series", "archives"]


@pytest.mark.parametrize(
    "value",
    [
        "http://space.bilibili.com/10/lists/20",
        "https://space.bilibili.com.evil/10/lists/20",
        "https://user@space.bilibili.com/10/lists/20",
        "https://space.bilibili.com:444/10/lists/20",
        URL + "&type=series",
        URL.replace("season", "unknown"),
        "https://space.bilibili.com/10/channel/collectiondetail?sid=20&sid=21",
        "https://[space.bilibili.com/10/lists/20",
    ],
)
def test_ambiguous_or_foreign_catalogue_urls_are_not_network_targets(value):
    with pytest.raises(AppError):
        catalogue_link(value)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_total",
        "short_page",
        "duplicate",
        "wrong_owner",
        "wrong_number",
        "wrong_group",
        "bad_meta",
        "bad_page",
        "bad_archive",
        "bad_data",
    ],
)
def test_partial_pages_are_not_silently_complete(catalogue_host, monkeypatch, mutation):
    repo, host, connection = catalogue_host
    provider = host.provider
    original = provider.json

    def malformed(grant, url, params):
        data = original(grant, url, params)
        if mutation == "missing_total":
            data["page"].pop("total")
        if mutation == "short_page":
            data["archives"].pop()
        if mutation == "duplicate":
            data["archives"][1] = data["archives"][0]
        if mutation == "wrong_owner":
            data["meta"]["mid"] = 999
        if mutation == "wrong_number":
            data["page"]["page_num"] = 2
        if mutation == "wrong_group":
            data["meta"]["season_id"] = 21
        if mutation == "bad_meta":
            data["meta"] = [1]
        if mutation == "bad_page":
            data["page"] = "invalid"
        if mutation == "bad_archive":
            data["archives"][0] = None
        if mutation == "bad_data":
            return []
        return data

    monkeypatch.setattr(provider, "json", malformed)
    with pytest.raises(AppError) as error:
        browse_catalogue(provider, host.access.grant(connection), catalogue_link(URL), 1)
    assert error.value.code == "BILI_CATALOGUE_PAGE_INCOMPLETE"


def test_http_inspection_contract_requires_session_csrf_and_uses_same_adapter(
    catalogue_host, tmp_path
):
    repo, _, _ = catalogue_host
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run")
    app = create_app(
        config, repository=repo, integrations_provider_factory=FixtureCatalogueProvider
    )
    path = "/api/v1/courses/a/integrations/bilibili/inspect"
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.post(path, json={}).status_code == 403
        client.headers["X-Learning-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        client.post("/api/v1/integrations/modules/bilibili", json={"enabled": True})
        connection = client.post(
            "/api/v1/integrations/bilibili/connect", json={"guest": True}
        ).json()["connection_id"]
        result = client.post(path, json={"connection_id": connection, "url": URL})
        assert result.status_code == 200 and result.json()["kind"] == "catalogue"
        assert "group_id" not in result.json()
