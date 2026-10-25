"""Append-only actual answers, assessments and a single daily review budget."""

import sqlalchemy as sa
from alembic import op

revision = "0018_practice_reviews"
down_revision = "0017_native_read_receipts"
branch_labels = None
depends_on = None


def upgrade():
    # This migration owns the first immutable schema for these tables.
    op.create_table(
        "learning_answers",
        sa.Column("answer_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column(
            "artifact_id",
            sa.String(),
            sa.ForeignKey("learning_artifacts.artifact_id"),
            nullable=False,
        ),
        sa.Column(
            "question_revision_id",
            sa.String(),
            sa.ForeignKey("learning_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column("block_id", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.String(), nullable=False),
        sa.Column("assessment_revision", sa.Integer(), nullable=False),
        sa.Column("effective_assessment_id", sa.String(), nullable=False),
        sa.UniqueConstraint("artifact_id", "block_id", "sequence"),
    )
    op.create_table(
        "learning_assessments",
        sa.Column("assessment_id", sa.String(), primary_key=True),
        sa.Column(
            "answer_id", sa.String(), sa.ForeignKey("learning_answers.answer_id"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("parent_assessment_id", sa.String()),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("score", sa.JSON()),
        sa.Column("run_id", sa.String(), sa.ForeignKey("learning_task_runs.run_id")),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.UniqueConstraint("answer_id", "revision"),
    )
    op.create_table(
        "learning_review_items",
        sa.Column("review_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column(
            "artifact_id",
            sa.String(),
            sa.ForeignKey("learning_artifacts.artifact_id"),
            nullable=False,
        ),
        sa.Column("block_id", sa.String(), nullable=False),
        sa.Column(
            "content_revision_id",
            sa.String(),
            sa.ForeignKey("learning_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column("source_answer_id", sa.String(), sa.ForeignKey("learning_answers.answer_id")),
        sa.Column(
            "assessment_id", sa.String(), sa.ForeignKey("learning_assessments.assessment_id")
        ),
        sa.Column("queue_state", sa.String(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("inactive_reason", sa.String()),
        sa.Column("schedule_revision", sa.Integer(), nullable=False),
        sa.Column("due_ms", sa.Integer(), nullable=False),
        sa.Column("scheduler", sa.String(), nullable=False),
        sa.Column("scheduler_state", sa.JSON(), nullable=False),
        sa.Column("scheduler_config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.UniqueConstraint("course_id", "artifact_id", "block_id", "kind"),
    )
    op.create_index(
        "ix_learning_review_due", "learning_review_items", ["queue_state", "eligible", "due_ms"]
    )
    op.create_table(
        "learning_review_events",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column(
            "review_id",
            sa.String(),
            sa.ForeignKey("learning_review_items.review_id"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("before", sa.JSON()),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.UniqueConstraint("review_id", "revision"),
    )
    op.create_table(
        "learning_review_preferences",
        sa.Column("preference_id", sa.String(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("day_rollover_hour", sa.Integer(), nullable=False),
    )
    op.create_table(
        "learning_review_days",
        sa.Column("logical_day", sa.String(), primary_key=True),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("day_rollover_hour", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_review_admissions",
        sa.Column(
            "logical_day",
            sa.String(),
            sa.ForeignKey("learning_review_days.logical_day"),
            primary_key=True,
        ),
        sa.Column(
            "review_id",
            sa.String(),
            sa.ForeignKey("learning_review_items.review_id"),
            primary_key=True,
        ),
        sa.Column("admitted_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_review_sessions",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column(
            "review_id",
            sa.String(),
            sa.ForeignKey("learning_review_items.review_id"),
            nullable=False,
        ),
        sa.Column(
            "logical_day",
            sa.String(),
            sa.ForeignKey("learning_review_days.logical_day"),
            nullable=False,
        ),
        sa.Column("schedule_revision", sa.Integer(), nullable=False),
        sa.Column(
            "content_revision_id",
            sa.String(),
            sa.ForeignKey("learning_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("opened_at", sa.String(), nullable=False),
        sa.Column("revealed_at", sa.String()),
        sa.Column("rating_event_id", sa.String(), sa.ForeignKey("learning_review_events.event_id")),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM learning_answers UNION ALL SELECT 1 FROM learning_review_items LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError("PRACTICE_DOWNGRADE_DENIED: retain actual answers and reviews")
    for name in [
        "learning_review_sessions",
        "learning_review_admissions",
        "learning_review_days",
        "learning_review_preferences",
        "learning_review_events",
        "learning_review_items",
        "learning_assessments",
        "learning_answers",
    ]:
        op.drop_table(name)
