"""Known question capability remains separate from a running Harness Host."""

import asyncio
from types import SimpleNamespace

import pytest

from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.types import ExecutionError
from learning_app.service import GenerationService


class LocalAccountPeer:
    def __init__(self):
        self.signed_in = False
        self.catalog_error = False
        self.calls = []

    async def rpc_args(self, method, _arguments):
        self.calls.append(method)
        if method == "account/getState":
            return {"status": "credential-stored" if self.signed_in else "signed-out"}
        if self.catalog_error:
            raise ExecutionError("ACCOUNT_CATALOG_UNAVAILABLE")
        return {
            "groups": [
                {
                    "id": "deepseek-account",
                    "models": [
                        {
                            "id": "synthetic-account-model",
                            "name": "合成目录模型",
                            "reasoning": {"efforts": [{"id": "high"}]},
                        }
                    ],
                }
            ]
        }


def test_account_readiness_requires_known_login_and_actual_catalog_and_property_does_no_io(
    tmp_path,
):
    adapter = ExecutionAdapter(
        tmp_path, tmp_path / "execution", mode="official", credential_route="account"
    )
    adapter.peer = LocalAccountPeer()
    adapter._open = True
    service = GenerationService(None, AppConfig(), execution=adapter)
    service.execution_ready = True

    async def exercise():
        assert not adapter.question_answering_ready
        assert not service.question_answering_ready
        adapter.peer.signed_in = True
        await adapter.account_state()
        assert not adapter.question_answering_ready
        # A Host catalog alone cannot stand in for the actual account listing.
        with pytest.raises(ExecutionError, match="ACCOUNT_CATALOG_UNAVAILABLE"):
            await adapter.refresh_account_models()
        assert not service.question_answering_ready
        adapter.boundary.account_catalog_observed = True
        adapter.boundary.account_model_modalities = {"synthetic-account-model": ["text"]}
        await adapter.refresh_account_models()
        assert adapter.question_answering_ready
        before = len(adapter.peer.calls)
        for _ in range(5):
            assert service.question_answering_ready
        assert len(adapter.peer.calls) == before
        # Failed refresh invalidates the advertised capability, while the Host
        # and non-model application features remain available.
        adapter.peer.catalog_error = True
        with pytest.raises(ExecutionError, match="ACCOUNT_CATALOG_UNAVAILABLE"):
            await adapter.refresh_account_models()
        assert service.execution_ready
        assert not service.question_answering_ready
        adapter.peer.catalog_error = False
        await adapter.refresh_account_models()
        assert service.question_answering_ready
        adapter.peer.signed_in = False
        await adapter.account_state()
        assert adapter._account_models == []
        assert not service.question_answering_ready
        assert adapter.credential_route == "account"

    try:
        asyncio.run(exercise())
    finally:
        adapter.boundary.stop()


def test_route_changes_and_account_unauthorized_recompute_readiness_without_network(tmp_path):
    adapter = ExecutionAdapter(
        tmp_path,
        tmp_path / "execution",
        mode="official",
        credential_route="account",
        api_key="synthetic-key",
    )
    try:
        adapter._open = True
        adapter._account_signed_in = True
        adapter._account_models = [{"id": "synthetic", "supports_images": False}]
        assert adapter.question_answering_ready
        adapter.boundary.account_token_rejected = True
        assert not adapter.question_answering_ready
        adapter.set_credential_route("api_key")
        assert adapter.question_answering_ready
        adapter.set_credential_route("account")
        assert not adapter.question_answering_ready
        adapter.boundary.account_token_rejected = False
        assert adapter.question_answering_ready
        adapter._open = False
        assert not adapter.question_answering_ready
    finally:
        adapter.boundary.stop()


def test_api_route_requires_key_while_fixture_is_explicitly_available(tmp_path):
    missing = ExecutionAdapter(
        tmp_path, tmp_path / "missing", mode="official", credential_route="api_key"
    )
    fixture = ExecutionAdapter(tmp_path, tmp_path / "fixture", mode="fixture")
    try:
        missing._open = fixture._open = True
        with pytest.raises(ExecutionError, match="MODEL_KEY_NOT_CONFIGURED"):
            missing.set_credential_route("api_key")
        assert not missing.question_answering_ready
        assert fixture.question_answering_ready
        fixture._open = False
        assert not fixture.question_answering_ready
        service = GenerationService(None, AppConfig(), execution=SimpleNamespace(mode="official"))
        service.execution_ready = True
        assert not service.question_answering_ready
        service.execution = SimpleNamespace(mode="fixture")
        assert service.question_answering_ready
        service.execution_ready = False
        assert not service.question_answering_ready
    finally:
        missing.boundary.stop()
        fixture.boundary.stop()
