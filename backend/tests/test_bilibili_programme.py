"""Synthetic programme metadata through the real provider and host interfaces."""

import asyncio
import copy
import os
import subprocess
import sys
import threading

import httpx
import pytest
from test_bilibili_media_faults import transport

from learning_app.config import PROJECT_ROOT
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.integrations.bilibili.provider import BilibiliProvider
from learning_app.integrations.host import IntegrationHost
from learning_app.integrations.models import ImportSelection, ResolveInput
from learning_app.integrations.native import input_links
from learning_app.storage import Repository
from learning_app.workbench.preparations import PreparationStore

CLIENT_TYPE = httpx.Client


def programme():
    return {
        "season_id": 100,
        "title": "合成会员目录",
        "total": 2,
        "episodes": [
            {
                "id": 110,
                "cid": 910,
                "aid": 810,
                "title": "1",
                "long_title": "合成第一集",
                "duration": 120000,
            },
            {
                "id": 120,
                "cid": 920,
                "aid": 820,
                "title": "2",
                "long_title": "合成第二集",
                "duration": 120000,
                "status": 13,
            },
        ],
    }


@pytest.fixture
def programme_host(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "data")
    host = IntegrationHost(repo, PROJECT_ROOT, provider_factory=BilibiliProvider)
    repo.integrations = host
    repo.create_course("合成课程", "a")
    folder = tmp_path / "course"
    folder.mkdir()
    CourseFileStore(repo).bind("a", str(folder))
    host.access.change("bilibili", True)
    connection = host.access.connect("10", "合成账号", "connected").connection_id
    data, calls = programme(), []

    def http(req):
        calls.append(req)
        assert req.url.host == "api.bilibili.com" and req.url.path == "/pgc/view/web/season"
        return httpx.Response(200, json={"code": 0, "result": copy.deepcopy(data)})

    transport(monkeypatch, http)
    yield repo, host, connection, data, calls
    repo.close()


def test_programme_season_is_metadata_only_and_manual_episode_import_keeps_order(programme_host):
    _, host, connection, _, calls = programme_host
    plan = host.resolve(
        "a",
        ResolveInput(connection_id=connection, url="https://www.bilibili.com/bangumi/play/ss100"),
    )
    assert plan.complete and [p.object_id for p in plan.parts] == [
        "bangumi:110:910",
        "bangumi:120:920",
    ]
    assert plan.parts[1].duration_ms == 120000 and plan.parts[1].availability == "unknown"
    item = host.import_selected(
        "a",
        ImportSelection(
            operation_id="programme-p2", plan_id=plan.plan_id, object_ids=[plan.parts[1].object_id]
        ),
    ).items[0]
    source = host.source(item.asset_id)
    assert source["payload"]["url"] == "https://www.bilibili.com/bangumi/play/ep120"
    assert source["payload"]["page"] == 2 and item.path.endswith("/002 2 合成第二集.bili")
    assert len(calls) == 1 and calls[0].url.params["season_id"] == "100"


def test_explicit_episode_freezes_only_that_episode_and_uses_same_native_link_scope(programme_host):
    _, host, connection, _, calls = programme_host
    url = "https://www.bilibili.com/bangumi/play/ep120"
    assert input_links("请只读查看 " + url) == [url]
    plan = host.resolve("a", ResolveInput(connection_id=connection, url=url))
    assert len(plan.parts) == 1 and plan.parts[0].object_id == "bangumi:120:920"
    assert calls[0].url.params["ep_id"] == "120"


def test_sections_keep_order_and_duplicate_episode_identity_is_not_imported_twice(programme_host):
    _, host, connection, data, _ = programme_host
    extra = {"id": 130, "cid": 930, "aid": 830, "title": "花絮", "duration": 30000}
    data["section"] = [
        {"title": "附加内容", "episodes": [copy.deepcopy(data["episodes"][1]), extra]}
    ]
    plan = host.resolve(
        "a",
        ResolveInput(connection_id=connection, url="https://www.bilibili.com/bangumi/play/ss100"),
    )
    assert [p.object_id for p in plan.parts] == [
        "bangumi:110:910",
        "bangumi:120:920",
        "bangumi:130:930",
    ]
    assert [p.page for p in plan.parts] == [1, 2, 3]


