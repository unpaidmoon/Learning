"""Explicit query courses and immutable foreign snapshot references."""

import sqlalchemy as sa
from alembic import op

revision = "0030_cross_course_sources"
down_revision = "0029_document_image_reads"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_source_snapshot_grants",
        sa.Column(
            "owner_course_id", sa.String(), sa.ForeignKey("courses.course_id"), primary_key=True
        ),
        sa.Column(
            "snapshot_id",
            sa.String(),
            sa.ForeignKey("learning_snapshots.snapshot_id"),
            primary_key=True,
        ),
        sa.Column(
            "source_course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False
        ),
        sa.Column("source_hash", sa.String(), nullable=False),
        sa.Column("authority", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )


def downgrade():
    queries = (
        "SELECT 1 FROM learning_source_snapshot_grants LIMIT 1",
        "SELECT 1 FROM input_revisions WHERE json_array_length(json_extract(payload, '$.read_course_ids')) > 0 LIMIT 1",
        "SELECT 1 FROM chat_drafts WHERE json_array_length(json_extract(payload, '$.read_course_ids')) > 0 LIMIT 1",
    )
    for query in queries:
        if op.get_bind().execute(sa.text(query)).first():
            raise RuntimeError(
                "CROSS_COURSE_DOWNGRADE_DENIED: preserve explicit scope and source references"
            )
    op.drop_table("learning_source_snapshot_grants")
