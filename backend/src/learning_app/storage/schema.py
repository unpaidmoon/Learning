"""Immutable schema for migration 0001. Add later changes in new revisions."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

courses = Table(
    "courses",
    metadata,
    Column("course_id", String, primary_key=True),
    Column("title", Text, nullable=False),
    Column("created_at", String, nullable=False),
)
assets = Table(
    "assets",
    metadata,
    Column("asset_id", String, primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("created_at", String, nullable=False),
)
course_assets = Table(
    "course_assets",
    metadata,
    Column("course_id", ForeignKey("courses.course_id"), primary_key=True),
    Column("asset_id", ForeignKey("assets.asset_id"), primary_key=True),
)
source_versions = Table(
    "source_versions",
    metadata,
    Column("asset_id", ForeignKey("assets.asset_id"), primary_key=True),
    Column("source_version", String, primary_key=True),
    Column("content_hash", String, nullable=False),
    Column("byte_length", Integer, nullable=False),
    Column("local_path", Text, nullable=False),
    Column("created_at", String, nullable=False),
)
chats = Table(
    "chats",
    metadata,
    Column("chat_id", String, primary_key=True),
    Column("course_id", ForeignKey("courses.course_id"), nullable=False),
    Column("history_revision", Integer, nullable=False, default=0),
    Column("current_question_id", String),
    Column("created_at", String, nullable=False),
)
attachments = Table(
    "attachments",
    metadata,
    Column("attachment_id", String, primary_key=True),
    Column("content_hash", String, nullable=False),
    Column("media_type", String, nullable=False),
    Column("byte_length", Integer, nullable=False),
    Column("confirmed", Boolean, nullable=False),
    Column("state", String, nullable=False),
    Column("created_at", String, nullable=False),
)
messages = Table(
    "messages",
    metadata,
    Column("sequence", Integer, primary_key=True, autoincrement=True),
    Column("message_id", String, nullable=False, unique=True),
    Column("chat_id", ForeignKey("chats.chat_id"), nullable=False),
    Column("role", String, nullable=False),
    Column("text", Text, nullable=False),
    Column("revision_id", String, nullable=False),
    Column("question_root_id", String, nullable=False),
    Column("in_reply_to", String),
    Column("parent_ids", JSON, nullable=False, default=list),
    Column("generation_id", String, unique=True),
    Column("status", String, nullable=False),
    Column("origin", String, nullable=False),
    Column("created_at", String, nullable=False),
)
input_revisions = Table(
    "input_revisions",
    metadata,
    Column("revision_id", String, primary_key=True),
    Column("message_id", ForeignKey("messages.message_id"), nullable=False),
    Column("supersedes", String),
    Column("content_hash", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", String, nullable=False),
)
message_attachments = Table(
    "message_attachments",
    metadata,
    Column("revision_id", ForeignKey("input_revisions.revision_id"), primary_key=True),
    Column("attachment_id", ForeignKey("attachments.attachment_id"), primary_key=True),
)
context_snapshots = Table(
    "context_snapshots",
    metadata,
    Column("snapshot_id", String, primary_key=True),
    Column("chat_id", ForeignKey("chats.chat_id"), nullable=False),
    Column("context_hash", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", String, nullable=False),
)
submissions = Table(
    "submissions",
    metadata,
    Column("submission_id", String, primary_key=True),
    Column("chat_id", ForeignKey("chats.chat_id"), nullable=False),
    Column("user_message_id", ForeignKey("messages.message_id"), nullable=False, unique=True),
    Column("payload_hash", String, nullable=False),
    Column("state", String, nullable=False),
    Column("created_at", String, nullable=False),
)
generations = Table(
    "generations",
    metadata,
    Column("generation_id", String, primary_key=True),
    Column("submission_id", ForeignKey("submissions.submission_id"), nullable=False, unique=True),
    Column("assistant_message_id", ForeignKey("messages.message_id"), nullable=False, unique=True),
    Column("snapshot_id", ForeignKey("context_snapshots.snapshot_id"), nullable=False),
    Column("state", String, nullable=False),
    Column("binding_id", String),
    Column("epoch", Integer),
    Column("harness_message_id", String),
    Column("request_id", String),
    Column("host_instance_id", String),
    Column("adapter_metadata", JSON, nullable=False, default=dict),
    Column("body", Text, nullable=False, default=""),
    Column("reasoning", Text, nullable=False, default=""),
    Column("cancel_requested", Boolean, nullable=False, default=False),
    Column("unknown_reason", String),
    Column("model", String, nullable=False),
    Column("thinking", String, nullable=False),
    Column("max_output_tokens", Integer, nullable=False),
    Column("timeout_seconds", Float, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)
execution_bindings = Table(
    "execution_bindings",
    metadata,
    Column("binding_id", String, primary_key=True),
    Column("chat_id", ForeignKey("chats.chat_id"), nullable=False),
    Column("session_id", String, nullable=False, unique=True),
    Column("epoch", Integer, nullable=False),
    Column("state", String, nullable=False),
    Column("active_generation_id", String),
    Column("last_event_watermark", Integer, nullable=False, default=0),
    Column("adapter_metadata", JSON, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("chat_id", "epoch"),
)
execution_events = Table(
    "execution_events",
    metadata,
    Column("binding_id", ForeignKey("execution_bindings.binding_id"), primary_key=True),
    Column("event_id", String, primary_key=True),
    Column("seq", Integer, nullable=False),
    Column("generation_id", ForeignKey("generations.generation_id"), nullable=False),
    Column("epoch", Integer, nullable=False),
    Column("kind", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", String, nullable=False),
)
rejected_events = Table(
    "rejected_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("generation_id", String, nullable=False),
    Column("binding_id", String, nullable=False),
    Column("epoch", Integer, nullable=False),
    Column("event_id", String, nullable=False),
    Column("reason", String, nullable=False),
    Column("created_at", String, nullable=False),
)
usage_records = Table(
    "usage_records",
    metadata,
    Column("generation_id", ForeignKey("generations.generation_id"), primary_key=True),
    Column("settlement_id", String, primary_key=True),
    Column("source", String, nullable=False),
    Column("raw_usage", JSON),
    Column("field_presence", JSON, nullable=False),
    Column("normalized", JSON, nullable=False),
    Column("created_at", String, nullable=False),
)
outbound_attempts = Table(
    "outbound_attempts",
    metadata,
    Column("attempt_id", String, primary_key=True),
    Column("generation_id", ForeignKey("generations.generation_id"), nullable=False),
    Column("kind", String, nullable=False),
    Column("request_id", String),
    Column("model", String, nullable=False),
    Column("thinking", String, nullable=False),
    Column("actual_outbound", Boolean, nullable=False, default=True),
    Column("created_at", String, nullable=False),
)
