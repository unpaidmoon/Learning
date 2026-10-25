"""Business authority and staged model effects, separate from transport journal."""

import sqlalchemy as sa
from alembic import op

revision = "0016_native_business"
down_revision = "0015_task_coverage"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_native_scopes",
        sa.Column("run_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("owner_kind", sa.String(), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("native_epoch", sa.Integer(), nullable=False),
        sa.Column("business_binding", sa.JSON(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=False),
        sa.Column("grants", sa.JSON(), nullable=False),
        sa.Column("raw_user_text", sa.Text(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("native_terminal", sa.JSON()),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.UniqueConstraint("session_id", "native_epoch", name="uq_learning_native_scope_epoch"),
    )
    op.create_table(
        "learning_native_source_handles",
        sa.Column("handle_id", sa.String(), primary_key=True),
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_native_scopes.run_id"), primary_key=True
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source_version", sa.String(), nullable=False),
        sa.Column("locator", sa.JSON(), nullable=False),
        sa.Column("parent_handle_id", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "parent_handle_id"],
            ["learning_native_source_handles.run_id", "learning_native_source_handles.handle_id"],
        ),
    )
    op.create_table(
        "learning_model_stages",
        sa.Column("stage_id", sa.String(), primary_key=True),
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_task_runs.run_id"), nullable=False
        ),
        sa.Column("operation_id", sa.String(), nullable=False, unique=True),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("stage_revision", sa.Integer(), nullable=False),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("object_version", sa.Integer(), nullable=False),
        sa.Column("expected_revision_id", sa.String()),
        sa.Column("input_hash", sa.String(), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("body_hash", sa.String(), nullable=False),
        sa.Column("native_call", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_learning_native_scopes_owner", "learning_native_scopes", ["session_id", "state"]
    )
    op.create_table(
        "learning_native_effects",
        sa.Column("effect_id", sa.String(), primary_key=True),
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_native_scopes.run_id"), nullable=False
        ),
        sa.Column(
            "stage_id", sa.String(), sa.ForeignKey("learning_model_stages.stage_id"), nullable=False
        ),
        sa.Column("native_call", sa.JSON(), nullable=False),
        sa.Column("receipt", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_learning_native_handles_owner", "learning_native_source_handles", ["run_id", "kind"]
    )
    op.create_index(
        "ix_learning_model_stages_run", "learning_model_stages", ["run_id", "stage_revision"]
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM learning_native_scopes LIMIT 1")).first():
        raise RuntimeError("NATIVE_SCOPE_DOWNGRADE_DENIED: retain authority and effect provenance")
    for name in (
        "learning_native_effects",
        "learning_model_stages",
        "learning_native_source_handles",
        "learning_native_scopes",
    ):
        op.drop_table(name)
