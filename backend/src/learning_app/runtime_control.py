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
from pydantic import BaseModel, ConfigDict, Field

from learning_app.errors import AppError
from learning_app.service import TERMINAL
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.runs import TaskRunStore

_INSTANCE_ID = re.compile(r"^[a-f0-9]{32}$")


class RuntimeGenerationResponse(BaseModel):
    generation_id: str
    chat_id: str
    state: str
    cancel_requested: bool


class RuntimeTaskResponse(BaseModel):
    task_id: str
    course_id: str
    state: str


class RuntimePreparationResponse(BaseModel):
    job_id: str
    course_id: str
    kind: str
    state: str


class RuntimeResponse(BaseModel):
    instance_id: str
    exit_prepared: bool
    can_exit: bool
    exit_requested: bool = False
    active_generations: list[RuntimeGenerationResponse]
    active_tasks: list[RuntimeTaskResponse] = Field(default_factory=list)
    active_preparations: list[RuntimePreparationResponse] = Field(default_factory=list)
    active_data_operations: list[dict] = Field(default_factory=list)
    execution_recovery: dict = Field(default_factory=dict)


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
        active_tasks=[
            RuntimeTaskResponse(**value)
            for value in _active_tasks(app)
        ],
        active_preparations=[
            RuntimePreparationResponse(**value)
            for value in PreparationStore(app.state.repository).active()
        ],
        active_data_operations=app.state.data_transfers.active() if hasattr(app.state, "data_transfers") else [],
        execution_recovery=app.state.execution_supervisor.status() if hasattr(app.state, "execution_supervisor") else {},
    )


def _active_generations(app: FastAPI) -> list[dict]:
    # Recovery also revisits terminal answers with missing usage. Those are not
    # active answers and must not trigger a misleading cancellation prompt.
    return [
        value
        for value in app.state.repository.recoverable_generations()
        if value["state"] not in TERMINAL
        and not app.state.service.imported.contains("generation", value["generation_id"])
    ]


def _active_tasks(app):
    return [value for value in TaskRunStore(app.state.repository).active_tasks() if not app.state.service.imported.contains("task", value["task_id"])]


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
            active_tasks = _active_tasks(app)
            active_preparations = PreparationStore(app.state.repository).active()
            active_data = app.state.data_transfers.active() if hasattr(app.state, "data_transfers") else []
            if (active or active_tasks or active_preparations or active_data) and not value.confirm_active:
                raise AppError(
                    "ACTIVE_GENERATIONS_CONFIRMATION_REQUIRED",
                    "仍有回答或学习任务在进行或结果待核对；确认退出后将保存并请求停止",
                    409,
                )
            app.state.exit_prepared = True
            location = getattr(app.state, "profile_location", None)
            if location and location.pending.exists():
                identity = json.loads(location.pending.read_text("utf-8"))["operation_id"]
                switch = json.loads(location.transfer(identity).read_text("utf-8"))
                if switch["kind"] == "move":
                    from learning_app.data_maintenance import DataMaintenance
                    DataMaintenance(app.state.repository).freeze_intent(identity)
            frozen_preparations = PreparationStore(app.state.repository).freeze_for_exit()
        for job in frozen_preparations:
            app.state.preparations.interrupt(job["job_id"])
        if hasattr(app.state, "data_transfers"):
            await app.state.data_transfers.close()
        for task in active_tasks:
            await app.state.task_executor.cancel_task(task["course_id"], task["task_id"])
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

    @router.post("/runtime/reconnect")
    async def reconnect():
        return await app.state.execution_supervisor.retry()

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
