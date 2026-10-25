"""Persist known stage rejection receipts and protect final-write ordering."""

import sqlalchemy as sa
from alembic import op

revision = "0032_stage_failures"
down_revision = "0031_task_source_courses"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_model_stage_failures",
        sa.Column("effect_id", sa.String(), primary_key=True),
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_task_runs.run_id"), nullable=False
        ),
        sa.Column("native_call", sa.JSON(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("details", sa.JSON()),
        sa.Column("created_at", sa.String(), nullable=False),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM learning_model_stage_failures LIMIT 1"))
        .first()
    ):
        raise RuntimeError("MODEL_STAGE_FAILURE_DOWNGRADE_DENIED: preserve rejected final writes")
    op.drop_table("learning_model_stage_failures")
