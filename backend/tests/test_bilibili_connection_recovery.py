"""Expired authentication is distinct from rejected resources; no real account used."""

# pytest resolves the imported fixture by name.
# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from test_bilibili_media_faults import media_host, transport  # noqa: F401

from learning_app.errors import AppError
from learning_app.integrations.bilibili.login import Login
from learning_app.integrations.bilibili.provider import BilibiliProvider
from learning_app.integrations.models import ImportSelection, ResolveInput
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.processing_models import PreparationControl


def login_fixture(host, monkeypatch, account="10"):
    polls = []

    def json_response(grant, url, params=None):
        host.access.check(grant)
        assert url.endswith("/qrcode/generate")
        return {"url": "https://passport.bilibili.com/synthetic-login", "qrcode_key": "fixture"}

    def poll(grant, key):
        host.access.check(grant)
        polls.append(key)
        return {
            "data": {"code": 0, "url": ""},
            "cookies": {"SESSDATA": "fixture-not-a-credential", "DedeUserID": account},
        }

    monkeypatch.setattr(host.provider, "json", json_response, raising=False)
    monkeypatch.setattr(host.provider, "credential_poll", poll, raising=False)
    login = Login(host)
    return login, login.start().login_id, polls


def connected_item(host):
    connection = host.access.connect("10", "合成账号", "connected").connection_id
    plan = host.resolve("a", ResolveInput(connection_id=connection, url="BV1234567890"))
    item = host.import_selected(
        "a",
        ImportSelection(
            operation_id="account-bound-import",
            plan_id=plan.plan_id,
            object_ids=[plan.parts[0].object_id],
        ),
    ).items[0]
    return connection, item


def test_confirmed_session_expiry_revokes_old_grant_and_updates_settings(media_host, monkeypatch):
    _, host, _ = media_host
    connection = host.access.connect("10", "合成账号", "connected").connection_id
    grant = host.access.grant(connection)
    calls = []

    def http(req):
        calls.append(req)
        return httpx.Response(200, json={"code": -101, "message": "synthetic logged out"})

    transport(monkeypatch, http)
    provider = BilibiliProvider(host.access, host.private, host.project_root)
    with pytest.raises(AppError) as expired:
        provider.json(grant, "https://api.bilibili.com/x/web-interface/nav")
    assert expired.value.code == "BILI_LOGIN_REQUIRED"
    view = next(v for v in host.access.modules() if v.platform == "bilibili")
    assert view.connection_id == connection and view.connection_state == "expired"
    with pytest.raises(AppError):
        provider.json(grant, "https://api.bilibili.com/x/web-interface/nav")
    assert len(calls) == 1


def test_resource_forbidden_does_not_claim_account_session_expired(media_host, monkeypatch):
    _, host, _ = media_host
    connection = host.access.connect("10", "合成账号", "connected").connection_id
    grant = host.access.grant(connection)
    transport(monkeypatch, lambda req: httpx.Response(200, json={"code": -403}))
    provider = BilibiliProvider(host.access, host.private, host.project_root)
    with pytest.raises(AppError) as restricted:
        provider.json(grant, "https://api.bilibili.com/synthetic-restricted-resource")
    assert restricted.value.code == "BILI_REMOTE_REJECTED"
    assert (
        next(v for v in host.access.modules() if v.platform == "bilibili").connection_state
        == "connected"
    )
    host.access.check(grant)


def test_pinned_media_engine_login_failure_uses_the_same_expiry_guard(media_host, monkeypatch):
    _, host, _ = media_host
    connection = host.access.connect("10", "合成账号", "connected").connection_id
    grant = host.access.grant(connection)
    provider = BilibiliProvider(host.access, host.private, host.project_root)
    base, _ = provider.engine()
    # Exercise the adapter's real GuardedDL.urlopen; upstream extraction is not the subject here.
    monkeypatch.setattr(
        base,
        "extract_info",
        lambda self, *args, **kwargs: self.urlopen(
            "https://api.bilibili.com/x/player/wbi/playurl?cid=1"
        ),
    )
    transport(monkeypatch, lambda req: httpx.Response(200, json={"code": -101}))
    with pytest.raises(AppError) as error:
        provider.media(grant, {"cid": 1, "bvid": "BV1234567890", "page": 1, "duration_ms": 2000})
    assert error.value.code == "BILI_LOGIN_REQUIRED"
    assert (
        next(v for v in host.access.modules() if v.platform == "bilibili").connection_state
        == "expired"
    )


