"""One ordered foreground source per UI client, across PDF and audio."""

import sqlalchemy as sa
from alembic import op

revision = "0014_preparation_focus"
down_revision = "0013_transcripts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_preparation_focus",
        sa.Column("reader_client_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id"), nullable=False),
        sa.Column("source_version", sa.String(), nullable=False),
        sa.Column("reader_epoch", sa.String(), nullable=False),
        sa.Column("view_sequence", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
    )


def downgrade():
    op.drop_table("learning_preparation_focus")
