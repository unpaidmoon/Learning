"""Frozen submitted crops, never a capability to read a video or screen."""

import hashlib

from sqlalchemy import select

from learning_app.errors import AppError
from learning_app.storage import schema as business

from .models import SubmittedImageRef
from .sources import SourceStore


class SubmittedImages:
    @staticmethod
    def _attachment(conn, repository, course_id, context_id, context_hash, message, identity):
        context = (
            conn.execute(
                select(business.context_snapshots)
                .join(business.chats)
                .where(
                    business.context_snapshots.c.snapshot_id == context_id,
                    business.context_snapshots.c.context_hash == context_hash,
                    business.chats.c.course_id == course_id,
                )
            )
            .mappings()
            .first()
        )
        if context is None:
            raise AppError("SUBMITTED_IMAGE_CONTEXT_DENIED", "图片不属于此课程的冻结提交", 403)
        actual = next(
            (
                item
                for item in context["payload"]["messages"]
                if item["message_id"] == message["message_id"]
                and item["revision_id"] == message["revision_id"]
            ),
            None,
        )
        submitted = set(context["payload"].get("required_attachment_ids", []))
        if (
            actual is None
            or identity not in actual.get("attachment_ids", [])
            or identity not in submitted
        ):
            raise AppError("SUBMITTED_IMAGE_NOT_INCLUDED", "图片未包含在此不可变提交中", 403)
        if not conn.execute(
            select(business.message_attachments).where(
                business.message_attachments.c.revision_id == message["revision_id"],
                business.message_attachments.c.attachment_id == identity,
            )
        ).first():
            raise AppError("SUBMITTED_IMAGE_NOT_INCLUDED", "图片与原始消息修订不符", 403)
        attachment = repository._attachment(conn, identity)
        if not attachment["confirmed"] or attachment["media_type"] not in {
            "image/png",
            "image/jpeg",
            "image/webp",
        }:
            raise AppError("SUBMITTED_IMAGE_NOT_CONFIRMED", "该附件不是已确认提交的图片", 403)
        return attachment

    @classmethod
    def freeze(cls, conn, repository, course_id, context_id, context_hash, context):
        snapshots = []
        submitted = set(context.get("required_attachment_ids", []))
        for message in context["messages"]:
            for identity in message.get("attachment_ids", []):
                if identity not in submitted:
                    continue
                # Entire document attachments use their separate document grant.
                row = (
                    conn.execute(
                        select(business.attachments).where(
                            business.attachments.c.attachment_id == identity
                        )
                    )
                    .mappings()
                    .one()
                )
                if not row["media_type"].startswith("image/"):
                    continue
                attachment = cls._attachment(
                    conn, repository, course_id, context_id, context_hash, message, identity
                )
                raw = attachment["path"].read_bytes()
                digest = attachment["content_hash"]
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise AppError("SUBMITTED_IMAGE_CORRUPT", "已提交图片字节校验失败")
                # This store has opaque digest names. Keep the exact confirmed
                # bytes and their real media type; do not re-render the source.
                target = SourceStore(repository).image_path(digest)
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with target.open("xb") as stream:
                        stream.write(raw)
                except FileExistsError:
                    if target.read_bytes() != raw:
                        raise AppError(
                            "SUBMITTED_IMAGE_CORRUPT", "保存的提交图片与原件不符"
                        ) from None
                ref = SubmittedImageRef(
                    context_snapshot_id=context_id,
                    context_hash=context_hash,
                    message_id=message["message_id"],
                    revision_id=message["revision_id"],
                    attachment_id=identity,
                    image_sha256=digest,
                    media_type=attachment["media_type"],
                )
                snapshots.append(
                    SourceStore.save_in_transaction(conn, course_id, ref, "", "original")
                )
        return snapshots

    @classmethod
    def validate(cls, conn, repository, snapshot):
        ref = snapshot.reference
        attachment = cls._attachment(
            conn,
            repository,
            snapshot.course_id,
            ref.context_snapshot_id,
            ref.context_hash,
            ref.model_dump(),
            ref.attachment_id,
        )
        path = SourceStore(repository).image_path(ref.image_sha256)
        if (
            attachment["content_hash"] != ref.image_sha256
            or attachment["media_type"] != ref.media_type
            or snapshot.text
            or snapshot.content_role != "original"
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != ref.image_sha256
        ):
            raise AppError("SUBMITTED_IMAGE_CORRUPT", "冻结图片与实际确认提交不符")
