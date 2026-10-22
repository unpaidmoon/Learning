"""Keep whole user-selected documents and their immutable preparation identities."""

import sqlalchemy as sa
from alembic import op

revision = "0005_document_attachments"
down_revision = "0004_course_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_attachments",
        sa.Column(
            "attachment_id",
            sa.String(),
            sa.ForeignKey("attachments.attachment_id"),
            primary_key=True,
        ),
        sa.Column("chat_id", sa.String(), sa.ForeignKey("chats.chat_id"), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("document_attachments")
