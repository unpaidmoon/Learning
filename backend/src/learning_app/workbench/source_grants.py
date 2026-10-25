"""Exact foreign snapshot references; these never grant course-wide search."""

from sqlalchemy import JSON, Column, ForeignKey, String, Table, insert, select

from learning_app.errors import AppError
from learning_app.storage import schema as business

from . import schema as s
from .common import now

snapshot_grants = Table(
    "learning_source_snapshot_grants",
    s.metadata,
    Column("owner_course_id", ForeignKey(business.courses.c.course_id), primary_key=True),
    Column("snapshot_id", ForeignKey(s.snapshots.c.snapshot_id), primary_key=True),
    Column("source_course_id", ForeignKey(business.courses.c.course_id), nullable=False),
    Column("source_hash", String, nullable=False),
    Column("authority", JSON, nullable=False),
    Column("created_at", String, nullable=False),
)


def declare_snapshot(conn, owner_course_id, snapshot_id, *, authority):
    """Called only for an explicit user selection or a scoped actual read."""
    from .sources import SourceStore

    origin = conn.execute(
        select(s.snapshots.c.course_id).where(s.snapshots.c.snapshot_id == snapshot_id)
    ).scalar_one_or_none()
    if origin is None:
        raise AppError("SOURCE_SELECTION_DENIED", "指定来源快照不存在", 403)
    snapshot = SourceStore.in_transaction(conn, origin, snapshot_id)
    if origin == owner_course_id:
        return snapshot
    if not conn.execute(
        select(business.courses.c.course_id).where(business.courses.c.course_id == owner_course_id)
    ).first():
        raise AppError("COURSE_NOT_FOUND", "引用归属课程不存在", 404)
    prior = (
        conn.execute(
            select(snapshot_grants).where(
                snapshot_grants.c.owner_course_id == owner_course_id,
                snapshot_grants.c.snapshot_id == snapshot_id,
            )
        )
        .mappings()
        .first()
    )
    if prior:
        if prior["source_course_id"] != origin or prior["source_hash"] != snapshot.content_hash:
            raise AppError("SOURCE_GRANT_CONFLICT", "既有跨课引用与原快照不符")
    else:
        conn.execute(
            insert(snapshot_grants).values(
                owner_course_id=owner_course_id,
                snapshot_id=snapshot_id,
                source_course_id=origin,
                source_hash=snapshot.content_hash,
                authority=authority,
                created_at=now(),
            )
        )
    return snapshot


def granted_origin(conn, owner_course_id, snapshot_id):
    grant = (
        conn.execute(
            select(snapshot_grants).where(
                snapshot_grants.c.owner_course_id == owner_course_id,
                snapshot_grants.c.snapshot_id == snapshot_id,
            )
        )
        .mappings()
        .first()
    )
    return dict(grant) if grant else None


def course_scope(conn, owner_course_id, declared):
    choices = list(declared) if declared else [owner_course_id]
    if (
        len(choices) > 32
        or any(not isinstance(value, str) for value in choices)
        or len(set(choices)) != len(choices)
    ):
        raise AppError("SOURCE_COURSE_SCOPE_INVALID", "读取工作区须为明确且不重复的课程", 422)
    found = set(
        conn.execute(
            select(business.courses.c.course_id).where(business.courses.c.course_id.in_(choices))
        ).scalars()
    )
    if found != set(choices):
        raise AppError(
            "SOURCE_COURSE_SCOPE_UNAVAILABLE", "部分明确指定的课程不存在，未替换范围", 422
        )
    return choices
