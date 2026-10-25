"""Explicit task read origins, separate from the immutable task/result owner."""

from sqlalchemy import select

from learning_app.errors import AppError
from learning_app.storage import schema as business

from . import schema as s


def source_course(owner_course_id, row):
    return row["declaration"].get("source_course_id") or owner_course_id


def validate_origins(conn, owner_course_id, declarations):
    ranges = {}
    for declaration in declarations:
        if declaration["kind"] == "document":
            key = (
                declaration.get("source_course_id") or owner_course_id,
                declaration["asset_id"],
                declaration["source_version"],
            )
            low = declaration.get("first_page", 0)
            high = declaration.get("last_page")
            high = float("inf") if high is None else high
            if any(low <= end and begin <= high for begin, end in ranges.get(key, [])):
                raise AppError(
                    "SOURCE_RANGE_OVERLAP", "同一来源课程的文档范围重叠，请合并范围", 422
                )
            ranges.setdefault(key, []).append((low, high))
        origin = declaration.get("source_course_id")
        if not origin or origin == owner_course_id:
            continue
        if not conn.execute(
            select(business.courses.c.course_id).where(business.courses.c.course_id == origin)
        ).first():
            raise AppError("TASK_SOURCE_COURSE_UNAVAILABLE", "明确指定的资料课程不存在", 422)
        if declaration["kind"] in {"document", "transcript"}:
            found = conn.execute(
                select(business.source_versions.c.source_version)
                .join(
                    business.course_assets,
                    business.course_assets.c.asset_id == business.source_versions.c.asset_id,
                )
                .where(
                    business.course_assets.c.course_id == origin,
                    business.source_versions.c.asset_id == declaration["asset_id"],
                    business.source_versions.c.source_version == declaration["source_version"],
                )
            ).first()
        elif declaration["kind"] == "artifact":
            found = conn.execute(
                select(s.artifacts.c.artifact_id).where(
                    s.artifacts.c.course_id == origin,
                    s.artifacts.c.artifact_id == declaration["artifact_id"],
                )
            ).first()
        else:
            found = True
        if not found:
            raise AppError("TASK_SOURCE_COURSE_DENIED", "资料与明确指定的来源课程不符", 403)


def grant_representations(conn, owner_course_id, origin, source, leaf, representations):
    if origin == owner_course_id:
        return
    from .source_grants import declare_snapshot
    from .sources import SourceStore

    scope_revision = conn.execute(
        select(s.tasks.c.scope_revision).where(s.tasks.c.task_id == source["task_id"])
    ).scalar_one()
    for representation in representations:
        snapshot = SourceStore.in_transaction(conn, origin, representation["snapshot_id"])
        if snapshot.course_id != origin or snapshot.content_hash != representation["content_hash"]:
            raise AppError("TASK_SOURCE_COURSE_DENIED", "任务来源与冻结课程或摘要不符", 403)
        declare_snapshot(
            conn,
            owner_course_id,
            snapshot.snapshot_id,
            authority={
                "kind": "task_source",
                "task_id": source["task_id"],
                "source_id": source["source_id"],
                "leaf_id": leaf["leaf_id"],
                "scope_revision": scope_revision,
            },
        )
