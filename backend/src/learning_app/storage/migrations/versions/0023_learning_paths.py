"""Persist suggested route navigation separately from actual assessed answers."""

import sqlalchemy as sa
from alembic import op

revision = "0023_learning_paths"
down_revision = "0022_subtitle_read_waits"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_path_states",
        sa.Column(
            "artifact_id",
            sa.String(),
            sa.ForeignKey("learning_artifacts.artifact_id"),
            primary_key=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("interactions", sa.JSON(), nullable=False),
        sa.Column("self_assessments", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_path_events",
        sa.Column("event_id", sa.String(), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(),
            sa.ForeignKey("learning_artifacts.artifact_id"),
            nullable=False,
        ),
        sa.Column(
            "content_revision_id",
            sa.String(),
            sa.ForeignKey("learning_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column("block_id", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.UniqueConstraint("artifact_id", "revision"),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM learning_path_events LIMIT 1")).first():
        raise RuntimeError("LEARNING_PATH_DOWNGRADE_DENIED: retain user learning evidence")
    op.drop_table("learning_path_events")
    op.drop_table("learning_path_states")
