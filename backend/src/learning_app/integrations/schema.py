"""Platform ownership and receipts only; learning tasks remain in the host queue."""

from sqlalchemy import JSON, Boolean, Column, ForeignKey, Integer, MetaData, String, Table, Text

from learning_app.storage import schema as business

metadata = MetaData()
modules = Table(
    "integration_modules",
    metadata,
    Column("platform", String, primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("epoch", Integer, nullable=False),
)
connections = Table(
    "integration_connections",
    metadata,
    Column("connection_id", String, primary_key=True),
    Column("platform", String, nullable=False),
    Column("account_id", String, nullable=False),
    Column("account_label", Text, nullable=False),
    Column("state", String, nullable=False),
    Column("epoch", Integer, nullable=False),
)
plans = Table(
    "integration_plans",
    metadata,
    Column("plan_id", String, primary_key=True),
    Column("course_id", ForeignKey(business.courses.c.course_id), nullable=False),
    Column("connection_id", ForeignKey(connections.c.connection_id), nullable=False),
    Column("module_epoch", Integer, nullable=False),
    Column("connection_epoch", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)
sources = Table(
    "integration_sources",
    metadata,
    Column("asset_id", ForeignKey(business.assets.c.asset_id), primary_key=True),
    Column("source_version", String, nullable=False),
    Column("course_id", ForeignKey(business.courses.c.course_id), nullable=False),
    Column("connection_id", ForeignKey(connections.c.connection_id), nullable=False),
    Column("object_id", String, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("local_path", Text),
    Column("local_sha256", String),
    Column("audio_path", Text),
    Column("audio_sha256", String),
)
operations = Table(
    "integration_operations",
    metadata,
    Column("operation_id", String, primary_key=True),
    Column("course_id", String, nullable=False),
    Column("connection_id", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("request_hash", String, nullable=False),
    Column("state", String, nullable=False),
    Column("receipt", JSON),
)
requests = Table(
    "integration_requests",
    metadata,
    Column("request_id", String, primary_key=True),
    Column("connection_id", String, nullable=False),
    Column("purpose", String, nullable=False),
    Column("state", String, nullable=False),
    Column("status_code", Integer),
)
