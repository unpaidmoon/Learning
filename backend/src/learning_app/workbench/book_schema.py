"""Immutable layout per artifact revision; generation still uses task/unit/run tables."""

from sqlalchemy import JSON, Column, ForeignKey, Integer, String, Table

from . import schema as s

book_layouts = Table(
    "learning_book_revision_layouts",
    s.metadata,
    Column("revision_id", ForeignKey(s.revisions.c.revision_id), primary_key=True),
    Column("artifact_id", ForeignKey(s.artifacts.c.artifact_id), nullable=False, index=True),
    Column("schema_version", Integer, nullable=False),
    Column("layout", JSON, nullable=False),
    Column("layout_hash", String, nullable=False),
    Column("created_at", String, nullable=False),
)
