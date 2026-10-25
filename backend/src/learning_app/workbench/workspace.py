"""Small paged workspace projections, separate from artifact bodies and task IO."""

from datetime import datetime

from pydantic import Field
from sqlalchemy import and_, func, or_, select

from learning_app.errors import AppError
from learning_app.storage import schema as business

from . import schema as s
from .models import (
    ArtifactKind,
    ContentRole,
    Contract,
    Identity,
    ModelSettings,
    TaskKind,
    TaskState,
)


class ArtifactIndexItem(Contract):
    artifact_id: Identity
    title: str
    kind: ArtifactKind
    content_role: ContentRole
    head_revision_id: Identity
    indexed: bool
    updated_at: str


class ArtifactIndexPage(Contract):
    items: list[ArtifactIndexItem]
    next_cursor: str | None


class RevisionIndexItem(Contract):
    revision_id: Identity
    parent_revision_id: Identity | None
    title: str
    content_role: ContentRole
    author: str
    task_id: Identity | None
    disposition: str
    created_at: str


class RevisionIndexPage(Contract):
    items: list[RevisionIndexItem]
    next_cursor: str | None


class TaskIndexItem(Contract):
    task_id: Identity
    title: str
    kind: TaskKind
    state: TaskState
    revision: int
    instruction_preview: str
    settings: ModelSettings
    completed_work: int
    total_work: int
    updated_at: str


class TaskIndexPage(Contract):
    items: list[TaskIndexItem]
    next_cursor: str | None


class WorkspaceQuery(Contract):
    cursor: str | None = Field(default=None, max_length=300)
    limit: int = Field(default=30, ge=1, le=100)
    query: str = Field(default="", max_length=200)


def _page(statement, table, identity, value, time_column=None):
    if time_column is None:
        time_column = table.c.updated_at
    if value.cursor:
        try:
            timestamp, marker = value.cursor.split("|", 1)
            datetime.fromisoformat(timestamp)
            if not marker or len(marker) > 160 or not all(c.isalnum() or c in "_-" for c in marker):
                raise ValueError("invalid marker")
        except ValueError as error:
            raise AppError("WORKSPACE_CURSOR_INVALID", "列表位置无效，请刷新列表", 422) from error
        statement = statement.where(
            or_(
                time_column < timestamp,
                and_(time_column == timestamp, identity < marker),
            )
        )
    return statement.order_by(time_column.desc(), identity.desc()).limit(value.limit + 1)


def _result(rows, identity, limit, time_key="updated_at"):
    more = len(rows) > limit
    selected = rows[:limit]
    cursor = f"{selected[-1][time_key]}|{selected[-1][identity]}" if more else None
    return selected, cursor


class WorkspaceStore:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _course(conn, course_id):
        if (
            conn.execute(
                select(business.courses.c.course_id).where(
                    business.courses.c.course_id == course_id
                )
            ).first()
            is None
        ):
            raise AppError("COURSE_NOT_FOUND", "课程不存在", 404)

    def artifacts(self, course_id: str, value: WorkspaceQuery) -> ArtifactIndexPage:
        with self.repository.engine.connect() as conn:
            self._course(conn, course_id)
            statement = select(
                *(s.artifacts.c[key] for key in ArtifactIndexItem.model_fields)
            ).where(s.artifacts.c.course_id == course_id)
            if value.query:
                statement = statement.where(
                    or_(
                        s.artifacts.c.title.contains(value.query, autoescape=True),
                        s.artifacts.c.artifact_id.in_(
                            select(s.search_entries.c.artifact_id).where(
                                s.search_entries.c.course_id == course_id,
                                s.search_entries.c.text.contains(value.query, autoescape=True),
                            )
                        ),
                    )
                )
            rows = list(
                conn.execute(
                    _page(statement, s.artifacts, s.artifacts.c.artifact_id, value)
                ).mappings()
            )
            entries, cursor = _result(rows, "artifact_id", value.limit)
            return ArtifactIndexPage(
                items=[ArtifactIndexItem(**item) for item in entries], next_cursor=cursor
            )

    def revisions(
        self, course_id: str, artifact_id: str, value: WorkspaceQuery
    ) -> RevisionIndexPage:
        with self.repository.engine.connect() as conn:
            self._artifact(conn, course_id, artifact_id)
            statement = select(
                *(s.revisions.c[key] for key in RevisionIndexItem.model_fields)
            ).where(s.revisions.c.artifact_id == artifact_id)
            rows = list(
                conn.execute(
                    _page(
                        statement,
                        s.revisions,
                        s.revisions.c.revision_id,
                        value,
                        s.revisions.c.created_at,
                    )
                ).mappings()
            )
            entries, cursor = _result(rows, "revision_id", value.limit, "created_at")
            return RevisionIndexPage(
                items=[RevisionIndexItem(**item) for item in entries], next_cursor=cursor
            )

    @staticmethod
    def _artifact(conn, course_id, artifact_id):
        if (
            conn.execute(
                select(s.artifacts.c.artifact_id).where(
                    s.artifacts.c.course_id == course_id, s.artifacts.c.artifact_id == artifact_id
                )
            ).first()
            is None
        ):
            raise AppError("ARTIFACT_NOT_FOUND", "指定课程的学习成果不存在", 404)

    def revision(self, course_id: str, artifact_id: str, revision_id: str):
        from .artifacts import ArtifactStore

        with self.repository.engine.connect() as conn:
            self._artifact(conn, course_id, artifact_id)
            return ArtifactStore._revision(conn, artifact_id, revision_id)

    def tasks(self, course_id: str, value: WorkspaceQuery) -> TaskIndexPage:
        with self.repository.engine.connect() as conn:
            self._course(conn, course_id)
            statement = (
                select(
                    *(
                        s.tasks.c[key]
                        for key in (
                            "task_id",
                            "kind",
                            "state",
                            "revision",
                            "instruction",
                            "settings",
                            "updated_at",
                        )
                    ),
                    s.task_targets.c.title.label("target_title"),
                )
                .outerjoin(s.task_targets, s.task_targets.c.task_id == s.tasks.c.task_id)
                .where(s.tasks.c.course_id == course_id)
            )
            if value.query:
                statement = statement.where(
                    or_(
                        s.tasks.c.instruction.contains(value.query, autoescape=True),
                        s.task_targets.c.title.contains(value.query, autoescape=True),
                    )
                )
            rows = list(
                conn.execute(_page(statement, s.tasks, s.tasks.c.task_id, value)).mappings()
            )
            entries, cursor = _result(rows, "task_id", value.limit)
            ids = [row["task_id"] for row in entries]
            counts = {}
            if ids:
                for identity, state, number in conn.execute(
                    select(s.task_units.c.task_id, s.task_units.c.state, func.count())
                    .where(
                        s.task_units.c.task_id.in_(ids),
                        s.task_units.c.excluded_at_scope_revision.is_(None),
                    )
                    .group_by(s.task_units.c.task_id, s.task_units.c.state)
                ):
                    counts.setdefault(identity, {})[state] = number
            return TaskIndexPage(
                items=[
                    TaskIndexItem(
                        task_id=row["task_id"],
                        title=row["target_title"] or row["instruction"][:80],
                        kind=row["kind"],
                        state=row["state"],
                        revision=row["revision"],
                        instruction_preview=row["instruction"][:300],
                        settings=row["settings"],
                        completed_work=counts.get(row["task_id"], {}).get("completed", 0),
                        total_work=sum(counts.get(row["task_id"], {}).values()),
                        updated_at=row["updated_at"],
                    )
                    for row in entries
                ],
                next_cursor=cursor,
            )
