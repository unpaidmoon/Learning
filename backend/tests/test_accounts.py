"""Account projections and explicit route controls, with no external requests."""

import copy
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from learning_app.app import create_app
from learning_app.config import AppConfig
from learning_app.execution.types import ExecutionError

PRIVATE_SENTINEL = "never-publish-this-credential"


class AccountExecution:
    mode = "official"
    credential_route = "account"

    def __init__(self, *, signed_in=True, api_key=None):
        self.boundary = SimpleNamespace(_api_key=api_key)
        self.host = SimpleNamespace(base="http://127.0.0.1:19456")
        self.peer = self
        self._account_models = []
        self.calls = []
        self.state = {
            "status": "credential-stored" if signed_in else "signed-out",
            "token": PRIVATE_SENTINEL,
            "links": {
                "usageUrl": "https://platform.deepseek.com/usage",
                "topUpUrl": "https://platform.deepseek.com/top_up",
            },
            "attempt": None,
        }

    async def account_state(self):
        return copy.deepcopy(self.state)

    @property
    def api_key_configured(self):
        return bool(self.boundary._api_key)

    async def refresh_account_models(self):
        if self.state["status"] != "credential-stored":
            raise ExecutionError("ACCOUNT_SIGN_IN_REQUIRED")
        self._account_models = [
            {
                "id": "official-fixture-model",
                "name": "官方目录合成项",
                "supports_images": False,
                "thinking_levels": ["high"],
            }
        ]
        return self._account_models

    async def rpc_args(self, method, arguments):
        self.calls.append((method, arguments))
        if method == "account/startSignIn":
            self.state["attempt"] = {
                "id": "attempt-one",
                "phase": "waiting-browser",
                "authorizeUrl": "https://platform.deepseek.com/dsh/authorize?authorize_id=fixture",
                "token": PRIVATE_SENTINEL,
            }
        elif method == "account/signOut":
            self.state["status"] = "signed-out"
        elif method == "account/cancelSignIn":
            self.state["attempt"]["phase"] = "cancelled"
        elif method == "account/getProfile":
            return {
                "status": "ready",
                "value": {
                    "id": PRIVATE_SENTINEL,
                    "name": "合成账号",
                    "contact": "***1234",
                    "token": PRIVATE_SENTINEL,
                },
            }
        elif method == "account/getBalance":
            return {
                "status": "ready",
                "value": [{"currency": "CNY", "balance": "1.20"}],
                "bonusWallets": [],
            }
        return await self.account_state()

    def set_credential_route(self, route):
        if route == "api_key" and not self.boundary._api_key:
            raise ExecutionError("MODEL_KEY_NOT_CONFIGURED")
        self.credential_route = route