def test_same_account_reauthentication_keeps_source_and_task_but_requires_resume(
    media_host, monkeypatch
):
    repo, host, _ = media_host
    connection, item = connected_item(host)
    grant = host.access.grant(connection)
    store = PreparationStore(repo)
    job = host.queue("a", item.asset_id, item.source_version, "platform_download")
    assert host.access.expire(grant)
    assert store.get("a", job.job_id).state == "paused"
    login, identity, polls = login_fixture(host, monkeypatch)
    result = login.poll(identity)
    assert result.connection.connection_id == connection
    assert host.source(item.asset_id)["connection_id"] == connection
    assert host.source(item.asset_id)["source_version"] == item.source_version
    assert store.get("a", job.job_id).parameters["connection_id"] == connection
    assert store.claim() is None and store.get("a", job.job_id).state == "paused"
    assert not host.access.expire(grant), "A late failure must not expire the new credential epoch"
    host.access.check(host.access.grant(connection))
    with pytest.raises(AppError):
        host.access.check(grant)
    assert login.poll(identity) == result and len(polls) == 1
    paused = store.get("a", job.job_id)
    store.control(
        "a",
        job.job_id,
        "resume",
        PreparationControl(
            operation_id="explicit-resume-after-login", expected_revision=paused.revision
        ),
    )
    assert store.claim()["parameters"]["connection_id"] == connection


def test_different_account_does_not_rebind_expired_source(media_host, monkeypatch):
    _, host, _ = media_host
    connection, item = connected_item(host)
    host.access.expire(host.access.grant(connection))
    login, identity, _ = login_fixture(host, monkeypatch, "20")
    result = login.poll(identity)
    assert result.connection.connection_id != connection
    assert host.source(item.asset_id)["connection_id"] == connection
    with pytest.raises(AppError):
        host.access.grant(connection)


def test_fresh_same_account_login_restores_disconnected_source_without_reimport(
    media_host, monkeypatch
):
    from learning_app.media import MediaPositionInput, MediaStore

    repo, host, _ = media_host
    connection, item = connected_item(host)
    media = MediaStore(repo)
    opened = media.open(item.asset_id, item.source_version)
    media.save_position(
        item.asset_id,
        MediaPositionInput(
            source_version=item.source_version,
            position_ms=1000,
            expected_revision=opened["position_revision"],
        ),
    )
    host.access.disconnect(connection)
    with pytest.raises(AppError):
        media.open(item.asset_id, item.source_version)
    login, identity, _ = login_fixture(host, monkeypatch)
    assert login.poll(identity).connection.connection_id == connection
    assert host.source(item.asset_id)["connection_id"] == connection
    host.access.check(host.access.grant(connection))
    restored = media.open(item.asset_id, item.source_version)
    assert (
        restored["asset_id"] == item.asset_id and restored["source_version"] == item.source_version
    )
    assert (
        restored["position_ms"] == 1000
        and restored["playback_session_id"] != opened["playback_session_id"]
    )


def test_reenable_cannot_overtake_disable_pause(media_host):
    _, host, _ = media_host
    entered, release, attempted, completed = Event(), Event(), Event(), Event()
    revoke = host.access.on_revoke

    def blocked(identity):
        entered.set()
        assert release.wait(5)
        revoke(identity)

    def enable():
        attempted.set()
        result = host.access.change("bilibili", True)
        completed.set()
        return result

    host.access.on_revoke = blocked
    with ThreadPoolExecutor(max_workers=2) as executor:
        disabled = executor.submit(host.access.change, "bilibili", False)
        assert entered.wait(5)
        enabled = executor.submit(enable)
        assert attempted.wait(5)
        try:
            assert not completed.wait(0.2)
        finally:
            release.set()
        disabled.result(timeout=5)
        assert enabled.result(timeout=5).enabled


