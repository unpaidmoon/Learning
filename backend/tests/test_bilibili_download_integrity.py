"""Entity identity and durable partial bytes; synthetic transport only."""

# pytest injects the imported fixture by name, shadowing its module binding.
# ruff: noqa: F811

import json
import os
import subprocess
import sys
import threading

import httpx
import pytest
from test_bilibili_media_faults import media_host as media_host
from test_bilibili_media_faults import transport

from learning_app.errors import AppError

CHUNK = 128 * 1024
DATA = b"a" * CHUNK + b"b" * CHUNK


class Broken(httpx.SyncByteStream):
    def __iter__(self):
        yield DATA[:CHUNK]
        raise httpx.ReadError("synthetic interruption")


def interrupted(host, item, target):
    session = host.media.session(host.source(item.asset_id))
    with pytest.raises(AppError) as error:
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    assert error.value.code == "BILI_DOWNLOAD_INTERRUPTED"
    return session


@pytest.mark.parametrize("range_info", [None, f"bytes */{CHUNK}"])
def test_upstream_416_preserves_checkpoint_until_explicit_full_restart(
    media_host, monkeypatch, tmp_path, range_info
):
    _, host, item = media_host
    requests = []
    replacement = b"x" * CHUNK

    def http(req):
        requests.append(req)
        if len(requests) == 1:
            return httpx.Response(
                200, stream=Broken(), headers={"Content-Length": str(len(DATA)), "ETag": '"one"'}
            )
        if len(requests) == 2:
            return httpx.Response(416, headers={"Content-Range": range_info} if range_info else {})
        return httpx.Response(
            200,
            content=replacement,
            headers={"Content-Length": str(len(replacement)), "ETag": '"two"'},
        )

    transport(monkeypatch, http)
    target = tmp_path / "track.mp4"
    session = interrupted(host, item, target)
    with pytest.raises(AppError) as rejected:
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    assert rejected.value.code == "BILI_DOWNLOAD_RANGE_REJECTED" and len(requests) == 2
    assert target.with_suffix(".mp4.partial").read_bytes() == DATA[:CHUNK] and not target.exists()
    assert (
        requests[1].headers["range"] == f"bytes={CHUNK}-"
        and requests[1].headers["if-range"] == '"one"'
    )
    host.media.download_track(session, session["video"], target, threading.Event(), lambda *_: None)
    assert (
        target.read_bytes() == replacement
        and len(requests) == 3
        and requests[2].headers.get("range") is None
    )
    retained = list(tmp_path.glob("track.mp4.retained-*.partial"))
    assert len(retained) == 1 and retained[0].read_bytes() == DATA[:CHUNK]


def test_416_restart_does_not_retarget_changed_business_source(media_host, monkeypatch, tmp_path):
    _, host, item = media_host
    calls = []

    def http(req):
        calls.append(req)
        return (
            httpx.Response(
                200, stream=Broken(), headers={"Content-Length": str(len(DATA)), "ETag": '"one"'}
            )
            if len(calls) == 1
            else httpx.Response(416)
        )

    transport(monkeypatch, http)
    target = tmp_path / "track.mp4"
    session = interrupted(host, item, target)
    with pytest.raises(AppError):
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    original_resolve = host.provider.resolve

    def changed(grant, value):
        result = original_resolve(grant, value)
        result["parts"][0]["cid"] += 1
        return result

    monkeypatch.setattr(host.provider, "resolve", changed)
    with pytest.raises(AppError) as blocked:
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    assert blocked.value.code == "BILI_SOURCE_VERSION_CHANGED" and len(calls) == 2
    assert target.with_suffix(".mp4.partial").read_bytes() == DATA[:CHUNK]
    assert not list(tmp_path.glob("track.mp4.retained-*.partial")) and not target.exists()


