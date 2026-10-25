"""Bounded UI-only cover fetching, stable source identity, and offline cache."""

import struct
import zlib

import httpx
import pytest
from fastapi.testclient import TestClient

from learning_app.app import create_app
from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore, CourseMoveInput, CourseRenameInput
from learning_app.errors import AppError
from learning_app.integrations.bilibili.provider import BilibiliProvider
from learning_app.integrations.covers import MAX_COVER_BYTES
from learning_app.integrations.host import IntegrationHost
from learning_app.integrations.models import ImportSelection, ResolveInput
from learning_app.storage import Repository

IMAGE_URL = "https://i0.hdslb.com/fixture-cover.png"


def synthetic_cover():
    def chunk(kind, data):
        return (
            struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
        )

    pixels = b"".join(b"\x00" + bytes((40 + y // 2, 130, 180)) * 240 for y in range(135))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 240, 135, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def cover_host(tmp_path, monkeypatch):
    calls = []
    state = {"url": IMAGE_URL, "status": 200, "data": synthetic_cover(), "during_read": None}

    class ImageStream(httpx.SyncByteStream):
        def __iter__(self):
            yield state["data"][:65536]
            if state["during_read"]:
                state["during_read"]()
            yield state["data"][65536:]

    def http(request):
        calls.append(str(request.url).split("?", 1)[0])
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "aid": 1,
                        "bvid": "BV1234567890",
                        "title": "合成封面视频",
                        "videos": 1,
                        "pic": state["url"],
                        "pages": [{"cid": 1, "page": 1, "duration": 2, "part": "合成第一P"}],
                    },
                },
            )
        assert str(request.url) == state["url"].replace("http://", "https://", 1)
        assert "Cookie" not in request.headers
        return httpx.Response(
            state["status"],
            stream=ImageStream(),
            headers={"Location": "https://invalid.example/private"},
        )

    client_type = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: client_type(*args, transport=httpx.MockTransport(http), **kwargs),
    )
    repo = Repository(tmp_path / "data")
    host = IntegrationHost(repo, PROJECT_ROOT, provider_factory=BilibiliProvider)
    repo.integrations = host
    root = tmp_path / "course"
    root.mkdir()
    course = CourseFileStore(repo).bind(None, str(root))["course_id"]
    host.access.change("bilibili", True)
    account = host.access.connect().connection_id
    host.private.put(account, {"cookies": {"SESSDATA": "fixture-only"}})
    plan = host.resolve(course, ResolveInput(connection_id=account, url="BV1234567890"))
    item = host.import_selected(
        course,
        ImportSelection(
            operation_id="cover_fixture",
            plan_id=plan.plan_id,
            object_ids=[plan.parts[0].object_id],
        ),
    ).items[0]
    yield repo, host, course, item, calls, state
    repo.close()