def test_fresh_login_to_current_account_preserves_source_and_rotates_old_grant(
    media_host, monkeypatch
):
    _, host, _ = media_host
    connection, item = connected_item(host)
    previous = host.access.grant(connection)
    login, identity, _ = login_fixture(host, monkeypatch)
    assert login.poll(identity).connection.connection_id == connection
    assert host.source(item.asset_id)["connection_id"] == connection
    with pytest.raises(AppError):
        host.access.check(previous)


def test_pending_qr_does_not_replace_current_business_connection_with_temporary_guest(
    media_host, monkeypatch
):
    _, host, _ = media_host
    connection, _ = connected_item(host)
    login_fixture(host, monkeypatch)
    module = next(m for m in host.access.modules() if m.platform == "bilibili")
    assert module.connection_id == connection and module.connection_state == "connected"


def test_cached_successful_receipt_cannot_reconnect_after_explicit_disconnect(
    media_host, monkeypatch
):
    _, host, _ = media_host
    connection, item = connected_item(host)
    login, identity, polls = login_fixture(host, monkeypatch)
    assert login.poll(identity).connection.connection_id == connection
    host.access.disconnect(connection, forget=lambda: host.private.remove(connection))
    with pytest.raises(AppError):
        login.poll(identity)
    assert len(polls) == 1 and host.private.get(connection) == {}
    assert host.source(item.asset_id)["connection_id"] == connection
    with pytest.raises(AppError):
        host.access.grant(connection)


@pytest.mark.parametrize("change", ["disconnect", "module", "anonymous"])
def test_pending_old_receipt_cannot_override_later_connection_action(
    media_host, monkeypatch, change
):
    _, host, _ = media_host
    connection, item = connected_item(host)
    login, identity, polls = login_fixture(host, monkeypatch)
    original_poll = host.provider.credential_poll

    def pending(grant, key):
        result = original_poll(grant, key)
        if change == "disconnect":
            host.access.disconnect(connection, forget=lambda: host.private.remove(connection))
        elif change == "module":
            host.access.change("bilibili", False)
            host.access.change("bilibili", True)
        else:
            host.access.connect()
        return result

    monkeypatch.setattr(host.provider, "credential_poll", pending)
    with pytest.raises(AppError):
        login.poll(identity)
    assert len(polls) == 1 and host.private.get(identity)["poll_receipt"]["data"]["code"] == 0
    with pytest.raises(AppError):
        login.poll(identity)
    assert len(polls) == 1 and host.source(item.asset_id)["connection_id"] == connection


def test_new_qr_intent_supersedes_inflight_old_receipt(media_host, monkeypatch):
    _, host, _ = media_host
    connection, _ = connected_item(host)
    login, old_identity, polls = login_fixture(host, monkeypatch)
    original_poll = host.provider.credential_poll
    newer = []

    def pending(grant, key):
        result = original_poll(grant, key)
        if not newer:
            newer.append(Login(host).start().login_id)
        return result

    monkeypatch.setattr(host.provider, "credential_poll", pending)
    with pytest.raises(AppError):
        login.poll(old_identity)
    with pytest.raises(AppError):
        login.poll(old_identity)
    assert len(polls) == 1
    assert Login(host).poll(newer[0]).connection.connection_id == connection
    assert len(polls) == 2


