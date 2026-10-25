"""Opt-in anonymous release checks. Never downloads or installs an application."""

import asyncio
import json
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from learning_app.data_operations import atomic_record
from learning_app.errors import AppError
from learning_app.storage.repository import _hash
from learning_app.workbench.external_http import PublicTextReader, public_url


def feed_url(value):
    normalized = public_url(value)
    parts = urlsplit(normalized)
    if parts.scheme != "https" or parts.query:
        raise AppError(
            "UPDATE_SOURCE_INVALID", "更新来源须为不含凭据或查询参数的公开HTTPS地址", 422
        )
    return normalized


class UpdateValues(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    feed_url: str | None = None
    automatic: bool = False

    @field_validator("feed_url")
    @classmethod
    def validate_url(cls, value):
        return feed_url(value) if value else None

    @model_validator(mode="after")
    def source_required(self):
        if self.automatic and not self.feed_url:
            raise ValueError("automatic checks require an explicit source")
        return self


class UpdatePreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    values: UpdateValues = Field(default_factory=UpdateValues)
    last_operation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,160}$")
    last_request_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class UpdateConfigureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")
    expected_revision: int = Field(ge=0)
    values: UpdateValues


class UpdateCheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")


class ReleaseOffer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release_id: str = Field(pattern=r"^learning-workbench-win-[0-9]{8}-r[0-9]+$")
    download_url: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(gt=0)

    @field_validator("download_url")
    @classmethod
    def validate_download(cls, value):
        return feed_url(value)


class ReleaseFeed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal[1]
    latest: ReleaseOffer


def release_rank(value):
    found = re.fullmatch(r"learning-workbench-win-([0-9]{8})-r([0-9]+)", value or "")
    return (int(found[1]), int(found[2])) if found else None