def account_client(tmp_path, execution):
    config = AppConfig(mode="test", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run")
    app = create_app(config, execution=execution)
    return app, TestClient(app, base_url="http://127.0.0.1:8765")


def connect(client):
    return {"X-Learning-CSRF": client.get("/api/v1/session").json()["csrf_token"]}


def test_account_state_projection_and_explicit_refresh_do_not_publish_credentials(tmp_path):
    execution = AccountExecution(api_key=PRIVATE_SENTINEL)
    _app, client = account_client(tmp_path, execution)
    with client:
        headers = connect(client)
        response = client.get("/api/v1/account")
        assert response.status_code == 200
        data = response.json()
        assert data["route"] == "account"
        assert data["status"] == "credential-stored"
        assert data["api_key_configured"] is True
        assert data["balance_status"] == "unknown"
        assert data["wallets"] == []
        assert execution.calls == []
        assert PRIVATE_SENTINEL not in response.text
        response = client.post("/api/v1/account/refresh", headers=headers)
        assert response.status_code == 200
        assert response.json()["balance_status"] == "ready"
        assert response.json()["profile"] == {"name": "合成账号", "contact": "***1234"}
        assert response.json()["models"][0]["id"] == "official-fixture-model"
        assert PRIVATE_SENTINEL not in response.text
        assert client.post("/api/v1/account/refresh").status_code == 403


def test_sign_in_uses_official_host_callback_and_rejects_arbitrary_inputs(tmp_path):
    execution = AccountExecution(signed_in=False)
    _app, client = account_client(tmp_path, execution)
    with client:
        headers = connect(client)
        response = client.post(
            "/api/v1/account/sign-in",
            headers=headers,
            json={"locale": "zh-CN", "timezone_offset_seconds": 28800},
        )
        assert response.status_code == 200
        assert response.json()["attempt"]["authorize_url"].startswith(
            "https://platform.deepseek.com/dsh/authorize?"
        )
        assert PRIVATE_SENTINEL not in response.text
        assert execution.calls[-1][1]["callbackOrigin"] == execution.host.base
        assert (
            client.post(
                "/api/v1/account/sign-in",
                headers=headers,
                json={"callbackOrigin": "https://evil.invalid"},
            ).status_code
            == 422
        )
        response = client.post(
            "/api/v1/account/cancel", headers=headers, json={"attempt_id": "attempt-one"}
        )
        assert response.json()["attempt"]["phase"] == "cancelled"


def test_route_is_explicit_and_missing_key_never_falls_back(tmp_path):
    execution = AccountExecution(signed_in=False)
    _app, client = account_client(tmp_path, execution)
    with client:
        headers = connect(client)
        response = client.post("/api/v1/account/route", headers=headers, json={"route": "api_key"})
        assert response.status_code == 409
        assert response.json()["code"] == "MODEL_KEY_NOT_CONFIGURED"
        assert execution.credential_route == "account"
        assert not (tmp_path / "data/credential-route.json").exists()
        execution.boundary._api_key = PRIVATE_SENTINEL
        response = client.post("/api/v1/account/route", headers=headers, json={"route": "api_key"})
        assert response.status_code == 200
        assert response.json()["route"] == "api_key"
        assert (
            json.loads((tmp_path / "data/credential-route.json").read_text())["route"] == "api_key"
        )
        assert PRIVATE_SENTINEL not in response.text
        assert execution.state["status"] == "signed-out"


def test_active_or_unknown_generation_blocks_route_change_and_signout(tmp_path):
    execution = AccountExecution(api_key=PRIVATE_SENTINEL)
    app, client = account_client(tmp_path, execution)
    with client:
        headers = connect(client)
        course = app.state.repository.create_course("合成课程")
        chat = app.state.repository.create_chat(course["course_id"])
        app.state.repository.publish_submission(chat["chat_id"], "synthetic", "pending-one")
        for path, body in (("/route", {"route": "api_key"}), ("/sign-out", None)):
            response = client.post("/api/v1/account" + path, headers=headers, json=body)
            assert response.status_code == 409
            assert response.json()["code"] == "ACCOUNT_ACTIVE_GENERATIONS"
        assert execution.credential_route == "account"
        assert not any(method == "account/signOut" for method, _args in execution.calls)


def test_fixture_mode_denies_account_operations_without_external_calls(tmp_path):
    execution = AccountExecution()
    execution.mode = "fixture"
    execution.credential_route = "api_key"
    _app, client = account_client(tmp_path, execution)
    with client:
        headers = connect(client)
        data = client.get("/api/v1/account").json()
        assert data["mode"] == "fixture"
        assert data["status"] == "fixture"
        assert data["can_sign_in"] is False
        assert data["models"]
        for path, body in (("/sign-in", {}), ("/refresh", None), ("/sign-out", None)):
            response = client.post("/api/v1/account" + path, headers=headers, json=body)
            assert response.status_code == 409
            assert response.json()["code"] == "ACCOUNT_FIXTURE_MODE"
        assert execution.calls == []


def test_expired_account_clears_display_details_and_does_not_switch_route(tmp_path):
    execution = AccountExecution()
    _app, client = account_client(tmp_path, execution)
    with client:
        headers = connect(client)
        assert (
            client.post("/api/v1/account/refresh", headers=headers).json()["balance_status"]
            == "ready"
        )
        execution.state["status"] = "signed-out"
        execution.state["links"]["usageUrl"] = "https://evil.invalid/"
        data = client.get("/api/v1/account").json()
        assert data["status"] == "signed-out"
        assert data["route"] == "account"
        assert data["profile"] is None
        assert data["wallets"] == []
        assert data["balance_status"] == "unknown"
        assert data["models"] == []
        assert data["links"]["usage_url"] is None