def test_durable_credential_write_with_lost_ack_reconciles_without_reinstall(
    media_host, monkeypatch
):
    _, host, _ = media_host
    connection, _ = connected_item(host)
    login, identity, polls = login_fixture(host, monkeypatch)
    original_put = host.private.put
    writes = []

    def lost_ack(key, value):
        original_put(key, value)
        if key == connection and "cookies" in value:
            writes.append(key)
            if len(writes) == 1:
                raise AppError(
                    "PRIVATE_STORE_FAILED", "synthetic lost credential acknowledgement", 503
                )

    monkeypatch.setattr(host.private, "put", lost_ack)
    with pytest.raises(AppError, match="synthetic lost credential acknowledgement"):
        login.poll(identity)
    with pytest.raises(AppError):
        host.access.grant(connection)
    assert login.poll(identity).connection.connection_id == connection
    assert len(writes) == 1 and len(polls) == 1


def test_disconnect_after_partial_login_prevents_old_plan_republishing_credentials(
    media_host, monkeypatch
):
    _, host, _ = media_host
    connection, _ = connected_item(host)
    login, identity, polls = login_fixture(host, monkeypatch)
    original_put = host.private.put

    def failed_install(key, value):
        if key == connection and "cookies" in value:
            raise AppError("PRIVATE_STORE_FAILED", "synthetic failed install", 503)
        original_put(key, value)

    monkeypatch.setattr(host.private, "put", failed_install)
    with pytest.raises(AppError, match="synthetic failed install"):
        login.poll(identity)
    host.access.disconnect(connection, forget=lambda: host.private.remove(connection))
    with pytest.raises(AppError):
        login.poll(identity)
    assert host.private.get(connection) == {} and len(polls) == 1


def test_credential_cleanup_cannot_delete_a_later_fresh_login(media_host, monkeypatch):
    _, host, _ = media_host
    connection, _ = connected_item(host)
    login, _, polls = login_fixture(host, monkeypatch)
    entered, release, started = Event(), Event(), Event()

    def forget():
        entered.set()
        assert release.wait(5)
        host.private.remove(connection)

    def reconnect():
        started.set()
        identity = login.start().login_id
        return login.poll(identity)

    with ThreadPoolExecutor(max_workers=2) as executor:
        removed = executor.submit(host.access.disconnect, connection, forget=forget)
        assert entered.wait(5)
        connected = executor.submit(reconnect)
        assert started.wait(5)
        try:
            assert not connected.done()
        finally:
            release.set()
        removed.result(timeout=5)
        assert connected.result(timeout=5).connection.connection_id == connection
    assert host.private.get(connection)["cookies"]["DedeUserID"] == "10" and len(polls) == 1


def test_unknown_poll_is_not_automatically_sent_again_and_old_connection_is_kept(
    media_host, monkeypatch
):
    _, host, _ = media_host
    connection, _ = connected_item(host)
    previous = host.access.grant(connection)
    login, identity, polls = login_fixture(host, monkeypatch)

    def unknown(grant, key):
        host.access.check(grant)
        polls.append(key)
        raise AppError("BILI_LOGIN_RESULT_UNKNOWN", "synthetic unknown login receipt", 502)

    monkeypatch.setattr(host.provider, "credential_poll", unknown)
    with pytest.raises(AppError):
        login.poll(identity)
    with pytest.raises(AppError) as again:
        login.poll(identity)
    assert again.value.code == "BILI_LOGIN_RESULT_UNKNOWN" and len(polls) == 1
    host.access.check(previous)


def test_known_waiting_poll_can_continue_without_being_mislabelled_unknown(media_host, monkeypatch):
    _, host, _ = media_host
    connection, _ = connected_item(host)
    login, identity, polls = login_fixture(host, monkeypatch)
    original_poll = host.provider.credential_poll
    waiting = True

    def poll(grant, key):
        nonlocal waiting
        if waiting:
            waiting = False
            host.access.check(grant)
            polls.append(key)
            return {"data": {"code": 86090}, "cookies": {}}
        return original_poll(grant, key)

    monkeypatch.setattr(host.provider, "credential_poll", poll)
    assert login.poll(identity).state == "scanned"
    assert login.poll(identity).connection.connection_id == connection and len(polls) == 2