def test_changed_etag_does_not_mix_entities_and_preserves_partial(
    media_host, monkeypatch, tmp_path
):  # noqa: F811
    _, host, item = media_host
    requests = []

    def http(req):
        requests.append(req)
        if len(requests) == 1:
            return httpx.Response(
                200, stream=Broken(), headers={"Content-Length": str(len(DATA)), "ETag": '"one"'}
            )
        return httpx.Response(
            206,
            content=b"x" * CHUNK,
            headers={
                "Content-Length": str(CHUNK),
                "Content-Range": f"bytes {CHUNK}-{len(DATA) - 1}/{len(DATA)}",
                "ETag": '"two"',
            },
        )

    transport(monkeypatch, http)
    target = tmp_path / "track.mp4"
    session = interrupted(host, item, target)
    with pytest.raises(AppError) as changed:
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    assert changed.value.code == "BILI_DOWNLOAD_SOURCE_CHANGED"
    assert target.with_suffix(".mp4.partial").read_bytes() == DATA[:CHUNK] and not target.exists()
    assert requests[1].headers["if-range"] == '"one"'


def test_without_validator_one_full_response_verifies_prefix_before_appending(
    media_host, monkeypatch, tmp_path
):  # noqa: F811
    _, host, item = media_host
    requests = []

    def http(req):
        requests.append(req)
        return (
            httpx.Response(200, stream=Broken(), headers={"Content-Length": str(len(DATA))})
            if len(requests) == 1
            else httpx.Response(200, content=DATA, headers={"Content-Length": str(len(DATA))})
        )

    transport(monkeypatch, http)
    target = tmp_path / "track.mp4"
    session = interrupted(host, item, target)
    host.media.download_track(session, session["video"], target, threading.Event(), lambda *_: None)
    assert target.read_bytes() == DATA and requests[1].headers.get("range") is None


def test_prefix_change_without_validator_preserves_old_bytes(media_host, monkeypatch, tmp_path):  # noqa: F811
    _, host, item = media_host
    requests = []

    def http(req):
        requests.append(req)
        return (
            httpx.Response(200, stream=Broken(), headers={"Content-Length": str(len(DATA))})
            if len(requests) == 1
            else httpx.Response(
                200, content=b"x" * len(DATA), headers={"Content-Length": str(len(DATA))}
            )
        )

    transport(monkeypatch, http)
    target = tmp_path / "track.mp4"
    session = interrupted(host, item, target)
    with pytest.raises(AppError) as changed:
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    assert changed.value.code == "BILI_DOWNLOAD_SOURCE_CHANGED"
    assert target.with_suffix(".mp4.partial").read_bytes() == DATA[:CHUNK] and not target.exists()


@pytest.mark.parametrize("etag", [None, '"one"', 'W/"weak"'])
def test_ignored_range_or_weak_validator_checks_full_prefix(
    media_host, monkeypatch, tmp_path, etag
):  # noqa: F811
    _, host, item = media_host
    requests = []

    def http(req):
        requests.append(req)
        headers = {"Content-Length": str(len(DATA)), **({"ETag": etag} if etag else {})}
        return (
            httpx.Response(200, stream=Broken(), headers=headers)
            if len(requests) == 1
            else httpx.Response(200, content=DATA, headers=headers)
        )

    transport(monkeypatch, http)
    target = tmp_path / "track.mp4"
    session = interrupted(host, item, target)
    host.media.download_track(session, session["video"], target, threading.Event(), lambda *_: None)
    assert target.read_bytes() == DATA and len(requests) == 2
    assert requests[1].headers.get("range") == (f"bytes={CHUNK}-" if etag == '"one"' else None)


