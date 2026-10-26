"""Opening a file must not wait for unrelated extractor metadata (no real network)."""

import json

import httpx
from test_integrations import imported, setup  # noqa: F401

from learning_app.course_files import CourseFileStore
from learning_app.integrations.bilibili.provider import BilibiliProvider
from learning_app.workbench.preparations import PreparationStore


def test_file_open_skips_slow_chapters_and_reuses_metadata_connection(setup, monkeypatch):  # noqa: F811
    repo, host = setup
    item = imported(host)
    host._provider = BilibiliProvider(host.access, host.private, host.project_root)
    calls, clients = [], []
    play_info = {
        "timelength": 2000,
        "dash": {
            "video": [
                {
                    "id": 80,
                    "codecs": "avc1.640028",
                    "mimeType": "video/mp4",
                    "width": 160,
                    "height": 96,
                    "baseUrl": "https://example.bilivideo.com/1/video.m4s",
                }
            ],
            "audio": [
                {
                    "id": 30280,
                    "codecs": "mp4a.40.2",
                    "mimeType": "audio/mp4",
                    "baseUrl": "https://example.bilivideo.com/1/audio.m4s",
                }
            ],
        },
    }
    state = {
        "videoData": {
            "bvid": "BV1234567890",
            "aid": 1,
            "cid": 1,
            "title": "Synthetic",
            "pages": [{"cid": 1}, {"cid": 2}],
        }
    }

    def http(request):
        calls.append(request.url.path)
        if request.url.host == "www.bilibili.com":
            return httpx.Response(
                200,
                text=(
                    "<script>window.__INITIAL_STATE__=" + json.dumps(state) + ";</script>"
                    "<script>window.__playinfo__=" + json.dumps({"data": play_info}) + ";</script>"
                ),
            )
        if request.url.path == "/x/player/pagelist":
            data = [{"page": 1, "cid": 1}, {"page": 2, "cid": 2}]
        elif request.url.path == "/x/web-interface/nav":
            data = {
                "wbi_img": {
                    "img_url": "https://i.hdslb.com/" + "a" * 32 + ".png",
                    "sub_url": "https://i.hdslb.com/" + "b" * 32 + ".png",
                }
            }
        elif request.url.path == "/x/player/wbi/playurl":
            assert request.url.params["cid"] == "1"
            data = play_info
        elif request.url.path == "/x/player/wbi/v2":
            # This optional request can take the entire upstream timeout before
            # files/open returns. Reproduce that failure without sleeping.
            raise httpx.ReadTimeout("synthetic slow chapter service", request=request)
        else:
            raise AssertionError(f"Unexpected request: {request.url.path}")
        return httpx.Response(200, json={"code": 0, "data": data})

    client_type = httpx.Client

    def client(*args, **kwargs):
        clients.append(True)
        return client_type(*args, transport=httpx.MockTransport(http), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    opened = CourseFileStore(repo).open("a", item.path)
    assert opened["video"]["playback_session_id"]
    assert "/x/player/wbi/v2" not in calls, f"File open waited for chapter timeout: {calls}"
    assert len(clients) == 1, "Reuse the HTTP connection during one media resolution"
    assert PreparationStore(repo).list("a") == []
    assert all("subtitle" not in call and "bilivideo" not in call for call in calls)
