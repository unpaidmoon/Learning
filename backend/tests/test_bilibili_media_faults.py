"""Synthetic transport faults through the real remote media Interface, no platform/media."""

import threading

import httpx
import pytest
from starlette.requests import Request
from test_integrations import FixtureProvider, imported

from learning_app.config import PROJECT_ROOT
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.integrations.host import IntegrationHost
from learning_app.integrations.models import ImportSelection, ResolveInput
from learning_app.storage import Repository


class FaultProvider(FixtureProvider):
    def __init__(self, access, private, root):
        super().__init__(access, private, root)
        self.resolutions = 0
        self.before_resolve = None

    def headers(self, grant, url):
        self.access.check(grant)
        return {"User-Agent": "synthetic-media-fault"}

    def media(self, grant, part):
        self.access.check(grant)
        if self.before_resolve:
            self.before_resolve(part)
        self.resolutions += 1
        return {
            "video": {
                "url": f"https://example.bilivideo.com/v{self.resolutions}",
                "format_id": "synthetic-h264",
            },
            "audio": None,
            "duration_ms": 2000,
        }


@pytest.fixture
def media_host(tmp_path):
    repo = Repository(tmp_path / "data")
    host = IntegrationHost(repo, PROJECT_ROOT, provider_factory=FaultProvider)
    repo.integrations = host
    repo.create_course("a", "a")
    root = tmp_path / "course"
    root.mkdir()
    CourseFileStore(repo).bind("a", str(root))
    item = imported(host)
    yield repo, host, item
    repo.close()


def transport(monkeypatch, handler):
    client_type = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *args, **kwargs: client_type(
            *args, transport=httpx.MockTransport(handler), **kwargs
        ),
    )


def request(range_header="bytes=0-3"):
    return Request(
        {"type": "http", "query_string": b"", "headers": [(b"range", range_header.encode())]}
    )


async def body(response):
    return b"".join([chunk async for chunk in response.body_iterator])


async def test_browser_416_does_not_poison_session_or_trigger_download_restart(
    media_host, monkeypatch
):
    _, host, item = media_host
    calls = []

    def http(req):
        calls.append(req)
        return (
            httpx.Response(416)
            if len(calls) == 1
            else httpx.Response(
                206,
                content=b"abcd",
                headers={"Content-Length": "4", "Content-Range": "bytes 0-3/4"},
            )
        )

    transport(monkeypatch, http)
    original = host.media.session(host.source(item.asset_id))["session_id"]
    with pytest.raises(AppError) as rejected:
        host.media.content(item.asset_id, item.source_version, request("bytes=999-"))
    assert rejected.value.code == "MEDIA_RANGE_INVALID" and len(calls) == 1
    response = host.media.content(item.asset_id, item.source_version, request("bytes=0-3"))
    assert await body(response) == b"abcd"
    await response.background()
    assert host.media.session(host.source(item.asset_id))["session_id"] == original
    assert host.provider.resolutions == 1 and len(calls) == 2


async def test_expired_url_does_not_retry_and_explicit_reopen_gets_fresh_tracks(
    media_host, monkeypatch
):
    _, host, item = media_host
    calls = []

    def http(req):
        calls.append(req.url.path)
        return (
            httpx.Response(403)
            if req.url.path == "/v1"
            else httpx.Response(
                206,
                content=b"abcd",
                headers={"Content-Range": "bytes 0-3/4", "Content-Length": "4"},
            )
        )

    transport(monkeypatch, http)
    host.media.open(item.asset_id, item.source_version)
    with pytest.raises(AppError) as expired:
        host.media.content(item.asset_id, item.source_version, request())
    assert expired.value.code == "BILI_MEDIA_UNAVAILABLE" and calls == ["/v1"]
    host.media.open(item.asset_id, item.source_version)
    assert host.provider.resolutions == 2
    assert await body(host.media.content(item.asset_id, item.source_version, request())) == b"abcd"
    assert calls == ["/v1", "/v2"]


async def test_late_old_failure_cannot_invalidate_a_new_playback_session(media_host, monkeypatch):
    _, host, item = media_host
    calls = []

    def http(req):
        calls.append(req.url.path)
        if req.url.path == "/v1":
            host.media.open(item.asset_id, item.source_version, refresh=True)
            return httpx.Response(403)
        return httpx.Response(
            206, content=b"abcd", headers={"Content-Range": "bytes 0-3/4", "Content-Length": "4"}
        )

    transport(monkeypatch, http)
    host.media.open(item.asset_id, item.source_version)
    with pytest.raises(AppError):
        host.media.content(item.asset_id, item.source_version, request())
    assert await body(host.media.content(item.asset_id, item.source_version, request())) == b"abcd"
    assert host.provider.resolutions == 2 and calls == ["/v1", "/v2"]


@pytest.mark.parametrize("value", ["bytes=-", "bytes=3-2", "bytes=-0", "anything"])
def test_invalid_client_ranges_do_not_poison_valid_session_or_dispatch(
    media_host, monkeypatch, value
):
    _, host, item = media_host
    calls = []
    transport(monkeypatch, lambda req: calls.append(req))
    host.media.open(item.asset_id, item.source_version)
    with pytest.raises(AppError) as invalid:
        host.media.content(item.asset_id, item.source_version, request(value))
    assert invalid.value.code == "MEDIA_RANGE_INVALID" and calls == []
    host.media.session(host.source(item.asset_id))
    assert host.provider.resolutions == 1


