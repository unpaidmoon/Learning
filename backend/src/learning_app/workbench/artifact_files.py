"""Publish generated documents to the course directory without duplicate writes."""

import hashlib
import os
import re
from uuid import uuid4

from sqlalchemy import Column, MetaData, String, Table, insert, select, update

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError

from . import schema as s
from .models import block_text

generated_files = Table(
    "generated_document_files",
    MetaData(),
    Column("asset_id", String, primary_key=True),
    Column("source_version", String, primary_key=True),
    Column("artifact_id", String, nullable=False),
    Column("revision_id", String, nullable=False),
    Column("content_role", String, nullable=False),
)


def source_role(conn, asset_id, source_version):
    role = conn.execute(
        select(generated_files.c.content_role).where(
            generated_files.c.asset_id == asset_id,
            generated_files.c.source_version == source_version,
        )
    ).scalar_one_or_none()
    if role:
        return role
    return (
        "mixed"
        if conn.execute(
            select(generated_files.c.asset_id).where(generated_files.c.asset_id == asset_id)
        ).first()
        else "original"
    )


def save_artifact_file(repository, artifact, *, run_id=None):
    course = artifact.course_id
    if run_id:
        with repository.engine.connect() as conn:
            spec = conn.execute(
                select(s.tasks.c.scope_spec)
                .join(s.task_runs, s.tasks.c.task_id == s.task_runs.c.task_id)
                .where(s.task_runs.c.run_id == run_id)
            ).scalar_one()
            course = (spec or {}).get("origin", {}).get("save_course_id", course)
    files = CourseFileStore(repository)
    root = files._root(course)
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", artifact.title).strip(" .")[:80] or "学习文档"
    name = f"{title}-{artifact.artifact_id[-8:]}.md"
    path = files._path(root, name, exists=False)
    body = "# " + artifact.title + "\n\n> AI 生成，请结合原资料核对。\n\n"
    body += "\n\n".join(block_text(block) for block in artifact.revision.body.blocks) + "\n"
    from .sources import SourceStore

    references = list(
        dict.fromkeys(
            identity
            for block in artifact.revision.body.blocks
            for identity in block.source_snapshot_ids
        )
    )
    if references:
        body += "\n## 来源\n\n"
        for index, identity in enumerate(references, 1):
            source = SourceStore(repository).snapshot(artifact.course_id, identity)
            page = getattr(source.reference, "page_index", None)
            label = f"来源 {index}" + (f" · 第 {page + 1} 页" if page is not None else "")
            if source.reference.kind in {
                "document",
                "document_selection",
                "subtitle",
                "document_image",
                "document_region_image",
                "snapshot_slice",
            }:
                body += f"- [{label}](/api/v1/courses/{artifact.course_id}/learning/selections/{identity})\n"
            else:
                role = {"original": "资料", "user_note": "用户内容", "ai_generated": "AI内容", "mixed": "混合内容"}[source.content_role]
                body += f"- {label}：已提交的对话内容或图片（{role}）。\n"
    content = body.encode("utf-8")
    # Match the existing file manager's lock order (file operations, then DB).
    with files._lock:
        expected = None
        if path.exists():
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            if expected != hashlib.sha256(content).hexdigest():
                from learning_app.storage import schema as business

                with repository.engine.connect() as conn:
                    owned = conn.execute(
                        select(generated_files.c.asset_id)
                        .join(
                            business.source_versions,
                            (generated_files.c.asset_id == business.source_versions.c.asset_id)
                            & (
                                generated_files.c.source_version
                                == business.source_versions.c.source_version
                            ),
                        )
                        .where(
                            generated_files.c.artifact_id == artifact.artifact_id,
                            business.source_versions.c.content_hash == expected,
                            business.source_versions.c.local_path == str(path),
                        )
                    ).first()
                if not owned:
                    raise AppError(
                        "GENERATED_FILE_CONFLICT", "生成文件已有本地修改，保留双方内容，未覆盖", 409
                    )
        if expected != hashlib.sha256(content).hexdigest():
            temporary = files._path(root, f".{name}.{uuid4().hex}.partial", exists=False)
            try:
                with temporary.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if (expected is not None and not path.exists()) or (
                    path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != expected
                ):
                    raise AppError("GENERATED_FILE_CONFLICT", "保存位置发生变化，未覆盖", 409)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        opened = files.open(course, name)
        with repository.engine.begin() as conn:
            exists = conn.execute(
                select(generated_files).where(
                    generated_files.c.asset_id == opened["asset_id"],
                    generated_files.c.source_version == opened["source_version"],
                )
            ).first()
            if not exists:
                conn.execute(
                    insert(generated_files).values(
                        asset_id=opened["asset_id"],
                        source_version=opened["source_version"],
                        artifact_id=artifact.artifact_id,
                        revision_id=artifact.head_revision_id,
                        content_role=artifact.content_role,
                    )
                )
            else:
                conn.execute(
                    update(generated_files)
                    .where(
                        generated_files.c.asset_id == opened["asset_id"],
                        generated_files.c.source_version == opened["source_version"],
                    )
                    .values(
                        revision_id=artifact.head_revision_id, content_role=artifact.content_role
                    )
                )
        return opened


def published_file(repository, task):
    from .artifacts import ArtifactStore

    with repository.engine.connect() as conn:
        target = (
            conn.execute(select(s.task_targets).where(s.task_targets.c.task_id == task.task_id))
            .mappings()
            .first()
        )
        if not target or not target["materialized_artifact_id"]:
            return None
        spec = conn.execute(
            select(s.tasks.c.scope_spec).where(s.tasks.c.task_id == task.task_id)
        ).scalar_one()
    artifact = ArtifactStore(repository).get(task.course_id, target["materialized_artifact_id"])
    course = (spec or {}).get("origin", {}).get("save_course_id", task.course_id)
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", artifact.title).strip(" .")[:80] or "学习文档"
    files = CourseFileStore(repository)
    name = f"{title}-{artifact.artifact_id[-8:]}.md"
    if not files._path(files._root(course), name, exists=False).is_file():
        return None
    return files.open(course, name)
