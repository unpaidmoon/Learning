"""Hide courses independently of folder existence while preserving all assets."""

import sqlalchemy as sa
from alembic import op

revision = "0006_course_visibility"
down_revision = "0005_document_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "course_visibility",
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), primary_key=True),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_table("course_visibility")
