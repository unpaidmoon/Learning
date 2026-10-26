"""Real local synthetic decoding plus priority, restart and version contracts."""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import func, select

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.media import MediaStore
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.video_previews import (
    TOOL_SHA256,
    PreviewPriorityInput,
    VideoPreviewCache,
    _frame_order,
    _Interrupted,
    thumbnail_times,
)

FFMPEG = Path(os.environ.get("LOCALAPPDATA", "")) / (
    "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/"
    "ffmpeg-8.1-full_build/bin/ffmpeg.exe"
)


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.02)
    raise AssertionError("preview condition not observed")


@pytest.fixture
def synthetic_video(tmp_path):
    if not FFMPEG.is_file():
        pytest.skip("fixed local FFmpeg 8.1 is not installed")
    path = tmp_path / "course" / "synthetic.webm"
    path.parent.mkdir()
    result = subprocess.run([
        str(FFMPEG), "-v", "error", "-nostdin", "-f", "lavfi", "-i",
        "color=c=0x008800:s=640x360:r=2:d=4", "-threads", "1", "-c:v", "libvpx", str(path),
    ], capture_output=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return path


@pytest.fixture
def repository(tmp_path):
    result = Repository(tmp_path / "data")
    result.create_course("合成课程", "course_preview")
    yield result
    result.close()


def test_thumbnail_plan_is_bounded_and_prioritizes_coarse_coverage():
    interval, times = thumbnail_times(24 * 3600 * 1000)
    assert len(times) <= 1200
    assert interval >= 2000
    assert times[0] == 0 and times[-1] < 24 * 3600 * 1000
    assert _frame_order(100)[:3] == [0, 7, 13]
    assert sorted(_frame_order(100)) == list(range(100))


def test_real_low_resolution_cache_survives_restart_and_cannot_retarget_changed_video(
    repository, synthetic_video, tmp_path,
):
    cache = VideoPreviewCache(repository, tmp_path, ffmpeg_path=FFMPEG)
    cache.start()
    try:
        asset = MediaStore(repository).register("course_preview", str(synthetic_video))
        result = wait_for(lambda: (value if (value := cache.manifest(
            asset["asset_id"], asset["source_version"])).status == "ready" else None))
        assert result.duration_ms == 4000
        assert len(result.frames) == 2
        output = Path(cache.frame(asset["asset_id"], asset["source_version"], 0).path)
        with Image.open(output) as image:
            assert image.size == (240, 135) and image.format == "JPEG"
            assert image.convert("RGB").getpixel((120, 67))[1] > 100
        before = (output.stat().st_mtime_ns, hashlib.sha256(output.read_bytes()).hexdigest())
        tool = cache.root / "tool.json"
        assert TOOL_SHA256 in tool.read_text("utf-8")
    finally:
        cache.close()
    restored = VideoPreviewCache(repository, tmp_path, ffmpeg_path=FFMPEG)
    restored.start()
    try:
        result = restored.manifest(asset["asset_id"], asset["source_version"])
        assert result.status == "ready" and len(result.frames) == 2
        assert (output.stat().st_mtime_ns, hashlib.sha256(output.read_bytes()).hexdigest()) == before
        with pytest.raises(AppError, match="来源版本"):
            restored.manifest(asset["asset_id"], "wrong-version")
        with synthetic_video.open("ab") as changed:
            changed.write(b"changed source")
        with pytest.raises(AppError, match="视频内容已变化"):
            restored.manifest(asset["asset_id"], asset["source_version"])
        # The previously completed derivative stays intact; it is not silently
        # served as the changed source and no user attachment is ever created.
        assert output.is_file()
        with repository.engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(business.attachments)) == 0
    finally:
        restored.close()


