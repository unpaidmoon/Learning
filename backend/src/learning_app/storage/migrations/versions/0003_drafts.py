"""Persist learning drafts and immutable confirmed-image provenance."""

import sqlalchemy as sa
from alembic import op

revision = "0003_drafts"
down_revision = "0002_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_drafts",
        sa.Column("chat_id", sa.String(), sa.ForeignKey("chats.chat_id"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("cleared_submission_id", sa.String()),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "attachment_sources",
        sa.Column(
            "attachment_id",
            sa.String(),
            sa.ForeignKey("attachments.attachment_id"),
            primary_key=True,
        ),
        sa.Column("chat_id", sa.String(), sa.ForeignKey("chats.chat_id"), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is unsupported; restore the migration snapshot")
