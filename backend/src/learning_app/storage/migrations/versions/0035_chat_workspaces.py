"""Keep legacy chat owners while separating the readable workspaces."""

import sqlalchemy as sa
from alembic import op

revision = "0035_chat_workspaces"
down_revision = "0034_data_import_fences"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_workspaces",
        sa.Column("chat_id", sa.String(), sa.ForeignKey("chats.chat_id"), primary_key=True),
        sa.Column("course_ids", sa.JSON(), nullable=False),
        sa.Column("revision", sa.String(), nullable=False),
    )
    # Removed capabilities must not restart old local work after an upgrade.
    op.execute(
        "UPDATE learning_preparations SET state = 'paused', epoch = epoch + 1 WHERE state IN ('queued', 'running')"
    )
    op.create_table(
        "generated_document_files",
        sa.Column("asset_id", sa.String(), primary_key=True),
        sa.Column("source_version", sa.String(), primary_key=True),
        sa.Column("artifact_id", sa.String(), nullable=False),
        sa.Column("revision_id", sa.String(), nullable=False),
        sa.Column("content_role", sa.String(), nullable=False),
    )


def downgrade():
    if (
        op.get_bind().execute(sa.text("SELECT 1 FROM chat_workspaces LIMIT 1")).first()
        or op.get_bind().execute(sa.text("SELECT 1 FROM generated_document_files LIMIT 1")).first()
    ):
        raise RuntimeError("WORKSPACE_DOWNGRADE_DENIED: preserve workspace and source ownership")
    op.drop_table("generated_document_files")
    op.drop_table("chat_workspaces")
