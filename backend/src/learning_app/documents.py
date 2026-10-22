"""Whole, explicitly selected chat documents; no course-file import or model call.

Original bytes and prepared page identities are immutable. Drafts and submissions
continue to use attachment_ids; only the dispatch seam expands document parts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import tempfile
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import JSON, Column, ForeignKey, MetaData, String, Table, Text, insert, select

from learning_app.document_processing import (
    MAX_PDF_BYTES,
    prepare_document,
    validate_original_bytes,
)
from learning_app.errors import AppError
from learning_app.storage import Repository

metadata = MetaData()
Table("attachments", metadata, Column("attachment_id", String, primary_key=True))
Table("chats", metadata, Column("chat_id", String, primary_key=True))
documents = Table(
    "document_attachments",
    metadata,
    Column("attachment_id", ForeignKey("attachments.attachment_id"), primary_key=True),
    Column("chat_id", ForeignKey("chats.chat_id"), nullable=False),
    Column("filename", Text, nullable=False),
    Column("kind", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", String, nullable=False),
)
MAX_DISPATCH_IMAGE_BYTES = 14 * 1024 * 1024
MAX_DISPATCH_IMAGES = 64
MAX_DISPATCH_TEXT_CHARACTERS = 300_000
_UPLOAD_GATE = threading.Lock()
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
_EXTENSIONS = {
    "application/pdf": {".pdf"},
    "text/plain": {".txt"},
    "text/markdown": {".md", ".markdown"},
}


class DocumentAttachmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: str = Field(min_length=1, max_length=160)
    filename: str = Field(min_length=1, max_length=240)
    media_type: Literal["application/pdf", "text/plain", "text/markdown"]
    data_base64: str = Field(min_length=1, max_length=((MAX_PDF_BYTES + 2) // 3) * 4)
    confirmed: Literal[True]
    upload_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")

    @model_validator(mode="after")
    def check_name(self):
        if (
            any(character in self.filename for character in "/\\\x00\r\n")
            or any(ord(character) < 32 for character in self.filename)
            or Path(self.filename).suffix.lower() not in _EXTENSIONS[self.media_type]
        ):
            raise ValueError("Document filename or extension invalid")
        return self


class AttachmentMetadataResponse(BaseModel):
    attachment_id: str
    sha256: str
    media_type: str
    size_bytes: int
    confirmed: bool
    kind: Literal["image", "pdf", "text", "markdown"]
    filename: str
    page_count: int | None = None
    character_count: int | None = None
    download_url: str


def _document(repository: Repository, attachment_id: str) -> dict | None:
    with repository.engine.connect() as conn:
        row = (
            conn.execute(select(documents).where(documents.c.attachment_id == attachment_id))
            .mappings()
            .first()
        )
        return dict(row) if row else None


def attachment_metadata(repository: Repository, attachment_id: str) -> dict:
    attachment = repository.resolve_attachment(attachment_id)
    document = _document(repository, attachment_id)
    if document is None and attachment["media_type"] not in _IMAGE_TYPES:
        raise AppError("DOCUMENT_NOT_READY", "文档处理记录不完整，原件仍保留", 409)
    payload = document["payload"] if document else {}
    return {
        "attachment_id": attachment_id,
        "sha256": attachment["content_hash"],
        "media_type": attachment["media_type"],
        "size_bytes": attachment["byte_length"],
        "confirmed": attachment["confirmed"],
        "kind": document["kind"] if document else "image",
        "filename": document["filename"] if document else "图片",
        "page_count": payload.get("page_count"),
        "character_count": payload.get("character_count"),
        "download_url": f"/api/v1/document-attachments/{attachment_id}/content"
        if document
        else f"/api/v1/attachments/{attachment_id}",
    }


def upload_document(repository: Repository, value: DocumentAttachmentInput) -> dict:
    repository.get_chat(value.chat_id)
    try:
        data = base64.b64decode(value.data_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AppError("DOCUMENT_INVALID", "文件编码无效", 422) from exc
    validate_original_bytes(data, value.media_type)
    aid = "document_" + (value.upload_id or uuid4().hex)
    digest = hashlib.sha256(data).hexdigest()
    # The same explicit upload identity can be reconciled/retried safely. It
    # cannot be repurposed to move another chat's document into this one.
    with _UPLOAD_GATE:
        previous = _document(repository, aid)
        if previous:
            original = repository.resolve_attachment(aid)
            if (
                previous["chat_id"] != value.chat_id
                or previous["filename"] != value.filename
                or original["content_hash"] != digest
                or original["media_type"] != value.media_type
            ):
                raise AppError("DOCUMENT_UPLOAD_CONFLICT", "同一上传身份的文件发生变化", 409)
            return attachment_metadata(repository, aid)
        # Preparation never obtains a model credential, sends a request, or
        # makes an attachment visible in a draft before every page succeeds.
        with tempfile.TemporaryDirectory(prefix="document-", dir=repository.staging) as work:
            prepared = prepare_document(data, value.media_type, value.filename, Path(work))
            pages = []
            for page in prepared["pages"]:
                image = Path(page["path"]).read_bytes()
                page_aid = f"{aid}_page_{page['page']}"
                stored = repository.stage_attachment(
                    image, "image/jpeg", confirmed=True, attachment_id=page_aid
                )
                pages.append(
                    {
                        "page": page["page"],
                        "attachment_id": page_aid,
                        "sha256": stored["content_hash"],
                        "width": page["width"],
                        "height": page["height"],
                    }
                )
            original = repository.stage_attachment(
                data, value.media_type, confirmed=True, attachment_id=aid
            )
            payload = {
                "preparation_version": "document/1-pdfium-5.14.0",
                "original_sha256": original["content_hash"],
                "pages": pages,
                "page_count": prepared["page_count"],
                "character_count": prepared["character_count"],
                "text": prepared["text"],
                "whole_document": True,
            }
            with repository.engine.begin() as conn:
                conn.execute(
                    insert(documents).values(
                        attachment_id=aid,
                        chat_id=value.chat_id,
                        filename=value.filename,
                        kind=prepared["kind"],
                        payload=payload,
                        created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
                    )
                )
        return attachment_metadata(repository, aid)


def validate_document_scope(
    repository: Repository, chat_id: str, attachment_ids: Iterable[str]
) -> None:
    for aid in dict.fromkeys(attachment_ids):
        attachment = repository.resolve_attachment(aid)
        document = _document(repository, aid)
        if document is None:
            if attachment["media_type"] not in _IMAGE_TYPES:
                raise AppError("DOCUMENT_NOT_READY", "文档处理记录不完整，未提交问题", 409)
            continue
        if document["chat_id"] != chat_id:
            raise AppError("DOCUMENT_NOT_IN_CHAT", "文件附件不属于当前对话", 422)


def requires_images(repository: Repository, attachment_ids: Iterable[str]) -> bool:
    for aid in dict.fromkeys(attachment_ids):
        item = repository.resolve_attachment(aid)
        if item["media_type"] in _IMAGE_TYPES or item["media_type"] == "application/pdf":
            return True
    return False


def expand_document_parts(
    repository: Repository, chat_id: str, originals: list[dict]
) -> tuple[list[dict], str]:
    """Use immutable preparation records; do not read arbitrary filesystem paths."""
    validate_document_scope(repository, chat_id, [item["attachment_id"] for item in originals])
    images, texts = [], []
    for original in originals:
        document = _document(repository, original["attachment_id"])
        if document is None:
            images.append(original)
            continue
        payload = document["payload"]
        if payload["original_sha256"] != original["content_hash"] or not payload.get(
            "whole_document"
        ):
            raise AppError("DOCUMENT_INTEGRITY_ERROR", "文档处理内容与原件不一致", 409)
        header = json.dumps(
            {"filename": document["filename"], "attachment_id": original["attachment_id"]},
            ensure_ascii=False,
        )
        if document["kind"] == "pdf":
            if len(payload["pages"]) != payload["page_count"]:
                raise AppError("DOCUMENT_INTEGRITY_ERROR", "PDF页面不完整，未发送部分页面", 409)
            texts.append(
                f"用户明确提交的整份PDF {header}：本次图片序号{len(images) + 1}至{len(images) + payload['page_count']}按该PDF页码1至{payload['page_count']}顺序排列；全部页面均包含，不是视频帧或自动补图。"
            )
            for number, page in enumerate(payload["pages"], 1):
                if page["page"] != number:
                    raise AppError("DOCUMENT_INTEGRITY_ERROR", "PDF页码记录无效", 409)
                image = repository.resolve_attachment(page["attachment_id"])
                if image["content_hash"] != page["sha256"]:
                    raise AppError("DOCUMENT_INTEGRITY_ERROR", "PDF页面内容已变化", 409)
                images.append(image)
        else:
            text = original["path"].read_bytes().decode("utf-8-sig", errors="strict")
            if text != payload["text"]:
                raise AppError("DOCUMENT_INTEGRITY_ERROR", "文本附件内容已变化", 409)
            texts.append(
                f"用户明确提交的完整文本附件 {header}：\n<user_document>\n{text}\n</user_document>"
            )
    if (
        len(images) > MAX_DISPATCH_IMAGES
        or sum(item["byte_length"] for item in images) > MAX_DISPATCH_IMAGE_BYTES
        or sum(len(text) for text in texts) > MAX_DISPATCH_TEXT_CHARACTERS
    ):
        raise AppError("DOCUMENT_CONTEXT_LIMIT", "确认附件总量超过单次输入上限，未截断或发送", 422)
    return images, "\n\n".join(texts)


def document_content(repository: Repository, attachment_id: str) -> FileResponse:
    document = _document(repository, attachment_id)
    if document is None:
        raise AppError("DOCUMENT_NOT_FOUND", "文件附件不存在", 404)
    original = repository.resolve_attachment(attachment_id)
    return FileResponse(
        original["path"],
        media_type=original["media_type"],
        filename=document["filename"],
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


def install_document_routes(app: FastAPI) -> None:
    @app.post("/api/v1/document-attachments", response_model=AttachmentMetadataResponse)
    def upload(value: DocumentAttachmentInput, request: Request):
        return upload_document(request.app.state.repository, value)

    @app.get(
        "/api/v1/attachments/{attachment_id}/metadata", response_model=AttachmentMetadataResponse
    )
    def inspect(attachment_id: str, request: Request):
        return attachment_metadata(request.app.state.repository, attachment_id)

    @app.get("/api/v1/document-attachments/{attachment_id}/content")
    def content(attachment_id: str, request: Request):
        return document_content(request.app.state.repository, attachment_id)
