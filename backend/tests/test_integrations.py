"""Deterministic platform/host contracts. The fixture is never a registered platform."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from learning_app.app import create_app
from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore, CourseMoveInput, CourseRenameInput
from learning_app.errors import AppError
from learning_app.integrations import schema as s
from learning_app.integrations.bilibili.provider import allowed_url, video_url
from learning_app.integrations.host import IntegrationHost
from learning_app.integrations.models import ImportSelection, ResolveInput
from learning_app.integrations.private import PrivateStore
from learning_app.media import MediaPositionInput, MediaStore
from learning_app.storage import Repository
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.transcript_models import SubtitleChange, TranscriptEdit
from learning_app.workbench.transcripts import TranscriptStore


class FixtureProvider:
    """No network; strict grants expose disable/course/account mistakes."""

    calls = []

    def __init__(self, access, private, root):
        self.access = access

    def resolve(self, grant, value):
        self.access.check(grant)
        self.calls.append(("resolve", grant.connection_id))
        return {
            "title": "合成两P",
            "complete": True,
            "multipart": True,
            "parts": [
                {
                    "object_id": f"BV1234567890:{i}",
                    "bvid": "BV1234567890",
                    "cid": i,
                    "aid": 1,
                    "page": i,
                    "title": f"合成课{i}",
                    "duration_ms": 2000,
                }
                for i in [1, 2]
            ],
        }

    def media(self, grant, part):
        self.access.check(grant)
        self.calls.append(("media", grant.connection_id))
        return {
            "video": {
                "url": "https://example.bilivideo.com/video",
                "vcodec": "avc1",
                "format_id": "v",
            },
            "audio": {
                "url": "https://example.bilivideo.com/audio",
                "acodec": "mp4a",
                "format_id": "a",
            },
            "duration_ms": 2000,
        }

    def subtitle(self, grant, part):
        self.access.check(grant)
        self.calls.append(("subtitle", grant.connection_id))
        return {
            "state": "available",
            "tracks": [
                {
                    "language": "zh-CN",
                    "origin": "platform_cc",
                    "segments": [{"start_ms": 0, "end_ms": 2000, "text": "确定性合成字幕"}],
                }
            ],
        }


@pytest.fixture
def setup(tmp_path):
    repo = Repository(tmp_path / "data")
    host = IntegrationHost(repo, PROJECT_ROOT, provider_factory=FixtureProvider)
    repo.integrations = host
    for identity in ["a", "b"]:
        repo.create_course(identity, identity)
        root = tmp_path / identity
        root.mkdir()
        CourseFileStore(repo).bind(identity, str(root))
    FixtureProvider.calls = []
    yield repo, host
    repo.close()


def plan(host, course="a", connection=None):
    host.access.change("bilibili", True)
    connection = connection or host.access.connect().connection_id
    return host.resolve(course, ResolveInput(connection_id=connection, url="BV1234567890"))


def imported(host):
    value = plan(host)
    return host.import_selected(
        "a",
        ImportSelection(
            operation_id="import_a", plan_id=value.plan_id, object_ids=[value.parts[0].object_id]
        ),
    ).items[0]


def test_disabled_zero_load_and_other_platform_unavailable(setup):
    repo, host = setup
    assert len(host.access.modules()) == 8
    assert not any(m.enabled for m in host.access.modules())
    assert host._provider is None
    with pytest.raises(AppError) as error:
        host.access.connect()
    assert error.value.code == "PLATFORM_DISABLED"
    with pytest.raises(AppError) as error:
        host.access.change("zotero", True)
    assert error.value.code == "PLATFORM_NOT_IMPLEMENTED"
    assert FixtureProvider.calls == []


def test_disable_revokes_saved_grant_and_keeps_receipt(setup):
    repo, host = setup
    value = plan(host)
    grant = host.access.grant(value.connection_id)
    request = host.access.request(grant, "fixture-inflight")
    host.access.change("bilibili", False)
    host.access.receipt(request, 200)
    with pytest.raises(AppError):
        host.access.check(grant)
    host.access.change("bilibili", True)
    with pytest.raises(AppError) as error:
        host.access.check(grant)
    assert error.value.code == "PLATFORM_GRANT_REVOKED"
    with repo.engine.connect() as conn:
        assert conn.execute(select(s.requests.c.state)).scalar_one() == "received"


def test_import_selection_idempotent_account_course_and_plan_scope(setup):
    repo, host = setup
    value = plan(host)
    selected = ImportSelection(
        operation_id="one", plan_id=value.plan_id, object_ids=[value.parts[1].object_id]
    )
    first = host.import_selected("a", selected)
    assert first == host.import_selected("a", selected)
    assert len(first.items) == 1 and "002 " in first.items[0].path
    with pytest.raises(AppError):
        host.import_selected("b", selected)
    new_plan = plan(host, connection=value.connection_id)
    second = host.import_selected(
        "a", selected.model_copy(update={"operation_id": "two", "plan_id": new_plan.plan_id})
    )
    assert second.items[0].asset_id == first.items[0].asset_id
    other = plan(host, course="b", connection=value.connection_id)
    third = host.import_selected(
        "b", selected.model_copy(update={"operation_id": "three", "plan_id": other.plan_id})
    )
    assert third.items[0].asset_id != first.items[0].asset_id
    assert len(MediaStore(repo).list("a")) == 1


def test_saved_plan_cannot_bypass_disable_or_disconnect(setup):
    repo, host = setup
    value = plan(host)
    action = ImportSelection(
        operation_id="one", plan_id=value.plan_id, object_ids=[value.parts[0].object_id]
    )
    host.access.change("bilibili", False)
    with pytest.raises(AppError):
        host.import_selected("a", action)
    host.access.change("bilibili", True)
    host.access.disconnect(value.connection_id)
    with pytest.raises(AppError):
        host.import_selected("a", action)


def test_rename_move_and_local_copy_keep_progress_version_and_offline_access(setup, tmp_path):
    repo, host = setup
    item = imported(host)
    media = MediaStore(repo)
    media.save_position(
        item.asset_id,
        MediaPositionInput(
            source_version=item.source_version, position_ms=1200, expected_revision=0
        ),
    )
    files = CourseFileStore(repo)
    renamed = files.rename(
        "a", CourseRenameInput(operation_id="rename", path=item.path, name="重命名.bili")
    )
    moved = files.move(
        "a", CourseMoveInput(operation_id="move", path=renamed["entry"]["path"], destination="")
    )
    assert moved["entry"]["asset_id"] == item.asset_id
    host.access.change("bilibili", False)
    with pytest.raises(AppError):
        media.open(item.asset_id, item.source_version)
    from learning_app.integrations.media import digest

    local = repo.root / "copy.mp4"
    local.write_bytes(b"synthetic-container-bytes-only")
    from sqlalchemy import update

    with repo.engine.begin() as conn:
        conn.execute(
            update(s.sources)
            .where(s.sources.c.asset_id == item.asset_id)
            .values(local_path=str(local), local_sha256=digest(local))
        )
    opened = files.open("a", moved["entry"]["path"])
    assert opened["video"]["position_ms"] == 1200
    assert opened["video"]["source_version"] == item.source_version
    assert len(media.list("a")) == 1
    assert opened["video"]["audio_content_url"] is None


def test_platform_subtitle_publishes_host_track_and_keeps_manual_edit(setup):
    repo, host = setup
    item = imported(host)
    result = host.subtitles("a", item.asset_id, item.source_version)
    tracks = TranscriptStore(repo)
    view = tracks.view("a", result.track_ids[0])
    assert view.source_type == "platform_cc"
    segment = view.segments[0]
    tracks.edit(
        "a",
        view.track_id,
        TranscriptEdit(
            operation_id="edit",
            expected_edit_head_id=None,
            changes=[
                SubtitleChange(
                    segment_id=segment.segment_id,
                    raw_revision_id=segment.raw_revision_id,
                    text="用户手改",
                    start_ms=0,
                    end_ms=2000,
                )
            ],
        ),
    )
    host.subtitles("a", item.asset_id, item.source_version)
    assert tracks.view("a", view.track_id).segments[0].text == "用户手改"


def test_download_uses_existing_persistent_queue_and_does_not_load_engine(setup):
    repo, host = setup
    item = imported(host)
    first = host.queue("a", item.asset_id, item.source_version, "platform_download")
    second = host.queue("a", item.asset_id, item.source_version, "platform_download")
    assert first.job_id == second.job_id
    assert PreparationStore(repo).list("a")[0].job_id == first.job_id
    host.access.change("bilibili", False)
    assert PreparationStore(repo).get("a", first.job_id).state == "paused"
    assert PreparationStore(repo).claim() is None


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1/",
        "https://evil.example/",
        "https://bilibili.com.evil.example/",
        "https://a.bilivideo.cn:4483/v",
        "https://user:pass@api.bilibili.com/x",
    ],
)
def test_network_scope_rejects_arbitrary_targets(value):
    with pytest.raises(AppError):
        allowed_url(value)


@pytest.mark.parametrize(
    "value",
    [
        "file:///C:/a",
        "https://bilibili.com.evil/video/BV1234567890",
        "https://www.bilibili.com/video/BV1234567890?p=-1",
        "https://www.bilibili.com/",
    ],
)
def test_link_scope(value):
    with pytest.raises(AppError):
        video_url(value)


def test_dpapi_private_round_trip_and_disconnect_preserves_assets(setup):
    repo, host = setup
    item = imported(host)
    connection = host.source(item.asset_id)["connection_id"]
    private = PrivateStore(repo.root)
    private.put(connection, {"cookies": {"fixture": "not-a-real-secret"}})
    assert b"not-a-real-secret" not in private.path(connection).read_bytes()
    assert private.get(connection)["cookies"]["fixture"] == "not-a-real-secret"
    host.access.disconnect(connection)
    private.remove(connection)
    assert host.source(item.asset_id)["connection_id"] == connection


def test_http_routes_csrf_and_no_sdk_on_default_start(tmp_path):
    config = AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run")
    with TestClient(create_app(config), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/v1/integrations/modules").status_code == 403
        token = client.get("/api/v1/session").json()["csrf_token"]
        modules = client.get("/api/v1/integrations/modules").json()
        assert all(not row["enabled"] for row in modules)
        assert client.app.state.integrations._provider is None
        assert (
            client.post("/api/v1/integrations/modules/bilibili", json={"enabled": True}).status_code
            == 403
        )
        client.headers["X-Learning-CSRF"] = token
        assert (
            client.post("/api/v1/integrations/modules/bilibili", json={"enabled": True}).status_code
            == 200
        )


def test_login_header_cookies_persist_privately_and_reconcile_once(setup):
    from learning_app.integrations.bilibili.login import Login

    repo, host = setup
    host.access.change("bilibili", True)

    class LoginFixture(FixtureProvider):
        calls = 0

        def json(self, grant, url, params=None):
            self.access.check(grant)
            assert url.endswith("/qrcode/generate")
            return {
                "url": "https://passport.bilibili.com/synthetic-login",
                "qrcode_key": "fixture-key",
            }

        def credential_poll(self, grant, key):
            self.access.check(grant)
            self.calls += 1
            return {
                "data": {"code": 0, "url": "https://www.bilibili.com/"},
                "cookies": {"SESSDATA": "fixture-cookie-not-real", "DedeUserID": "12345"},
            }

    host.provider_factory = LoginFixture
    identity = Login(host).start().login_id
    first = Login(host).poll(identity)
    second = Login(host).poll(identity)
    assert first == second and first.state == "connected"
    assert "fixture-cookie-not-real" not in first.model_dump_json()
    assert (
        host.private.get(first.connection.connection_id)["cookies"]["SESSDATA"]
        == "fixture-cookie-not-real"
    )
    assert host.provider.calls == 1


def test_course_directory_preserves_episode_identity_and_denies_restricted_media(
    setup, monkeypatch
):
    from learning_app.integrations.bilibili.provider import BilibiliProvider

    repo, host = setup
    host.access.change("bilibili", True)
    grant = host.access.grant(host.access.connect().connection_id)
    provider = BilibiliProvider(host.access, host.private, PROJECT_ROOT)
    episodes = [
        {
            "id": 10 + i,
            "aid": 20 + i,
            "cid": 30 + i,
            "duration": 60,
            "title": f"分集{i}",
            "playable": i == 1,
        }
        for i in [1, 2]
    ]
    monkeypatch.setattr(
        provider,
        "json",
        lambda *args, **kwargs: {
            "title": "合成课程",
            "season_id": 999,
            "ep_count": 2,
            "episodes": episodes,
            "episode_page": {"next": False},
        },
    )
    result = provider.resolve(grant, "https://www.bilibili.com/cheese/play/ss999")
    assert result["group_id"] == "cheese:999"
    assert [p["page"] for p in result["parts"]] == [1, 2]
    assert result["parts"][0]["object_id"] == "cheese:11:31"
    assert result["parts"][1]["availability"] == "restricted"
    with pytest.raises(AppError) as error:
        provider.media(grant, result["parts"][1])
    assert error.value.code == "BILI_ACCESS_RESTRICTED"
    monkeypatch.setattr(
        provider,
        "json",
        lambda *args, **kwargs: {
            "ep_count": 3,
            "episodes": episodes,
            "episode_page": {"next": True},
        },
    )
    with pytest.raises(AppError) as error:
        provider.resolve(grant, "https://www.bilibili.com/cheese/play/ss999")
    assert error.value.code == "BILI_COURSE_DIRECTORY_INCOMPLETE"


def test_multiple_accounts_same_course_do_not_share_folders_or_resources(setup):
    repo, host = setup
    first_plan = plan(host)
    first = host.import_selected(
        "a",
        ImportSelection(
            operation_id="account_first",
            plan_id=first_plan.plan_id,
            object_ids=[first_plan.parts[0].object_id],
        ),
    ).items[0]
    second_plan = plan(host)
    second = host.import_selected(
        "a",
        ImportSelection(
            operation_id="account_second",
            plan_id=second_plan.plan_id,
            object_ids=[second_plan.parts[0].object_id],
        ),
    ).items[0]
    assert first.asset_id != second.asset_id
    assert Path(first.path).parent != Path(second.path).parent


def test_refresh_selects_chinese_platform_track(setup):
    repo, host = setup
    item = imported(host)
    tracks = TranscriptStore(repo)
    segments = [{"start_ms": 0, "end_ms": 2000, "text": "fixture transcript"}]
    chinese = tracks.publish_platform(
        "a", item.asset_id, item.source_version, "ai-zh", "platform_ai", segments, 2000
    )
    tracks.publish_platform(
        "a", item.asset_id, item.source_version, "ai-en", "platform_ai", segments, 2000
    )
    assert tracks.list("a", item.asset_id, item.source_version)[0] == chinese


def test_late_download_cannot_publish_after_pause_and_resume_reconciles_local_effect(setup):
    import hashlib

    from learning_app.workbench.processing_models import PreparationControl

    repo, host = setup
    item = imported(host)
    store = PreparationStore(repo)
    job = host.queue("a", item.asset_id, item.source_version, "platform_download")
    lease = store.claim()
    path = repo.root / "verified-synthetic-copy.mp4"
    path.write_bytes(b"fixture bytes, not a real media acceptance")
    result = {
        "kind": "platform_download",
        "source_digest": lease["source_digest"],
        "unit_index": 0,
        "copy": {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
    }
    paused = store.control(
        "a",
        job.job_id,
        "pause",
        PreparationControl(
            operation_id="pause_download", expected_revision=store.get("a", job.job_id).revision
        ),
    )
    assert store.finish(lease, result) is False
    assert host.source(item.asset_id)["local_path"] is None
    store.control(
        "a",
        job.job_id,
        "resume",
        PreparationControl(operation_id="resume_download", expected_revision=paused.revision),
    )
    resumed = store.claim()
    assert store.finish(resumed, result) is True
    assert host.source(item.asset_id)["local_path"] == str(path)
    assert store.get("a", job.job_id).state == "completed"


def test_task_cancel_blocks_next_metadata_request_without_revoking_connection(setup):
    from threading import Event

    repo, host = setup
    value = plan(host)
    grant = host.access.grant(value.connection_id)
    cancelled = Event()
    scoped = host.access.scoped(grant, cancelled)
    first = host.access.request(scoped, "fixture-started")
    host.access.receipt(first, 200)
    cancelled.set()
    with pytest.raises(AppError) as error:
        host.access.request(scoped, "fixture-next")
    assert error.value.code == "PREPARATION_INTERRUPTED"
    host.access.check(grant)
    with repo.engine.connect() as conn:
        assert len(conn.execute(select(s.requests)).all()) == 1


def test_resumed_task_does_not_reuse_cancelled_operation_grant_from_media_cache(setup):
    from threading import Event

    repo, host = setup
    item = imported(host)
    row = host.source(item.asset_id, item.source_version)
    first_cancel, resumed_cancel = Event(), Event()
    first = host.media.session(row, first_cancel)
    first_cancel.set()
    with pytest.raises(AppError):
        host.access.check(first["grant"])
    resumed = host.media.session(row, resumed_cancel)
    host.access.check(resumed["grant"])
    assert resumed["grant"].cancelled is resumed_cancel
    assert [call[0] for call in FixtureProvider.calls].count("media") == 1


def test_wrapped_sdk_guard_error_keeps_cancellation_semantics():
    from learning_app.integrations.bilibili.provider import extraction_error

    try:
        raise AppError("PREPARATION_INTERRUPTED", "fixture cancellation", 409)
    except AppError as guard:
        try:
            raise RuntimeError("fixture SDK wrapper") from guard
        except RuntimeError as wrapped:
            result = extraction_error(wrapped)
    assert result.code == "PREPARATION_INTERRUPTED"
    assert result.status == 409
    assert extraction_error(RuntimeError("network failure")).code == "BILI_EXTRACTION_FAILED"
