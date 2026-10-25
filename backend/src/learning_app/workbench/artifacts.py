"""Editable independent course products, atomic revisions and conflict retention."""

from __future__ import annotations

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Connection

from learning_app.errors import AppError
from learning_app.storage import Repository
from learning_app.storage import schema as business

from . import schema as s
from .common import new_id, now, prior_operation, run_still_in_scope, save_operation
from .models import (
    ArtifactBody,
    ArtifactCreate,
    ArtifactReceipt,
    ArtifactRestore,
    ArtifactRevision,
    ArtifactUpdate,
    ArtifactView,
    CardBlock,
    PathStepBlock,
    QuestionBlock,
    RevisionConflict,
    SearchHit,
    TranslationBlock,
    VisualizationBlock,
    block_text,
)
from .sources import SourceStore


def _stable_block_id(block_id: str) -> str:
    from learning_app.storage.repository import _hash

    return _hash(block_id)[:24]


class ArtifactStore:
    def __init__(self, repository: Repository):
        self.repository = repository

    @staticmethod
    def _view(conn: Connection, course_id: str, artifact_id: str) -> ArtifactView:
        row = (
            conn.execute(
                select(s.artifacts).where(
                    s.artifacts.c.artifact_id == artifact_id,
                    s.artifacts.c.course_id == course_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("LEARNING_ARTIFACT_NOT_FOUND", "指定课程的学习成果不存在", 404)
        revision = (
            conn.execute(
                select(s.revisions).where(
                    s.revisions.c.revision_id == row["head_revision_id"],
                )
            )
            .mappings()
            .one()
        )
        return ArtifactView(**dict(row), revision=ArtifactRevision.model_validate(dict(revision)))

    def get(self, course_id: str, artifact_id: str) -> ArtifactView:
        with self.repository.engine.connect() as conn:
            return self._view(conn, course_id, artifact_id)

    def list(self, course_id: str) -> list[ArtifactView]:
        with self.repository.engine.connect() as conn:
            ids = (
                conn.execute(
                    select(s.artifacts.c.artifact_id)
                    .where(
                        s.artifacts.c.course_id == course_id,
                    )
                    .order_by(s.artifacts.c.updated_at.desc())
                )
                .scalars()
                .all()
            )
            return [self._view(conn, course_id, identity) for identity in ids]

    def history(self, course_id: str, artifact_id: str) -> list[ArtifactRevision]:
        with self.repository.engine.connect() as conn:
            self._view(conn, course_id, artifact_id)
            return [
                ArtifactRevision.model_validate(dict(row))
                for row in conn.execute(
                    select(s.revisions)
                    .where(s.revisions.c.artifact_id == artifact_id)
                    .order_by(s.revisions.c.created_at, s.revisions.c.revision_id),
                ).mappings()
            ]

    @staticmethod
    def _validate_body(conn: Connection, course_id: str, body: ArtifactBody) -> None:
        from .paths import PathStore

        PathStore.validate_links(conn, course_id, body)
        for block in body.blocks:
            snapshots = [
                SourceStore.in_transaction(conn, course_id, identity)
                for identity in block.source_snapshot_ids
            ]
            if isinstance(block, TranslationBlock):
                text = []
                for part in block.translation.source_parts:
                    source = next(
                        source for source in snapshots if source.snapshot_id == part.snapshot_id
                    )
                    if part.end > len(source.text):
                        raise AppError(
                            "TRANSLATION_SOURCE_RANGE_INVALID", "译文原文范围超出实际来源", 422
                        )
                    text.append(source.text[part.start : part.end])
                if "".join(text) != block.translation.source_text:
                    raise AppError(
                        "TRANSLATION_SOURCE_TEXT_MISMATCH", "对照原文与冻结来源不符", 422
                    )
            if block.attribution == "source_quote" and not any(
                block.markdown in snapshot.text for snapshot in snapshots
            ):
                raise AppError("SOURCE_QUOTE_MISMATCH", "原文引述须是所引用快照中的实际文字", 422)

    def _index(self, conn: Connection, artifact: ArtifactView) -> None:
        # Kept as a compatibility seam; new assets no longer feed a knowledge
        # index or automatically enroll reviews. Ordinary files remain readable.
        return

    def _legacy_index(self, conn: Connection, artifact: ArtifactView) -> None:
        conn.execute(
            delete(s.search_entries).where(
                s.search_entries.c.artifact_id == artifact.artifact_id,
            )
        )
        if artifact.indexed:
            conn.execute(
                insert(s.search_entries).values(
                    artifact_id=artifact.artifact_id,
                    revision_id=artifact.head_revision_id,
                    course_id=artifact.course_id,
                    title=artifact.title,
                    text="\n\n".join(block_text(block) for block in artifact.revision.body.blocks),
                    content_role=artifact.content_role,
                )
            )
        from .reviews import ReviewStore

        ReviewStore(self.repository).sync_cards(conn, artifact)
        if artifact.kind == "card_set":
            self.repository._hook(
                "learning_review_enqueue_before_commit", artifact_id=artifact.artifact_id
            )

    @staticmethod
    def _validate_kind(kind: str, body: ArtifactBody) -> None:
        expected = {
            "question_set": QuestionBlock,
            "card_set": CardBlock,
            "learning_path": PathStepBlock,
            "visualization": VisualizationBlock,
        }.get(kind)
        if expected is not None and not any(isinstance(block, expected) for block in body.blocks):
            raise AppError(
                "LEARNING_CONTENT_KIND_INVALID", "成果必须包含与类型对应的结构化内容", 422
            )
        if kind == "learning_path" and sum(isinstance(b, PathStepBlock) for b in body.blocks) > 256:
            raise AppError("PATH_STEP_LIMIT", "一条路线最多包含256个知识点", 422)
        if kind == "report" and any(
            b.kind not in {"markdown", "visualization"} for b in body.blocks
        ):
            raise AppError(
                "RESEARCH_REPORT_CONTENT_INVALID",
                "报告只包含正文和受限图形，不自动创建练习或复习记录",
                422,
            )

    @staticmethod
    def _validate_author(author: str, body: ArtifactBody) -> None:
        if author == "model" and any(
            isinstance(block, QuestionBlock) and block.question.answer_authority == "user_confirmed"
            for block in body.blocks
        ):
            raise AppError("TASK_USER_VERDICT_DENIED", "模型不能把参考答案标成用户已确认", 403)

    def create(self, course_id: str, value: ArtifactCreate) -> ArtifactReceipt:
        """User creation endpoint; it cannot label arbitrary text as original material."""
        if value.kind not in {"note", "handout", "book", "report"}:
            raise AppError("LEARNING_FEATURE_REMOVED", "此专门学习功能已取消", 410)
        with self.repository._lock, self.repository.engine.begin() as conn:
            return self._create(conn, course_id, value, author="user", task_id=None)

    def publish_run_result(self, run_id: str, value: ArtifactCreate) -> ArtifactReceipt:
        result = self._publish_run_result(run_id, value)
        from .artifact_files import save_artifact_file

        save_artifact_file(self.repository, result.artifact, run_id=run_id)
        return result

    def _publish_run_result(self, run_id: str, value: ArtifactCreate) -> ArtifactReceipt:
        """Exactly one product per task run, independent of ordinary chat generations."""
        value = value.model_copy(update={"operation_id": f"publish_{run_id}"})
        with self.repository._lock, self.repository.engine.begin() as conn:
            run = (
                conn.execute(select(s.task_runs).where(s.task_runs.c.run_id == run_id))
                .mappings()
                .first()
            )
            if run is None:
                raise AppError("TASK_RUN_NOT_FOUND", "任务执行不存在", 404)
            task = (
                conn.execute(select(s.tasks).where(s.tasks.c.task_id == run["task_id"]))
                .mappings()
                .one()
            )
            payload = {
                "input": value.model_dump(mode="json"),
                "author": "model",
                "task_id": task["task_id"],
            }
            prior = prior_operation(
                conn, task["course_id"], "artifact_create", value.operation_id, payload
            )
            if prior is not None:
                return ArtifactReceipt.model_validate(prior)
            unit = (
                conn.execute(
                    select(s.task_units).where(
                        s.task_units.c.task_id == run["task_id"],
                        s.task_units.c.unit_id == run["unit_id"],
                        s.task_units.c.run_id == run_id,
                    )
                )
                .mappings()
                .first()
            )
            if (
                unit is None
                or unit["excluded_at_scope_revision"] is not None
                or not run_still_in_scope(run, task, unit)
            ):
                raise AppError("TASK_WRITE_SCOPE_STALE", "原任务范围已变化，保留原结果而不覆盖")
            if task["state"] in {"completed", "partial", "cancelled"} or unit["result_artifact_id"]:
                raise AppError("TASK_WRITE_CLOSED", "任务单元已有成果或已结束")
            if run["state"] != "completed":
                raise AppError("TASK_GENERATION_NOT_COMPLETE", "生成尚未完整结束，保留已收到的内容")
            if task["layout_version"] == 2:
                result = self._publish_target(
                    conn, dict(task), dict(unit), dict(run), value, payload
                )
                if result.outcome == "applied":
                    from .practice import PracticeStore

                    PracticeStore(self.repository).publish_feedback(
                        conn, dict(task), dict(run), value.body
                    )
                    if task["kind"] == "essay_review":
                        from .essays import EssayStore

                        EssayStore(self.repository).publish_feedback(
                            conn, dict(task), dict(run), value.body, result.artifact.artifact_id
                        )
                return result
            for block in value.body.blocks:
                if block.attribution == "user_authored":
                    raise AppError("TASK_USER_AUTHOR_DENIED", "模型不能冒充用户创作身份", 403)
                if not set(block.source_snapshot_ids).issubset({unit["source_snapshot_id"]}):
                    raise AppError("TASK_SOURCE_DENIED", "结果引用了本次读取范围外的来源", 403)
                if task["sources_only"] and (
                    block.attribution
                    not in {"source_quote", "source_paraphrase", "source_inference"}
                    or not block.source_snapshot_ids
                ):
                    raise AppError(
                        "TASK_SUPPLEMENT_DENIED", "本任务只允许依据指定资料并标明实际来源", 403
                    )
            from .paths import PathStore

            PathStore.validate_links(
                conn,
                task["course_id"],
                value.body,
                allowed_snapshots=[unit["source_snapshot_id"]],
            )
            result = self._create(
                conn, task["course_id"], value, author="model", task_id=task["task_id"]
            )
            conn.execute(
                update(s.task_units)
                .where(
                    s.task_units.c.task_id == run["task_id"],
                    s.task_units.c.unit_id == run["unit_id"],
                    s.task_units.c.run_id == run_id,
                )
                .values(
                    result_artifact_id=result.artifact.artifact_id,
                    state="completed",
                    error_code=None,
                )
            )
            self.repository._hook("learning_run_product_before_commit", run_id=run_id)
            return result

    def _create(
        self,
        conn: Connection,
        course_id: str,
        value: ArtifactCreate,
        *,
        author: str,
        task_id: str | None,
        reserved_id: str | None = None,
        operation_payload: dict | None = None,
        book_context: dict | None = None,
    ) -> ArtifactReceipt:
        payload = operation_payload or {
            "input": value.model_dump(mode="json"),
            "author": author,
            "task_id": task_id,
        }
        prior = prior_operation(conn, course_id, "artifact_create", value.operation_id, payload)
        if prior is not None:
            return ArtifactReceipt.model_validate(prior)
        if not conn.execute(
            select(business.courses).where(
                business.courses.c.course_id == course_id,
            )
        ).first():
            raise AppError("COURSE_NOT_FOUND", "课程不存在", 404)
        self._validate_body(conn, course_id, value.body)
        self._validate_kind(value.kind, value.body)
        self._validate_author(author, value.body)
        identity, revision_id, timestamp = (
            reserved_id or new_id("learning"),
            new_id("revision"),
            now(),
        )
        role = "ai_generated" if author == "model" else "user_note"
        conn.execute(
            insert(s.artifacts).values(
                artifact_id=identity,
                course_id=course_id,
                kind=value.kind,
                title=value.title,
                head_revision_id=revision_id,
                content_role=role,
                indexed=True,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        revision = ArtifactRevision(
            revision_id=revision_id,
            artifact_id=identity,
            parent_revision_id=None,
            title=value.title,
            body=value.body,
            content_role=role,
            author=author,
            task_id=task_id,
            disposition="published",
            created_at=timestamp,
        )
        conn.execute(insert(s.revisions).values(**revision.model_dump(mode="json")))
        self.repository._hook("learning_artifact_before_publish", artifact_id=identity)
        view = self._view(conn, course_id, identity)
        if value.kind == "book":
            from .books import BookStore

            BookStore.persist_revision(conn, view, revision, context=book_context)
            self.repository._hook("learning_book_layout_before_commit", artifact_id=identity)
        self._index(conn, view)
        result = ArtifactReceipt(outcome="applied", artifact=view)
        save_operation(conn, course_id, "artifact_create", value.operation_id, payload, result)
        return result

    def update(
        self,
        course_id: str,
        artifact_id: str,
        value: ArtifactUpdate,
        *,
        assistance: dict | None = None,
    ) -> ArtifactReceipt:
        with self.repository._lock, self.repository.engine.begin() as conn:
            payload = {"artifact_id": artifact_id, "input": value.model_dump(mode="json")}
            if assistance is not None:
                payload["assistance"] = assistance
            prior = prior_operation(conn, course_id, "artifact_update", value.operation_id, payload)
            if prior is not None:
                return ArtifactReceipt.model_validate(prior)
            current = self._view(conn, course_id, artifact_id)
            base = self._revision(conn, artifact_id, value.expected_revision_id)
            blocks = {block.block_id: block for block in base.body.blocks}
            for change in value.changes:
                if change.replacement is None:
                    if change.block_id not in blocks:
                        raise AppError("LEARNING_BLOCK_NOT_FOUND", "要删除的内容块不存在", 404)
                    del blocks[change.block_id]
                else:
                    blocks[change.block_id] = change.replacement
            body = ArtifactBody(blocks=list(blocks.values()))
            self._validate_body(conn, course_id, body)
            role = (
                "mixed"
                if assistance is not None or base.content_role in {"ai_generated", "mixed"}
                else "user_note"
            )
            result = self._revision_result(
                conn,
                current,
                base,
                body,
                value.title or base.title,
                role,
            )
            save_operation(conn, course_id, "artifact_update", value.operation_id, payload, result)
            return result

    @staticmethod
    def _revision(conn: Connection, artifact_id: str, revision_id: str) -> ArtifactRevision:
        row = (
            conn.execute(
                select(s.revisions).where(
                    s.revisions.c.artifact_id == artifact_id,
                    s.revisions.c.revision_id == revision_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AppError("LEARNING_REVISION_NOT_FOUND", "指定成果的修订不存在", 404)
        return ArtifactRevision.model_validate(dict(row))

    def _revision_result(
        self,
        conn: Connection,
        current: ArtifactView,
        base: ArtifactRevision,
        body: ArtifactBody,
        title: str,
        role: str,
        *,
        author: str = "user",
        task_id: str | None = None,
        book_context: dict | None = None,
    ) -> ArtifactReceipt:
        self._validate_kind(current.kind, body)
        self._validate_author(author, body)
        conflict = current.head_revision_id != base.revision_id
        proposed = ArtifactRevision(
            revision_id=new_id("revision"),
            artifact_id=current.artifact_id,
            parent_revision_id=base.revision_id,
            title=title,
            body=body,
            content_role=role,
            author=author,
            task_id=task_id,
            disposition="conflict" if conflict else "published",
            created_at=now(),
        )
        conn.execute(insert(s.revisions).values(**proposed.model_dump(mode="json")))
        if current.kind == "book":
            from .books import BookStore

            BookStore.persist_revision(conn, current, proposed, base, book_context)
            self.repository._hook(
                "learning_book_layout_before_commit", artifact_id=current.artifact_id
            )
        if conflict:
            detail = RevisionConflict(
                conflict_id=new_id("conflict"),
                artifact_id=current.artifact_id,
                expected_revision_id=base.revision_id,
                current_revision_id=current.head_revision_id,
                proposed_revision_id=proposed.revision_id,
                created_at=now(),
            )
            conn.execute(insert(s.conflicts).values(**detail.model_dump(mode="json")))
            return ArtifactReceipt(outcome="conflict", artifact=current, conflict=detail)
        conn.execute(
            update(s.artifacts)
            .where(s.artifacts.c.artifact_id == current.artifact_id)
            .values(
                head_revision_id=proposed.revision_id,
                title=title,
                content_role=role,
                updated_at=now(),
            )
        )
        self.repository._hook("learning_revision_before_publish", artifact_id=current.artifact_id)
        view = self._view(conn, current.course_id, current.artifact_id)
        self._index(conn, view)
        return ArtifactReceipt(outcome="applied", artifact=view)

    def _publish_target(self, conn, task, unit, run, value, payload):
        target = dict(
            conn.execute(select(s.task_targets).where(s.task_targets.c.task_id == task["task_id"]))
            .mappings()
            .one()
        )
        authority = run["input_contract"]["target"]
        if (
            target["reserved_artifact_id"] != authority["reserved_artifact_id"]
            or target["state"] == "conflict"
        ):
            raise AppError("TASK_TARGET_WRITE_CLOSED", "成果写入范围已变化，原结果仍保留")
        self.validate_run_body(conn, task, run, value.body)
        if value.kind != target["artifact_kind"]:
            raise AppError("TASK_TARGET_KIND_DENIED", "成果类型与原任务保留对象不符", 403)
        if target["mode"] == "create":
            prefix = unit["work_spec"]["output_prefix"] + "_"
            blocks = [
                block.model_copy(update={"block_id": prefix + _stable_block_id(block.block_id)})
                for block in value.body.blocks
            ]
            if value.kind == "learning_path":
                previous = (
                    self._view(conn, task["course_id"], target["materialized_artifact_id"])
                    if target["materialized_artifact_id"]
                    else None
                )
                start = (
                    max(
                        (
                            b.step.suggested_order
                            for b in previous.revision.body.blocks
                            if isinstance(b, PathStepBlock)
                        ),
                        default=-1,
                    )
                    + 1
                    if previous
                    else 0
                )
                # Each finite model batch supplies a local order. Append the next
                # batch after the user's current order without renumbering old steps.
                ordered = sorted(
                    (b for b in blocks if isinstance(b, PathStepBlock)),
                    key=lambda b: b.step.suggested_order,
                )
                ranks = {b.block_id: start + index for index, b in enumerate(ordered)}
                if ranks and max(ranks.values()) > 1_000_000:
                    raise AppError("PATH_ORDER_LIMIT", "路线顺序超过当前上限，原结果仍保留")
                blocks = [
                    b.model_copy(
                        update={
                            "step": b.step.model_copy(
                                update={
                                    "suggested_order": ranks[b.block_id],
                                }
                            )
                        }
                    )
                    if isinstance(b, PathStepBlock)
                    else b
                    for b in blocks
                ]
        else:
            if not {block.block_id for block in value.body.blocks}.issubset(
                set(authority["write_scope"]["block_ids"])
            ):
                raise AppError("TASK_TARGET_BLOCK_DENIED", "模型修改了指定内容块以外的部分", 403)
            blocks = value.body.blocks
        book_context = None
        if value.kind == "book" and not run["input_contract"].get("edit_range"):
            from .books import BookStore

            book_context = BookStore.publication_context(conn, task, unit, run, blocks)
        return self._write_target(
            conn,
            task,
            unit,
            run,
            value,
            payload,
            target,
            authority,
            blocks,
            book_context=book_context,
        )

    @classmethod
    def validate_run_body(cls, conn, task, run, body):
        """One attribution/coverage guard for model stages and publication."""
        allowed_sources = set(
            conn.execute(
                select(s.run_inputs.c.snapshot_id).where(s.run_inputs.c.run_id == run["run_id"])
            ).scalars()
        )
        if task["kind"] == "research" and not run["input_contract"].get("edit_range"):
            from .research_external import ResearchExternal
            from .research_synthesis import ResearchSynthesis

            allowed_sources.update(ResearchSynthesis.validate_output(conn, task, run, body))
            allowed_sources.update(ResearchExternal.validate_output(conn, task, run, body))
        for index, block in enumerate(body.blocks):
            if block.attribution == "user_authored":
                raise AppError("TASK_USER_AUTHOR_DENIED", "模型不能冒充用户创作身份", 403)
            if (
                isinstance(block, QuestionBlock)
                and block.question.answer_authority == "user_confirmed"
            ):
                raise AppError("TASK_USER_VERDICT_DENIED", "模型不能把参考答案标成用户已确认", 403)
            if not block.markdown.strip() or not set(block.source_snapshot_ids).issubset(
                allowed_sources
            ):
                from .stage_contract import source_details

                raise AppError(
                    "TASK_SOURCE_DENIED",
                    "结果正文为空或引用了本次实际读取范围外的来源",
                    403,
                    details=source_details(index, block, allowed_sources),
                )
            if task["sources_only"] and (
                block.attribution not in {"source_quote", "source_paraphrase", "source_inference"}
                or not block.source_snapshot_ids
            ):
                raise AppError(
                    "TASK_SUPPLEMENT_DENIED", "本任务只允许依据指定资料并标明实际来源", 403
                )
        from .range_edits import RangeEdits

        RangeEdits.validate_body(conn, task, run, body)
        if task["coverage_mode"] == "complete":
            cited = {
                identity
                for block in body.blocks
                if block.attribution in {"source_quote", "source_paraphrase", "source_inference"}
                for identity in block.source_snapshot_ids
            }
            by_leaf = {}
            for item in run["input_contract"]["inputs"]:
                by_leaf.setdefault(item["leaf_id"], set()).add(item["snapshot_id"])
            if any(not identities & cited for identities in by_leaf.values()):
                raise AppError(
                    "TASK_OUTPUT_COVERAGE_INVALID",
                    "本次完整整理遗漏了实际输入单位的来源对应，原结果仍保留",
                )
        # Validate only newly written blocks. Previously published chapters keep
        # their own source chains and do not need this run to reread old sources.
        cls._validate_body(conn, task["course_id"], body)
        from .paths import PathStore

        PathStore.validate_links(conn, task["course_id"], body, allowed_snapshots=allowed_sources)
        from .practice import PracticeStore

        PracticeStore.validate_feedback(conn, task, body)
        from .essays import EssayStore

        EssayStore.validate_feedback(conn, task, body)

    def _write_target(
        self, conn, task, unit, run, value, payload, target, authority, blocks, *, book_context=None
    ):
        if authority.get("range_edit") or run["input_contract"].get("edit_range"):
            from .range_edits import RangeEdits

            result = RangeEdits.merge(self, conn, task, unit, run, target, value.body)
            save_operation(
                conn, task["course_id"], "artifact_create", value.operation_id, payload, result
            )
        elif target["materialized_artifact_id"] is None:
            result = self._create(
                conn,
                task["course_id"],
                value.model_copy(
                    update={"title": target["title"], "body": ArtifactBody(blocks=blocks)}
                ),
                author="model",
                task_id=task["task_id"],
                reserved_id=target["reserved_artifact_id"],
                operation_payload=payload,
                book_context=book_context,
            )
        else:
            current = self._view(conn, task["course_id"], target["materialized_artifact_id"])
            base_id = authority["expected_revision_id"]
            base = self._revision(conn, current.artifact_id, base_id) if base_id else None
            base_blocks = {block.block_id: block for block in base.body.blocks} if base else {}
            current_blocks = {block.block_id: block for block in current.revision.body.blocks}
            safe = all(
                current_blocks.get(block.block_id) == base_blocks.get(block.block_id)
                for block in blocks
            )
            # Commute unrelated edits; same-block edits retain both revisions.
            parent = current.revision if safe else base
            if parent is None:
                raise AppError("TASK_TARGET_BASE_MISSING", "成果版本发生冲突，无法安全确定原始内容")
            proposed = {block.block_id: block for block in parent.body.blocks}
            for block in blocks:
                proposed[block.block_id] = block
            role = "mixed" if current.content_role in {"mixed", "user_note"} else "ai_generated"
            try:
                merged = ArtifactBody(blocks=list(proposed.values()))
            except ValueError as exc:
                raise AppError(
                    "TASK_ARTIFACT_SIZE_LIMIT", "累积成果超过当前大小上限，已有内容与本次结果均保留"
                ) from exc
            result = self._revision_result(
                conn,
                current,
                parent,
                merged,
                current.title if safe else parent.title,
                role,
                author="model",
                task_id=task["task_id"],
                book_context=book_context,
            )
            save_operation(
                conn, task["course_id"], "artifact_create", value.operation_id, payload, result
            )
        conn.execute(
            update(s.task_targets)
            .where(s.task_targets.c.task_id == task["task_id"])
            .values(
                materialized_artifact_id=result.artifact.artifact_id,
                expected_revision_id=result.artifact.head_revision_id
                if result.outcome == "applied"
                else target["expected_revision_id"],
                structure_revision=target["structure_revision"] + (result.outcome == "applied"),
                state="materialized" if result.outcome == "applied" else "conflict",
                last_receipt=result.model_dump(mode="json"),
            )
        )
        conn.execute(
            update(s.task_units)
            .where(
                s.task_units.c.task_id == task["task_id"], s.task_units.c.unit_id == unit["unit_id"]
            )
            .values(
                result_artifact_id=result.artifact.artifact_id
                if result.outcome == "applied"
                else None,
                state="completed" if result.outcome == "applied" else "failed",
                error_code=None if result.outcome == "applied" else "TASK_TARGET_CONFLICT",
            )
        )
        self.repository._hook("learning_run_product_before_commit", run_id=run["run_id"])
        return result

    def restore(self, course_id: str, artifact_id: str, value: ArtifactRestore) -> ArtifactReceipt:
        with self.repository._lock, self.repository.engine.begin() as conn:
            payload = {"artifact_id": artifact_id, "input": value.model_dump(mode="json")}
            prior = prior_operation(
                conn, course_id, "artifact_restore", value.operation_id, payload
            )
            if prior is not None:
                return ArtifactReceipt.model_validate(prior)
            current = self._view(conn, course_id, artifact_id)
            base = self._revision(conn, artifact_id, value.expected_revision_id)
            restored = self._revision(conn, artifact_id, value.restore_revision_id)
            result = self._revision_result(
                conn,
                current,
                base,
                restored.body,
                restored.title,
                restored.content_role,
                book_context={"restore_revision_id": restored.revision_id}
                if current.kind == "book"
                else None,
            )
            save_operation(conn, course_id, "artifact_restore", value.operation_id, payload, result)
            return result

    def search(self, course_id: str, query: str, *, limit: int = 20) -> list[SearchHit]:
        if not query.strip() or len(query) > 500 or not 1 <= limit <= 100:
            raise AppError("LEARNING_SEARCH_INPUT_INVALID", "请输入有效检索词", 422)
        # Literal matching is a bounded baseline, not a semantic retrieval claim.
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.repository.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(s.search_entries, s.artifacts.c.kind)
                    .join(s.artifacts)
                    .where(
                        s.search_entries.c.course_id == course_id,
                        s.artifacts.c.indexed.is_(True),
                        s.search_entries.c.revision_id == s.artifacts.c.head_revision_id,
                        (s.search_entries.c.text.like(f"%{escaped}%", escape="\\"))
                        | (s.search_entries.c.title.like(f"%{escaped}%", escape="\\")),
                    )
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        results = []
        for row in rows:
            text = row["text"]
            location = text.casefold().find(query.strip().casefold())
            start = max(0, location - 80)
            results.append(
                SearchHit(
                    **{
                        key: row[key]
                        for key in (
                            "artifact_id",
                            "revision_id",
                            "course_id",
                            "title",
                            "kind",
                            "content_role",
                        )
                    },
                    excerpt=text[start : start + 400],
                )
            )
        return results
