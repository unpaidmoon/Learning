"""Open and preview regressions through the interfaces used by the desktop."""

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import update
from starlette.requests import Request
from test_integrations import FixtureProvider, imported

from learning_app import media
from learning_app.config import PROJECT_ROOT
from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.integrations import schema as platform_schema
from learning_app.integrations.host import IntegrationHost
from learning_app.storage import Repository
from learning_app.storage import repository as storage
from learning_app.video_previews import REVISION, PreviewPriorityInput, VideoPreviewCache


@pytest.fixture
def pipeline(tmp_path):
    repo = Repository(tmp_path / "data")
    repo.create_course("Pipeline", "a")
    root = tmp_path / "course"
    root.mkdir()
    path = root / "synthetic.mp4"
    path.write_bytes(b"\0\0\0\x18ftypmp42" + bytes(4096))
    files = CourseFileStore(repo)
    files.bind("a", str(root))
    host = IntegrationHost(repo, PROJECT_ROOT, provider_factory=FixtureProvider)
    repo.integrations = host
    yield repo, files, path, host
    repo.close()


def test_file_open_and_first_ranges_share_one_full_validation(pipeline, monkeypatch):
    repo, files, path, _ = pipeline
    calls = []
    storage_hash, media_hash = storage._file_hash, media._hash_file

    def counted(original, value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(storage, "_file_hash", lambda p: counted(storage_hash, p))
    monkeypatch.setattr(media, "_hash_file", lambda p: counted(media_hash, p))
    opened = files.open("a", path.name)
    for _ in range(2):
        # The media route owns a different MediaStore from the file route.
        response = media.MediaStore(repo).content(opened["asset_id"], opened["source_version"],
            Request({"type": "http", "method": "HEAD", "headers": [(b"range", b"bytes=0-31")]}))
        assert response.status_code == 206
    assert len(calls) == 1
    files.open("a", path.name)
    assert len(calls) == 2  # An explicit later open still verifies full identity.


def test_single_thumbnail_read_checks_only_requested_image(pipeline, monkeypatch):
    repo, files, path, _ = pipeline
    opened = files.open("a", path.name)
    cache = VideoPreviewCache(repo, PROJECT_ROOT)
    digest = opened["source_version"]
    target = cache._manifest_path(digest)
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"revision": REVISION, "content_hash": digest,
        "status": "ready", "duration_ms": 2400000, "interval_ms": 2000,
        "times": [i * 2000 for i in range(1200)], "frames": list(range(1200))}), "utf-8")
    calls = []
    original = Path.is_file

    def exists(value):
        if value.parent == target.parent and value.suffix == ".jpg":
            calls.append(value.name)
            return True
        return original(value)

    monkeypatch.setattr(Path, "is_file", exists)
    assert Path(cache.frame(opened["asset_id"], digest, 600).path).name == "600.jpg"
    assert calls == ["600.jpg"]


def test_online_preview_advertises_media_strategy_without_local_signature_or_resolution(pipeline):
    repo, _, _, host = pipeline
    item = imported(host)
    cache = VideoPreviewCache(repo, PROJECT_ROOT)
    result = cache.manifest(item.asset_id, item.source_version)
    assert result.strategy == "media" and result.frames == []
    assert not any(call[0] == "media" for call in host.provider.calls)
    host.access.change("bilibili", False)
    with pytest.raises(AppError, match="停用"):
        cache.manifest(item.asset_id, item.source_version)


def test_saved_platform_copy_uses_local_thumbnails_after_disable(pipeline):
    repo, _, _, host = pipeline
    item = imported(host)
    copy = repo.root / "saved.mp4"
    copy.write_bytes(b"\0\0\0\x18ftypmp42" + bytes(4096))
    with repo.engine.begin() as conn:
        conn.execute(update(platform_schema.sources).where(
            platform_schema.sources.c.asset_id == item.asset_id).values(
                local_path=str(copy), local_sha256=hashlib.sha256(copy.read_bytes()).hexdigest()))
    host.access.change("bilibili", False)
    cache = VideoPreviewCache(repo, PROJECT_ROOT)
    assert cache.manifest(item.asset_id, item.source_version).strategy == "thumbnails"


def test_hover_requests_target_time_without_pausing_its_own_decoder(pipeline):
    repo, files, path, _ = pipeline
    opened = files.open("a", path.name)
    cache = VideoPreviewCache(repo, PROJECT_ROOT)
    cache.priority(PreviewPriorityInput(lease_id="hover", active=True, kind="preview",
        asset_id=opened["asset_id"], source_version=opened["source_version"], time_ms=12000))
    assert not cache._foreground()
