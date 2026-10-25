"""Versioned raw local transcripts and separate user edits."""

import sqlalchemy as sa
from alembic import op

revision = "0013_transcripts"
down_revision = "0012_local_preparations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_transcript_tracks",
        sa.Column("track_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id"), nullable=False),
        sa.Column("source_version", sa.String(), nullable=False),
        sa.Column("source_digest", sa.String(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("probe", sa.JSON()),
        sa.Column("edit_head_id", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_transcript_units",
        sa.Column(
            "track_id",
            sa.String(),
            sa.ForeignKey("learning_transcript_tracks.track_id"),
            primary_key=True,
        ),
        sa.Column("unit_index", sa.Integer(), primary_key=True),
        sa.Column("revision_id", sa.String(), nullable=False, unique=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_transcript_edits",
        sa.Column("revision_id", sa.String(), primary_key=True),
        sa.Column(
            "track_id",
            sa.String(),
            sa.ForeignKey("learning_transcript_tracks.track_id"),
            nullable=False,
        ),
        sa.Column("parent_revision_id", sa.String()),
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("disposition", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )


def downgrade():
    op.drop_table("learning_transcript_edits")
    op.drop_table("learning_transcript_units")
    op.drop_table("learning_transcript_tracks")
