"""Exact immutable read responses survive a transport receipt loss."""

import sqlalchemy as sa
from alembic import op

revision = "0017_native_read_receipts"
down_revision = "0016_native_business"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_native_read_receipts",
        sa.Column("effect_id", sa.String(), primary_key=True),
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_native_scopes.run_id"), nullable=False
        ),
        sa.Column(
            "snapshot_id",
            sa.String(),
            sa.ForeignKey("learning_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("native_call", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("result_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM learning_native_read_receipts LIMIT 1"))
        .first()
    ):
        raise RuntimeError("NATIVE_READ_RECEIPT_DOWNGRADE_DENIED: retain exact read provenance")
    op.drop_table("learning_native_read_receipts")
