"""Protect immutable-base range-edit contracts and their publication receipts."""

import sqlalchemy as sa
from alembic import op

revision = "0027_learning_range_edits"
down_revision = "0026_learning_text_contracts"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    for query in (
        "SELECT 1 FROM learning_task_targets WHERE json_extract(write_scope, '$.range_edit') IS NOT NULL LIMIT 1",
        "SELECT 1 FROM learning_operations WHERE kind = 'task_range_patch' LIMIT 1",
    ):
        if op.get_bind().execute(sa.text(query)).first():
            raise RuntimeError(
                "LEARNING_RANGE_DOWNGRADE_DENIED: preserve fixed ranges and receipts"
            )