@pytest.mark.parametrize(
    "change", ["count", "missing_cid", "duration", "duplicate", "sections", "season"]
)
def test_incomplete_or_changed_programme_never_becomes_a_complete_import(programme_host, change):
    repo, host, connection, data, calls = programme_host
    if change == "count":
        data["total"] = 3
    elif change == "missing_cid":
        data["episodes"][1].pop("cid")
    elif change == "duration":
        data["episodes"][1]["duration"] = True
    elif change == "duplicate":
        duplicate = {**data["episodes"][1], "cid": 999}
        data["section"] = [{"episodes": [duplicate]}]
    elif change == "sections":
        data["section"] = [{"episodes": None}]
    else:
        data["season_id"] = 999
    with pytest.raises(AppError) as error:
        host.resolve(
            "a",
            ResolveInput(
                connection_id=connection, url="https://www.bilibili.com/bangumi/play/ss100"
            ),
        )
    assert error.value.code == (
        "BILI_SOURCE_VERSION_CHANGED"
        if change == "season"
        else "BILI_PROGRAMME_DIRECTORY_INCOMPLETE"
    )
    with repo.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM integration_sources").scalar_one() == 0
    assert len(calls) == 1


def test_selected_episode_does_not_claim_a_not_yet_complete_season(programme_host):
    _, host, connection, data, _ = programme_host
    data["total"] = 3
    data["episodes"][0].pop("cid")
    plan = host.resolve(
        "a",
        ResolveInput(connection_id=connection, url="https://www.bilibili.com/bangumi/play/ep120"),
    )
    assert plan.complete and len(plan.parts) == 1 and plan.parts[0].page == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://www.bilibili.com/bangumi/play/ss0",
        "http://www.bilibili.com/bangumi/play/ep120",
        "https://www.bilibili.com:443/bangumi/play/ep120",
        "https://user@www.bilibili.com/bangumi/play/ep120",
        "https://www.bilibili.com.evil.invalid/bangumi/play/ep120",
    ],
)
def test_unsupported_or_foreign_programme_link_is_denied_without_requests(programme_host, url):
    _, host, connection, _, calls = programme_host
    assert input_links(url) == []
    with pytest.raises(AppError):
        host.resolve("a", ResolveInput(connection_id=connection, url=url))
    assert calls == []


def test_programme_native_read_uses_only_frozen_link_and_never_imports(programme_host):
    from test_bilibili_native_query import call

    repo, host, _, _, calls = programme_host
    url = "https://www.bilibili.com/bangumi/play/ep120"
    frozen = host.native.freeze("a", "r1", "请只读查看 " + url)
    args = {"operation": "inspect", "handle_id": frozen["links"][0]["handle_id"], "page": 1}
    result = host.native.query(frozen, args, call(frozen, args), lambda: None)
    assert result["coverage"] == "directory_only" and len(result["parts"]) == 1
    assert not result["media_opened"] and not result["imported"] and len(calls) == 1
    with repo.engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM integration_sources").scalar_one() == 0


