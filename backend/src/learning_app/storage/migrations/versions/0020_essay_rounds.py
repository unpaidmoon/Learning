"""Version-bound essay drafts, reference rounds and independent user assessments."""

import sqlalchemy as sa
from alembic import op

revision = "0020_essay_rounds"
down_revision = "0019_answer_reviews"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_essay_rounds",
        sa.Column("round_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column(
            "artifact_id",
            sa.String(),
            sa.ForeignKey("learning_artifacts.artifact_id"),
            nullable=False,
        ),
        sa.Column("block_id", sa.String(), nullable=False),
        sa.Column(
            "draft_revision_id",
            sa.String(),
            sa.ForeignKey("learning_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column("draft_content_hash", sa.String(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column(
            "task_id",
            sa.String(),
            sa.ForeignKey("learning_tasks.task_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "source_snapshot_id",
            sa.String(),
            sa.ForeignKey("learning_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column(
            "feedback_artifact_id", sa.String(), sa.ForeignKey("learning_artifacts.artifact_id")
        ),
        sa.Column("assessment_revision", sa.Integer(), nullable=False),
        sa.Column("reference_assessment_id", sa.String()),
        sa.Column("user_assessment_id", sa.String()),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.UniqueConstraint("artifact_id", "block_id", "round_number"),
    )
    op.create_table(
        "learning_essay_assessments",
        sa.Column("assessment_id", sa.String(), primary_key=True),
        sa.Column(
            "round_id", sa.String(), sa.ForeignKey("learning_essay_rounds.round_id"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("total", sa.JSON()),
        sa.Column("max_total", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("supersedes", sa.String()),
        sa.Column("run_id", sa.String(), sa.ForeignKey("learning_task_runs.run_id")),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.UniqueConstraint("round_id", "revision"),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM learning_essay_rounds LIMIT 1")).first():
        raise RuntimeError("ESSAY_DOWNGRADE_DENIED: retain actual drafts and assessments")
    op.drop_table("learning_essay_assessments")
    op.drop_table("learning_essay_rounds")