@pytest.mark.parametrize("difference", ["etag", "prefix", "total"])
def test_changed_representation_requires_explicit_retry_and_retains_old_checkpoint(
    media_host, monkeypatch, tmp_path, difference
):  # noqa: F811
    _, host, item = media_host
    requests = []
    original_etag = '"one"' if difference != "prefix" else None
    replacement = b"x" * (len(DATA) + (1 if difference == "total" else 0))

    def http(req):
        requests.append(req)
        if len(requests) == 1:
            return httpx.Response(
                200,
                stream=Broken(),
                headers={
                    "Content-Length": str(len(DATA)),
                    **({"ETag": original_etag} if original_etag else {}),
                },
            )
        return httpx.Response(
            200,
            content=replacement,
            headers={"Content-Length": str(len(replacement)), "ETag": '"two"'},
        )

    transport(monkeypatch, http)
    target = tmp_path / "track.mp4"
    session = interrupted(host, item, target)
    with pytest.raises(AppError) as changed:
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    assert changed.value.code == "BILI_DOWNLOAD_SOURCE_CHANGED" and len(requests) == 2
    partial = target.with_suffix(".mp4.partial")
    assert partial.read_bytes() == DATA[:CHUNK] and not target.exists()
    host.media.download_track(session, session["video"], target, threading.Event(), lambda *_: None)
    assert target.read_bytes() == replacement and len(requests) == 3
    assert requests[2].headers.get("range") is None
    retained = list(tmp_path.glob("track.mp4.retained-*.partial"))
    assert len(retained) == 1 and retained[0].read_bytes() == DATA[:CHUNK]
    receipt = json.loads(retained[0].with_suffix(".json").read_text("utf-8"))
    assert receipt["restart_required"] is True and receipt["committed"] == CHUNK


@pytest.mark.parametrize("tamper", ["owner", "bytes", "etag", "version", "json-type"])
def test_invalid_checkpoint_is_rejected_before_platform_request(
    media_host, monkeypatch, tmp_path, tamper
):  # noqa: F811
    _, host, item = media_host
    requests = []

    def http(req):
        requests.append(req)
        return httpx.Response(
            200, stream=Broken(), headers={"Content-Length": str(len(DATA)), "ETag": '"one"'}
        )

    transport(monkeypatch, http)
    target = tmp_path / "track.mp4"
    session = interrupted(host, item, target)
    metadata = target.with_suffix(".mp4.resume.json")
    receipt = json.loads(metadata.read_text("utf-8"))
    if tamper == "bytes":
        target.with_suffix(".mp4.partial").write_bytes(b"x" * CHUNK)
    elif tamper == "owner":
        receipt["owner"]["source_key"] = ["another", "version"]
    elif tamper == "etag":
        receipt["etag"] = ["bad"]
    elif tamper == "version":
        receipt["version"] = 999
    else:
        receipt = []
    metadata.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(AppError) as conflict:
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    assert conflict.value.code == "BILI_DOWNLOAD_CHECKPOINT_CONFLICT" and len(requests) == 1
    assert not target.exists()


def test_cancel_after_all_bytes_does_not_publish_and_retry_does_not_request_eof_range(
    media_host, monkeypatch, tmp_path
):
    _, host, item = media_host
    requests = []

    def http(req):
        requests.append(req)
        return httpx.Response(
            200, content=DATA, headers={"Content-Length": str(len(DATA)), "ETag": '"one"'}
        )

    transport(monkeypatch, http)
    target = tmp_path / "track.mp4"
    session = host.media.session(host.source(item.asset_id))
    cancelled = threading.Event()

    def progress(received, total):
        if received == total:
            cancelled.set()

    with pytest.raises(AppError) as stopped:
        host.media.download_track(session, session["video"], target, cancelled, progress)
    assert stopped.value.code == "PREPARATION_INTERRUPTED" and not target.exists()
    receipt = json.loads(target.with_suffix(".mp4.resume.json").read_text("utf-8"))
    assert receipt["committed"] == len(DATA) and len(requests) == 1
    host.media.download_track(session, session["video"], target, threading.Event(), lambda *_: None)
    assert target.read_bytes() == DATA and len(requests) == 2
    assert requests[1].headers.get("range") is None


