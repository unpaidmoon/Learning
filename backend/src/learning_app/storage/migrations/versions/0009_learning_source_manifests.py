"""Server-enumerated coverage proof, separate from a caller's selected excerpts."""

import sqlalchemy as sa
from alembic import op

revision = "0009_learning_source_manifests"
down_revision = "0008_learning_task_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_source_manifests",
        sa.Column("manifest_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("documents", sa.JSON(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("units", sa.JSON(), nullable=False),
        sa.Column("scope_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.add_column(
        "learning_tasks",
        sa.Column("coverage_mode", sa.String(), nullable=False, server_default="selection"),
    )
    # SQLite accepts a nullable REFERENCES column directly. Rebuilding this
    # parent table would endanger existing units/events/runs with foreign_keys ON.
    op.execute(
        "ALTER TABLE learning_tasks ADD COLUMN scope_manifest_id VARCHAR REFERENCES learning_source_manifests(manifest_id)"
    )


def downgrade() -> None:
    op.drop_column("learning_tasks", "scope_manifest_id")
    op.drop_column("learning_tasks", "coverage_mode")
    op.drop_table("learning_source_manifests")
