"""Protect frozen document crops from incompatible source-kind downgrades."""

import sqlalchemy as sa
from alembic import op

revision = "0028_document_region_snapshots"
down_revision = "0027_learning_range_edits"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM learning_snapshots WHERE json_extract(reference, '$.kind') = 'document_region_image' LIMIT 1"
            )
        )
        .first()
    ):
        raise RuntimeError(
            "DOCUMENT_REGION_DOWNGRADE_DENIED: preserve immutable document crop sources"
        )
