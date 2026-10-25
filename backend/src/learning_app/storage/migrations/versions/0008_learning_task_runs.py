"""Isolated task execution ownership; retain all existing chat constraints."""

import sqlalchemy as sa
from alembic import op

revision = "0008_learning_task_runs"
down_revision = "0007_learning_workbench"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_task_runs",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("learning_tasks.task_id"), nullable=False),
        sa.Column("unit_id", sa.String(), nullable=False),
        sa.Column("scope_revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.String(), nullable=False),
        sa.Column("binding", sa.JSON()),
        sa.Column("host_instance_id", sa.String()),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String()),
        sa.Column("event_watermark", sa.Integer(), nullable=False),
        sa.Column("native_watermark", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    with op.batch_alter_table("learning_task_units") as batch:
        batch.drop_column("generation_id")
        batch.add_column(sa.Column("run_id", sa.String()))
        batch.create_foreign_key(
            "fk_learning_unit_run", "learning_task_runs", ["run_id"], ["run_id"]
        )
    op.create_table(
        "learning_run_events",
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_task_runs.run_id"), primary_key=True
        ),
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_run_attempts",
        sa.Column("attempt_id", sa.String(), primary_key=True),
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_task_runs.run_id"), nullable=False
        ),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("thinking", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("actual_outbound", sa.Boolean(), nullable=False),
        sa.Column("actual_outbound_attempt", sa.Integer()),
        sa.Column("status", sa.String()),
        sa.Column("received_ns", sa.Integer()),
        sa.Column("finished_ns", sa.Integer()),
        sa.Column("request_sha256", sa.String()),
    )
    op.create_table(
        "learning_run_usage",
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_task_runs.run_id"), primary_key=True
        ),
        sa.Column("settlement_id", sa.String(), primary_key=True),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("raw_usage", sa.JSON()),
        sa.Column("field_presence", sa.JSON(), nullable=False),
        sa.Column("normalized", sa.JSON(), nullable=False),
    )
    op.create_index("ix_learning_runs_owner", "learning_task_runs", ["task_id", "unit_id"])
    op.create_index("ix_learning_runs_state", "learning_task_runs", ["state"])


def downgrade() -> None:
    for name in ("learning_run_usage", "learning_run_attempts", "learning_run_events"):
        op.drop_table(name)
    with op.batch_alter_table("learning_task_units") as batch:
        batch.drop_constraint("fk_learning_unit_run", type_="foreignkey")
        batch.drop_column("run_id")
        batch.add_column(sa.Column("generation_id", sa.String()))
        batch.create_foreign_key(
            "fk_learning_unit_chat_generation", "generations", ["generation_id"], ["generation_id"]
        )
    op.drop_table("learning_task_runs")