def test_cover_is_ui_only_and_cache_survives_rename_move_disable_and_reopen(cover_host):
    repo, host, course, item, calls, _ = cover_host
    before = repo.resolve_source(item.asset_id, item.source_version)
    response = host.covers.response(course, item.asset_id, item.source_version)
    assert response.body == synthetic_cover() and response.media_type == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    files = CourseFileStore(repo)
    renamed = files.rename(
        course, CourseRenameInput(operation_id="cover_rename", path=item.path, name="本地改名.bili")
    )
    moved = files.move(
        course,
        CourseMoveInput(operation_id="cover_move", path=renamed["entry"]["path"], destination=""),
    )
    assert moved["entry"]["asset_id"] == item.asset_id
    assert (
        repo.resolve_source(item.asset_id, item.source_version)["content_hash"]
        == before["content_hash"]
    )
    host.access.change("bilibili", False)
    reopened = IntegrationHost(repo, PROJECT_ROOT, provider_factory=BilibiliProvider)
    assert (
        reopened.covers.response(course, item.asset_id, item.source_version).body == response.body
    )
    assert reopened._provider is None
    assert calls == ["https://api.bilibili.com/x/web-interface/view", IMAGE_URL]
    with pytest.raises(AppError) as denied:
        reopened.covers.response("another-course", item.asset_id, item.source_version)
    assert denied.value.code == "SOURCE_COURSE_DENIED"
    with pytest.raises(AppError) as denied:
        reopened.covers.response(course, item.asset_id, "old-version")
    assert denied.value.code == "SOURCE_VERSION_MISMATCH"


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("foreign", "PLATFORM_URL_DENIED"),
        ("non_image_service", "BILI_COVER_TARGET_DENIED"),
        ("redirect", "BILI_COVER_UNAVAILABLE"),
        ("svg", "BILI_COVER_FORMAT_UNSUPPORTED"),
        ("oversize", "BILI_COVER_SIZE_INVALID"),
        ("disabled", "PLATFORM_DISABLED"),
        ("during_read", "PLATFORM_DISABLED"),
        ("malformed", "BILI_COVER_TARGET_DENIED"),
        ("bad_port", "BILI_COVER_TARGET_DENIED"),
    ],
)
def test_cover_failure_cannot_publish_or_fetch_other_targets(cover_host, scenario, expected):
    repo, host, course, item, calls, state = cover_host
    # Existing objects intentionally exercise metadata-only backfill as well.
    part = dict(host.source(item.asset_id)["payload"])
    part.pop("cover_url")
    if scenario == "foreign":
        state["url"] = "https://invalid.example/cover.png"
    if scenario == "non_image_service":
        state["url"] = "https://api.bilibili.com/cover.png"
    if scenario == "redirect":
        state["status"] = 302
    if scenario == "malformed":
        state["url"] = "https://[invalid/cover.png"
    if scenario == "bad_port":
        state["url"] = "https://i0.hdslb.com:invalid/cover.png"
    if scenario == "svg":
        state["data"] = b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'
    if scenario == "oversize":
        state["data"] = b"\xff\xd8\xff" + b"0" * MAX_COVER_BYTES
    if scenario == "disabled":
        host.access.change("bilibili", False)
        with pytest.raises(AppError) as error:
            host.covers.response(course, item.asset_id, item.source_version)
    else:
        grant = host.access.grant(host.source(item.asset_id)["connection_id"])
        if scenario == "during_read":
            state["data"] += b"0" * 65536
            state["during_read"] = lambda: host.access.change("bilibili", False)
        with pytest.raises(AppError) as error:
            host.provider.cover(grant, part)
    assert error.value.code == expected
    assert not list((repo.root / "platform-covers").glob("*.image"))
    assert all("invalid.example" not in call for call in calls)


def test_http_cover_requires_host_browser_session_and_checks_version(cover_host, tmp_path):
    repo, _, course, item, _, _ = cover_host
    app = create_app(
        AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "run"),
        repository=repo,
        integrations_provider_factory=BilibiliProvider,
    )
    path = f"/api/v1/courses/{course}/integrations/{item.asset_id}/cover"
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get(path, params={"source_version": item.source_version}).status_code == 403
        client.get("/api/v1/session")
        assert (
            client.get(path, params={"source_version": item.source_version}).content
            == synthetic_cover()
        )
        assert client.get(path, params={"source_version": "old-version"}).status_code == 409


def test_observed_cover_host_is_allowed_only_for_images_not_media(cover_host):
    from learning_app.integrations.bilibili.provider import allowed_url

    _, host, _, item, _, state = cover_host
    state["url"] = "https://archive.biliimg.com/fixture.png"
    part = dict(host.source(item.asset_id)["payload"])
    part.pop("cover_url")
    grant = host.access.grant(host.source(item.asset_id)["connection_id"])
    assert host.provider.cover(grant, part) == synthetic_cover()
    with pytest.raises(AppError) as denied:
        allowed_url(state["url"])
    assert denied.value.code == "PLATFORM_URL_DENIED"


def test_old_metadata_image_scheme_is_upgraded_before_dispatch(cover_host):
    _, host, _, item, calls, state = cover_host
    state["url"] = "http://i2.hdslb.com/fixture.png"
    part = dict(host.source(item.asset_id)["payload"])
    part.pop("cover_url")
    grant = host.access.grant(host.source(item.asset_id)["connection_id"])
    assert host.provider.cover(grant, part) == synthetic_cover()
    assert calls[-1] == "https://i2.hdslb.com/fixture.png"
    state["url"] = "https://archive.biliimg.com.invalid/fixture.png"
    with pytest.raises(AppError) as denied:
        host.provider.cover(grant, part)
    assert denied.value.code == "PLATFORM_URL_DENIED"
