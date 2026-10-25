"""Display-safe account operations backed by the pinned official Harness.

PKCE, callback handling and stored credentials belong to Harness. Public DTOs
are reconstructed from an allowlist rather than forwarding native responses.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, ConfigDict, Field

from learning_app.errors import AppError
from learning_app.execution.selection import model_choices, supports_images
from learning_app.execution.types import ExecutionError


class AccountAttempt(BaseModel):
    id: str
    phase: str
    authorize_url: str | None = None
    expires_at: int | None = None
    error_code: str | None = None


class AccountProfile(BaseModel):
    name: str | None = None
    contact: str | None = None


class AccountWallet(BaseModel):
    currency: Literal["CNY", "USD"]
    balance: str
    kind: Literal["recharge", "bonus"]


class AccountLinks(BaseModel):
    usage_url: str | None = None
    top_up_url: str | None = None


class AccountModelChoice(BaseModel):
    id: str
    name: str
    supports_images: bool
    thinking_levels: list[str]


class AccountResponse(BaseModel):
    route: Literal["account", "api_key"]
    mode: Literal["fixture", "official", "unavailable"]
    status: Literal["signed-out", "credential-stored", "fixture", "unavailable"]
    api_key_configured: bool = False
    can_sign_in: bool = False
    attempt: AccountAttempt | None = None
    profile: AccountProfile | None = None
    balance_status: Literal["unknown", "ready", "failed"] = "unknown"
    wallets: list[AccountWallet] = Field(default_factory=list)
    links: AccountLinks | None = None
    models: list[AccountModelChoice] = Field(default_factory=list)
    error_code: str | None = None


class AccountSignInInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locale: str = Field(default="zh-CN", min_length=2, max_length=35)
    timezone_offset_seconds: int = Field(default=28800, ge=-43200, le=50400)


class AccountCancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attempt_id: str = Field(min_length=1, max_length=200)


class AccountRouteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: Literal["account", "api_key"]


def _client(value: AccountSignInInput | None = None) -> dict:
    value = value or AccountSignInInput()
    return {
        "version": "0.1.0",
        "locale": value.locale,
        "timezoneOffsetSeconds": value.timezone_offset_seconds,
    }


def _execution(app: FastAPI, *, required: bool = False):
    execution = getattr(app.state.service, "execution", None)
    if required and (execution is None or not app.state.service.execution_ready):
        raise AppError("ACCOUNT_UNAVAILABLE", "Harness 账号服务尚未连接，请启用执行层后重试", 503)
    return execution


def _official_url(value: object, *, authorize: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "platform.deepseek.com"
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return None
    if authorize and parsed.path != "/dsh/authorize":
        return None
    return value


def _api_models(config) -> list[AccountModelChoice]:
    return [
        AccountModelChoice(
            id=model,
            name=model,
            supports_images=supports_images(model),
            thinking_levels=["none", "low", "high", "max"],
        )
        for model in model_choices(config.model)
    ]


async def _view(app: FastAPI) -> AccountResponse:
    execution = _execution(app)
    config = app.state.config
    if execution is None or not app.state.service.execution_ready:
        return AccountResponse(
            route=config.credential_route,
            mode="unavailable",
            status="unavailable",
            error_code=app.state.service.execution_error or "HARNESS_NOT_CONFIGURED",
        )
    mode = getattr(execution, "mode", "fixture")
    route = getattr(execution, "credential_route", "api_key")
    if mode != "official":
        return AccountResponse(
            route=route, mode="fixture", status="fixture", models=_api_models(config)
        )
    response = AccountResponse(
        route=route,
        mode="official",
        status="signed-out",
        can_sign_in=True,
        api_key_configured=execution.api_key_configured,
    )
    try:
        state = await execution.account_state()
    except (ExecutionError, httpx.HTTPError, OSError, TimeoutError):
        response.status = "unavailable"
        response.error_code = "ACCOUNT_STATE_UNAVAILABLE"
        return response
    response.status = (
        "credential-stored" if state.get("status") == "credential-stored" else "signed-out"
    )
    links = state.get("links") or {}
    response.links = AccountLinks(
        usage_url=_official_url(links.get("usageUrl")),
        top_up_url=_official_url(links.get("topUpUrl")),
    )
    attempt = state.get("attempt")
    if isinstance(attempt, dict) and isinstance(attempt.get("id"), str):
        response.attempt = AccountAttempt(
            id=attempt["id"],
            phase=attempt.get("phase", "failed"),
            authorize_url=_official_url(attempt.get("authorizeUrl"), authorize=True),
            expires_at=attempt.get("expiresAt"),
            error_code=attempt.get("errorCode"),
        )
    details = app.state.account_details
    if response.status == "credential-stored":
        response.profile = details.get("profile")
        response.balance_status = details.get("balance_status", "unknown")
        response.wallets = details.get("wallets", [])
    else:
        app.state.account_details = {}
        execution._account_models = []
    response.models = (
        _api_models(config)
        if route == "api_key"
        else [AccountModelChoice(**choice) for choice in execution._account_models]
    )
    response.error_code = (
        details.get("error_code") if response.status == "credential-stored" else None
    )
    return response


def _require_official(app: FastAPI):
    execution = _execution(app, required=True)
    if getattr(execution, "mode", "fixture") != "official":
        raise AppError("ACCOUNT_FIXTURE_MODE", "当前是本地验证模式，不会连接官方账号服务", 409)
    return execution


def _require_idle(app: FastAPI) -> None:
    from learning_app.workbench.runs import TaskRunStore
    if any(
        value["state"] not in {"completed", "cancelled", "failed", "truncated"}
        for value in app.state.repository.recoverable_generations()
    ) or TaskRunStore(app.state.repository).has_active_runs():
        raise AppError(
            "ACCOUNT_ACTIVE_GENERATIONS",
            "请等待当前回答结束或先停止并完成对账，再切换登录方式或退出账号",
            409,
        )


async def _refresh(app: FastAPI) -> AccountResponse:
    execution = _require_official(app)
    state = await execution.account_state()
    if state.get("status") != "credential-stored":
        raise AppError("ACCOUNT_SIGN_IN_REQUIRED", "请先登录 DeepSeek 官方账号", 409)
    async with app.state.account_refresh_gate:

        async def query(method: str):
            try:
                return await execution.peer.rpc_args(method, {"client": _client()})
            except (ExecutionError, httpx.HTTPError, OSError, TimeoutError):
                return {"status": "failed"}

        profile, balance = await asyncio.gather(
            query("account/getProfile"), query("account/getBalance")
        )
        details: dict = {"balance_status": "failed", "wallets": []}
        if isinstance(profile, dict) and profile.get("status") == "ready":
            display = profile.get("value", {})
            details["profile"] = AccountProfile(
                name=display.get("name"), contact=display.get("contact")
            )
        if isinstance(balance, dict) and balance.get("status") == "ready":
            details["balance_status"] = "ready"
            details["wallets"] = [
                AccountWallet(currency=wallet["currency"], balance=wallet["balance"], kind=kind)
                for field, kind in (("value", "recharge"), ("bonusWallets", "bonus"))
                for wallet in balance.get(field, [])
                if wallet.get("currency") in {"CNY", "USD"}
                and isinstance(wallet.get("balance"), str)
            ]
        try:
            await execution.refresh_account_models()
        except (ExecutionError, httpx.HTTPError, OSError, TimeoutError):
            details["error_code"] = "ACCOUNT_CATALOG_UNAVAILABLE"
        app.state.account_details = details
    return await _view(app)


def install_account_routes(app: FastAPI) -> None:
    app.state.account_details = {}
    app.state.account_refresh_gate = asyncio.Lock()
    router = APIRouter(prefix="/api/v1/account")

    @router.get("", response_model=AccountResponse)
    async def account_state():
        return await _view(app)

    @router.post("/sign-in", response_model=AccountResponse)
    async def sign_in(value: AccountSignInInput):
        execution = _require_official(app)
        try:
            await execution.peer.rpc_args(
                "account/startSignIn",
                {
                    "client": _client(value),
                    "callbackOrigin": execution.host.base,
                    "loginSource": "web",
                },
            )
        except (ExecutionError, httpx.HTTPError, OSError, TimeoutError) as exc:
            raise AppError(
                "ACCOUNT_SIGN_IN_START_FAILED", "官方登录暂时无法启动，请稍后重试", 502
            ) from exc
        return await _view(app)

    @router.post("/cancel", response_model=AccountResponse)
    async def cancel_sign_in(value: AccountCancelInput):
        execution = _require_official(app)
        await execution.peer.rpc_args("account/cancelSignIn", {"attemptId": value.attempt_id})
        return await _view(app)

    @router.post("/sign-out", response_model=AccountResponse)
    async def sign_out():
        execution = _require_official(app)
        async with app.state.submission_gate:
            _require_idle(app)
            await execution.peer.rpc_args("account/signOut", {"client": _client()})
            app.state.account_details = {}
            execution._account_models = []
        return await _view(app)

    @router.post("/refresh", response_model=AccountResponse)
    async def refresh_account():
        return await _refresh(app)

    @router.post("/route", response_model=AccountResponse)
    async def set_route(value: AccountRouteInput):
        execution = _execution(app, required=True)
        async with app.state.submission_gate:
            _require_idle(app)
            previous_route = execution.credential_route
            try:
                execution.set_credential_route(value.route)
            except ExecutionError as exc:
                raise AppError(
                    exc.code, "API Key 尚未配置，请检查本机私有配置后重新启动", 409
                ) from exc
            path: Path = app.state.config.data_dir / "credential-route.json"
            temporary = path.with_name(f"credential-route.{uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8") as stream:
                    json.dump({"route": value.route}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            except OSError as exc:
                execution.set_credential_route(previous_route)
                raise AppError(
                    "CREDENTIAL_ROUTE_SAVE_FAILED",
                    "登录方式保存失败，已保持原有选择，请检查数据目录后重试",
                    409,
                ) from exc
            finally:
                temporary.unlink(missing_ok=True)
        return await _view(app)

    app.include_router(router)
