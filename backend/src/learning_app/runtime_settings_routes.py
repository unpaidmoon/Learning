"""Local resource controls, without model or system-wide configuration access."""

import asyncio
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from learning_app.errors import AppError
from learning_app.runtime_preferences import (
    RuntimePreferenceInput,
    RuntimePreferenceRecord,
    RuntimePreferenceValues,
    resource_policy,
)
from learning_app.workbench.preparations import PreparationStore
from learning_app.workbench.processing_models import PreparationControl


class RuntimeResourceStatus(BaseModel):
    mode: Literal["gentle", "accelerated"]
    cpu_threads: int
    priority: Literal["below_normal", "normal"]
    local_processing: bool


class RuntimePowerStatus(BaseModel):
    available: bool
    held: bool | None
    error_code: str | None


class RuntimePreferencesResponse(BaseModel):
    record: RuntimePreferenceRecord | None
    effective: RuntimePreferenceValues
    resources: RuntimeResourceStatus
    keep_awake: RuntimePowerStatus
    error_code: str | None = None


def install_runtime_settings_routes(app: FastAPI):
    def view():
        store = app.state.runtime_preferences
        record, error = None, None
        try:
            with store.lock:
                record = store.read()
        except AppError as exception:
            error = exception.code
        values = record.values if record else RuntimePreferenceValues()
        return {
            "record": record,
            "effective": values,
            "resources": {
                **resource_policy(values.resource_mode),
                "local_processing": app.state.preparations._active is not None,
            },
            "keep_awake": app.state.power_keeper.status(),
            "error_code": error,
        }

    @app.get("/api/v1/runtime/preferences", response_model=RuntimePreferencesResponse)
    def preferences():
        return view()

    @app.post("/api/v1/runtime/preferences", response_model=RuntimePreferencesResponse)
    async def set_preferences(value: RuntimePreferenceInput):
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "软件正在安全退出")
            if value.values.keep_awake and not app.state.power_keeper.platform.available:
                raise AppError("POWER_KEEP_AWAKE_UNAVAILABLE", "当前系统不支持保持唤醒")
            await asyncio.to_thread(app.state.runtime_preferences.set, value)
            app.state.power_keeper.wake()
            return view()

    @app.post("/api/v1/runtime/preferences/operations/{identity}/reconcile")
    async def reconcile(identity: str):
        return await asyncio.to_thread(app.state.runtime_preferences.reconcile, identity)

    @app.post("/api/v1/runtime/release-local-resources")
    async def release():
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "软件正在安全退出")
            store = PreparationStore(app.state.repository)
            paused = []
            for row in store.active():
                current = store.get(row["course_id"], row["job_id"])
                if current.state not in {"queued", "running"}:
                    continue
                import uuid

                result = store.control(
                    row["course_id"],
                    row["job_id"],
                    "pause",
                    PreparationControl(
                        operation_id="release_" + uuid.uuid4().hex,
                        expected_revision=current.revision,
                    ),
                )
                app.state.preparations.interrupt(result.job_id)
                paused.append(result.job_id)
            return {"paused_local_jobs": paused, "state": "release_requested"}
