"""Reject late open requests before doing potentially slow source validation."""

import sqlalchemy as sa
from alembic import op

revision = "0011_reading_open_fences"
down_revision = "0010_document_reading"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_reading_open_fences",
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), primary_key=True),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id"), primary_key=True),
        sa.Column("source_version", sa.String(), primary_key=True),
        sa.Column("reader_client_id", sa.String(), primary_key=True),
        sa.Column("view_sequence", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("reader_epoch", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("learning_reading_open_fences")