def test_reauthentication_cannot_overtake_pausing_expired_connection_tasks(media_host):
    repo, host, _ = media_host
    connection, item = connected_item(host)
    store = PreparationStore(repo)
    job = host.queue("a", item.asset_id, item.source_version, "platform_download")
    entered, release, started, authenticated = Event(), Event(), Event(), Event()
    original_revoke = host.access.on_revoke

    def revoke(identity):
        entered.set()
        assert release.wait(5)
        original_revoke(identity)

    def authenticate():
        started.set()
        result = host.access.authenticate("10", "合成账号", lambda _: None)
        authenticated.set()
        return result

    host.access.on_revoke = revoke
    with ThreadPoolExecutor(max_workers=2) as executor:
        expired = executor.submit(host.access.expire, host.access.grant(connection))
        assert entered.wait(5)
        restored = executor.submit(authenticate)
        assert started.wait(5)
        try:
            assert not authenticated.wait(0.2), (
                "New credentials became available before old tasks paused"
            )
        finally:
            release.set()
        assert expired.result(timeout=5)
        assert restored.result(timeout=5).connection_id == connection
    assert store.get("a", job.job_id).state == "paused" and store.claim() is None


def test_expiry_pauses_running_lease_and_late_completion_cannot_publish(media_host):
    repo, host, _ = media_host
    connection, item = connected_item(host)
    store = PreparationStore(repo)
    job = host.queue("a", item.asset_id, item.source_version, "platform_download")
    lease = store.claim()
    assert host.access.expire(host.access.grant(connection))
    assert not store.finish(lease, None, error_code="BILI_LOGIN_REQUIRED")
    assert store.get("a", job.job_id).state == "paused"
    assert host.source(item.asset_id)["local_path"] is None


def test_simultaneous_login_checks_reuse_one_receipt_and_one_connection(media_host, monkeypatch):
    _, host, _ = media_host
    connection, _ = connected_item(host)
    host.access.expire(host.access.grant(connection))
    login, identity, polls = login_fixture(host, monkeypatch)
    original_poll = host.provider.credential_poll
    entered, release, second_started, second_polled = Event(), Event(), Event(), Event()

    def blocking_poll(grant, key):
        if entered.is_set():
            second_polled.set()
        entered.set()
        assert release.wait(5)
        return original_poll(grant, key)

    def second_check():
        second_started.set()
        return Login(host).poll(identity)

    monkeypatch.setattr(host.provider, "credential_poll", blocking_poll)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(login.poll, identity)
        assert entered.wait(5)
        second = executor.submit(second_check)
        assert second_started.wait(5)
        try:
            assert not second_polled.wait(0.2), (
                "Two concurrent checks dispatched the same pending login"
            )
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    assert len(polls) == 1 and login.poll(identity).connection.connection_id == connection


@pytest.mark.parametrize(
    "result", ["guest-login", "http-forbidden", "network-error", "old-module-epoch"]
)
def test_nondefinitive_or_stale_failure_cannot_expire_current_identity(
    media_host, monkeypatch, result
):
    _, host, _ = media_host
    connection = host.access.connect(
        "10", "合成账号", "guest" if result == "guest-login" else "connected"
    ).connection_id
    grant = host.access.grant(connection)
    if result == "old-module-epoch":
        host.access.change("bilibili", False)
        host.access.change("bilibili", True)
        assert not host.access.expire(grant)
    else:

        def http(req):
            if result == "network-error":
                raise httpx.ConnectError("synthetic offline")
            return (
                httpx.Response(403)
                if result == "http-forbidden"
                else httpx.Response(200, json={"code": -101})
            )

        transport(monkeypatch, http)
        provider = BilibiliProvider(host.access, host.private, host.project_root)
        with pytest.raises(AppError):
            provider.json(grant, "https://api.bilibili.com/x/web-interface/nav")
    view = next(v for v in host.access.modules() if v.platform == "bilibili")
    assert view.connection_state == ("guest" if result == "guest-login" else "connected")
    host.access.check(host.access.grant(connection))


