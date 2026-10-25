"""Question-owned local subtitle work, independent of manually paused batches."""

import sqlalchemy as sa
from alembic import op

revision = "0022_subtitle_read_waits"
down_revision = "0021_course_corpus"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_subtitle_read_waits",
        sa.Column("wait_id", sa.String(), primary_key=True),
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_native_scopes.run_id"), nullable=False
        ),
        sa.Column(
            "job_id", sa.String(), sa.ForeignKey("learning_preparations.job_id"), nullable=False
        ),
        sa.Column("anchor_id", sa.String(), nullable=False),
        sa.Column("deadline", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM learning_subtitle_read_waits LIMIT 1")).first():
        raise RuntimeError("SUBTITLE_READ_DOWNGRADE_DENIED: retain original audio-read provenance")
    op.drop_table("learning_subtitle_read_waits")
