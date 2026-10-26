"""Local workspace pin preferences; no conversation or filesystem authority."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator

from learning_app.errors import AppError


class WorkspacePins(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned_course_ids: list[str] = []

    @field_validator("pinned_course_ids")
    @classmethod
    def valid_ids(cls, values):
        if len(values) > 10000 or len(set(values)) != len(values) or any(
            not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value) for value in values
        ):
            raise ValueError("invalid workspace pin identities")
        return values


class WorkspacePinInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pinned: StrictBool


def read_workspace_pins(path: Path) -> WorkspacePins:
    if not path.exists():
        return WorkspacePins()
    try:
        if path.stat().st_size > 2_000_000:
            raise ValueError("oversized workspace preferences")
        return WorkspacePins.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise AppError("WORKSPACE_PINS_INVALID", "工作区置顶记录无法读取，请检查本地数据", 503) from exc


def install_workspace_navigation_routes(app: FastAPI) -> None:
    def path():
        return app.state.repository.root / "desktop-ui" / "workspaces.json"

    @app.get("/api/v1/courses/navigation", response_model=WorkspacePins)
    def navigation():
        with app.state.repository._lock:
            return read_workspace_pins(path())

    @app.post("/api/v1/courses/{course_id}/pin", response_model=WorkspacePins)
    def pin_workspace(course_id: str, value: WorkspacePinInput):
        repository = app.state.repository
        with repository._lock:
            if not any(row["course_id"] == course_id for row in repository.list_courses()):
                raise AppError("WORKSPACE_UNAVAILABLE", "工作区不存在或已移除", 404)
            current = read_workspace_pins(path())
            ids = current.pinned_course_ids
            if (course_id in ids) == value.pinned:
                return current
            updated = WorkspacePins(pinned_course_ids=(
                [*ids, course_id] if value.pinned else [identity for identity in ids if identity != course_id]
            ))
            target = path()
            temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(updated.model_dump(), stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            except OSError as exc:
                raise AppError("WORKSPACE_PINS_SAVE_UNKNOWN", "置顶保存结果尚未确认，请刷新工作区核对", 503) from exc
            finally:
                temporary.unlink(missing_ok=True)
            return updated
