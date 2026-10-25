"""Separate frozen source coverage, generation work and one task target."""

import sqlalchemy as sa
from alembic import op

revision = "0015_task_coverage"
down_revision = "0014_preparation_focus"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "learning_tasks",
        sa.Column("layout_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "learning_tasks",
        sa.Column("plan_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("learning_tasks", sa.Column("scope_spec", sa.JSON()))
    with op.batch_alter_table("learning_task_units") as batch:
        batch.alter_column("source_snapshot_id", existing_type=sa.String(), nullable=True)
        batch.add_column(sa.Column("work_spec", sa.JSON()))
    op.add_column("learning_task_runs", sa.Column("input_contract", sa.JSON()))
    op.create_table(
        "learning_task_sources",
        sa.Column("source_id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("learning_tasks.task_id"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("declaration", sa.JSON(), nullable=False),
        sa.Column("frozen", sa.JSON()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("expected_leaf_count", sa.Integer()),
        sa.Column("error_code", sa.String()),
        sa.Column("preparation_job_id", sa.String()),
        sa.Column("excluded_at_scope_revision", sa.Integer()),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_task_source_leaves",
        sa.Column("leaf_id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), sa.ForeignKey("learning_tasks.task_id"), nullable=False),
        sa.Column(
            "source_id",
            sa.String(),
            sa.ForeignKey("learning_task_sources.source_id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("locator", sa.JSON(), nullable=False),
        sa.Column("required_representations", sa.JSON(), nullable=False),
        sa.Column("representations", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("error_code", sa.String()),
        sa.Column("excluded_at_scope_revision", sa.Integer()),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_run_inputs",
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_task_runs.run_id"), primary_key=True
        ),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("leaf_id", sa.String(), sa.ForeignKey("learning_task_source_leaves.leaf_id")),
        sa.Column(
            "snapshot_id",
            sa.String(),
            sa.ForeignKey("learning_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("snapshot_hash", sa.String(), nullable=False),
        sa.Column("content_role", sa.String(), nullable=False),
        sa.Column("representation", sa.String(), nullable=False),
        sa.Column("char_start", sa.Integer()),
        sa.Column("char_end", sa.Integer()),
        sa.Column("proof", sa.JSON()),
    )
    op.create_table(
        "learning_task_targets",
        sa.Column(
            "task_id", sa.String(), sa.ForeignKey("learning_tasks.task_id"), primary_key=True
        ),
        sa.Column("reserved_artifact_id", sa.String(), nullable=False),
        sa.Column(
            "materialized_artifact_id", sa.String(), sa.ForeignKey("learning_artifacts.artifact_id")
        ),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("artifact_kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("base_revision_id", sa.String(), sa.ForeignKey("learning_revisions.revision_id")),
        sa.Column(
            "expected_revision_id", sa.String(), sa.ForeignKey("learning_revisions.revision_id")
        ),
        sa.Column("structure_revision", sa.Integer(), nullable=False),
        sa.Column("write_scope", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("last_receipt", sa.JSON()),
    )
    op.create_index(
        "ix_learning_task_sources_owner", "learning_task_sources", ["task_id", "ordinal"]
    )
    op.create_index(
        "ix_learning_task_leaves_owner",
        "learning_task_source_leaves",
        ["task_id", "source_id", "ordinal"],
    )


def downgrade():
    # A downgrade cannot silently erase v2-only generation work or its provenance.
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM learning_tasks WHERE layout_version = 2 LIMIT 1"))
        .first()
    ):
        raise RuntimeError("TASK_V2_DOWNGRADE_DENIED: retain learning tasks and provenance")
    for name in (
        "learning_task_targets",
        "learning_run_inputs",
        "learning_task_source_leaves",
        "learning_task_sources",
    ):
        op.drop_table(name)
    op.drop_column("learning_task_runs", "input_contract")
    with op.batch_alter_table("learning_task_units") as batch:
        batch.drop_column("work_spec")
        batch.alter_column("source_snapshot_id", existing_type=sa.String(), nullable=False)
    for name in ("scope_spec", "plan_revision", "layout_version"):
        op.drop_column("learning_tasks", name)