def test_enrolling_a_course_warms_cache_before_any_video_is_opened(
    repository, synthetic_video, tmp_path,
):
    cache = VideoPreviewCache(repository, tmp_path, ffmpeg_path=FFMPEG)
    cache.start()
    try:
        CourseFileStore(repository).bind("course_preview", str(synthetic_video.parent), "合成课程")
        digest = hashlib.sha256(synthetic_video.read_bytes()).hexdigest()
        wait_for(lambda: cache._load_manifest(digest)["status"] == "ready")
        # Background work may discover and decode; it must not manufacture a
        # business file opening or alter the course's recently-used ordering.
        with repository.engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(business.assets)) == 0
        cover = cache.course_cover("course_preview", synthetic_video.name)
        assert cover.status == "ready" and cover.url and cover.source_version
        assert Path(cache.course_cover_frame("course_preview", synthetic_video.name, cover.source_version).path).is_file()
        with pytest.raises(AppError):
            cache.course_cover("course_preview", "../outside.webm")
        # Cover reads must never register or open a business media asset.
        with repository.engine.connect() as connection:
            assert connection.scalar(select(func.count()).select_from(business.assets)) == 0
        opened = CourseFileStore(repository).open("course_preview", synthetic_video.name)
        result = cache.manifest(opened["asset_id"], opened["source_version"])
        assert result.status == "ready" and result.frames
        with synthetic_video.open("ab") as changed:
            changed.write(b"cover version changed")
        with pytest.raises(AppError, match="封面来源已变化"):
            cache.course_cover_frame("course_preview", synthetic_video.name, cover.source_version)
    finally:
        cache.close()


def test_interaction_lease_and_active_generation_pause_work_then_resume(
    repository, synthetic_video, tmp_path,
):
    active = [True]
    cache = VideoPreviewCache(repository, tmp_path, ffmpeg_path=FFMPEG, is_busy=lambda: active[0])
    cache.priority(PreviewPriorityInput(lease_id="capture", active=True, kind="interaction", ttl_ms=10000))
    cache.start()
    try:
        asset = MediaStore(repository).register("course_preview", str(synthetic_video))
        time.sleep(.15)
        assert cache.manifest(asset["asset_id"], asset["source_version"]).frames == []
        active[0] = False
        cache.priority(PreviewPriorityInput(lease_id="capture", active=False))
        wait_for(lambda: cache.manifest(asset["asset_id"], asset["source_version"]).status == "ready")
        cache.priority(PreviewPriorityInput(lease_id="playing", active=True, kind="playback"))
        assert not cache._foreground()
        assert cache._delay() == .3
    finally:
        cache.close()


def test_mismatched_local_tool_is_rejected_without_spawning(repository, tmp_path):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"untrusted executable")
    cache = VideoPreviewCache(repository, tmp_path, ffmpeg_path=fake)
    cache.start()
    try:
        wait_for(lambda: cache._tool_error == "PREVIEW_TOOL_HASH_MISMATCH")
        assert cache._current_group is None
    finally:
        cache.close()


def test_hovered_missing_frame_precedes_coarse_coverage_and_background_batches(
    repository, synthetic_video, tmp_path,
):
    asset = MediaStore(repository).register("course_preview", str(synthetic_video))
    cache = VideoPreviewCache(repository, tmp_path, ffmpeg_path=FFMPEG)
    cache._resolve_tool()
    cache.priority(PreviewPriorityInput(lease_id="hover-target", active=True, kind="preview",
        asset_id=asset["asset_id"], source_version=asset["source_version"], time_ms=2000))
    job = next(iter(cache._jobs.values()))
    assert not cache._step(job)
    assert cache._load_manifest(asset["source_version"])["frames"] == [1]
    assert cache._step(job)
    assert cache._load_manifest(asset["source_version"])["frames"] == [0, 1]

    # A fresh background pass publishes both images in one bounded decode.
    other = VideoPreviewCache(repository, tmp_path, ffmpeg_path=FFMPEG)
    for frame in cache._manifest_path(asset["source_version"]).parent.glob("*.jpg"):
        frame.unlink()
    cache._manifest_path(asset["source_version"]).unlink()
    other._resolve_tool()
    other.enqueue_asset(asset["asset_id"], asset["source_version"])
    assert other._step(next(iter(other._jobs.values())))
    assert len(other._load_manifest(asset["source_version"])["frames"]) == 2


