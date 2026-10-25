"""Version-bound PDF text cache and independently fenced reading positions."""

import sqlalchemy as sa
from alembic import op

revision = "0010_document_reading"
down_revision = "0009_learning_source_manifests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_document_pages",
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), primary_key=True),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id"), primary_key=True),
        sa.Column("source_version", sa.String(), primary_key=True),
        sa.Column("page_index", sa.Integer(), primary_key=True),
        sa.Column("parser_version", sa.String(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_reading_states",
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), primary_key=True),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id"), primary_key=True),
        sa.Column("source_version", sa.String(), primary_key=True),
        sa.Column("reader_epoch", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("position", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("learning_reading_states")
    op.drop_table("learning_document_pages")
