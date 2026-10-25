"""Protect explicit whole-source read origins before any leaf is prepared."""

import sqlalchemy as sa
from alembic import op

revision = "0031_task_source_courses"
down_revision = "0030_cross_course_sources"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    for query in (
        "SELECT 1 FROM learning_task_sources WHERE json_extract(declaration, '$.source_course_id') IS NOT NULL LIMIT 1",
        "SELECT 1 FROM learning_tasks, json_each(scope_spec, '$.sources') AS declared WHERE json_extract(declared.value, '$.source_course_id') IS NOT NULL LIMIT 1",
    ):
        if op.get_bind().execute(sa.text(query)).first():
            raise RuntimeError(
                "TASK_SOURCE_COURSE_DOWNGRADE_DENIED: preserve explicit task read origins"
            )
