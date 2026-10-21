"""Synthetic media tests. No real video frames or provider calls are used."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.errors import AppError
from learning_app.media import MediaPositionInput, MediaStore
from learning_app.storage import Repository

VIDEO = b"\x1aE\xdf\xa3" + bytes(range(256)) * 1024


@pytest.fixture
def media(tmp_path):
    repo = Repository(tmp_path / "data")
    repo.create_course("数学", "course_a")
    repo.create_course("另一门课", "course_b")
    store = MediaStore(repo)
    video = tmp_path / "第2课.webm"
    video.write_bytes(VIDEO)
    yield repo, store, video
    repo.close()


@pytest.fixture
def browser(tmp_path):
    config = AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run")
    with TestClient(create_app(config), base_url="http://127.0.0.1:8765") as client:
        csrf = client.get("/api/v1/session").json()["csrf_token"]
        client.headers["X-Learning-CSRF"] = csrf
        course = client.post("/api/v1/courses", json={"title": "数学"}).json()
        video = tmp_path / "合成视频.webm"
        video.write_bytes(VIDEO)
        registered = client.post(
            f"/api/v1/courses/{course['course_id']}/videos", json={"path": str(video)}
        )
        assert registered.status_code == 200, registered.text
        yield client, registered.json(), video


def content_url(asset):
    return f"/api/v1/videos/{asset['asset_id']}/content?source_version={asset['source_version']}"


def test_registration_reuses_business_identity_without_copying_and_associates_courses(media):
    repo, store, video = media
    first = store.register("course_a", str(video))
    second = store.register("course_b", str(video))
    again = store.register("course_a", str(video))
    assert first["asset_id"] == second["asset_id"] == again["asset_id"]
    assert first["source_version"] == second["source_version"]
    assert store.list("course_a")[0]["asset_id"] == first["asset_id"]
    assert store.list("course_b")[0]["asset_id"] == first["asset_id"]
    assert "local_path" not in first and "path" not in first
    assert list(repo.objects.glob("*/*")) == []
    assert video.read_bytes() == VIDEO
    assert first["last_opened_at"] is None


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=0-3", VIDEO[:4]),
        ("bytes=4-99", VIDEO[4:100]),
        ("bytes=262140-", VIDEO[262140:]),
        ("bytes=-13", VIDEO[-13:]),
        ("bytes=262144-999999", VIDEO[262144:]),
    ],
)
def test_range_returns_only_the_requested_bytes_with_exact_lengths(browser, header, expected):
    client, asset, _ = browser
    response = client.get(content_url(asset), headers={"Range": header})
    assert response.status_code == 206
    assert response.content == expected
    assert int(response.headers["content-length"]) == len(expected)
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-range"].endswith(f"/{len(VIDEO)}")
    assert response.headers["content-type"] == "video/webm"


@pytest.mark.parametrize("header", ["bytes=999999-", "bytes=4-2", "bytes=-0", "bytes=-", "x=1-2"])
def test_unsatisfiable_or_invalid_ranges_are_416(browser, header):
    client, asset, _ = browser
    response = client.get(content_url(asset), headers={"Range": header})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(VIDEO)}"
    assert response.content == b""


def test_head_and_if_range_version_handling(browser):
    client, asset, _ = browser
    url = content_url(asset)
    response = client.head(url)
    assert response.status_code == 200
    assert response.content == b""
    assert int(response.headers["content-length"]) == len(VIDEO)
    etag = response.headers["etag"]
    matching = client.get(url, headers={"Range": "bytes=0-3", "If-Range": etag})
    assert matching.status_code == 206 and matching.content == VIDEO[:4]
    changed = client.get(url, headers={"Range": "bytes=0-3", "If-Range": '"other-version"'})
    assert changed.status_code == 200 and changed.content == VIDEO
    assert client.get(url.replace(asset["source_version"], "wrong")).status_code == 409
    assert client.get(url.replace(asset["asset_id"], "not_registered")).status_code == 404


def test_missing_changed_and_relocated_source_preserves_version_and_position(media):
    repo, store, video = media
    asset = store.register("course_a", str(video))
    store.save_position(
        asset["asset_id"],
        MediaPositionInput(
            source_version=asset["source_version"], position_ms=12345, expected_revision=0
        ),
    )
    video.write_bytes(VIDEO[:-1] + b"x")
    assert store.list("course_a")[0]["availability"] == "version_mismatch"
    with pytest.raises(AppError, match="旧来源"):
        store.open(asset["asset_id"], asset["source_version"])
    with pytest.raises(AppError) as registration:
        store.register("course_a", str(video))
    assert registration.value.code == "SOURCE_VERSION_MISMATCH"
    video.unlink()
    assert store.list("course_a")[0]["availability"] == "missing"
    with pytest.raises(AppError) as missing:
        store.open(asset["asset_id"], asset["source_version"])
    assert missing.value.code == "SOURCE_MISSING"
    replacement = video.with_name("手动重定位.webm")
    replacement.write_bytes(VIDEO)
    relocated = store.relocate(asset["asset_id"], asset["source_version"], str(replacement))
    assert relocated["asset_id"] == asset["asset_id"]
    assert relocated["source_version"] == asset["source_version"]
    assert relocated["position_ms"] == 12345
    assert relocated["availability"] == "available"
    assert repo.resolve_source(asset["asset_id"], asset["source_version"])["local_path"] == str(
        replacement
    )


def test_explicit_open_rehashes_even_when_size_and_modified_time_match(media):
    _repo, store, video = media
    asset = store.register("course_a", str(video))
    stat = video.stat()
    video.write_bytes(VIDEO[:-1] + b"x")
    os.utime(video, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(AppError) as changed:
        store.open(asset["asset_id"], asset["source_version"])
    assert changed.value.code == "SOURCE_VERSION_MISMATCH"


def test_positions_use_revision_not_max_position_and_survive_reopen(media):
    repo, store, video = media
    asset = store.register("course_a", str(video))
    first = MediaPositionInput(
        source_version=asset["source_version"], position_ms=9000, expected_revision=0
    )
    saved = store.save_position(asset["asset_id"], first)
    assert saved["position_revision"] == 1
    with pytest.raises(AppError) as old:
        store.save_position(asset["asset_id"], first)
    assert old.value.code == "POSITION_CONFLICT"
    rewind = MediaPositionInput(
        source_version=asset["source_version"], position_ms=1200, expected_revision=1
    )
    store.save_position(asset["asset_id"], rewind)
    opened = store.open(asset["asset_id"], asset["source_version"])
    assert opened["position_ms"] == 1200 and opened["position_revision"] == 2
    reopened = Repository(repo.root)
    try:
        restored = MediaStore(reopened).list("course_a")[0]
        assert restored["position_ms"] == 1200 and restored["position_revision"] == 2
        assert restored["last_opened_at"] == opened["last_opened_at"]
    finally:
        reopened.close()


def test_directory_orders_unused_naturally_then_recent_successful_opens(media, monkeypatch):
    monkeypatch.setattr("learning_app.media._now", lambda: "2026-10-21T00:00:00.000000+00:00")
    _repo, store, video = media
    two = store.register("course_a", str(video))
    ten_path = video.with_name("第10课.webm")
    ten_path.write_bytes(VIDEO)
    ten = store.register("course_a", str(ten_path))
    assert [row["display_name"] for row in store.list("course_a")] == ["第2课.webm", "第10课.webm"]
    store.open(ten["asset_id"], ten["source_version"])
    assert store.list("course_a")[0]["asset_id"] == ten["asset_id"]
    store.open(two["asset_id"], two["source_version"])
    assert store.list("course_a")[0]["asset_id"] == two["asset_id"]


def test_native_selection_cancellation_and_registration_are_explicit(browser, monkeypatch):
    client, _asset, video = browser
    course = client.post("/api/v1/courses", json={"title": "物理"}).json()
    url = f"/api/v1/courses/{course['course_id']}/videos/pick"
    monkeypatch.setattr("learning_app.media.choose_video", lambda: None)
    assert client.post(url).json() is None
    assert client.get(f"/api/v1/courses/{course['course_id']}/videos").json() == []
    monkeypatch.setattr("learning_app.media.choose_video", lambda: str(video))
    assert client.post(url).json()["display_name"] == video.name
    assert client.post(url, headers={"X-Learning-CSRF": "wrong"}).status_code == 403


def test_registration_rejects_nonvideo_or_relative_paths(browser):
    client, asset, video = browser
    course_id = client.get("/api/v1/courses").json()[0]["course_id"]
    url = f"/api/v1/courses/{course_id}/videos"
    assert client.post(url, json={"path": "relative.webm"}).status_code == 422
    wrong = video.with_suffix(".mp4")
    wrong.write_text("not a video", encoding="utf-8")
    assert client.post(url, json={"path": str(wrong)}).status_code == 422
    assert client.post(url, json={"path": str(video.with_suffix(".txt"))}).status_code == 404
    assert client.get(content_url(asset)).content == VIDEO


def test_content_detects_path_replacement_before_headers(browser, monkeypatch):
    import learning_app.media as module

    client, asset, video = browser
    original = module._read_locked
    opens = 0

    # Register has verified the source; substitute it only at content handle open.
    def replace_at_open(path: Path):
        nonlocal opens
        opens += 1
        if opens == 2:
            video.write_bytes(VIDEO + b"changed")
        return original(path)

    monkeypatch.setattr(module, "_read_locked", replace_at_open)
    response = client.get(content_url(asset))
    assert response.status_code == 409
    assert response.json()["code"] == "SOURCE_CHANGED_DURING_READ"
    assert VIDEO not in response.content
