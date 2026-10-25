"""Resumable document indexing. Snapshots remain independent user provenance."""

from sqlalchemy import JSON, Boolean, Column, ForeignKey, Integer, String, Table, Text

from learning_app.storage import schema as business

from .schema import metadata

profiles = Table(
    "learning_corpus_profiles",
    metadata,
    Column("course_id", ForeignKey(business.courses.c.course_id), primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("exclusions", JSON, nullable=False),
    Column("updated_at", String, nullable=False),
)
scans = Table(
    "learning_corpus_scans",
    metadata,
    Column("course_id", ForeignKey(business.courses.c.course_id), primary_key=True),
    Column("scan_id", String, nullable=False),
    Column("scope_hash", String, nullable=False),
    Column("pending", JSON, nullable=False),
    Column("state", String, nullable=False),
    Column("error_code", String),
    Column("updated_at", String, nullable=False),
)
documents = Table(
    "learning_corpus_documents",
    metadata,
    Column("document_id", String, primary_key=True),
    Column("course_id", ForeignKey(business.courses.c.course_id), nullable=False),
    Column("relative_path", Text, nullable=False),
    Column("asset_id", String, nullable=False),
    Column("source_version", String, nullable=False),
    Column("source_digest", String, nullable=False),
    Column("seen_scan", String, nullable=False),
    Column("current", Boolean, nullable=False),
    Column("cursor", Integer, nullable=False),
    Column("total", Integer),
    Column("state", String, nullable=False),
    Column("error_code", String),
    Column("updated_at", String, nullable=False),
)
units = Table(
    "learning_corpus_units",
    metadata,
    Column("document_id", ForeignKey(documents.c.document_id), primary_key=True),
    Column("unit_index", Integer, primary_key=True),
    Column("text_revision", String, nullable=False),
    Column("state", String, nullable=False),
)
chunks = Table(
    "learning_corpus_chunks",
    metadata,
    Column("chunk_id", String, primary_key=True),
    Column("document_id", ForeignKey(documents.c.document_id), nullable=False),
    Column("unit_index", Integer, nullable=False),
    Column("snapshot_id", ForeignKey("learning_snapshots.snapshot_id"), nullable=False),
    Column("active", Boolean, nullable=False),
)
query_receipts = Table(
    "learning_native_query_receipts",
    metadata,
    Column("effect_id", String, primary_key=True),
    Column("run_id", ForeignKey("learning_native_scopes.run_id"), nullable=False),
    Column("native_call", JSON, nullable=False),
    Column("result", JSON, nullable=False),
    Column("result_hash", String, nullable=False),
    Column("created_at", String, nullable=False),
)
