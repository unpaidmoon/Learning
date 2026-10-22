"""Additive course roots and recoverable user-initiated file operations."""

from sqlalchemy import JSON, Boolean, Column, ForeignKey, MetaData, String, Table, Text

from learning_app.storage import schema as business

metadata = MetaData()

course_roots = Table(
    "course_roots",
    metadata,
    Column("course_id", ForeignKey(business.courses.c.course_id), primary_key=True),
    Column("local_path", Text, nullable=False, unique=True),
    Column("state", String, nullable=False),
    Column("created_at", String, nullable=False),
)

course_file_entries = Table(
    "course_file_entries",
    metadata,
    Column("course_id", ForeignKey(business.courses.c.course_id), primary_key=True),
    Column("relative_path", Text, primary_key=True),
    Column("asset_id", ForeignKey(business.assets.c.asset_id), nullable=False),
    Column("source_version", String, nullable=False),
)

course_file_operations = Table(
    "course_file_operations",
    metadata,
    Column("operation_id", String, primary_key=True),
    Column("course_id", ForeignKey(business.courses.c.course_id), nullable=False),
    Column("kind", String, nullable=False),
    Column("state", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("result", JSON),
    Column("error_code", String),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)


course_visibility = Table(
    "course_visibility",
    metadata,
    Column("course_id", ForeignKey(business.courses.c.course_id), primary_key=True),
    Column("hidden", Boolean, nullable=False, default=False),
)
