"""Book outline authority and revision layouts over the existing task/asset store."""

import json

from deeptutor_reuse.book_compiler_state import finalize_terminal_page
from deeptutor_reuse.book_models import (
    Block,
    BlockStatus,
    BlockType,
    Book,
    BookStatus,
    Chapter,
    Page,
    Spine,
)
from sqlalchemy import insert, select, update

from learning_app.errors import AppError
from learning_app.imported_executions import imported_executions
from learning_app.storage import schema as business
from learning_app.storage.repository import _hash

from . import book_schema as b
from . import schema as s
from .artifacts import ArtifactStore
from .book_models import (
    BookChapterView,
    BookDirectoryUpdate,
    BookOutlineConfirm,
    BookOutlineDraft,
    BookOutlineView,
    BookPageLayout,
    BookPageView,
    BookRevisionLayout,
    BookSourceItem,
    BookView,
)
from .common import now, prior_operation, run_still_in_scope, save_operation
from .models import ArtifactReceipt, OutlineItem, QuestionBlock, TaskView, VisualizationBlock
from .tasks import TaskStore

BOOK_PHASE = "outline"


class BookStore:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def target(conn, task_id):
        return dict(
            conn.execute(select(s.task_targets).where(s.task_targets.c.task_id == task_id))
            .mappings()
            .one()
        )

    @staticmethod
    def _write_metadata(conn, target, metadata):
        conn.execute(
            update(s.task_targets)
            .where(s.task_targets.c.task_id == target["task_id"])
            .values(
                write_scope={**target["write_scope"], "book": metadata},
            )
        )

    @staticmethod
    def catalog(conn, task_id):
        rows = list(
            conn.execute(
                select(s.task_sources)
                .where(
                    s.task_sources.c.task_id == task_id,
                    s.task_sources.c.excluded_at_scope_revision.is_(None),
                )
                .order_by(s.task_sources.c.ordinal)
            ).mappings()
        )
        asset_ids = [
            r["declaration"].get("asset_id") for r in rows if r["declaration"].get("asset_id")
        ]
        labels = (
            dict(
                conn.execute(
                    select(business.assets.c.asset_id, business.assets.c.display_name).where(
                        business.assets.c.asset_id.in_(asset_ids)
                    )
                ).all()
            )
            if asset_ids
            else {}
        )
        return [
            BookSourceItem(
                source_id=row["source_id"],
                kind=row["declaration"].get("kind", "unknown"),
                label=row["declaration"].get("relative_path")
                or labels.get(row["declaration"].get("asset_id"))
                or f"资料 {row['ordinal'] + 1}",
                declaration={
                    key: value for key, value in row["declaration"].items() if key != "stat"
                },
            )
            for row in rows
            if row["declaration"].get("kind") != "course"
        ]

    @staticmethod
    def planning_prompt(task, work):
        return (
            "这是用户明确要求组织交互书的目录规划阶段，只得到下列完整、已授权的资料清单，不是正文；不要声称读完教材，也不推测未给出的事实。"
            "根据学习目标建议可调整的章节，并把每个指定source_id分配到相关章节；可重组、跨资料合并，一份来源可用于多章，不能遗漏或增加其他来源。"
            "已有明确目录时只能补source_ids，须逐项保留原chapter_id/title/objective与顺序，不再要求用户确认。"
            "sources_only时每章必须有来源；允许AI补充时，确有必要的补充章可以source_ids为空，正文会明确标记。"
            "不用工具，不创建正文或用户学习记录。仅返回严格JSON，不加围栏："
            '{"chapters":[{"chapter_id":"chapter_1","title":"章节标题","objective":"本章学习目标","source_ids":["清单中实际source_id"]}],"rationale":"结构建议及资料清单的局限"}。'
            "用户目标："
            + task.instruction
            + "\n完整资料清单与已有目录："
            + json.dumps(work["book_context"], ensure_ascii=False)
        )

    @staticmethod
    def validate_page_work(conn, task, spec):
        target = BookStore.target(conn, task["task_id"])
        if target["mode"] == "edit":
            return False
        metadata = target["write_scope"].get("book", {})
        page = spec.get("book", {})
        chapter = next(
            (
                c
                for c in metadata.get("confirmed_outline", {}).get("chapters", [])
                if c["chapter_id"] == page.get("chapter_id")
            ),
            None,
        )
        if (
            not metadata.get("confirmed")
            or not chapter
            or page.get("outline_hash") != metadata.get("outline_hash")
        ):
            raise AppError("BOOK_PAGE_PLAN_CHANGED", "书页不属于原已确认目录")
        leaf_sources = dict(
            conn.execute(
                select(s.task_source_leaves.c.leaf_id, s.task_source_leaves.c.source_id).where(
                    s.task_source_leaves.c.task_id == task["task_id"]
                )
            ).all()
        )
        if any(
            leaf_sources.get(item["leaf_id"]) not in chapter["source_ids"]
            for item in spec["inputs"]
        ):
            raise AppError("BOOK_PAGE_SOURCE_DENIED", "书页读取超出本章来源分配", 403)
        return not chapter["source_ids"] and not task["sources_only"]

    @classmethod
    def planning_context(cls, conn, task):
        sources = cls.catalog(conn, task["task_id"])
        value = {
            "schema_version": 1,
            "task_id": task["task_id"],
            "scope_revision": task["scope_revision"],
            "sources": [item.model_dump(mode="json") for item in sources],
            "supplied_outline": task["outline"] if task["outline_confirmed"] else [],
            "sources_only": task["sources_only"],
        }
        if len(json.dumps(value, ensure_ascii=False)) > 80_000:
            raise AppError(
                "BOOK_OUTLINE_INPUT_LIMIT", "完整目录资料清单超过本次规划上限，原范围保留"
            )
        return value

    @classmethod
    def validate_planning_work(cls, conn, task, spec):
        context = spec.get("book_context")
        if (
            task["kind"] != "book"
            or spec.get("phase") != BOOK_PHASE
            or spec.get("inputs") != []
            or not isinstance(context, dict)
            or context != cls.planning_context(conn, task)
        ):
            raise AppError(
                "BOOK_OUTLINE_SCOPE_CHANGED", "目录规划与原资料范围不符，未创建空材料调用"
            )

    @staticmethod
    def validate_proposal(context, proposal):
        declared = {item["source_id"] for item in context["sources"]}
        assigned = {identity for chapter in proposal.chapters for identity in chapter.source_ids}
        if assigned != declared:
            raise AppError(
                "BOOK_OUTLINE_SOURCE_COVERAGE", "目录必须分配全部指定来源，不能漏项或增加其他来源"
            )
        if context["sources_only"] and any(not chapter.source_ids for chapter in proposal.chapters):
            raise AppError("BOOK_OUTLINE_SOURCES_REQUIRED", "只依据资料的每一章都须指定实际来源")
        supplied = context.get("supplied_outline") or []
        if (
            supplied
            and [
                OutlineItem(
                    **{key: chapter.model_dump()[key] for key in OutlineItem.model_fields}
                ).model_dump(mode="json")
                for chapter in proposal.chapters
            ]
            != supplied
        ):
            raise AppError("BOOK_SUPPLIED_OUTLINE_CHANGED", "已有明确目录不能被内部编排改写")

    def outline(self, course_id, task_id):
        with self.repository.engine.connect() as conn:
            task = TaskStore._row(conn, course_id, task_id)
            if task["kind"] != "book" or task["layout_version"] != 2:
                raise AppError("BOOK_TASK_REQUIRED", "此任务不是新版交互书任务", 422)
            metadata = self.target(conn, task_id)["write_scope"].get("book", {})
            return BookOutlineView(
                task_id=task_id,
                task_revision=task["revision"],
                proposal_hash=metadata.get("proposal_hash"),
                proposal=metadata.get("confirmed_outline")
                if metadata.get("confirmed")
                else metadata.get("proposal"),
                confirmed=bool(metadata.get("confirmed")),
                confirmation_count=metadata.get("confirmation_count", 0),
                supplied=bool(task["outline_confirmed"] and not metadata.get("user_confirmed")),
                sources=self.catalog(conn, task_id),
            )

    def accept_outline(self, run_id: str, value: BookOutlineDraft):
        """Local publication receipt for a completed planning run, not a fake asset."""
        with self.repository._lock, self.repository.engine.begin() as conn:
            run = dict(
                conn.execute(select(s.task_runs).where(s.task_runs.c.run_id == run_id))
                .mappings()
                .one()
            )
            task = dict(
                conn.execute(select(s.tasks).where(s.tasks.c.task_id == run["task_id"]))
                .mappings()
                .one()
            )
            unit = dict(
                conn.execute(
                    select(s.task_units).where(
                        s.task_units.c.task_id == run["task_id"],
                        s.task_units.c.unit_id == run["unit_id"],
                    )
                )
                .mappings()
                .one()
            )
            target = self.target(conn, task["task_id"])
            metadata = dict(target["write_scope"].get("book", {}))
            digest = _hash(value.model_dump(mode="json"))
            prior = metadata.get("planning_receipts", {}).get(run_id)
            if prior:
                if prior != digest:
                    raise AppError("BOOK_OUTLINE_RECEIPT_CONFLICT", "原目录结果不能替换")
                return
            if (
                run["state"] != "completed"
                or unit["work_spec"].get("phase") != BOOK_PHASE
                or not run_still_in_scope(run, task, unit)
                or task["state"] in {"cancelled", "completed", "partial"}
            ):
                raise AppError("BOOK_OUTLINE_PUBLICATION_CLOSED", "原目录执行未完成或范围已关闭")
            self.validate_planning_work(conn, task, unit["work_spec"])
            self.validate_proposal(unit["work_spec"]["book_context"], value)
            supplied = bool(unit["work_spec"]["book_context"]["supplied_outline"])
            metadata.update(
                proposal=value.model_dump(mode="json"),
                proposal_hash=digest,
                planning_receipts={**metadata.get("planning_receipts", {}), run_id: digest},
                confirmed=supplied,
                confirmation_count=1 if supplied else 0,
            )
            if supplied:
                metadata["confirmed_outline"] = value.model_dump(mode="json")
                metadata["outline_hash"] = digest
            self._write_metadata(conn, target, metadata)
            conn.execute(
                update(s.task_units)
                .where(
                    s.task_units.c.task_id == task["task_id"],
                    s.task_units.c.unit_id == unit["unit_id"],
                )
                .values(state="completed", error_code=None)
            )
            TaskStore._event(
                conn,
                task,
                "book_outline_proposed",
                {"proposal_hash": digest, "run_id": run_id},
                outline=[
                    OutlineItem(
                        chapter_id=c.chapter_id, title=c.title, objective=c.objective
                    ).model_dump(mode="json")
                    for c in value.chapters
                ],
                state=task["state"]
                if task["state"] == "paused"
                else "queued"
                if supplied
                else "awaiting_outline",
            )
            self.repository._hook("learning_book_outline_before_commit", task_id=task["task_id"])

    def confirm(self, course_id, task_id, value: BookOutlineConfirm):
        imported_executions(self.repository).require_fresh("task", task_id)
        payload = {"task_id": task_id, "input": value.model_dump(mode="json")}
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(
                conn, course_id, "book_outline_confirm", value.operation_id, payload
            )
            if prior is not None:
                return TaskView.model_validate(prior)
            task = TaskStore._row(conn, course_id, task_id)
            TaskStore._check_revision(task, value.expected_revision)
            target = self.target(conn, task_id)
            metadata = dict(target["write_scope"].get("book", {}))
            if (
                task["kind"] != "book"
                or metadata.get("confirmed")
                or task["state"] not in {"awaiting_outline", "paused"}
                or value.proposal_hash != metadata.get("proposal_hash")
            ):
                raise AppError("BOOK_OUTLINE_CONFIRMATION_STALE", "当前目录已变化、已确认或已关闭")
            context = self.planning_context(conn, {**task, "outline_confirmed": False})
            self.validate_proposal(context, value.outline)
            metadata.update(
                confirmed=True,
                confirmation_count=1,
                user_confirmed=True,
                confirmed_outline=value.outline.model_dump(mode="json"),
                outline_hash=_hash(value.outline.model_dump(mode="json")),
            )
            self._write_metadata(conn, target, metadata)
            TaskStore._event(
                conn,
                task,
                "outline_confirmed",
                {"proposal_hash": value.proposal_hash},
                outline_confirmed=True,
                outline=[
                    OutlineItem(
                        chapter_id=c.chapter_id, title=c.title, objective=c.objective
                    ).model_dump(mode="json")
                    for c in value.outline.chapters
                ],
                state="paused" if task["state"] == "paused" else "queued",
            )
            result = TaskStore._view(conn, course_id, task_id)
            save_operation(
                conn, course_id, "book_outline_confirm", value.operation_id, payload, result
            )
            return result

    @staticmethod
    def layout(conn, artifact_id, revision_id, *, required=True):
        row = (
            conn.execute(
                select(b.book_layouts).where(
                    b.book_layouts.c.artifact_id == artifact_id,
                    b.book_layouts.c.revision_id == revision_id,
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            if required:
                raise AppError("BOOK_LAYOUT_NOT_FOUND", "该书版本没有可核对的章节布局", 404)
            return None
        value = BookRevisionLayout.model_validate(row["layout"])
        if _hash(value.model_dump(mode="json")) != row["layout_hash"]:
            raise AppError("BOOK_LAYOUT_HASH_MISMATCH", "章节布局与原版本摘要不符")
        return value

    @classmethod
    def persist_revision(cls, conn, artifact, revision, base=None, context=None):
        """Called inside the same transaction as every book asset revision."""
        if artifact.kind != "book":
            return
        context = context or {}
        origin_revision = context.get("restore_revision_id") or (base.revision_id if base else None)
        layout = (
            cls.layout(conn, artifact.artifact_id, origin_revision, required=False)
            if origin_revision
            else None
        )
        page = context.get("new_page")
        if layout is None and page is None:
            return  # Preserve pre-existing unstructured books without inventing a layout.
        if layout is None:
            metadata = context["metadata"]
            layout = BookRevisionLayout(
                origin_task_id=context["task_id"],
                outline_hash=metadata["outline_hash"],
                chapters=metadata["confirmed_outline"]["chapters"],
            )
        else:
            layout = layout.model_copy(deep=True)
        if context.get("chapter_labels"):
            labels = context["chapter_labels"]
            originals = {c.chapter_id: c for c in layout.chapters}
            if len(labels) != len(originals) or {c.chapter_id for c in labels} != set(originals):
                raise AppError(
                    "BOOK_DIRECTORY_MEMBERSHIP_DENIED",
                    "调整目录须保留现有章节，新增生成须使用明确任务",
                )
            layout.chapters = [
                originals[c.chapter_id].model_copy(
                    update={"title": c.title, "objective": c.objective}
                )
                for c in labels
            ]
            layout.outline_hash = _hash([c.model_dump(mode="json") for c in layout.chapters])
        if page is not None:
            if any(p.page_id == page["page_id"] for p in layout.pages):
                raise AppError("BOOK_PAGE_ALREADY_PUBLISHED", "原页面已经保存，未重复追加")
            layout.pages.append(BookPageLayout(**page))
        blocks = {block.block_id: block for block in revision.body.blocks}
        for entry in layout.pages:
            for identity in entry.block_ids:
                block = blocks.get(identity)
                if block is None:
                    continue
                digest = _hash(block.model_dump(mode="json"))
                pinned = entry.question_revisions.get(identity)
                prior_hash = entry.block_hashes.get(identity)
                if isinstance(block, QuestionBlock):
                    published = (
                        conn.execute(
                            select(s.revisions.c.disposition).where(
                                s.revisions.c.revision_id == pinned
                            )
                        ).scalar_one_or_none()
                        if pinned
                        else None
                    )
                    if digest != prior_hash or published != "published":
                        entry.question_revisions[identity] = revision.revision_id
                else:
                    entry.question_revisions.pop(identity, None)
                entry.block_hashes[identity] = digest
        layout = BookRevisionLayout.model_validate(layout.model_dump(mode="json"))
        value = layout.model_dump(mode="json")
        conn.execute(
            insert(b.book_layouts).values(
                revision_id=revision.revision_id,
                artifact_id=artifact.artifact_id,
                schema_version=1,
                layout=value,
                layout_hash=_hash(value),
                created_at=now(),
            )
        )

    @staticmethod
    def publication_context(conn, task, unit, run, blocks):
        if task["kind"] != "book" or (unit["work_spec"] or {}).get("phase") != "content":
            return None
        target = BookStore.target(conn, task["task_id"])
        if target["mode"] == "edit":
            return None
        metadata = target["write_scope"].get("book", {})
        page = unit["work_spec"].get("book") or {}
        if not metadata.get("confirmed") or page.get("outline_hash") != metadata.get(
            "outline_hash"
        ):
            raise AppError("BOOK_PAGE_PLAN_CHANGED", "本页不属于已确认的目录版本")
        chapter = next(
            (
                c
                for c in metadata["confirmed_outline"]["chapters"]
                if c["chapter_id"] == page.get("chapter_id")
            ),
            None,
        )
        if chapter is None or any(
            block.kind not in {"markdown", "question", "visualization"} for block in blocks
        ):
            raise AppError(
                "BOOK_PAGE_CONTENT_KIND_DENIED", "书页只接收已授权的正文、同一题库题目或受限图形"
            )
        return {
            "task_id": task["task_id"],
            "metadata": metadata,
            "new_page": {
                "page_id": page["page_id"],
                "chapter_id": page["chapter_id"],
                "unit_id": unit["unit_id"],
                "title": page["title"],
                "order_key": page["order_key"],
                "block_ids": [block.block_id for block in blocks],
                "block_hashes": {
                    block.block_id: _hash(block.model_dump(mode="json")) for block in blocks
                },
                "question_revisions": {},
                "source_snapshot_ids": list(
                    dict.fromkeys(item["snapshot_id"] for item in run["input_contract"]["inputs"])
                ),
            },
        }

    def get(self, course_id, artifact_id, revision_id=None):
        from .task_coverage import TaskCoverage

        with self.repository.engine.connect() as conn:
            artifact = ArtifactStore._view(conn, course_id, artifact_id)
            revision = (
                ArtifactStore._revision(conn, artifact_id, revision_id)
                if revision_id
                else artifact.revision
            )
            layout = self.layout(conn, artifact_id, revision.revision_id)
            task = TaskStore._row(conn, course_id, layout.origin_task_id)
            units = {
                row["unit_id"]: dict(row)
                for row in conn.execute(
                    select(s.task_units).where(s.task_units.c.task_id == task["task_id"])
                ).mappings()
            }
            blocks = {block.block_id: block for block in revision.body.blocks}
            sources = {
                row["source_id"]: dict(row)
                for row in conn.execute(
                    select(s.task_sources).where(
                        s.task_sources.c.task_id == task["task_id"],
                        s.task_sources.c.excluded_at_scope_revision.is_(None),
                    )
                ).mappings()
            }
            leaves = [
                dict(row)
                for row in conn.execute(
                    select(s.task_source_leaves).where(
                        s.task_source_leaves.c.task_id == task["task_id"],
                        s.task_source_leaves.c.excluded_at_scope_revision.is_(None),
                    )
                ).mappings()
            ]
            historical = revision.revision_id != artifact.head_revision_id
            result = []
            spine = Spine(
                book_id=artifact_id,
                chapters=[
                    Chapter(id=c.chapter_id, title=c.title, order=i)
                    for i, c in enumerate(layout.chapters)
                ],
            )
            for chapter in layout.chapters:
                pages = []
                for item in sorted(
                    (p for p in layout.pages if p.chapter_id == chapter.chapter_id),
                    key=lambda p: p.order_key,
                ):
                    available = all(identity in blocks for identity in item.block_ids)
                    if any(
                        identity in blocks
                        and _hash(blocks[identity].model_dump(mode="json"))
                        != item.block_hashes[identity]
                        for identity in item.block_ids
                    ):
                        raise AppError("BOOK_BLOCK_HASH_MISMATCH", "书页内容与本版本布局摘要不符")
                    page = Page(
                        id=item.page_id,
                        book_id=artifact_id,
                        chapter_id=chapter.chapter_id,
                        title=item.title,
                        blocks=[
                            Block(
                                id=identity,
                                type=BlockType.QUIZ
                                if isinstance(blocks.get(identity), QuestionBlock)
                                else BlockType.INTERACTIVE
                                if isinstance(blocks.get(identity), VisualizationBlock)
                                else BlockType.TEXT,
                                status=BlockStatus.READY
                                if identity in blocks
                                else BlockStatus.ERROR,
                            )
                            for identity in item.block_ids
                        ],
                    )
                    finalize_terminal_page(page)
                    if spine.chapter_by_id(chapter.chapter_id) is None or not page.block_by_id(
                        item.block_ids[0]
                    ):
                        raise AppError("BOOK_IR_PROJECTION_INVALID", "书页与章节投影不一致")
                    pages.append(
                        BookPageView(layout=item, state="ready" if available else "content_removed")
                    )
                owned = [
                    u
                    for u in units.values()
                    if (u["work_spec"] or {}).get("book", {}).get("chapter_id")
                    == chapter.chapter_id
                    and u["excluded_at_scope_revision"] is None
                ]
                page_units = {p.layout.unit_id for p in pages}
                received = {}
                for row in conn.execute(
                    select(s.run_inputs)
                    .join(s.task_runs, s.task_runs.c.run_id == s.run_inputs.c.run_id)
                    .join(
                        s.task_units,
                        (s.task_units.c.task_id == s.task_runs.c.task_id)
                        & (s.task_units.c.unit_id == s.task_runs.c.unit_id)
                        & (s.task_units.c.run_id == s.task_runs.c.run_id),
                    )
                    .where(
                        s.task_units.c.task_id == task["task_id"],
                        s.task_units.c.unit_id.in_(page_units),
                        s.task_units.c.state == "completed",
                        s.task_units.c.result_artifact_id == artifact_id,
                    )
                ).mappings():
                    received.setdefault(row["leaf_id"], []).append(row)
                required = [
                    sources[identity] for identity in chapter.source_ids if identity in sources
                ]
                relevant = [
                    leaf
                    for leaf in leaves
                    if leaf["source_id"] in chapter.source_ids and leaf["source_id"] in sources
                ]
                full_source = all(
                    row["state"] == "ready" and row["expected_leaf_count"] is not None
                    for row in required
                ) and all(
                    TaskCoverage.leaf_processed(conn, leaf, received.get(leaf["leaf_id"], []))
                    for leaf in relevant
                )
                state = (
                    "ready"
                    if pages
                    and owned
                    and all(u["state"] == "completed" for u in owned)
                    and full_source
                    else "generating"
                )
                if any(row["state"] == "blocked" for row in required) or any(
                    leaf["state"] == "blocked" for leaf in relevant
                ):
                    state = "needs_sources"
                if chapter.source_ids and len(required) != len(chapter.source_ids):
                    state = "partial" if pages else "excluded"
                if state != "ready" and task["state"] in {"paused", "unknown", "needs_sources"}:
                    state = task["state"]
                if any(p.state == "content_removed" for p in pages):
                    state = "content_removed"
                result.append(BookChapterView(chapter=chapter, pages=pages, state=state))
            status = (
                BookStatus.READY
                if result
                and all(ch.state == "ready" for ch in result)
                and task["state"] == "completed"
                else BookStatus.PAUSED
                if historical
                or task["state"]
                in {
                    "paused",
                    "unknown",
                    "needs_sources",
                    "failed",
                    "cancelled",
                    "partial",
                    "completed",
                }
                else BookStatus.COMPILING
            )
            book = Book(id=artifact_id, title=revision.title, language="zh", status=status)
            return BookView(
                artifact_id=book.id,
                revision_id=revision.revision_id,
                title=book.title,
                layout_hash=_hash(layout.model_dump(mode="json")),
                origin_task_id=layout.origin_task_id,
                task_state=task["state"],
                book_status=book.status.value,
                is_historical=historical,
                chapters=result,
            )

    def update_directory(
        self, course_id, artifact_id, value: BookDirectoryUpdate
    ) -> ArtifactReceipt:
        payload = {"artifact_id": artifact_id, "input": value.model_dump(mode="json")}
        with self.repository._lock, self.repository.engine.begin() as conn:
            prior = prior_operation(
                conn, course_id, "book_directory_update", value.operation_id, payload
            )
            if prior is not None:
                return ArtifactReceipt.model_validate(prior)
            store = ArtifactStore(self.repository)
            current = store._view(conn, course_id, artifact_id)
            base = store._revision(conn, artifact_id, value.expected_revision_id)
            self.layout(conn, artifact_id, base.revision_id)
            result = store._revision_result(
                conn,
                current,
                base,
                base.body,
                base.title,
                "mixed" if base.content_role in {"ai_generated", "mixed"} else "user_note",
                book_context={"chapter_labels": value.chapters},
            )
            save_operation(
                conn, course_id, "book_directory_update", value.operation_id, payload, result
            )
            return result
