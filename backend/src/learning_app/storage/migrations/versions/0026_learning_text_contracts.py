"""Gate newly durable learning JSON kinds and bounded OCR range semantics."""

import sqlalchemy as sa
from alembic import op

revision = "0026_learning_text_contracts"
down_revision = "0025_research_external_reads"
branch_labels = None
depends_on = None


def upgrade():
    # Existing rows stay byte-for-byte unchanged. The revision prevents an old
    # application from treating unfamiliar JSON and permission kinds as its data.
    pass


def downgrade():
    checks = (
        ("learning_tasks", "kind IN ('note', 'aligned_translation')"),
        ("learning_snapshots", "json_extract(reference, '$.kind') = 'submitted_image'"),
        ("learning_preparations", "scope = 'range'"),
        (
            "learning_revisions",
            "EXISTS (SELECT 1 FROM json_each(body, '$.blocks') b WHERE json_extract(b.value, '$.kind') = 'translation')",
        ),
        (
            "learning_native_scopes",
            "EXISTS (SELECT 1 FROM json_each(allowed_tools) t WHERE t.value = 'admit_requested_artifact')",
        ),
    )
    for table, condition in checks:
        if (
            op.get_bind()
            .execute(sa.text(f"SELECT 1 FROM {table} WHERE {condition} LIMIT 1"))
            .first()
        ):
            raise RuntimeError(
                "LEARNING_TEXT_DOWNGRADE_DENIED: preserve durable learning records in " + table
            )
