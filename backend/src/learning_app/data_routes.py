"""User initiated data exports and staged local-profile switches."""

import asyncio
import os
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from learning_app.data_operations import DataOperations
from learning_app.errors import AppError
from learning_app.native_files import pick_paths


class DataOperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")


class DataExportInput(DataOperationInput):
    include_originals: bool = False


class DataTransferInput(DataOperationInput):
    kind: Literal["import", "move"]
    parent: str | None = None
    package: str | None = None


class DataOperationResponse(BaseModel):
    operation_id: str
    kind: str
    state: str
    created_at: str
    updated_at: str
    error_code: str | None = None
    result: dict | None = None


class DataLocationResponse(BaseModel):
    data_directory: str
    business_bytes: int
    execution_bytes: int
    cache_bytes: int
    pending_switch: bool
    pending_operation_id: str | None = None
    operations: list[DataOperationResponse]


class DataTransferService:
    def __init__(self, app):
        self.app = app
        self.operations = DataOperations(app.state.repository, app.state.profile_location.anchor)
        self.tasks = {}

    def start(self, record):
        identity = record["operation_id"]
        if record["state"] != "prepared" or identity in self.tasks:
            return

        async def run():
            try:
                if record["kind"] == "export":
                    await asyncio.to_thread(self.operations.perform_export, identity)
                else:
                    await asyncio.to_thread(
                        self.operations.perform_transfer, identity, self.app.state.profile_location
                    )
            finally:
                self.tasks.pop(identity, None)

        self.tasks[identity] = asyncio.create_task(run())

    def active(self):
        return [self.operations.status(identity) for identity in tuple(self.tasks)]

    async def close(self):
        for identity in tuple(self.tasks):
            self.operations.cancel(identity)
        await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)

    def information(self):
        import json

        root = self.app.state.repository.root
        totals = {"business": 0, "execution": 0, "cache": 0}
        for parent, dirs, files in os.walk(root, followlinks=False):
            from pathlib import Path

            from learning_app.course_files import _is_link

            dirs[:] = [d for d in dirs if not _is_link(Path(parent) / d)]
            for name in files:
                path = Path(parent) / name
                if not _is_link(path):
                    first = path.relative_to(root).parts[0]
                    category = (
                        "execution"
                        if first == "execution"
                        else "cache"
                        if first == "video-previews"
                        else "business"
                    )
                    try:
                        totals[category] += path.stat().st_size
                    except FileNotFoundError:
                        pass
        return {
            "data_directory": str(root),
            "business_bytes": totals["business"],
            "execution_bytes": totals["execution"],
            "cache_bytes": totals["cache"],
            "pending_switch": self.app.state.profile_location.pending.exists(),
            "pending_operation_id": json.loads(
                self.app.state.profile_location.pending.read_text("utf-8")
            )["operation_id"]
            if self.app.state.profile_location.pending.exists()
            else None,
            "operations": self.operations.list(),
        }


def install_data_routes(app: FastAPI):
    def manager():
        return app.state.data_transfers

    def available(identity):
        if app.state.exit_prepared:
            raise AppError("APPLICATION_EXIT_PREPARED", "软件正在安全退出")
        if any(key != identity for key in manager().tasks):
            raise AppError("DATA_OPERATION_BUSY", "已有数据操作在进行，请等待或停止原操作")

    @app.get("/api/v1/data", response_model=DataLocationResponse)
    async def information():
        return await asyncio.to_thread(manager().information)

    @app.post("/api/v1/data/exports", response_model=DataOperationResponse)
    async def export(value: DataExportInput):
        async with app.state.submission_gate:
            available(value.operation_id)
            record = manager().operations.prepare_export(
                value.operation_id, include_originals=value.include_originals
            )
            manager().start(record)
            return record

    @app.post("/api/v1/data/transfers", response_model=DataOperationResponse | None)
    async def transfer(value: DataTransferInput):
        available(value.operation_id)
        package = value.package
        if value.kind == "import" and package is None:
            selected = await asyncio.to_thread(pick_paths, directory=False)
            if not selected:
                return None
            if len(selected) != 1:
                raise AppError("DATA_IMPORT_SINGLE_PACKAGE", "一次请选择一个完整数据包；不同数据包不会合并")
            package = selected[0]
        parent = value.parent
        if parent is None:
            selected = await asyncio.to_thread(pick_paths, directory=True)
            if not selected:
                return None
            parent = selected[0]
        async with app.state.submission_gate:
            available(value.operation_id)
            record = manager().operations.prepare(
                value.operation_id, {"kind": value.kind, "parent": parent, "package": package}
            )
            manager().start(record)
            return record

    @app.get("/api/v1/data/operations/{identity}", response_model=DataOperationResponse)
    def status(identity: str):
        return manager().operations.status(identity)

    @app.post("/api/v1/data/operations/{identity}/reconcile", response_model=DataOperationResponse)
    async def reconcile(identity: str):
        return await asyncio.to_thread(manager().operations.reconcile, identity)

    @app.post("/api/v1/data/operations/{identity}/cancel", response_model=DataOperationResponse)
    def cancel(identity: str):
        return manager().operations.cancel(identity)

    @app.get("/api/v1/data/operations/{identity}/download")
    async def download(identity: str):
        path, _ = await asyncio.to_thread(manager().operations.download, identity)
        return FileResponse(
            path,
            media_type="application/zip",
            filename="学习数据-" + identity + ".zip",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/v1/data/operations/{identity}/schedule")
    async def schedule(identity: str):
        async with app.state.submission_gate:
            available(identity)
            operation = manager().operations.status(identity)
            if operation["kind"] == "export" or operation["state"] not in {"ready", "completed", "publish_blocked"}:
                raise AppError("DATA_SWITCH_NOT_READY", "请先完成数据校验")
            result = app.state.profile_location.schedule(identity)
            return {
                "operation_id": identity,
                "state": "completed" if result["state"] == "completed" else "scheduled",
                "target": result["target"],
            }

    @app.post("/api/v1/data/operations/{identity}/unschedule")
    async def unschedule(identity: str):
        async with app.state.submission_gate:
            if app.state.exit_prepared:
                raise AppError("APPLICATION_EXIT_PREPARED", "已开始安全退出，请等待原操作完成")
            app.state.profile_location.cancel_schedule(identity)
            return {"operation_id": identity, "state": "unscheduled"}