def test_old_playback_url_is_rejected_locally_after_explicit_refresh(media_host, monkeypatch):
    _, host, item = media_host
    host.media.open(item.asset_id, item.source_version)
    old = host.media.view(item.asset_id, item.source_version)["playback_session_id"]
    host.media.open(item.asset_id, item.source_version, refresh=True)
    calls = []
    transport(monkeypatch, lambda req: calls.append(req))
    stale = Request(
        {
            "type": "http",
            "query_string": ("playback_session=" + old).encode(),
            "headers": [(b"range", b"bytes=0-3")],
        }
    )
    with pytest.raises(AppError) as blocked:
        host.media.content(item.asset_id, item.source_version, stale)
    assert blocked.value.code == "BILI_MEDIA_SESSION_STALE" and calls == []


def test_failed_session_blocks_browser_followup_until_explicit_open(media_host, monkeypatch):
    _, host, item = media_host
    calls = []

    def http(req):
        calls.append(req.url.path)
        return httpx.Response(403)

    transport(monkeypatch, http)
    host.media.open(item.asset_id, item.source_version)
    with pytest.raises(AppError):
        host.media.content(item.asset_id, item.source_version, request())
    with pytest.raises(AppError) as blocked:
        host.media.content(item.asset_id, item.source_version, request())
    assert blocked.value.code == "BILI_MEDIA_SESSION_STALE"
    assert calls == ["/v1"] and host.provider.resolutions == 1


def test_download_rejects_inconsistent_range_lengths_before_writing(
    media_host, monkeypatch, tmp_path
):
    _, host, item = media_host
    transport(
        monkeypatch,
        lambda req: httpx.Response(
            206, content=b"abcd", headers={"Content-Range": "bytes 3-99/100", "Content-Length": "4"}
        ),
    )
    target = tmp_path / "track.mp4"
    target.with_suffix(".mp4.partial").write_bytes(b"old")
    session = host.media.session(host.source(item.asset_id))
    with pytest.raises(AppError):
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    assert not target.exists() and target.with_suffix(".mp4.partial").read_bytes() == b"old"


def test_another_sources_slow_resolution_does_not_block_existing_playback_session(media_host):
    _, host, item = media_host
    account = host.source(item.asset_id)["connection_id"]
    plan = host.resolve("a", ResolveInput(connection_id=account, url="BV1234567890"))
    other = host.import_selected(
        "a",
        ImportSelection(
            operation_id="other_part", plan_id=plan.plan_id, object_ids=[plan.parts[1].object_id]
        ),
    ).items[0]
    host.media.open(item.asset_id, item.source_version)
    entered, release, foreground = threading.Event(), threading.Event(), threading.Event()

    def slow(part):
        if part["cid"] == 2:
            entered.set()
            release.wait(5)

    host.provider.before_resolve = slow
    background = threading.Thread(
        target=lambda: host.media.session(host.source(other.asset_id)), daemon=True
    )
    reader = threading.Thread(
        target=lambda: (host.media.session(host.source(item.asset_id)), foreground.set()),
        daemon=True,
    )
    background.start()
    try:
        assert entered.wait(2)
        reader.start()
        ready = foreground.wait(1)
    finally:
        release.set()
        background.join(5)
        if reader.ident:
            reader.join(5)
    assert ready, "Existing source waited on another source's network resolution"


def test_interrupted_track_keeps_checkpoint_and_only_explicit_call_resumes(
    media_host, monkeypatch, tmp_path
):
    _, host, item = media_host
    chunk = 128 * 1024
    data = b"a" * chunk + b"b" * chunk
    calls = []

    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield data[:chunk]
            raise httpx.ReadError("synthetic disconnected stream")

    def http(req):
        calls.append(req.headers.get("range"))
        if len(calls) == 1:
            return httpx.Response(
                200,
                stream=Interrupted(),
                headers={"Content-Length": str(len(data)), "ETag": '"unchanged"'},
            )
        return httpx.Response(
            206,
            content=data[chunk:],
            headers={
                "Content-Length": str(chunk),
                "Content-Range": f"bytes {chunk}-{len(data) - 1}/{len(data)}",
                "ETag": '"unchanged"',
            },
        )

    transport(monkeypatch, http)
    target = tmp_path / "track.mp4"
    session = host.media.session(host.source(item.asset_id))
    with pytest.raises(AppError) as interrupted:
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    assert interrupted.value.code == "BILI_DOWNLOAD_INTERRUPTED"
    assert not target.exists() and target.with_suffix(".mp4.partial").read_bytes() == data[:chunk]
    assert calls == [None]
    host.media.download_track(session, session["video"], target, threading.Event(), lambda *_: None)
    assert target.read_bytes() == data and calls == [None, f"bytes={chunk}-"]


def test_cancel_during_download_keeps_bytes_and_does_not_publish_complete_track(
    media_host, monkeypatch, tmp_path
):
    _, host, item = media_host
    cancel = threading.Event()
    data = b"a" * (256 * 1024)
    transport(
        monkeypatch,
        lambda req: httpx.Response(200, content=data, headers={"Content-Length": str(len(data))}),
    )
    session = host.media.session(host.source(item.asset_id), cancel)
    target = tmp_path / "track.mp4"

    def progress(received, total):
        cancel.set()

    with pytest.raises(AppError) as stopped:
        host.media.download_track(session, session["video"], target, cancel, progress)
    assert stopped.value.code == "PREPARATION_INTERRUPTED" and not target.exists()
    assert target.with_suffix(".mp4.partial").stat().st_size == 128 * 1024
