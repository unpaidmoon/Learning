"""Local, capability-protected exit preparation for the owned MVP launcher.

Closing a browser does not call these routes. Exit is an explicit business
operation: freeze new submissions, persist scoped cancellation intent, then ask
only the launcher whose live record matches this backend to stop its services.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, ConfigDict

from learning_app.errors import AppError
from learning_app.service import TERMINAL

_INSTANCE_ID = re.compile(r"^[a-f0-9]{32}$")


class RuntimeGenerationResponse(BaseModel):
    generation_id: str
    chat_id: str
    state: str
    cancel_requested: bool


class RuntimeResponse(BaseModel):
    instance_id: str
    exit_prepared: bool
    can_exit: bool
    exit_requested: bool = False
    active_generations: list[RuntimeGenerationResponse]


class PrepareExitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instance_id: str
    confirm_active: bool = False


def _owned_run_directory(app: FastAPI) -> Path | None:
    config = app.state.config
    identity = config.instance_id
    if os.environ.get("LEARNING_APP_OWNED_STDIN") != "1" or not _INSTANCE_ID.fullmatch(identity):
        return None
    run_root = (config.runtime_dir / "apps").resolve()
    run_dir = (run_root / identity).resolve()
    if run_dir.parent != run_root:
        return None
    try:
        record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        record.get("instance_id") != identity
        or record.get("backend_pid") != os.getpid()
        or record.get("status") != "ready"
    ):
        return None
    return run_dir


def _runtime_view(app: FastAPI, *, exit_requested: bool = False) -> RuntimeResponse:
    active = _active_generations(app)
    return RuntimeResponse(
        instance_id=app.state.config.instance_id,
        exit_prepared=app.state.exit_prepared,
        can_exit=_owned_run_directory(app) is not None,
        exit_requested=exit_requested,
        active_generations=[
            RuntimeGenerationResponse(
                generation_id=value["generation_id"],
                chat_id=value["chat_id"],
                state=value["state"],
                cancel_requested=value["cancel_requested"],
            )
            for value in active
        ],
    )


def _active_generations(app: FastAPI) -> list[dict]:
    # Recovery also revisits terminal answers with missing usage. Those are not
    # active answers and must not trigger a misleading cancellation prompt.
    return [
        value
        for value in app.state.repository.recoverable_generations()
        if value["state"] not in TERMINAL
    ]


def install_runtime_routes(app: FastAPI) -> None:
    # The submission route holds the same lock through business publication.
    # The lock never spans provider generation or waits for a full answer.
    app.state.submission_gate = asyncio.Lock()
    app.state.exit_prepared = False
    router = APIRouter(prefix="/api/v1")

    async def prepare(value: PrepareExitInput) -> None:
        if value.instance_id != app.state.config.instance_id:
            raise AppError("INSTANCE_MISMATCH", "后台实例已变化，请重新连接后退出", 409)
        async with app.state.submission_gate:
            active = _active_generations(app)
            if active and not value.confirm_active:
                raise AppError(
                    "ACTIVE_GENERATIONS_CONFIRMATION_REQUIRED",
                    "仍有回答正在生成或结果待核对；确认退出后将保存并请求停止",
                    409,
                )
            app.state.exit_prepared = True
        # Publication has stopped. Per-generation cancellation persists its
        # intent before calling Harness, and preserves uncertain executions.
        # This is not a substitute for ordinary answer cancellation in the UI.
        for generation in active:
            gid = generation["generation_id"]
            if generation["state"] == "unknown":
                await app.state.service.reconcile(gid, resume_observer=False)
            if app.state.repository.get_generation(gid)["state"] not in TERMINAL:
                await app.state.service.cancel(gid)

    @router.get("/runtime", response_model=RuntimeResponse)
    async def runtime_status():
        return _runtime_view(app)

    @router.post("/runtime/prepare-exit", response_model=RuntimeResponse)
    async def prepare_exit(value: PrepareExitInput):
        await prepare(value)
        return _runtime_view(app)

    @router.post("/runtime/exit", response_model=RuntimeResponse)
    async def exit_application(value: PrepareExitInput):
        run_dir = _owned_run_directory(app)
        if run_dir is None:
            raise AppError("OWNED_LAUNCHER_REQUIRED", "请使用项目统一入口启动后安全退出", 409)
        await prepare(value)
        # Atomic, instance-bound mailbox; no PID read from disk is terminated.
        temporary = run_dir / f"stop.request.{uuid4().hex}.tmp"
        with temporary.open("w", encoding="ascii") as stream:
            stream.write(app.state.config.instance_id)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(run_dir / "stop.request")
        return _runtime_view(app, exit_requested=True)

    app.include_router(router)
