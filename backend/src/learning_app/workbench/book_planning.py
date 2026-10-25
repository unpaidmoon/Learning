"""Book phases and chapter grouping in the existing finite generation queue."""

from sqlalchemy import insert, select

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from . import schema as s
from .books import BOOK_PHASE, BookStore
from .tasks import TaskStore


class BookPlanner:
    def __init__(self, repository, shared):
        self.repository, self.shared = repository, shared

    def plan(self, course_id, task_id):
        with self.repository.engine.connect() as conn:
            task = TaskStore._row(conn, course_id, task_id)
            target = BookStore.target(conn, task_id)
            if task["state"] not in {"queued", "running", "needs_sources"}:
                return False
            if target["mode"] == "edit":
                conn.close()
                return self.shared._plan_regular(course_id, task_id)
            metadata = target["write_scope"].get("book", {})
            works = [
                dict(row)
                for row in conn.execute(
                    select(s.task_units)
                    .where(
                        s.task_units.c.task_id == task_id,
                        s.task_units.c.excluded_at_scope_revision.is_(None),
                    )
                    .order_by(s.task_units.c.ordinal)
                ).mappings()
            ]
            if not metadata.get("confirmed"):
                inventory = list(
                    conn.execute(
                        select(s.task_sources).where(
                            s.task_sources.c.task_id == task_id,
                            s.task_sources.c.excluded_at_scope_revision.is_(None),
                        )
                    ).mappings()
                )
                # The course container expands to concrete members in bounded
                # preparation ticks. Freeze the complete inventory first.
                if any(
                    row["expected_leaf_count"] is None and row["state"] != "blocked"
                    for row in inventory
                ):
                    return False
                if metadata.get("proposal") or any(
                    (w["work_spec"] or {}).get("phase") == BOOK_PHASE for w in works
                ):
                    return False
                try:
                    context = BookStore.planning_context(conn, task)
                    if not context["sources"] and task["scope_spec"]["sources"]:
                        return False
                except AppError as e:
                    conn.close()
                    return self.shared._fail_plan(course_id, task_id, e.code)
                plans = [{"version": 2, "phase": BOOK_PHASE, "inputs": [], "book_context": context}]
            else:
                sources = [
                    dict(row)
                    for row in conn.execute(
                        select(s.task_sources).where(
                            s.task_sources.c.task_id == task_id,
                            s.task_sources.c.excluded_at_scope_revision.is_(None),
                        )
                    ).mappings()
                ]
                leaves = [
                    dict(row)
                    for row in conn.execute(
                        select(s.task_source_leaves).where(
                            s.task_source_leaves.c.task_id == task_id,
                            s.task_source_leaves.c.source_id.in_([r["source_id"] for r in sources]),
                            s.task_source_leaves.c.excluded_at_scope_revision.is_(None),
                        )
                    ).mappings()
                ]
                order = {source["source_id"]: source["ordinal"] for source in sources}
                leaves.sort(key=lambda leaf: (order[leaf["source_id"]], leaf["ordinal"]))
                by_leaf = {leaf["leaf_id"]: leaf for leaf in leaves}
                # Repository connections use BEGIN IMMEDIATE, including reads.
                # Material/slice helpers open their own transactions.
                conn.close()
                plans = []
                for chapter in metadata["confirmed_outline"]["chapters"]:
                    prior = [
                        w
                        for w in works
                        if (w["work_spec"] or {}).get("phase") == "content"
                        and (w["work_spec"].get("book") or {}).get("chapter_id")
                        == chapter["chapter_id"]
                    ]
                    assigned = {
                        (
                            i["leaf_id"],
                            i["representation_snapshot_id"],
                            i["char_start"],
                            i["char_end"],
                        )
                        for w in prior
                        for i in w["work_spec"]["inputs"]
                    }
                    owned = [leaf for leaf in leaves if leaf["source_id"] in chapter["source_ids"]]
                    inputs = self.shared.source_inputs(
                        course_id,
                        owned,
                        assigned,
                        chunker=task["scope_spec"].get("source_chunker", "project-codepoint-v1"),
                    )
                    batches = self.shared._pack(inputs, course_id)
                    if isinstance(batches, str):
                        return self.shared._fail_plan(course_id, task_id, batches)
                    if not chapter["source_ids"] and not prior and not task["sources_only"]:
                        batches = [[]]
                    for items in batches:
                        key = min(
                            (
                                (
                                    order[by_leaf[i["leaf_id"]]["source_id"]],
                                    by_leaf[i["leaf_id"]]["ordinal"],
                                    i["char_start"],
                                )
                                for i in items
                            ),
                            default=(0, 0, 0),
                        )
                        page_id = (
                            "page_"
                            + _hash(
                                {"task": task_id, "chapter": chapter["chapter_id"], "inputs": items}
                            )[:32]
                        )
                        plans.append(
                            {
                                "version": 2,
                                "phase": "content",
                                "inputs": items,
                                "book": {
                                    "chapter_id": chapter["chapter_id"],
                                    "page_id": page_id,
                                    "title": "学习内容",
                                    "outline_hash": metadata["outline_hash"],
                                    "order_key": list(key),
                                },
                            }
                        )
        if not plans:
            return False
        with self.repository._lock, self.repository.engine.begin() as conn:
            current = TaskStore._row(conn, course_id, task_id)
            if current["revision"] != task["revision"] or current["state"] in {
                "paused",
                "cancelled",
                "unknown",
            }:
                return False
            revision = current["plan_revision"] + 1
            ordinal = max((w["ordinal"] for w in works), default=-1) + 1
            for index, plan in enumerate(plans):
                identity = (
                    "work_"
                    + _hash(
                        {
                            "task": task_id,
                            "phase": plan["phase"],
                            "book": plan.get("book"),
                            "context": plan.get("book_context"),
                            "inputs": plan["inputs"],
                        }
                    )[:32]
                )
                spec = {**plan, "plan_revision": revision, "output_prefix": identity}
                conn.execute(
                    insert(s.task_units).values(
                        task_id=task_id,
                        unit_id=identity,
                        ordinal=ordinal + index,
                        label="拟定交互书目录"
                        if plan["phase"] == BOOK_PHASE
                        else f"交互书内容 {ordinal + index}",
                        source_snapshot_id=None,
                        work_spec=spec,
                        state="pending",
                    )
                )
            TaskStore._event(
                conn,
                current,
                "book_work_planned",
                {"plan_revision": revision, "work_count": len(plans)},
                plan_revision=revision,
                state="queued",
            )
        return True
