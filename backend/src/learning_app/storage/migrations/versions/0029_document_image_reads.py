"""Frozen crop reads and immutable per-execution encoding allowances."""

import sqlalchemy as sa
from alembic import op

revision = "0029_document_image_reads"
down_revision = "0028_document_region_snapshots"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_document_image_reads",
        sa.Column("effect_id", sa.String(), primary_key=True),
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_native_scopes.run_id"), nullable=False
        ),
        sa.Column("handle_id", sa.String(), nullable=False),
        sa.Column("parent_effect_id", sa.String(), nullable=False),
        sa.Column("native_call", sa.JSON(), nullable=False),
        sa.Column(
            "snapshot_id",
            sa.String(),
            sa.ForeignKey("learning_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("result_hash", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_document_image_allowances",
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_native_scopes.run_id"), primary_key=True
        ),
        sa.Column(
            "effect_id",
            sa.String(),
            sa.ForeignKey("learning_document_image_reads.effect_id"),
            primary_key=True,
        ),
        sa.Column("proof", sa.JSON(), nullable=False),
        sa.Column("proof_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )


def downgrade():
    for table in ("learning_document_image_reads", "learning_document_image_allowances"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(
                "DOCUMENT_IMAGE_READ_DOWNGRADE_DENIED: preserve crop and encoding facts"
            )
    op.drop_table("learning_document_image_allowances")
    op.drop_table("learning_document_image_reads")
