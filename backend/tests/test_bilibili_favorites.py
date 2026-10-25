"""Created/followed directories, scoped pages, and explicit video/P selection."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_bilibili_catalogue import FixtureCatalogueProvider
from test_integrations import FixtureProvider

from learning_app.app import create_app
from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.integrations import schema as s
from learning_app.integrations.bilibili.favorites import Favorites
from learning_app.integrations.host import IntegrationHost
from learning_app.integrations.models import FavoriteOrigin, ImportSelection, ResolveInput
from learning_app.integrations.routes import FavoriteBrowse, FavoriteFolderBrowse
from learning_app.storage import Repository


class FixtureFavoriteProvider(FixtureCatalogueProvider):
    after_read = None

    def json(self, grant, url, params=None):
        self.access.check(grant)
        params = params or {}
        self.calls.append(("favorite", url, dict(params)))
        if url.endswith("/created/list-all"):
            account = int(params["up_mid"])
            data = {
                "count": 21,
                "list": [
                    {
                        "id": account * 1000 + i,
                        "mid": account,
                        "title": f"合成收藏夹{i}",
                        "media_count": 21,
                    }
                    for i in range(1, 22)
                ],
            }
        elif url.endswith("/collected/list"):
            data = {
                "count": 2,
                "has_more": False,
                "list": [
                    {"id": 500, "type": 11, "title": "合成关注收藏夹", "media_count": 21},
                    {"id": 500, "type": 21, "title": "合成关注合集", "media_count": 21},
                ],
            }
        elif url.endswith("/resource/list") or url.endswith("/season/list"):
            collection = url.endswith("/season/list")
            identity = str(params["season_id"] if collection else params["media_id"])
            page = int(params["pn"])
            indices = (
                range(1, 22) if collection else range((page - 1) * 20 + 1, min(page * 20, 21) + 1)
            )
            data = {
                "info": {
                    "id": int(identity),
                    "title": "合成关注合集" if collection else "合成收藏视频目录",
                    "media_count": 21,
                },
                "medias": [
                    {
                        "id": i,
                        "type": 12 if i == 3 else 2,
                        "title": f"合成收藏视频{i}",
                        "bvid": f"BV{i:010d}" if i not in {3, 4} else "",
                        "attr": 1 if i == 2 else 0,
                    }
                    for i in indices
                ],
            }
            if collection:
                data["info"]["upper"] = {"mid": 99}
            if not collection:
                data["has_more"] = page < 2
        elif url.endswith("/qrcode/generate"):
            return {
                "url": "https://passport.bilibili.com/synthetic-login",
                "qrcode_key": "synthetic-key",
            }
        else:
            raise AssertionError("Unexpected fixture metadata endpoint")
        if self.after_read:
            self.after_read(url, data)
        return data

    def credential_poll(self, grant, key):
        self.access.check(grant)
        return {
            "data": {"code": 0, "url": ""},
            "cookies": {"SESSDATA": "fixture-only", "DedeUserID": "10"},
        }


@pytest.fixture
def favorites_host(tmp_path):
    repo = Repository(tmp_path / "data")
    host = IntegrationHost(repo, PROJECT_ROOT, provider_factory=FixtureFavoriteProvider)
    repo.integrations = host
    repo.create_course("合成课程", "a")
    folder = tmp_path / "course"
    folder.mkdir()
    CourseFileStore(repo).bind("a", str(folder))
    host.access.change("bilibili", True)
    connection = host.access.connect("10", "合成账号", "connected").connection_id
    FixtureProvider.calls = []
    yield repo, host, connection
    repo.close()


def browse(connection, **values):
    return FavoriteBrowse(connection_id=connection, folder_id="10021", folder_page=2, **values)


def test_created_folder_and_video_pages_never_import_or_resolve_unselected_content(favorites_host):
    repo, host, connection = favorites_host
    favorites = Favorites(host)
    first = favorites.folder_page(FavoriteFolderBrowse(connection_id=connection))
    second = favorites.folder_page(FavoriteFolderBrowse(connection_id=connection, page=2))
    assert (
        len(first["items"]) == 20
        and first["has_more"]
        and second["items"][0]["folder_id"] == "10021"
    )
    videos = favorites.browse(browse(connection))
    assert len(videos["items"]) == 20 and videos["has_more"] and videos["total"] == 21
    assert [v["unavailable_reason"] for v in videos["items"][:4]] == [
        None,
        "removed",
        "unsupported",
        "identity_missing",
    ]
    assert [v["available"] for v in videos["items"][:4]] == [True, False, False, False]
    last = favorites.browse(browse(connection, page=2))
    assert last["items"][0]["bvid"] == "BV0000000021" and not last["has_more"]
    assert all(call[0] == "favorite" for call in FixtureProvider.calls)
    with repo.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(s.sources)).scalar_one() == 0
    plan = host.resolve("a", ResolveInput(connection_id=connection, url="BV0000000021"))
    selected = host.import_selected(
        "a",
        ImportSelection(
            operation_id="only_p2", plan_id=plan.plan_id, object_ids=[plan.parts[1].object_id]
        ),
    )
    assert len(selected.items) == 1 and selected.items[0].object_id == "BV0000000021:2"
    assert not any(c[0] in {"media", "subtitle"} for c in FixtureProvider.calls)


def test_followed_folder_and_collection_keep_type_identity_and_correct_read_protocol(
    favorites_host,
):
    _, host, connection = favorites_host
    favorites = Favorites(host)
    rows = favorites.folder_page(FavoriteFolderBrowse(connection_id=connection, kind="collected"))
    assert [(v["folder_id"], v["resource_type"]) for v in rows["items"]] == [
        ("500", "folder"),
        ("500", "collection"),
    ]
    collection = favorites.browse(
        FavoriteBrowse(
            connection_id=connection,
            kind="collected",
            folder_id="500",
            resource_type="collection",
            page=2,
        )
    )
    assert len(collection["items"]) == 1 and collection["items"][0]["bvid"] == "BV0000000021"
    assert FixtureProvider.calls[-1][1].endswith("/season/list")
    favorites.browse(
        FavoriteBrowse(
            connection_id=connection, kind="collected", folder_id="500", resource_type="folder"
        )
    )
    assert FixtureProvider.calls[-1][1].endswith("/resource/list")


def test_foreign_account_folder_and_wrong_directory_page_cannot_read_resources(favorites_host):
    _, host, connection = favorites_host
    for values in ({"folder_id": "20001"}, {"folder_page": 1}, {"resource_type": "collection"}):
        value = browse(connection).model_copy(update=values)
        with pytest.raises(AppError) as error:
            Favorites(host).browse(value)
        assert error.value.code == "BILI_FAVORITE_SCOPE_DENIED"
    assert not any(c[1].endswith("/resource/list") for c in FixtureProvider.calls)


def test_one_grant_covers_ownership_and_resource_fetch_even_after_disable_reenable(favorites_host):
    _, host, connection = favorites_host

    def revoke(_url, _data):
        host.access.change("bilibili", False)
        host.access.change("bilibili", True)

    host.provider.after_read = revoke
    with pytest.raises(AppError) as error:
        Favorites(host).browse(browse(connection))
    assert error.value.code == "PLATFORM_GRANT_REVOKED"
    assert len(FixtureProvider.calls) == 1


@pytest.mark.parametrize(
    "mutation", ["missing_count", "count_mismatch", "duplicate", "foreign_owner", "invalid_data"]
)
def test_created_directory_gaps_are_not_reported_as_complete(favorites_host, mutation):
    _, host, connection = favorites_host

    def change(_url, data):
        if mutation == "missing_count":
            data.pop("count")
        if mutation == "count_mismatch":
            data["count"] += 1
        if mutation == "duplicate":
            data["list"][1] = data["list"][0]
        if mutation == "foreign_owner":
            data["list"][0]["mid"] = 20
        if mutation == "invalid_data":
            data["list"] = "invalid"

    host.provider.after_read = change
    with pytest.raises(AppError) as error:
        Favorites(host).folders(connection)
    assert error.value.code == "BILI_FAVORITE_PAGE_INCOMPLETE"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_total",
        "missing_more",
        "wrong_more",
        "wrong_identity",
        "short_page",
        "duplicate",
        "bad_row",
        "bad_attr",
    ],
)
def test_video_page_gaps_and_shape_changes_are_explicit(favorites_host, mutation):
    _, host, connection = favorites_host

    def change(url, data):
        if not url.endswith("/resource/list"):
            return
        if mutation == "missing_total":
            data["info"].pop("media_count")
        if mutation == "missing_more":
            data.pop("has_more")
        if mutation == "wrong_more":
            data["has_more"] = False
        if mutation == "wrong_identity":
            data["info"]["id"] = 999
        if mutation == "short_page":
            data["medias"].pop()
        if mutation == "duplicate":
            data["medias"][1] = data["medias"][0]
        if mutation == "bad_row":
            data["medias"][0] = None
        if mutation == "bad_attr":
            data["medias"][0]["attr"] = "0"

    host.provider.after_read = change
    with pytest.raises(AppError) as error:
        Favorites(host).browse(browse(connection))
    assert error.value.code == "BILI_FAVORITE_PAGE_INCOMPLETE"


def test_guest_and_expired_connections_do_not_query_account_favorites(favorites_host):
    _, host, _ = favorites_host
    guest = host.access.connect().connection_id
    expired = host.access.connect("20", "expired fixture", "expired").connection_id
    for connection in (guest, expired):
        with pytest.raises(AppError):
            Favorites(host).folders(connection)
    assert FixtureProvider.calls == []


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("unknown_type", "BILI_FAVORITE_TYPE_UNKNOWN"),
        ("missing_more", "BILI_FAVORITE_PAGE_INCOMPLETE"),
        ("truncated_collection", "BILI_FAVORITE_PAGE_INCOMPLETE"),
        ("bad_title", "BILI_FAVORITE_PAGE_INCOMPLETE"),
    ],
)
def test_followed_protocol_gaps_are_not_silently_other_directory_types(
    favorites_host, mutation, code
):
    _, host, connection = favorites_host

    def change(url, data):
        if url.endswith("/collected/list"):
            if mutation == "unknown_type":
                data["list"][0]["type"] = 99
            if mutation == "missing_more":
                data.pop("has_more")
        if url.endswith("/season/list"):
            if mutation == "truncated_collection":
                data["medias"].pop()
            if mutation == "bad_title":
                data["info"]["title"] = {"unexpected": True}

    host.provider.after_read = change
    with pytest.raises(AppError) as error:
        Favorites(host).browse(
            FavoriteBrowse(
                connection_id=connection,
                kind="collected",
                folder_id="500",
                resource_type="collection",
            )
        )
    assert error.value.code == code


def test_favorite_pages_use_the_host_session_and_csrf_contract(favorites_host, tmp_path):
    repo, _, connection = favorites_host
    app = create_app(
        AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "runtime"),
        repository=repo,
        integrations_provider_factory=FixtureFavoriteProvider,
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        path = "/api/v1/integrations/bilibili/favorites/folders"
        assert client.post(path, json={"connection_id": connection}).status_code == 403
        client.headers["X-Learning-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        page = client.post(path, json={"connection_id": connection, "page": 2})
        assert page.status_code == 200 and len(page.json()["items"]) == 1
        assert page.json()["items"][0]["resource_type"] == "folder"
        denied = client.post(
            "/api/v1/integrations/bilibili/favorites/browse",
            json={"connection_id": connection, "folder_id": "10021", "folder_page": 1},
        )
        assert denied.status_code == 403 and denied.json()["code"] == "BILI_FAVORITE_SCOPE_DENIED"


def test_followed_collection_import_preserves_original_collection_video_and_part_order(
    favorites_host,
):
    _, host, connection = favorites_host
    origin = FavoriteOrigin(
        folder_id="500", kind="collected", resource_type="collection", folder_page=1, video_page=2
    )
    plan = host.resolve(
        "a", ResolveInput(connection_id=connection, url="BV0000000021", favorite_origin=origin)
    )
    receipt = host.import_selected(
        "a",
        ImportSelection(
            operation_id="from_followed", plan_id=plan.plan_id, object_ids=[plan.parts[1].object_id]
        ),
    )
    item = receipt.items[0]
    assert (
        item.path.startswith("合成关注合集-")
        and "/000021 " in item.path
        and item.path.endswith("/002 合成课2.bili")
    )
    relation = host.source(item.asset_id)["payload"]["collection"]
    assert relation["group_id"] == "collection:99:500" and relation["ordinal"] == 21
    assert relation["url"] == "https://space.bilibili.com/99/lists/500?type=season"


@pytest.mark.parametrize(
    "bvid,page", [("BV0000000001", 2), ("BV0000000002", 1), ("BV0000000004", 1)]
)
def test_favorite_origin_revalidates_selected_page_and_available_video_before_part_resolution(
    favorites_host, bvid, page
):
    _, host, connection = favorites_host
    origin = FavoriteOrigin(
        folder_id="10021", kind="created", resource_type="folder", folder_page=2, video_page=page
    )
    with pytest.raises(AppError) as denied:
        host.resolve("a", ResolveInput(connection_id=connection, url=bvid, favorite_origin=origin))
    assert denied.value.code == "BILI_FAVORITE_VIDEO_DENIED"
    assert not any(c[0] == "resolve" for c in FixtureProvider.calls)
