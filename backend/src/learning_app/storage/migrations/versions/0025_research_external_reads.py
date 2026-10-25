"""Versioned external read receipts, without another provider execution queue."""

import sqlalchemy as sa
from alembic import op

revision = "0025_research_external_reads"
down_revision = "0024_book_revision_layouts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_research_external_reads",
        sa.Column("effect_id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("learning_tasks.task_id"), nullable=False),
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_task_runs.run_id"), nullable=False
        ),
        sa.Column(
            "source_id",
            sa.String(),
            sa.ForeignKey("learning_task_sources.source_id"),
            nullable=False,
        ),
        sa.Column("source_epoch", sa.Integer(), nullable=False),
        sa.Column("native_call", sa.JSON(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("network_events", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("result_hash", sa.String()),
        sa.Column("error_code", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("finished_at", sa.String()),
    )
    for field in ("task_id", "run_id"):
        op.create_index(
            f"ix_learning_research_external_reads_{field}",
            "learning_research_external_reads",
            [field],
        )


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM learning_research_external_reads LIMIT 1"))
        .first()
    ):
        raise RuntimeError(
            "RESEARCH_READ_DOWNGRADE_DENIED: preserve source and unknown request facts"
        )
    op.drop_table("learning_research_external_reads")
