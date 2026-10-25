"""Reading actions are not answers; each route keeps its own immutable journal."""

from sqlalchemy import JSON, Column, ForeignKey, Integer, String, Table, UniqueConstraint

from . import schema as s

path_states = Table(
    "learning_path_states",
    s.metadata,
    Column("artifact_id", ForeignKey(s.artifacts.c.artifact_id), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("progress", JSON, nullable=False),
    Column("interactions", JSON, nullable=False),
    Column("self_assessments", JSON, nullable=False),
    Column("updated_at", String, nullable=False),
)
path_events = Table(
    "learning_path_events",
    s.metadata,
    Column("event_id", String, primary_key=True),
    Column("artifact_id", ForeignKey(s.artifacts.c.artifact_id), nullable=False),
    Column("content_revision_id", ForeignKey(s.revisions.c.revision_id), nullable=False),
    Column("block_id", String, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("action", String, nullable=False),
    Column("detail", JSON, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("artifact_id", "revision"),
)
