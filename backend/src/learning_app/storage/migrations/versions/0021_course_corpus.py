"""Durable course document slices and exact on-demand query receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0021_course_corpus"
down_revision = "0020_essay_rounds"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_corpus_profiles",
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("exclusions", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_corpus_scans",
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), primary_key=True),
        sa.Column("scan_id", sa.String(), nullable=False),
        sa.Column("scope_hash", sa.String(), nullable=False),
        sa.Column("pending", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("error_code", sa.String()),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_corpus_documents",
        sa.Column("document_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("source_version", sa.String(), nullable=False),
        sa.Column("source_digest", sa.String(), nullable=False),
        sa.Column("seen_scan", sa.String(), nullable=False),
        sa.Column("current", sa.Boolean(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("error_code", sa.String()),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_corpus_units",
        sa.Column(
            "document_id",
            sa.String(),
            sa.ForeignKey("learning_corpus_documents.document_id"),
            primary_key=True,
        ),
        sa.Column("unit_index", sa.Integer(), primary_key=True),
        sa.Column("text_revision", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_corpus_chunks",
        sa.Column("chunk_id", sa.String(), primary_key=True),
        sa.Column(
            "document_id",
            sa.String(),
            sa.ForeignKey("learning_corpus_documents.document_id"),
            nullable=False,
        ),
        sa.Column("unit_index", sa.Integer(), nullable=False),
        sa.Column(
            "snapshot_id",
            sa.String(),
            sa.ForeignKey("learning_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "learning_native_query_receipts",
        sa.Column("effect_id", sa.String(), primary_key=True),
        sa.Column(
            "run_id", sa.String(), sa.ForeignKey("learning_native_scopes.run_id"), nullable=False
        ),
        sa.Column("native_call", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("result_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.execute(
        "CREATE INDEX ix_corpus_pending ON learning_corpus_documents(course_id, current, state)"
    )
    op.execute(
        "CREATE VIRTUAL TABLE learning_corpus_fts USING fts5(chunk_id UNINDEXED, text, tokenize='trigram')"
    )


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM learning_native_query_receipts LIMIT 1"))
        .first()
    ):
        raise RuntimeError("CORPUS_DOWNGRADE_DENIED: retain exact model read provenance")
    op.execute("DROP TABLE learning_corpus_fts")
    for name in (
        "learning_native_query_receipts",
        "learning_corpus_chunks",
        "learning_corpus_units",
        "learning_corpus_documents",
        "learning_corpus_scans",
        "learning_corpus_profiles",
    ):
        op.drop_table(name)
