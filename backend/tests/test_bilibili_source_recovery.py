"""Source-bound reconnects preserve legacy identities rather than reimporting assets."""

# pytest resolves the imported fixture by name.
# ruff: noqa: F811
import pytest
from fastapi.testclient import TestClient
from test_bilibili_connection_recovery import connected_item, login_fixture
from test_bilibili_media_faults import FaultProvider, media_host  # noqa: F401

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.errors import AppError
from learning_app.integrations.bilibili.login import Login
from learning_app.integrations.models import RemoteAction
from learning_app.integrations.recovery import SourceRecovery


def test_http_recovery_restores_exact_anonymous_source_without_supplier_login(media_host, tmp_path):
    repo, host, item = media_host
    connection = host.source(item.asset_id)["connection_id"]
    previous = host.access.grant(connection)
    host.access.disconnect(connection)
    app = create_app(
        AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "runtime"),
        repository=repo,
        integrations_provider_factory=FaultProvider,
    )
    path = f"/api/v1/courses/a/integrations/{item.asset_id}/reconnect"
    value = {"operation_id": "restore-original-guest", "source_version": item.source_version}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.headers["X-Learning-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        response = client.post(path, json=value)
        assert response.status_code == 200
        result = response.json()
        assert result["kind"] == "anonymous" and result["connection"]["connection_id"] == connection
        restored = app.state.integrations
        restored.access.check(restored.access.grant(connection))
        with pytest.raises(AppError):
            restored.access.check(previous)
        assert client.post(path, json=value).json() == result
        assert restored._provider is None


def test_source_recovery_targets_older_same_uid_identity_not_most_recent(
    media_host, monkeypatch, tmp_path
):
    repo, host, _ = media_host
    original, item = connected_item(host)
    host.access.disconnect(original)
    newer = host.access.connect("10", "同账号另一个历史身份", "connected").connection_id
    app = create_app(
        AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "runtime"),
        repository=repo,
        integrations_provider_factory=FaultProvider,
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        active = app.state.integrations
        login_fixture(active, monkeypatch)
        client.headers["X-Learning-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
        path = f"/api/v1/courses/a/integrations/{item.asset_id}/reconnect"
        response = client.post(
            path,
            json={"operation_id": "restore-exact-source", "source_version": item.source_version},
        )
        assert response.status_code == 200
        recovery = response.json()
        assert recovery["kind"] == "login"
        result = Login(active).poll(recovery["login"]["login_id"])
        assert result.connection.connection_id == original
        assert active.source(item.asset_id)["connection_id"] == original
        active.access.check(active.access.grant(newer))


def test_wrong_scanned_account_cannot_rebind_source_or_create_business_identity(
    media_host, monkeypatch
):
    repo, host, _ = media_host
    original, item = connected_item(host)
    host.access.disconnect(original)
    _, _, polls = login_fixture(host, monkeypatch, "20")
    recovery = SourceRecovery(host).begin(
        "a",
        item.asset_id,
        RemoteAction(operation_id="wrong-account", source_version=item.source_version),
    )
    with pytest.raises(AppError) as error:
        Login(host).poll(recovery.login.login_id)
    assert error.value.code == "BILI_ACCOUNT_MISMATCH" and len(polls) == 1
    assert host.source(item.asset_id)["connection_id"] == original
    with repo.engine.connect() as conn:
        assert (
            conn.exec_driver_sql(
                "SELECT count(*) FROM integration_connections WHERE account_id='20'"
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("change", ["course", "version", "disabled"])
def test_wrong_source_scope_is_rejected_before_any_login_or_restore(media_host, change):
    _, host, item = media_host
    connection = host.source(item.asset_id)["connection_id"]
    host.access.disconnect(connection)
    if change == "disabled":
        host.access.change("bilibili", False)
    with pytest.raises(AppError):
        SourceRecovery(host).begin(
            "other" if change == "course" else "a",
            item.asset_id,
            RemoteAction(
                operation_id="wrong-scope",
                source_version="old-version" if change == "version" else item.source_version,
            ),
        )
    with pytest.raises(AppError):
        host.access.grant(connection)
    assert host._provider is not None  # Only the original deterministic import adapter existed.


def test_account_recovery_check_reuses_same_qr_and_never_requeries_supplier(
    media_host, monkeypatch
):
    _, host, _ = media_host
    original, item = connected_item(host)
    host.access.disconnect(original)
    login_fixture(host, monkeypatch)
    count = []
    original_json = host.provider.json

    def json_response(*args, **kwargs):
        count.append(1)
        return original_json(*args, **kwargs)

    monkeypatch.setattr(host.provider, "json", json_response)
    action = RemoteAction(operation_id="same-qr", source_version=item.source_version)
    first = SourceRecovery(host).begin("a", item.asset_id, action)
    assert SourceRecovery(host).begin("a", item.asset_id, action) == first and len(count) == 1


def test_same_operation_cannot_be_used_for_another_source(media_host, monkeypatch):
    _, host, guest = media_host
    _, other = connected_item(host)
    login_fixture(host, monkeypatch)
    SourceRecovery(host).begin(
        "a",
        guest.asset_id,
        RemoteAction(operation_id="collision", source_version=guest.source_version),
    )
    with pytest.raises(AppError) as error:
        SourceRecovery(host).begin(
            "a",
            other.asset_id,
            RemoteAction(operation_id="collision", source_version=other.source_version),
        )
    assert error.value.code == "BILI_RECOVERY_OPERATION_CONFLICT"


def test_old_anonymous_receipt_cannot_restore_after_new_disconnect(media_host):
    _, host, item = media_host
    connection = host.source(item.asset_id)["connection_id"]
    host.access.disconnect(connection)
    action = RemoteAction(operation_id="guest-old-receipt", source_version=item.source_version)
    SourceRecovery(host).begin("a", item.asset_id, action)
    host.access.disconnect(connection)
    with pytest.raises(AppError):
        SourceRecovery(host).begin("a", item.asset_id, action)
    with pytest.raises(AppError):
        host.access.grant(connection)


def test_anonymous_write_ack_failure_reconciles_original_epoch_without_new_identity(
    media_host, monkeypatch
):
    _, host, item = media_host
    connection = host.source(item.asset_id)["connection_id"]
    host.access.disconnect(connection)
    action = RemoteAction(operation_id="guest-unknown", source_version=item.source_version)
    original_put = host.private.put
    failed = False

    def lose_ack(key, value):
        nonlocal failed
        if "result" in value and not failed:
            failed = True
            raise AppError(
                "PRIVATE_STORE_FAILED", "synthetic missing recovery acknowledgement", 503
            )
        original_put(key, value)

    monkeypatch.setattr(host.private, "put", lose_ack)
    with pytest.raises(AppError):
        SourceRecovery(host).begin("a", item.asset_id, action)
    epoch = host.access.grant(connection).connection_epoch
    result = SourceRecovery(host).begin("a", item.asset_id, action)
    assert (
        result.connection.connection_id == connection
        and host.access.grant(connection).connection_epoch == epoch
    )
