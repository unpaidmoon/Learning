"""Additive media state. The P0 schema remains immutable."""

from sqlalchemy import JSON, Column, ForeignKey, Integer, MetaData, String, Table

from learning_app.storage import schema as business

metadata = MetaData()

media_assets = Table(
    "media_assets",
    metadata,
    Column("asset_id", ForeignKey(business.assets.c.asset_id), primary_key=True),
    Column("source_version", String, nullable=False),
    Column("media_type", String, nullable=False),
    Column("file_signature", JSON, nullable=False),
)

media_positions = Table(
    "media_positions",
    metadata,
    Column("asset_id", ForeignKey(business.assets.c.asset_id), primary_key=True),
    Column("source_version", String, primary_key=True),
    Column("position_ms", Integer, nullable=False, default=0),
    Column("revision", Integer, nullable=False, default=0),
    Column("last_opened_at", String),
)
