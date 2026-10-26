"""Persist conversation presentation independently of execution identity."""
import sqlalchemy as sa
from alembic import op

revision = "0037_chat_presentation"
down_revision = "0036_platform_sources"
branch_labels = depends_on = None


def upgrade():
    op.create_table("chat_presentation",
        sa.Column("chat_id", sa.String(), sa.ForeignKey("chats.chat_id"), primary_key=True),
        sa.Column("title", sa.String()), sa.Column("manual", sa.Boolean(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False), sa.Column("title_request_id", sa.String()),
        sa.Column("title_receipt", sa.JSON(), nullable=False))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM chat_presentation LIMIT 1")).first():
        raise RuntimeError("CHAT_PRESENTATION_DOWNGRADE_DENIED: preserve conversation state")
    op.drop_table("chat_presentation")
