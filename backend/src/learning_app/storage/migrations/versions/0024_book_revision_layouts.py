"""Book layout follows immutable artifact revisions, not mutable worker memory."""

import sqlalchemy as sa
from alembic import op

revision = "0024_book_revision_layouts"
down_revision = "0023_learning_paths"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_book_revision_layouts",
        sa.Column(
            "revision_id",
            sa.String(),
            sa.ForeignKey("learning_revisions.revision_id"),
            primary_key=True,
        ),
        sa.Column(
            "artifact_id",
            sa.String(),
            sa.ForeignKey("learning_artifacts.artifact_id"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("layout", sa.JSON(), nullable=False),
        sa.Column("layout_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_learning_book_revision_layouts_artifact_id",
        "learning_book_revision_layouts",
        ["artifact_id"],
    )


def downgrade():
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM learning_book_revision_layouts LIMIT 1"))
        .first()
    ):
        raise RuntimeError("BOOK_LAYOUT_DOWNGRADE_DENIED: preserve book and question versions")
    op.drop_table("learning_book_revision_layouts")
