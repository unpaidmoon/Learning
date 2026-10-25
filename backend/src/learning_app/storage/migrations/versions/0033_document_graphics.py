"""Guard clip-aware document candidates and their immutable image receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0033_document_graphics"
down_revision = "0032_stage_failures"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    for query in (
        "SELECT 1 FROM learning_snapshots WHERE json_extract(reference, '$.region_parser_version') = 'pdfium-5.14.0-local-graphics-v3' LIMIT 1",
        "SELECT 1 FROM learning_native_source_handles WHERE json_extract(locator, '$.candidate.region_parser_version') = 'pdfium-5.14.0-local-graphics-v3' LIMIT 1",
    ):
        if op.get_bind().execute(sa.text(query)).first():
            raise RuntimeError(
                "DOCUMENT_GRAPHICS_DOWNGRADE_DENIED: preserve clip-aware region identities"
            )
