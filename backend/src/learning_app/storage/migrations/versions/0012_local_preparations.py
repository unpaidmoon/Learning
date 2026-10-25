"""Durable local processing leases/checkpoints, separate from provider attempts."""

import sqlalchemy as sa
from alembic import op

revision = "0012_local_preparations"
down_revision = "0011_reading_open_fences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_preparations",
        sa.Column("job_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id"), nullable=False),
        sa.Column("source_version", sa.String(), nullable=False),
        sa.Column("source_digest", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_preparation_units",
        sa.Column(
            "job_id", sa.String(), sa.ForeignKey("learning_preparations.job_id"), primary_key=True
        ),
        sa.Column("unit_index", sa.Integer(), primary_key=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.String()),
        sa.Column("lease_epoch", sa.Integer()),
        sa.Column("error_code", sa.String()),
        sa.Column("result_digest", sa.String()),
        sa.Column("local_attempts", sa.Integer(), nullable=False),
        sa.Column("progress", sa.JSON()),
    )
    op.create_table(
        "learning_preparation_events",
        sa.Column(
            "job_id", sa.String(), sa.ForeignKey("learning_preparations.job_id"), primary_key=True
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("operation_id", sa.String()),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("receipt", sa.JSON()),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_document_ocr",
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), primary_key=True),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id"), primary_key=True),
        sa.Column("source_version", sa.String(), primary_key=True),
        sa.Column("page_index", sa.Integer(), primary_key=True),
        sa.Column("parameters_version", sa.String(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )


def downgrade() -> None:
    for name in (
        "learning_document_ocr",
        "learning_preparation_events",
        "learning_preparation_units",
        "learning_preparations",
    ):
        op.drop_table(name)
