"""One project-owned set of immutable essay rounds and evaluation history."""

from sqlalchemy import JSON, Column, ForeignKey, Integer, String, Table, Text, UniqueConstraint

from learning_app.storage import schema as business

from . import schema as s

essay_rounds = Table(
    "learning_essay_rounds",
    s.metadata,
    Column("round_id", String, primary_key=True),
    Column("course_id", ForeignKey(business.courses.c.course_id), nullable=False),
    Column("artifact_id", ForeignKey(s.artifacts.c.artifact_id), nullable=False),
    Column("block_id", String, nullable=False),
    Column("draft_revision_id", ForeignKey(s.revisions.c.revision_id), nullable=False),
    Column("draft_content_hash", String, nullable=False),
    Column("round_number", Integer, nullable=False),
    Column("task_id", ForeignKey(s.tasks.c.task_id), nullable=False, unique=True),
    Column("source_snapshot_id", ForeignKey(s.snapshots.c.snapshot_id), nullable=False),
    Column("feedback_artifact_id", ForeignKey(s.artifacts.c.artifact_id)),
    Column("assessment_revision", Integer, nullable=False),
    Column("reference_assessment_id", String),
    Column("user_assessment_id", String),
    Column("result", JSON),
    Column("created_at", String, nullable=False),
    UniqueConstraint("artifact_id", "block_id", "round_number"),
)
essay_assessments = Table(
    "learning_essay_assessments",
    s.metadata,
    Column("assessment_id", String, primary_key=True),
    Column("round_id", ForeignKey(essay_rounds.c.round_id), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("origin", String, nullable=False),
    Column("total", JSON),
    Column("max_total", JSON, nullable=False),
    Column("reason", Text, nullable=False),
    Column("supersedes", String),
    Column("run_id", ForeignKey(s.task_runs.c.run_id)),
    Column("payload", JSON, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("round_id", "revision"),
)