class DesktopUpdates:
    def __init__(self, data_directory, profile_anchor, project_root, *, reader=None, now=None):
        self.preferences_path = Path(data_directory) / "desktop-ui/updates.json"
        self.directory = Path(profile_anchor) / "update-operations"
        self.project_root = Path(project_root)
        self.reader = reader or PublicTextReader(
            media_types={"application/json", "text/plain"}, schemes={"https"}, allow_query=False
        )
        self.now = now or (lambda: datetime.now(UTC))
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.changed = asyncio.Event()
        self.active = {}
        self.monitor = None

    def preferences(self):
        if not self.preferences_path.is_file():
            return UpdatePreferences()
        try:
            if self.preferences_path.stat().st_size > 64 * 1024:
                raise ValueError()
            return UpdatePreferences.model_validate_json(self.preferences_path.read_bytes())
        except (OSError, ValueError):
            raise AppError(
                "UPDATE_PREFERENCES_INVALID", "更新设置无法读取，未自动联网", 503
            ) from None

    def file(self, identity):
        if (
            not isinstance(identity, str)
            or identity == "last-check"
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", identity)
        ):
            raise AppError("UPDATE_OPERATION_ID_INVALID", "更新检查操作身份无效")
        return self.directory / (identity + ".json")

    def current_release(self):
        try:
            return json.loads((self.project_root / "desktop-release.json").read_text("utf-8"))[
                "release_id"
            ]
        except (OSError, ValueError, KeyError):
            return "development"

    def status(self):
        with self.lock:
            preferences = self.preferences()
            last = self.directory / "last-check.json"
            check = json.loads(last.read_text("utf-8")) if last.is_file() else None
            if (
                check
                and check["state"] in {"prepared", "running"}
                and check["operation_id"] not in self.active
            ):
                check = {**check, "state": "unknown", "error_code": "UPDATE_CHECK_INTERRUPTED"}
            return {
                "preferences": preferences.model_dump(mode="json"),
                "current_release": self.current_release(),
                "last_check": check,
                "checking": bool(self.active),
                "automatic_interval_hours": 24,
                "installs_updates": False,
            }

    def configure(self, value):
        with self.lock:
            current = self.preferences()
            if current.last_operation_id:
                old_file = self.file(current.last_operation_id)
                if old_file.is_file():
                    old = json.loads(old_file.read_text("utf-8"))
                    if (
                        old["state"] == "prepared"
                        and old["request_hash"] == current.last_request_hash
                    ):
                        atomic_record(old_file, {**old, "state": "completed"})
            file = self.file(value.operation_id)
            digest = _hash(value.model_dump(mode="json"))
            if file.is_file():
                previous = json.loads(file.read_text("utf-8"))
                if previous.get("kind") != "configuration":
                    raise AppError(
                        "UPDATE_OPERATION_CONFLICT", "此身份已用于更新检查，不能改作设置操作"
                    )
                if previous["request_hash"] != digest:
                    raise AppError("UPDATE_OPERATION_CONFLICT", "同一更新设置身份已用于不同内容")
                if previous["state"] == "completed" or (
                    current.last_operation_id == value.operation_id
                    and current.last_request_hash == digest
                ):
                    atomic_record(file, {**previous, "state": "completed"})
                    return self.status()
                raise AppError("UPDATE_CONFIGURATION_UNKNOWN", "更新设置结果尚待核对，未重复保存")
            if current.revision != value.expected_revision:
                raise AppError("UPDATE_PREFERENCE_CONFLICT", "更新设置已有新版本，请刷新后选择")
            record = {
                "operation_id": value.operation_id,
                "kind": "configuration",
                "request_hash": digest,
                "state": "prepared",
            }
            atomic_record(file, record)
            updated = UpdatePreferences(
                revision=current.revision + 1,
                values=value.values,
                last_operation_id=value.operation_id,
                last_request_hash=digest,
            )
            atomic_record(self.preferences_path, updated.model_dump(mode="json"))
            atomic_record(file, {**record, "state": "completed"})
            return self.status()

    def due(self):
        preferences = self.preferences()
        if not preferences.values.automatic or not preferences.values.feed_url:
            return False
        last = self.directory / "last-check.json"
        if not last.exists():
            return True
        record = json.loads(last.read_text("utf-8"))
        return record[
            "feed_url"
        ] != preferences.values.feed_url or self.now() - datetime.fromisoformat(
            record["started_at"]
        ) >= timedelta(hours=24)

    async def check(self, identity, *, automatic=False):
        with self.lock:
            file = self.file(identity)
            if file.exists():
                if json.loads(file.read_text("utf-8")).get("kind") != "check":
                    raise AppError(
                        "UPDATE_OPERATION_CONFLICT", "此身份已用于设置，不能改作更新检查"
                    )
                return self.reconcile(identity)
            if self.active:
                raise AppError("UPDATE_CHECK_BUSY", "已有更新检查在进行，请核对原操作")
            preferences = self.preferences()
            if automatic and not self.due():
                return {"state": "not_due"}
            if not preferences.values.feed_url:
                raise AppError("UPDATE_SOURCE_NOT_CONFIGURED", "请先设置公开更新清单地址")
            record = {
                "operation_id": identity,
                "kind": "check",
                "state": "prepared",
                "started_at": self.now().isoformat(),
                "feed_url": preferences.values.feed_url,
                "automatic": automatic,
                "network_events": [],
            }
            atomic_record(file, record)
            atomic_record(self.directory / "last-check.json", record)
            task = asyncio.create_task(asyncio.to_thread(self._perform, record))
            self.active[identity] = task
            task.add_done_callback(lambda _: self.active.pop(identity, None))
            return record

    def _perform(self, record):
        def check():
            if self.stop_event.is_set():
                raise AppError("UPDATE_CHECK_INTERRUPTED", "更新检查已停止")

        try:
            record["state"] = "running"
            result = self.reader.read(
                record["feed_url"],
                check=check,
                observe=lambda kind, data: record["network_events"].append({"kind": kind, **data}),
            )
            feed = ReleaseFeed.model_validate_json(result["text"])
            current = release_rank(self.current_release())
            newest = release_rank(feed.latest.release_id)
            record.update(
                state="completed",
                outcome="new_version"
                if current and newest > current
                else "up_to_date"
                if current
                else "current_release_unknown",
                offer=feed.latest.model_dump(mode="json"),
                response_sha256=result["response_sha256"],
                checked_at=self.now().isoformat(),
            )
        except Exception as error:
            record.update(
                state="failed",
                error_code=getattr(error, "code", "UPDATE_FEED_INVALID"),
                checked_at=self.now().isoformat(),
            )
        with self.lock:
            atomic_record(self.file(record["operation_id"]), record)
            atomic_record(self.directory / "last-check.json", record)
        return record

    def reconcile(self, identity):
        with self.lock:
            file = self.file(identity)
            if not file.is_file():
                raise AppError("UPDATE_OPERATION_NOT_FOUND", "没有此更新操作记录", 404)
            record = json.loads(file.read_text("utf-8"))
            if record["kind"] == "configuration":
                current = self.preferences()
                if (
                    current.last_operation_id == identity
                    and current.last_request_hash == record["request_hash"]
                ):
                    record["state"] = "completed"
                    atomic_record(file, record)
            elif identity not in self.active and record["state"] in {"prepared", "running"}:
                record.update(state="unknown", error_code="UPDATE_CHECK_INTERRUPTED")
                atomic_record(file, record)
            return record

    def start(self):
        self.monitor = asyncio.create_task(self._monitor())

    async def _monitor(self):
        while not self.stop_event.is_set():
            self.changed.clear()
            try:
                if self.due() and not self.active:
                    await self.check("automatic_" + uuid4().hex, automatic=True)
            except (AppError, OSError, ValueError):
                pass
            try:
                await asyncio.wait_for(self.changed.wait(), 60)
            except TimeoutError:
                pass

    async def close(self):
        self.stop_event.set()
        self.changed.set()
        if self.monitor:
            await self.monitor
        await asyncio.gather(*tuple(self.active.values()), return_exceptions=True)
