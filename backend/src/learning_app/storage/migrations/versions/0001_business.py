"""Initial project-owned business schema, independent of Harness storage."""

from alembic import op

from learning_app.storage.schema import metadata

revision = "0001_business"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is unsupported; restore the migration snapshot")