@pytest.mark.parametrize("case", ["full", "preview", "short", "wrong_cid", "premium_only", "area"])
def test_pinned_programme_extractor_checks_access_preview_and_cid_before_media(
    programme_host, monkeypatch, case
):
    _, host, connection, data, _ = programme_host
    plan = host.resolve(
        "a",
        ResolveInput(connection_id=connection, url="https://www.bilibili.com/bangumi/play/ep120"),
    )
    item = host.import_selected(
        "a",
        ImportSelection(
            operation_id="programme-media",
            plan_id=plan.plan_id,
            object_ids=[plan.parts[0].object_id],
        ),
    ).items[0]
    # This is the actual pinned extractor, with synthetic HTML and API bodies only.
    calls = []
    cid = 999 if case == "wrong_cid" else 920
    video_info = {
        "timelength": 30000 if case == "short" else 120000,
        "dash": {
            "video": [
                {
                    "id": 80,
                    "codecs": "avc1.640028",
                    "mimeType": "video/mp4",
                    "width": 160,
                    "height": 96,
                    "baseUrl": f"https://example.bilivideo.com/{cid}/video.m4s",
                }
            ],
            "audio": [
                {
                    "id": 30280,
                    "codecs": "mp4a.40.2",
                    "mimeType": "audio/mp4",
                    "baseUrl": f"https://example.bilivideo.com/{cid}/audio.m4s",
                }
            ],
        },
    }

    def http(req):
        calls.append(req)
        assert req.url.host in {"www.bilibili.com", "api.bilibili.com"}, (
            "Unselected media must never be read"
        )
        if req.url.path == "/bangumi/play/ep120":
            return httpx.Response(
                200,
                text="您所在的地区无法观看本片"
                if case == "area"
                else "<html><body>synthetic programme page</body></html>",
            )
        if req.url.path == "/pgc/player/web/v2/playurl":
            assert req.url.params["ep_id"] == "120"
            if case == "premium_only":
                return httpx.Response(200, json={"code": -10403, "result": {}})
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "result": {
                        "video_info": video_info,
                        "play_check": {
                            "play_detail": "PLAY_PREVIEW" if case == "preview" else "PLAY_WHOLE"
                        },
                    },
                },
            )
        assert req.url.path == "/pgc/view/web/season" and req.url.params["ep_id"] == "120"
        return httpx.Response(200, json={"code": 0, "result": data})

    # Replace the fixture transport without wrapping the already monkeypatched Client factory twice.
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: CLIENT_TYPE(*args, transport=httpx.MockTransport(http), **kwargs),
    )
    if case == "full":
        session = host.media.session(host.source(item.asset_id))
        assert f"/{cid}/" in session["video"]["url"] and session["audio"] is not None
    else:
        with pytest.raises(AppError) as error:
            host.media.session(host.source(item.asset_id))
        assert (
            error.value.code
            == {
                "preview": "BILI_PREVIEW_ONLY",
                "short": "BILI_PREVIEW_ONLY",
                "wrong_cid": "BILI_MEDIA_IDENTITY_UNKNOWN",
                "premium_only": "BILI_ACCESS_RESTRICTED",
                "area": "BILI_AREA_RESTRICTED",
            }[case]
        )
    assert all(req.url.host != "example.bilivideo.com" for req in calls)


