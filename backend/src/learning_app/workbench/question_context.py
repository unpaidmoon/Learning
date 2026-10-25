"""Freeze only question-bound subtitle anchors; never read a video image."""

from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select

from learning_app.dto import QuestionVideoContext
from learning_app.errors import AppError
from learning_app.storage import schema as business
from learning_app.storage.repository import _hash

from .audio_sources import AUDIO_CONTAINERS, AudioSources


def question_video_locator(repository, chat_id, value):
    try:
        frozen = QuestionVideoContext.model_validate(value)
    except ValidationError as error:
        raise AppError("QUESTION_VIDEO_CONTEXT_INVALID", "问题时间来源格式不符", 422) from error
    with repository.engine.connect() as conn:
        course = conn.execute(
            select(business.chats.c.course_id).where(business.chats.c.chat_id == chat_id)
        ).scalar_one_or_none()
    if course is None:
        raise AppError("QUESTION_VIDEO_CONTEXT_DENIED", "问题时间没有对应课程对话", 403)
    # This verifies recorded association/version only. Deleted originals cannot
    # prevent a question about an intact confirmed crop or saved transcript.
    AudioSources(repository).associated(frozen.source_course_id or course, frozen.asset_id, frozen.source_version)
    return {"origin": "video_question", **frozen.model_dump(mode="json")}


def freeze_subtitle_anchors(conn, course, context, current):
    def candidates(message, *, images_only=False):
        result = []
        for locator in message.get("source_locators", []):
            is_image = locator.get("attachment_id") in message.get("attachment_ids", [])
            if (
                is_image
                and conn.execute(
                    select(business.attachments.c.confirmed).where(
                        business.attachments.c.attachment_id == locator["attachment_id"]
                    )
                ).scalar_one_or_none()
                is not True
            ):
                continue
            if images_only and not is_image:
                continue
            if not is_image and locator.get("origin") != "video_question":
                continue
            try:
                value = QuestionVideoContext.model_validate(
                    {key: locator.get(key) for key in QuestionVideoContext.model_fields}
                )
            except ValidationError:
                continue
            source = (
                conn.execute(
                    select(business.source_versions)
                    .join(
                        business.course_assets,
                        business.course_assets.c.asset_id == business.source_versions.c.asset_id,
                    )
                    .where(
                        business.course_assets.c.course_id == (value.source_course_id or course),
                        business.source_versions.c.asset_id == value.asset_id,
                        business.source_versions.c.source_version == value.source_version,
                    )
                )
                .mappings()
                .first()
            )
            if source is None or Path(source["local_path"]).suffix.lower() not in AUDIO_CONTAINERS:
                continue
            payload = {
                "message_id": message["message_id"],
                **value.model_dump(mode="json"),
                "source_digest": source["content_hash"],
            }
            result.append({"anchor_id": "subtitle_anchor_" + _hash(payload), **payload})
        return result

    # Current confirmed picture provenance takes precedence over an active
    # playhead. A text-only question may explicitly submit its current time.
    selected = candidates(current, images_only=True) or candidates(current)
    if not selected:
        for message in reversed(context["messages"]):
            if message.get("role") != "user" or message.get("question_root_id") != current.get(
                "question_root_id"
            ):
                continue
            selected = candidates(message, images_only=True)
            if selected:
                break
    unique = list({item["anchor_id"]: item for item in selected}.values())
    if len(unique) > 8:
        raise AppError(
            "QUESTION_SUBTITLE_ANCHOR_LIMIT", "本次问题的字幕时间来源过多，未静默截断", 422
        )
    return unique
