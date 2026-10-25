"""Course-owned learning products, immutable revisions and persistent tasks."""

import sqlalchemy as sa
from alembic import op

revision = "0007_learning_workbench"
down_revision = "0006_course_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_snapshots",
        sa.Column("snapshot_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("reference", sa.JSON(), nullable=False),
        sa.Column("content_role", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_artifacts",
        sa.Column("artifact_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("head_revision_id", sa.String(), nullable=False),
        sa.Column("content_role", sa.String(), nullable=False),
        sa.Column("indexed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_revisions",
        sa.Column("revision_id", sa.String(), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(),
            sa.ForeignKey("learning_artifacts.artifact_id"),
            nullable=False,
        ),
        sa.Column("parent_revision_id", sa.String()),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("content_role", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("task_id", sa.String()),
        sa.Column("disposition", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_conflicts",
        sa.Column("conflict_id", sa.String(), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(),
            sa.ForeignKey("learning_artifacts.artifact_id"),
            nullable=False,
        ),
        sa.Column(
            "expected_revision_id",
            sa.String(),
            sa.ForeignKey("learning_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column(
            "current_revision_id",
            sa.String(),
            sa.ForeignKey("learning_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column(
            "proposed_revision_id",
            sa.String(),
            sa.ForeignKey("learning_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_operations",
        sa.Column("operation_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_search_entries",
        sa.Column(
            "artifact_id",
            sa.String(),
            sa.ForeignKey("learning_artifacts.artifact_id"),
            primary_key=True,
        ),
        sa.Column(
            "revision_id",
            sa.String(),
            sa.ForeignKey("learning_revisions.revision_id"),
            nullable=False,
        ),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_role", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_tasks",
        sa.Column("task_id", sa.String(), primary_key=True),
        sa.Column("course_id", sa.String(), sa.ForeignKey("courses.course_id"), nullable=False),
        sa.Column("chat_id", sa.String(), sa.ForeignKey("chats.chat_id")),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("scope_revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("sources_only", sa.Boolean(), nullable=False),
        sa.Column("allow_external", sa.Boolean(), nullable=False),
        sa.Column("outline", sa.JSON(), nullable=False),
        sa.Column("outline_confirmed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "learning_task_units",
        sa.Column(
            "task_id", sa.String(), sa.ForeignKey("learning_tasks.task_id"), primary_key=True
        ),
        sa.Column("unit_id", sa.String(), primary_key=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column(
            "source_snapshot_id",
            sa.String(),
            sa.ForeignKey("learning_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("generation_id", sa.String(), sa.ForeignKey("generations.generation_id")),
        sa.Column(
            "result_artifact_id", sa.String(), sa.ForeignKey("learning_artifacts.artifact_id")
        ),
        sa.Column("error_code", sa.String()),
        sa.Column("excluded_at_scope_revision", sa.Integer()),
    )
    op.create_table(
        "learning_task_events",
        sa.Column(
            "task_id", sa.String(), sa.ForeignKey("learning_tasks.task_id"), primary_key=True
        ),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
    )
    op.create_index("ix_learning_artifacts_course", "learning_artifacts", ["course_id"])
    op.create_index("ix_learning_tasks_state", "learning_tasks", ["state", "course_id"])
    op.create_index("ix_learning_snapshots_course", "learning_snapshots", ["course_id"])


def downgrade() -> None:
    for name in (
        "learning_task_events",
        "learning_task_units",
        "learning_tasks",
        "learning_search_entries",
        "learning_operations",
        "learning_conflicts",
        "learning_revisions",
        "learning_artifacts",
        "learning_snapshots",
    ):
        op.drop_table(name)