def test_programme_selected_episode_streams_saves_and_reopens_same_local_source(
    programme_host, monkeypatch, tmp_path
):
    from starlette.requests import Request

    from learning_app.integrations.mux import av_runtime
    from learning_app.media import MediaPositionInput, MediaStore

    repo, host, connection, data, _ = programme_host
    data["episodes"][1]["duration"] = 2000
    video, audio = tmp_path / "synthetic.mp4", tmp_path / "synthetic.m4a"
    code = """import sys
from fractions import Fraction
sys.path.insert(0,sys.argv[1])
import av
with av.open(sys.argv[2],'w',options={'movflags':'faststart'}) as out:
 s=out.add_stream('libx264',rate=10);s.width=160;s.height=96;s.pix_fmt='yuv420p'
 for i in range(20):
  f=av.VideoFrame(160,96,'yuv420p')
  for n,p in enumerate(f.planes):p.update(bytes([64+i if n==0 else 128])*p.buffer_size)
  for p in s.encode(f):out.mux(p)
 for p in s.encode(None):out.mux(p)
with av.open(sys.argv[3],'w') as out:
 s=out.add_stream('aac',rate=48000);s.layout='mono'
 for i in range(94):
  f=av.AudioFrame(format='fltp',layout='mono',samples=1024)
  f.sample_rate=48000;f.pts=i*1024;f.time_base=Fraction(1,48000)
  for p in f.planes:p.update(bytes(p.buffer_size))
  for p in s.encode(f):out.mux(p)
 for p in s.encode(None):out.mux(p)
"""
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            code,
            str(av_runtime(PROJECT_ROOT)),
            str(video),
            str(audio),
        ],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    blobs = {"/920/video.m4s": video.read_bytes(), "/920/audio.m4s": audio.read_bytes()}
    calls = []

    def http(req):
        calls.append(req)
        if req.url.path == "/bangumi/play/ep120":
            return httpx.Response(200, text="<html>synthetic programme</html>")
        if req.url.path == "/pgc/view/web/season":
            return httpx.Response(200, json={"code": 0, "result": data})
        if req.url.path == "/pgc/player/web/v2/playurl":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "result": {
                        "video_info": {
                            "timelength": 2000,
                            "dash": {
                                "video": [
                                    {
                                        "id": 80,
                                        "codecs": "avc1.640028",
                                        "mimeType": "video/mp4",
                                        "width": 160,
                                        "height": 96,
                                        "baseUrl": "https://example.bilivideo.com/920/video.m4s",
                                    }
                                ],
                                "audio": [
                                    {
                                        "id": 30280,
                                        "codecs": "mp4a.40.2",
                                        "mimeType": "audio/mp4",
                                        "baseUrl": "https://example.bilivideo.com/920/audio.m4s",
                                    }
                                ],
                            },
                        },
                        "play_check": {"play_detail": "PLAY_WHOLE"},
                    },
                },
            )
        assert req.url.host == "example.bilivideo.com" and req.url.path in blobs
        body = blobs[req.url.path]
        if requested := req.headers.get("range"):
            assert requested == "bytes=0-31"
            return httpx.Response(
                206,
                content=body[:32],
                headers={"Content-Length": "32", "Content-Range": f"bytes 0-31/{len(body)}"},
            )
        return httpx.Response(
            200, content=body, headers={"Content-Length": str(len(body)), "ETag": '"fixture"'}
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: CLIENT_TYPE(*args, transport=httpx.MockTransport(http), **kwargs),
    )
    plan = host.resolve(
        "a",
        ResolveInput(connection_id=connection, url="https://www.bilibili.com/bangumi/play/ss100"),
    )
    item = host.import_selected(
        "a",
        ImportSelection(
            operation_id="save-programme-p2",
            plan_id=plan.plan_id,
            object_ids=[plan.parts[1].object_id],
        ),
    ).items[0]
    opened = MediaStore(repo).open(item.asset_id, item.source_version)
    assert opened["playback_session_id"] is not None and opened["audio_content_url"]
    assert not any(req.url.host == "example.bilivideo.com" for req in calls)
    for track in ("video", "audio"):
        request = Request(
            {
                "type": "http",
                "query_string": (
                    "track=" + track + "&playback_session=" + opened["playback_session_id"]
                ).encode(),
                "headers": [(b"range", b"bytes=0-31")],
            }
        )
        response = host.media.content(item.asset_id, item.source_version, request)
        assert response.status_code == 206

        async def body():
            return b"".join([block async for block in response.body_iterator])

        assert asyncio.run(body()) == blobs[f"/920/{track}.m4s"][:32]
        asyncio.run(response.background())
    MediaStore(repo).save_position(
        item.asset_id,
        MediaPositionInput(
            source_version=item.source_version,
            position_ms=1000,
            expected_revision=opened["position_revision"],
        ),
    )
    job = host.queue("a", item.asset_id, item.source_version, "platform_download")
    store = PreparationStore(repo)
    lease = store.claim()
    result = host.prepare(lease, threading.Event(), lambda *_: None)
    assert host.source(item.asset_id)["local_path"] is None
    assert store.finish(lease, result) and store.get("a", job.job_id).state == "completed"
    before = len(calls)
    host.access.change("bilibili", False)
    local = MediaStore(repo).open(item.asset_id, item.source_version)
    assert local["asset_id"] == item.asset_id and local["source_version"] == item.source_version
    assert local["playback_session_id"] is None and local["size_bytes"] > 0 and len(calls) == before
    assert local["position_ms"] == 1000
