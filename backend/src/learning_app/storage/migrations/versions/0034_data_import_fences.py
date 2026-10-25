"""Preserve imported owners as history before any local execution recovery."""

import sqlalchemy as sa
from alembic import op

revision = "0034_data_import_fences"
down_revision = "0033_document_graphics"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "data_import_events",
        sa.Column("import_id", sa.String(), primary_key=True),
        sa.Column("package_id", sa.String(), nullable=False),
        sa.Column("package_sha256", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "data_imported_owners",
        sa.Column("kind", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.String(), primary_key=True),
        sa.Column(
            "import_id", sa.String(), sa.ForeignKey("data_import_events.import_id"), nullable=False
        ),
        sa.Column("facts", sa.JSON(), nullable=False),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM data_import_events LIMIT 1")).first():
        raise RuntimeError(
            "DATA_IMPORT_FENCE_DOWNGRADE_DENIED: preserve imported execution isolation"
        )
    op.drop_table("data_imported_owners")
    op.drop_table("data_import_events")