@pytest.mark.parametrize("change", ["cid", "duration_ms", "page"])
def test_changed_business_source_cannot_replace_frozen_version_on_retry(
    media_host, monkeypatch, tmp_path, change
):
    _, host, item = media_host
    requests = []

    def http(req):
        requests.append(req)
        return (
            httpx.Response(200, stream=Broken(), headers={"Content-Length": str(len(DATA))})
            if len(requests) == 1
            else httpx.Response(
                200, content=b"x" * len(DATA), headers={"Content-Length": str(len(DATA))}
            )
        )

    transport(monkeypatch, http)
    target = tmp_path / "track.mp4"
    session = interrupted(host, item, target)
    with pytest.raises(AppError, match="远端表示已变化"):
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    original_resolve = host.provider.resolve

    def changed_source(grant, value):
        plan = original_resolve(grant, value)
        plan["parts"][0][change] += 1
        return plan

    monkeypatch.setattr(host.provider, "resolve", changed_source)
    with pytest.raises(AppError) as conflict:
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    assert conflict.value.code == "BILI_SOURCE_VERSION_CHANGED" and len(requests) == 2
    assert target.with_suffix(".mp4.partial").read_bytes() == DATA[:CHUNK]
    assert not list(tmp_path.glob("track.mp4.retained-*.partial")) and not target.exists()
    assert host.source(item.asset_id)["source_version"] == item.source_version
    assert host.source(item.asset_id)["local_path"] is None


def test_legacy_partial_longer_than_new_response_requires_explicit_restart(
    media_host, monkeypatch, tmp_path
):
    _, host, item = media_host
    target = tmp_path / "track.mp4"
    partial = target.with_suffix(".mp4.partial")
    partial.write_bytes(DATA)
    requests = []

    def http(req):
        requests.append(req)
        return httpx.Response(200, content=DATA[:CHUNK], headers={"Content-Length": str(CHUNK)})

    transport(monkeypatch, http)
    session = host.media.session(host.source(item.asset_id))
    with pytest.raises(AppError) as changed:
        host.media.download_track(
            session, session["video"], target, threading.Event(), lambda *_: None
        )
    assert changed.value.code == "BILI_DOWNLOAD_SOURCE_CHANGED"
    assert partial.read_bytes() == DATA and not target.exists() and len(requests) == 1
    host.media.download_track(session, session["video"], target, threading.Event(), lambda *_: None)
    assert target.read_bytes() == DATA[:CHUNK] and len(requests) == 2


@pytest.mark.parametrize("complete", [False, True])
def test_uncommitted_tail_or_full_cancelled_partial_replays_instead_of_range(
    media_host, monkeypatch, tmp_path, complete
):  # noqa: F811
    _, host, item = media_host
    requests = []

    def http(req):
        requests.append(req)
        return (
            httpx.Response(
                200, stream=Broken(), headers={"Content-Length": str(len(DATA)), "ETag": '"one"'}
            )
            if len(requests) == 1
            else httpx.Response(
                200, content=DATA, headers={"Content-Length": str(len(DATA)), "ETag": '"one"'}
            )
        )

    transport(monkeypatch, http)
    target = tmp_path / "track.mp4"
    session = interrupted(host, item, target)
    # Simulate a crash after writing bytes but before committing the checkpoint.
    with target.with_suffix(".mp4.partial").open("ab") as out:
        out.write(DATA[CHUNK:] if complete else DATA[CHUNK : CHUNK + 9])
    host.media.download_track(session, session["video"], target, threading.Event(), lambda *_: None)
    assert target.read_bytes() == DATA and requests[1].headers.get("range") is None


