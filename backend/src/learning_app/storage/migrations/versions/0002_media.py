"""Media references and optimistic playback positions; no source file copying."""

from alembic import op

from learning_app.media_schema import metadata

revision = "0002_media"
down_revision = "0001_business"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind())


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is unsupported; restore the migration snapshot")
