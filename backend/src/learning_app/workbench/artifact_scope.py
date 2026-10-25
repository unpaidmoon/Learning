"""Read-only notices about later task scope decisions; saved text is never rewritten."""

from fastapi import FastAPI
from pydantic import Field
from sqlalchemy import select

from . import schema as s
from .artifacts import ArtifactStore
from .models import Contract, Identity


class ArtifactScopeView(Contract):
    revision_id: Identity
    notices: list[str] = Field(default_factory=list)
    block_notices: dict[str, str] = Field(default_factory=dict)


class ArtifactScopeStore:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def in_transaction(conn, course_id, artifact_id, revision_id):
        artifact = ArtifactStore._view(conn, course_id, artifact_id)
        revision = ArtifactStore._revision(conn, artifact_id, revision_id)
        rows = conn.execute(
            select(
                s.task_units.c.work_spec,
                s.task_units.c.excluded_at_scope_revision,
                s.tasks.c.task_id,
                s.tasks.c.state,
                s.tasks.c.scope_revision,
            )
            .join(s.tasks)
            .where(
                s.task_units.c.result_artifact_id == artifact_id,
                s.tasks.c.course_id == course_id,
            )
        ).mappings()
        prefixes, tasks = {}, {}
        for row in rows:
            prefix = (row["work_spec"] or {}).get("output_prefix")
            if prefix and row["excluded_at_scope_revision"] is not None:
                prefixes[prefix] = (
                    "原范围内容：资料范围后来已调整，此处保留较早范围的文字，请结合当前范围的结果阅读。"
                )
            tasks[row["task_id"]] = row
        notices = []
        if revision_id != artifact.head_revision_id:
            notices.append("正在查看历史修订；后续范围决定不改写本版原文。")
        for row in tasks.values():
            if row["state"] == "partial":
                notices.append("此任务已明确接受部分结果，缺口未被当作完整覆盖。")
            elif row["state"] == "needs_sources":
                notices.append("此任务仍有资料缺口；已保存内容可以阅读，尚不代表全部范围完成。")
            elif row["state"] != "completed":
                notices.append("此任务尚未完成当前指定范围，以下为本版已保存内容。")
            elif row["scope_revision"]:
                notices.append("任务按调整后的资料范围完成；保留的较早范围内容另有标识。")
        block_notices = {}
        for block in revision.body.blocks:
            prefix = block.block_id.rsplit("_", 1)[0]
            if prefix in prefixes:
                block_notices[block.block_id] = prefixes[prefix]
        return ArtifactScopeView(
            revision_id=revision_id,
            notices=list(dict.fromkeys(notices)),
            block_notices=block_notices,
        )

    def get(self, course_id, artifact_id, revision_id):
        with self.repository.engine.connect() as conn:
            return self.in_transaction(conn, course_id, artifact_id, revision_id)


def install_artifact_scope_routes(app: FastAPI):
    @app.get(
        "/api/v1/courses/{course_id}/learning/artifacts/{artifact_id}/scope",
        response_model=ArtifactScopeView,
    )
    def artifact_scope(course_id: str, artifact_id: str, revision_id: str):
        return ArtifactScopeStore(app.state.repository).get(course_id, artifact_id, revision_id)
