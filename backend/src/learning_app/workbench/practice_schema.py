"""Learning content revisions stay in artifacts; these tables own user evidence."""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)

from learning_app.storage import schema as business

from . import schema as s

answers = Table(
    "learning_answers",
    s.metadata,
    Column("answer_id", String, primary_key=True),
    Column("course_id", ForeignKey(business.courses.c.course_id), nullable=False),
    Column("artifact_id", ForeignKey(s.artifacts.c.artifact_id), nullable=False),
    Column("question_revision_id", ForeignKey(s.revisions.c.revision_id), nullable=False),
    Column("block_id", String, nullable=False),
    Column("text", Text, nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("submitted_at", String, nullable=False),
    Column("assessment_revision", Integer, nullable=False),
    Column("effective_assessment_id", String, nullable=False),
    UniqueConstraint("artifact_id", "block_id", "sequence"),
)
assessments = Table(
    "learning_assessments",
    s.metadata,
    Column("assessment_id", String, primary_key=True),
    Column("answer_id", ForeignKey(answers.c.answer_id), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("parent_assessment_id", String),
    Column("verdict", String, nullable=False),
    Column("author", String, nullable=False),
    Column("reason", Text, nullable=False),
    Column("score", JSON),
    Column("run_id", ForeignKey(s.task_runs.c.run_id)),
    Column("evidence", JSON, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("answer_id", "revision"),
)
review_items = Table(
    "learning_review_items",
    s.metadata,
    Column("review_id", String, primary_key=True),
    Column("course_id", ForeignKey(business.courses.c.course_id), nullable=False),
    Column("kind", String, nullable=False),
    Column("artifact_id", ForeignKey(s.artifacts.c.artifact_id), nullable=False),
    Column("block_id", String, nullable=False),
    Column("content_revision_id", ForeignKey(s.revisions.c.revision_id), nullable=False),
    Column("source_answer_id", ForeignKey(answers.c.answer_id)),
    Column("assessment_id", ForeignKey(assessments.c.assessment_id)),
    Column("queue_state", String, nullable=False),
    Column("eligible", Boolean, nullable=False),
    Column("inactive_reason", String),
    Column("schedule_revision", Integer, nullable=False),
    Column("due_ms", Integer, nullable=False),
    Column("scheduler", String, nullable=False),
    Column("scheduler_state", JSON, nullable=False),
    Column("scheduler_config", JSON, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    UniqueConstraint("course_id", "artifact_id", "block_id", "kind"),
)
review_events = Table(
    "learning_review_events",
    s.metadata,
    Column("event_id", String, primary_key=True),
    Column("review_id", ForeignKey(review_items.c.review_id), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("kind", String, nullable=False),
    Column("before", JSON),
    Column("after", JSON, nullable=False),
    Column("detail", JSON, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("review_id", "revision"),
)
review_preferences = Table(
    "learning_review_preferences",
    s.metadata,
    Column("preference_id", String, primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("daily_limit", Integer, nullable=False),
    Column("timezone", String, nullable=False),
    Column("day_rollover_hour", Integer, nullable=False),
)
review_days = Table(
    "learning_review_days",
    s.metadata,
    Column("logical_day", String, primary_key=True),
    Column("daily_limit", Integer, nullable=False),
    Column("timezone", String, nullable=False),
    Column("day_rollover_hour", Integer, nullable=False),
    Column("created_at", String, nullable=False),
)
review_admissions = Table(
    "learning_review_admissions",
    s.metadata,
    Column("logical_day", ForeignKey(review_days.c.logical_day), primary_key=True),
    Column("review_id", ForeignKey(review_items.c.review_id), primary_key=True),
    Column("admitted_at", String, nullable=False),
)
review_sessions = Table(
    "learning_review_sessions",
    s.metadata,
    Column("session_id", String, primary_key=True),
    Column("review_id", ForeignKey(review_items.c.review_id), nullable=False),
    Column("logical_day", ForeignKey(review_days.c.logical_day), nullable=False),
    Column("schedule_revision", Integer, nullable=False),
    Column("content_revision_id", ForeignKey(s.revisions.c.revision_id), nullable=False),
    Column("state", String, nullable=False),
    Column("opened_at", String, nullable=False),
    Column("revealed_at", String),
    Column("rating_event_id", ForeignKey(review_events.c.event_id)),
)
answer_reviews = Table(
    "learning_answer_reviews",
    s.metadata,
    Column("task_id", ForeignKey(s.tasks.c.task_id), primary_key=True),
    Column("answer_id", ForeignKey(answers.c.answer_id), nullable=False),
    Column("expected_revision", Integer, nullable=False),
    Column("parent_assessment_id", ForeignKey(assessments.c.assessment_id), nullable=False),
    Column("question_snapshot_id", ForeignKey(s.snapshots.c.snapshot_id), nullable=False),
    Column("answer_snapshot_id", ForeignKey(s.snapshots.c.snapshot_id), nullable=False),
    UniqueConstraint("answer_id", "expected_revision"),
)