@pytest.mark.parametrize("range_rejected", [False, True])
def test_real_synthetic_container_recovers_through_queue_and_local_first_open(
    media_host, monkeypatch, tmp_path, range_rejected
):
    from learning_app.config import PROJECT_ROOT
    from learning_app.integrations import track_download
    from learning_app.integrations.mux import av_runtime
    from learning_app.media import MediaStore
    from learning_app.workbench.preparations import PreparationStore
    from learning_app.workbench.processing_models import PreparationControl

    code = """import sys
from fractions import Fraction
sys.path.insert(0,sys.argv[1])
import av
with av.open(sys.argv[2],'w') as out:
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
    paths = [tmp_path / "encoded.mp4", tmp_path / "encoded.m4a"]
    subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(av_runtime(PROJECT_ROOT)), *map(str, paths)],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    repo, host, item = media_host
    provider = host.provider

    def media(grant, part):
        host.access.check(grant)
        return {
            "video": {"url": "https://example.bilivideo.com/video", "format_id": "h264"},
            "audio": {"url": "https://example.bilivideo.com/audio", "format_id": "aac"},
            "duration_ms": 2000,
        }

    monkeypatch.setattr(provider, "media", media)
    monkeypatch.setattr(track_download, "BLOCK", 1024)
    data = {"/video": paths[0].read_bytes(), "/audio": paths[1].read_bytes()}
    assert len(data["/video"]) > 1024
    requests = []

    class BrokenVideo(httpx.SyncByteStream):
        def __iter__(self):
            yield data["/video"][:1024]
            raise httpx.ReadError("synthetic video interruption")

    def http(req):
        requests.append((req.url.path, req.headers.get("range")))
        body = data[req.url.path]
        headers = {"Content-Length": str(len(body)), "ETag": '"stable"'}
        if len(requests) == 1:
            return httpx.Response(200, stream=BrokenVideo(), headers=headers)
        if range_rejected and len(requests) == 2:
            return httpx.Response(416)
        if req.headers.get("range"):
            assert req.headers.get("if-range") == '"stable"'
            headers.update(
                {
                    "Content-Length": str(len(body) - 1024),
                    "Content-Range": f"bytes 1024-{len(body) - 1}/{len(body)}",
                }
            )
            return httpx.Response(206, content=body[1024:], headers=headers)
        return httpx.Response(200, content=body, headers=headers)

    transport(monkeypatch, http)
    store = PreparationStore(repo)
    job = host.queue("a", item.asset_id, item.source_version, "platform_download")
    lease = store.claim()
    with pytest.raises(AppError) as interrupted_error:
        host.prepare(lease, threading.Event(), lambda *_: None)
    assert interrupted_error.value.code == "BILI_DOWNLOAD_INTERRUPTED"
    assert store.finish(lease, None, error_code=interrupted_error.value.code)
    assert store.claim() is None and requests == [("/video", None)]
    failed = store.get("a", job.job_id)
    store.control(
        "a",
        job.job_id,
        "retry",
        PreparationControl(operation_id="explicit-retry", expected_revision=failed.revision),
    )
    lease = store.claim()
    if range_rejected:
        with pytest.raises(AppError) as range_error:
            host.prepare(lease, threading.Event(), lambda *_: None)
        assert range_error.value.code == "BILI_DOWNLOAD_RANGE_REJECTED"
        assert store.finish(lease, None, error_code=range_error.value.code)
        assert store.claim() is None and len(requests) == 2
        failed = store.get("a", job.job_id)
        store.control(
            "a",
            job.job_id,
            "retry",
            PreparationControl(
                operation_id="explicit-range-restart", expected_revision=failed.revision
            ),
        )
        lease = store.claim()
    result = host.prepare(lease, threading.Event(), lambda *_: None)
    assert host.source(item.asset_id)["local_path"] is None
    assert store.finish(lease, result) and store.get("a", job.job_id).state == "completed"
    assert requests == [
        ("/video", None),
        ("/video", "bytes=1024-"),
        *([("/video", None)] if range_rejected else []),
        ("/audio", None),
    ]
    host.access.change("bilibili", False)
    opened = MediaStore(repo).open(item.asset_id, item.source_version)
    assert opened["asset_id"] == item.asset_id and opened["source_version"] == item.source_version
    assert host.media.local(host.source(item.asset_id)).is_file()
    assert len(requests) == (4 if range_rejected else 3)
