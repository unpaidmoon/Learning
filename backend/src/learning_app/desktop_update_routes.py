import asyncio

from fastapi import FastAPI
from pydantic import BaseModel

from learning_app.desktop_updates import (
    ReleaseOffer,
    UpdateCheckInput,
    UpdateConfigureInput,
    UpdatePreferences,
)
from learning_app.errors import AppError


class UpdateCheckView(BaseModel):
    operation_id: str
    state: str
    started_at: str
    checked_at: str | None = None
    automatic: bool
    outcome: str | None = None
    offer: ReleaseOffer | None = None
    error_code: str | None = None


class DesktopUpdateStatus(BaseModel):
    preferences: UpdatePreferences
    current_release: str
    last_check: UpdateCheckView | None
    checking: bool
    automatic_interval_hours: int
    installs_updates: bool


def install_update_routes(app: FastAPI):
    @app.get("/api/v1/runtime/updates", response_model=DesktopUpdateStatus)
    async def status():
        return await asyncio.to_thread(app.state.desktop_updates.status)

    @app.post("/api/v1/runtime/updates", response_model=DesktopUpdateStatus)
    async def configure(value: UpdateConfigureInput):
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "软件正在安全退出")
            result = await asyncio.to_thread(app.state.desktop_updates.configure, value)
            app.state.desktop_updates.changed.set()
            return result

    @app.post("/api/v1/runtime/updates/check", response_model=UpdateCheckView)
    async def check(value: UpdateCheckInput):
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "软件正在安全退出")
            return await app.state.desktop_updates.check(value.operation_id)

    @app.post("/api/v1/runtime/updates/operations/{identity}/reconcile")
    async def reconcile(identity: str):
        return await asyncio.to_thread(app.state.desktop_updates.reconcile, identity)
