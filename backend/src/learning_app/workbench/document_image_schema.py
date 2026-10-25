"""Read-only document crops and per-execution image encodings."""

from sqlalchemy import JSON, Column, ForeignKey, String, Table

from .schema import metadata

image_reads = Table(
    "learning_document_image_reads",
    metadata,
    Column("effect_id", String, primary_key=True),
    Column("run_id", ForeignKey("learning_native_scopes.run_id"), nullable=False),
    Column("handle_id", String, nullable=False),
    Column("parent_effect_id", String, nullable=False),
    Column("native_call", JSON, nullable=False),
    Column("snapshot_id", ForeignKey("learning_snapshots.snapshot_id"), nullable=False),
    Column("state", String, nullable=False),
    Column("result", JSON),
    Column("result_hash", String),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)
image_allowances = Table(
    "learning_document_image_allowances",
    metadata,
    Column("run_id", ForeignKey("learning_native_scopes.run_id"), primary_key=True),
    Column("effect_id", ForeignKey(image_reads.c.effect_id), primary_key=True),
    Column("proof", JSON, nullable=False),
    Column("proof_hash", String, nullable=False),
    Column("created_at", String, nullable=False),
)