@pytest.mark.parametrize("action", ["disconnect", "expire"])
def test_account_revoke_does_not_invalidate_other_connections_playback_session(media_host, action):
    _, host, item = media_host
    active = host.media.session(host.source(item.asset_id))
    other = host.access.connect("20", "另一个合成账号", "connected").connection_id
    if action == "expire":
        host.access.expire(host.access.grant(other))
    else:
        host.access.disconnect(other)
    continued = host.media.session(host.source(item.asset_id))
    assert continued["session_id"] == active["session_id"]
    assert host.provider.resolutions == 1


def test_http_reauthentication_preserves_source_scope_and_never_returns_credentials(
    media_host, monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient
    from test_bilibili_media_faults import FaultProvider

    from learning_app.app import create_app
    from learning_app.config import AppConfig

    repo, old_host, _ = media_host
    connection, item = connected_item(old_host)
    job = old_host.queue("a", item.asset_id, item.source_version, "platform_download")
    old_host.access.expire(old_host.access.grant(connection))
    app = create_app(
        AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "runtime"),
        repository=repo,
        integrations_provider_factory=FaultProvider,
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        host = app.state.integrations
        _, _, polls = login_fixture(host, monkeypatch)
        path = "/api/v1/integrations/bilibili/login"
        assert client.post(path).status_code == 403
        client.headers["X-Learning-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        requested = client.post(path).raise_for_status().json()
        result = client.post(path + "/" + requested["login_id"]).raise_for_status()
        assert result.json()["connection"]["connection_id"] == connection
        assert "SESSDATA" not in result.text and "fixture-not-a-credential" not in result.text
        assert client.post(path + "/" + requested["login_id"]).json() == result.json()
        assert len(polls) == 1
        assert host.source(item.asset_id)["connection_id"] == connection
        assert PreparationStore(repo).get("a", job.job_id).state == "paused"


def test_first_login_cannot_publish_connected_before_private_credentials_are_durable(
    media_host, monkeypatch
):
    _, host, _ = media_host
    login, identity, polls = login_fixture(host, monkeypatch, "20")
    original_put = host.private.put
    fail = True

    def fail_credentials_once(key, value):
        nonlocal fail
        if "cookies" in value and fail:
            fail = False
            raise AppError("PRIVATE_STORE_FAILED", "synthetic first credential failure", 503)
        original_put(key, value)

    monkeypatch.setattr(host.private, "put", fail_credentials_once)
    with pytest.raises(AppError, match="synthetic first credential failure"):
        login.poll(identity)
    assert (
        next(v for v in host.access.modules() if v.platform == "bilibili").connection_state
        == "guest"
    )
    target = host.private.get(identity)["target_connection_id"]
    result = login.poll(identity)
    assert not fail and result.connection.connection_id == target and len(polls) == 1
    host.access.check(host.access.grant(target))


@pytest.mark.parametrize("failure", ["credentials", "result"])
def test_private_publication_failure_reconciles_original_login_without_duplicate_connection(
    media_host, monkeypatch, failure
):
    _, host, _ = media_host
    connection, _ = connected_item(host)
    host.access.expire(host.access.grant(connection))
    login, identity, polls = login_fixture(host, monkeypatch)
    original_put = host.private.put
    failed = False

    def fail_once(key, value):
        nonlocal failed
        selected = (
            key == connection and "cookies" in value
            if failure == "credentials"
            else key == identity and "result" in value
        )
        if selected and not failed:
            failed = True
            raise AppError("PRIVATE_STORE_FAILED", "synthetic storage failure", 503)
        original_put(key, value)

    monkeypatch.setattr(host.private, "put", fail_once)
    with pytest.raises(AppError, match="synthetic storage failure"):
        login.poll(identity)
    assert len(polls) == 1
    if failure == "credentials":
        with pytest.raises(AppError):
            host.access.grant(connection)
    result = login.poll(identity)
    assert failed and result.connection.connection_id == connection and len(polls) == 1
    assert login.poll(identity) == result
    host.access.check(host.access.grant(connection))
