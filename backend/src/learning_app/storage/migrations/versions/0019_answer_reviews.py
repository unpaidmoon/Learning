"""AI reference feedback is bound to an existing user answer and assessment version."""

import sqlalchemy as sa
from alembic import op

revision = "0019_answer_reviews"
down_revision = "0018_practice_reviews"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_answer_reviews",
        sa.Column(
            "task_id", sa.String(), sa.ForeignKey("learning_tasks.task_id"), primary_key=True
        ),
        sa.Column(
            "answer_id", sa.String(), sa.ForeignKey("learning_answers.answer_id"), nullable=False
        ),
        sa.Column("expected_revision", sa.Integer(), nullable=False),
        sa.Column(
            "parent_assessment_id",
            sa.String(),
            sa.ForeignKey("learning_assessments.assessment_id"),
            nullable=False,
        ),
        sa.Column(
            "question_snapshot_id",
            sa.String(),
            sa.ForeignKey("learning_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column(
            "answer_snapshot_id",
            sa.String(),
            sa.ForeignKey("learning_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.UniqueConstraint("answer_id", "expected_revision"),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM learning_answer_reviews LIMIT 1")).first():
        raise RuntimeError("ANSWER_REVIEW_DOWNGRADE_DENIED: retain model attribution")
    op.drop_table("learning_answer_reviews")