def test_interaction_cancels_only_the_running_owned_decoder(repository, tmp_path):
    if not FFMPEG.is_file():
        pytest.skip("fixed local FFmpeg 8.1 is not installed")
    cache = VideoPreviewCache(repository, tmp_path, ffmpeg_path=FFMPEG)
    cache._resolve_tool()
    stopped = threading.Event()
    errors = []

    def decode():
        try:
            cache._command(["-re", "-f", "lavfi", "-i", "color=s=32x32:r=2",
                            "-t", "30", "-f", "null", "-"], timeout=6)
        except _Interrupted:
            stopped.set()
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=decode, daemon=True)
    thread.start()
    try:
        group = wait_for(lambda: cache._current_group if cache._current_group
                         and cache._current_group.pids() else None)
        started = time.monotonic()
        cache.priority(PreviewPriorityInput(lease_id="capture-now", active=True, kind="interaction"))
        thread.join(timeout=2)
        assert stopped.is_set() and not errors
        assert time.monotonic() - started < 2
        assert group.pids() == []
        # The business repository remains usable. No application/Host process
        # group participates in cancellation of the thumbnail subprocess.
        assert repository.get_chat(repository.create_chat("course_preview")["chat_id"])
    finally:
        cache.close()
        thread.join(timeout=2)


def test_preview_http_reuses_application_authentication_and_version_scope(tmp_path, synthetic_video):
    from fastapi.testclient import TestClient

    from learning_app.app import create_app
    from learning_app.config import AppConfig

    application = create_app(AppConfig(mode="test", data_dir=tmp_path / "http-data"))
    with TestClient(application, base_url="http://127.0.0.1:8765") as client:
        route = "/api/v1/videos/arbitrary/previews?source_version=wrong"
        assert client.get(route).status_code == 403
        assert client.get("/api/v1/courses/arbitrary/files/preview?path=video.mp4").status_code == 403
        assert client.get("/api/v1/courses/arbitrary/files/cover.jpg?path=video.mp4&source_version=x").status_code == 403
        auth = client.get("/api/v1/session").json()
        assert client.post("/api/v1/video-previews/priority", json={
            "lease_id": "test", "active": True,
        }).status_code == 403
        client.headers["X-Learning-CSRF"] = auth["csrf_token"]
        course = client.post("/api/v1/courses", json={"title": "HTTP合成课程"}).json()
        asset = client.post(f"/api/v1/courses/{course['course_id']}/videos", json={
            "path": str(synthetic_video),
        }).json()
        route = f"/api/v1/videos/{asset['asset_id']}/previews?source_version={asset['source_version']}"
        result = wait_for(lambda: (value if (value := client.get(route).json())["status"] == "ready" else None))
        assert result["frames"] and result["width"] == 240 and result["height"] == 135
        response = client.get(result["frames"][0]["url"])
        assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
        CourseFileStore(application.state.repository).bind(course["course_id"], str(synthetic_video.parent), "HTTP合成课程")
        cover_route = f"/api/v1/courses/{course['course_id']}/files/preview"
        cover = client.get(cover_route, params={"path": synthetic_video.name}).json()
        assert cover["status"] == "ready"
        assert client.get(cover["url"]).headers["content-type"] == "image/jpeg"
        assert client.get(cover_route, params={"path": "../outside.webm"}).status_code == 422
        assert client.get(cover["url"].replace(cover["source_version"], "wrong-version")).status_code == 409
        assert client.get(route.replace(asset["source_version"], "wrong-version")).status_code == 409
        assert client.get(f"/api/v1/videos/{asset['asset_id']}/previews/-1.jpg?source_version={asset['source_version']}").status_code == 404
        assert client.post("/api/v1/video-previews/priority", json={
            "lease_id": "capture", "active": True, "kind": "interaction",
        }).json() == {"accepted": True}
