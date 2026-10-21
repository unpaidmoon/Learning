"""Project-owned draft persistence and immutable confirmed-image provenance.

Only metadata and already confirmed attachment identities enter this module.
Uncropped capture originals stay in the browser and are never accepted here.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, Request
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, Column, ForeignKey, Integer, MetaData, String, Table, insert, select
from sqlalchemy import update as sql_update

from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.storage import schema as business

metadata = MetaData()
# Existing tables are reflected only as FK targets; migrations create the two
# explicitly named new tables, never the immutable 0001 schema again.
Table("chats", metadata, Column("chat_id", String, primary_key=True))
Table("attachments", metadata, Column("attachment_id", String, primary_key=True))
drafts = Table(
    "chat_drafts",
    metadata,
    Column("chat_id", ForeignKey("chats.chat_id"), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("cleared_submission_id", String),
    Column("updated_at", String, nullable=False),
)
attachment_sources = Table(
    "attachment_sources",
    metadata,
    Column("attachment_id", ForeignKey("attachments.attachment_id"), primary_key=True),
    Column("chat_id", ForeignKey("chats.chat_id"), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", String, nullable=False),
)

Thinking = Literal["none", "low", "medium", "high", "xhigh", "max"]


class DraftContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=100_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=8)
    conditions: list[str] = Field(default_factory=list, max_length=100)
    corrections: list[str] = Field(default_factory=list, max_length=100)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    thinking: Thinking | None = None

    @model_validator(mode="after")
    def validate_content(self):
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("Duplicate attachment identities")
        if any(len(value) > 100_000 for value in self.conditions + self.corrections):
            raise ValueError("Condition or correction too long")
        return self


class DraftInput(DraftContent):
    expected_revision: int = Field(ge=0)


class DraftResponse(DraftContent):
    revision: int = Field(ge=0)


class DraftClearInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    submission_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class SourceCrop(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0, le=16_384)
    height: int = Field(gt=0, le=16_384)


class AttachmentSourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(min_length=1, max_length=160)
    origin: Literal["video", "image"] = "video"
    asset_id: str | None = Field(default=None, min_length=1, max_length=160)
    source_version: str | None = Field(default=None, min_length=1, max_length=160)
    time_ms: int | None = Field(default=None, ge=0)
    source_width: int = Field(gt=0, le=16_384)
    source_height: int = Field(gt=0, le=16_384)
    crop: SourceCrop
    edit_root_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    parent_attachment_id: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_source(self):
        if self.source_width * self.source_height > 40_000_000:
            raise ValueError("Source dimensions too large")
        if self.crop.x + self.crop.width > self.source_width:
            raise ValueError("Crop exceeds source width")
        if self.crop.y + self.crop.height > self.source_height:
            raise ValueError("Crop exceeds source height")
        if self.origin == "video":
            if self.asset_id is None or self.source_version is None or self.time_ms is None:
                raise ValueError("Video provenance requires a version and source time")
        elif any(value is not None for value in (self.asset_id, self.source_version, self.time_ms)):
            raise ValueError("External images cannot claim a video source")
        return self


class AttachmentSourceResponse(AttachmentSourceInput):
    attachment_id: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _chat(conn, chat_id: str):
    row = (
        conn.execute(select(business.chats).where(business.chats.c.chat_id == chat_id))
        .mappings()
        .first()
    )
    if row is None:
        raise AppError("CHAT_NOT_FOUND", "对话不存在", 404)
    return row


def _draft(conn, chat_id: str):
    return conn.execute(select(drafts).where(drafts.c.chat_id == chat_id)).mappings().first()


def _view(row) -> dict:
    if row is None:
        return {"revision": 0, **DraftContent().model_dump()}
    return {"revision": row["revision"], **row["payload"]}


def read_draft(repository: Repository, chat_id: str) -> dict:
    with repository.engine.connect() as conn:
        _chat(conn, chat_id)
        return _view(_draft(conn, chat_id))


def save_draft(repository: Repository, chat_id: str, value: DraftInput) -> dict:
    # Integrity-check confirmed bytes without placing images in the draft JSON.
    for attachment_id in value.attachment_ids:
        attachment = repository.resolve_attachment(attachment_id)
        if not attachment["confirmed"]:
            raise AppError("ATTACHMENT_NOT_CONFIRMED", "只能保存已确认的选区图片")
    payload = value.model_dump(exclude={"expected_revision"})
    with repository.engine.begin() as conn:
        chat = _chat(conn, chat_id)
        previous = _draft(conn, chat_id)
        revision = previous["revision"] if previous else 0
        if revision != value.expected_revision:
            raise AppError("DRAFT_REVISION_CONFLICT", "草稿已有较新版本，请先核对已保存内容")
        for attachment_id in value.attachment_ids:
            source = (
                conn.execute(
                    select(attachment_sources).where(
                        attachment_sources.c.attachment_id == attachment_id
                    )
                )
                .mappings()
                .first()
            )
            if source is not None:
                source_chat = _chat(conn, source["chat_id"])
                if source_chat["course_id"] != chat["course_id"]:
                    raise AppError("SOURCE_NOT_IN_COURSE", "截图来源不属于当前课程")
        values = {"revision": revision + 1, "payload": payload, "updated_at": _now()}
        if previous:
            conn.execute(sql_update(drafts).where(drafts.c.chat_id == chat_id).values(**values))
        else:
            conn.execute(insert(drafts).values(chat_id=chat_id, **values))
        return {"revision": revision + 1, **payload}


def clear_draft(repository: Repository, chat_id: str, value: DraftClearInput) -> dict:
    with repository.engine.begin() as conn:
        _chat(conn, chat_id)
        submission = (
            conn.execute(
                select(business.submissions).where(
                    business.submissions.c.submission_id == value.submission_id,
                    business.submissions.c.chat_id == chat_id,
                )
            )
            .mappings()
            .first()
        )
        if submission is None:
            raise AppError("SUBMISSION_NOT_FOUND", "尚未确认该次提交，请先核对执行结果", 404)
        previous = _draft(conn, chat_id)
        if previous and previous["cleared_submission_id"] == value.submission_id:
            return _view(previous)
        revision = previous["revision"] if previous else 0
        if revision != value.expected_revision:
            raise AppError("DRAFT_REVISION_CONFLICT", "新草稿已保存，未清除新的输入")
        payload = previous["payload"] if previous else DraftContent().model_dump()
        submitted = conn.execute(
            select(business.input_revisions.c.payload).where(
                business.input_revisions.c.message_id == submission["user_message_id"]
            )
        ).scalar_one()
        if any(
            payload[key] != submitted[key]
            for key in ("text", "attachment_ids", "conditions", "corrections")
        ):
            raise AppError("DRAFT_SUBMISSION_MISMATCH", "草稿与已提交内容不同，保留草稿以便核对")
        cleared = {
            **DraftContent().model_dump(),
            "model": payload["model"],
            "thinking": payload["thinking"],
        }
        values = {
            "revision": revision + 1,
            "payload": cleared,
            "cleared_submission_id": value.submission_id,
            "updated_at": _now(),
        }
        if previous:
            conn.execute(sql_update(drafts).where(drafts.c.chat_id == chat_id).values(**values))
        else:
            conn.execute(insert(drafts).values(chat_id=chat_id, **values))
        return {"revision": revision + 1, **cleared}


def read_attachment_source(repository: Repository, attachment_id: str) -> dict:
    with repository.engine.connect() as conn:
        row = (
            conn.execute(
                select(attachment_sources).where(
                    attachment_sources.c.attachment_id == attachment_id
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("ATTACHMENT_SOURCE_NOT_FOUND", "该图片没有视频来源记录", 404)
        return {"attachment_id": attachment_id, **row["payload"]}


def _validate_video_locator(conn, course_id: str, payload: dict) -> None:
    association = conn.execute(
        select(business.course_assets.c.asset_id).where(
            business.course_assets.c.course_id == course_id,
            business.course_assets.c.asset_id == payload["asset_id"],
        )
    ).first()
    if association is None:
        raise AppError("SOURCE_NOT_IN_COURSE", "截图来源不属于当前课程")
    version = conn.execute(
        select(business.source_versions.c.source_version).where(
            business.source_versions.c.asset_id == payload["asset_id"],
            business.source_versions.c.source_version == payload["source_version"],
        )
    ).first()
    if version is None:
        raise AppError("SOURCE_VERSION_NOT_FOUND", "截图来源版本不存在", 404)


def submission_source_locators(
    repository: Repository, chat_id: str, attachment_ids: Iterable[str]
) -> list[dict]:
    """Freeze confirmed-crop provenance at the existing publication boundary.

    This reads application metadata only. A missing or replaced original video
    cannot prevent asking about an intact confirmed crop, and this helper never
    resolves an original path or grants the model another image capability.
    Legacy uploads without provenance retain the existing attachment contract.
    """
    locators = []
    with repository.engine.connect() as conn:
        chat = _chat(conn, chat_id)
        for attachment_id in dict.fromkeys(attachment_ids):
            row = (
                conn.execute(
                    select(attachment_sources).where(
                        attachment_sources.c.attachment_id == attachment_id
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                continue
            source_chat = _chat(conn, row["chat_id"])
            if source_chat["course_id"] != chat["course_id"]:
                raise AppError("SOURCE_NOT_IN_COURSE", "截图来源不属于当前课程")
            payload = row["payload"]
            if payload["origin"] != "video":
                continue
            _validate_video_locator(conn, chat["course_id"], payload)
            locators.append(
                {
                    "attachment_id": attachment_id,
                    **{
                        key: payload[key]
                        for key in (
                            "asset_id",
                            "source_version",
                            "time_ms",
                            "source_width",
                            "source_height",
                            "crop",
                            "edit_root_id",
                            "parent_attachment_id",
                        )
                    },
                }
            )
    return locators


def save_attachment_source(
    repository: Repository, attachment_id: str, value: AttachmentSourceInput
) -> dict:
    attachment = repository.resolve_attachment(attachment_id)
    if not attachment["confirmed"]:
        raise AppError("ATTACHMENT_NOT_CONFIRMED", "来源只能绑定已确认的选区图片")
    # A source locator is descriptive only. It never authorizes reading or
    # decoding a video. Verify solely the already uploaded confirmed image.
    with Image.open(attachment["path"]) as image:
        if image.size != (value.crop.width, value.crop.height):
            raise AppError("CROP_IMAGE_MISMATCH", "已确认图片尺寸与选区不一致")
    payload = value.model_dump()
    with repository.engine.begin() as conn:
        chat = _chat(conn, value.chat_id)
        if value.origin == "video":
            _validate_video_locator(conn, chat["course_id"], payload)
        if value.parent_attachment_id:
            parent = (
                conn.execute(
                    select(attachment_sources).where(
                        attachment_sources.c.attachment_id == value.parent_attachment_id
                    )
                )
                .mappings()
                .first()
            )
            if parent is None:
                raise AppError("EDIT_PARENT_NOT_FOUND", "原编辑关系不存在，请保留当前选区", 404)
            protected = (
                "chat_id",
                "edit_root_id",
                "origin",
                "asset_id",
                "source_version",
                "time_ms",
                "source_width",
                "source_height",
            )
            if any(parent["payload"][key] != payload[key] for key in protected):
                raise AppError("EDIT_SOURCE_MISMATCH", "重新编辑必须沿用同一原截图")
            if value.parent_attachment_id == attachment_id:
                raise AppError("EDIT_SOURCE_MISMATCH", "编辑附件不能引用自身")
        previous = (
            conn.execute(
                select(attachment_sources).where(
                    attachment_sources.c.attachment_id == attachment_id
                )
            )
            .mappings()
            .first()
        )
        if previous:
            if previous["payload"] != payload:
                raise AppError("ATTACHMENT_SOURCE_CONFLICT", "已保存图片的来源不可覆盖")
        else:
            conn.execute(
                insert(attachment_sources).values(
                    attachment_id=attachment_id,
                    chat_id=value.chat_id,
                    payload=payload,
                    created_at=_now(),
                )
            )
        return {"attachment_id": attachment_id, **payload}


def install_draft_routes(app: FastAPI) -> None:
    @app.get("/api/v1/chats/{chat_id}/draft", response_model=DraftResponse)
    def get_chat_draft(chat_id: str, request: Request):
        return read_draft(request.app.state.repository, chat_id)

    @app.post("/api/v1/chats/{chat_id}/draft", response_model=DraftResponse)
    def put_chat_draft(chat_id: str, value: DraftInput, request: Request):
        return save_draft(request.app.state.repository, chat_id, value)

    @app.post("/api/v1/chats/{chat_id}/draft/clear", response_model=DraftResponse)
    def clear_chat_draft(chat_id: str, value: DraftClearInput, request: Request):
        return clear_draft(request.app.state.repository, chat_id, value)

    @app.get("/api/v1/attachments/{attachment_id}/source", response_model=AttachmentSourceResponse)
    def get_source(attachment_id: str, request: Request):
        return read_attachment_source(request.app.state.repository, attachment_id)

    @app.post("/api/v1/attachments/{attachment_id}/source", response_model=AttachmentSourceResponse)
    def put_source(attachment_id: str, value: AttachmentSourceInput, request: Request):
        return save_attachment_source(request.app.state.repository, attachment_id, value)
