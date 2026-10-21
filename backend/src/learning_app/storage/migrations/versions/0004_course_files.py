"""Bind one real root per course without moving existing assets."""

import sqlalchemy as sa
from alembic import op

revision = "0004_course_files"
down_revision = "0003_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "course_roots",
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), primary_key=True),
        sa.Column("local_path", sa.Text(), nullable=False, unique=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "course_file_entries",
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), primary_key=True),
        sa.Column("relative_path", sa.Text(), primary_key=True),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id"), nullable=False),
        sa.Column("source_version", sa.String(), nullable=False),
    )
    op.create_table(
        "course_file_operations",
        sa.Column("operation_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("error_code", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("course_file_operations")
    op.drop_table("course_file_entries")
    op.drop_table("course_roots")
