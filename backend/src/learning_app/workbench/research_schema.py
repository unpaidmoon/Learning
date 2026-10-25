"""External read facts are separate from model attempts and original source leaves."""

from sqlalchemy import JSON, Column, ForeignKey, Integer, String, Table

from . import schema as s

external_reads = Table(
    "learning_research_external_reads",
    s.metadata,
    Column("effect_id", String, primary_key=True),
    Column("task_id", ForeignKey(s.tasks.c.task_id), nullable=False, index=True),
    Column("run_id", ForeignKey(s.task_runs.c.run_id), nullable=False, index=True),
    Column("source_id", ForeignKey(s.task_sources.c.source_id), nullable=False),
    Column("source_epoch", Integer, nullable=False),
    Column("native_call", JSON, nullable=False),
    Column("request", JSON, nullable=False),
    Column("state", String, nullable=False),
    Column("network_events", JSON, nullable=False),
    Column("result", JSON),
    Column("result_hash", String),
    Column("error_code", String),
    Column("created_at", String, nullable=False),
    Column("finished_at", String),
)
