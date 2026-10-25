"""Bounded research reduction using completed publications in the same task queue."""

import json

from sqlalchemy import func, insert, select

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from . import schema as s
from .artifacts import ArtifactStore
from .models import ArtifactBody
from .research import ResearchStore
from .runs import TaskRunStore
from .sources import SourceStore
from .tasks import TaskStore

MAX_NOTES_CHARACTERS = 32_000


class ResearchSynthesis:
    @staticmethod
    def citation_detail(conn, task, identity):
        snapshot = current = SourceStore.in_transaction(conn, task["course_id"], identity)
        offset, depth = 0, 0
        while current.reference.kind == "snapshot_slice":
            depth += 1
            if depth > 16:
                raise AppError("SOURCE_REFERENCE_DEPTH", "来源分段层次超出范围")
            ref = current.reference
            parent = SourceStore.in_transaction(conn, task["course_id"], ref.parent_snapshot_id)
            if (
                parent.content_hash != ref.parent_hash
                or current.text != parent.text[ref.start : ref.end]
            ):
                raise AppError("SOURCE_SLICE_CORRUPT", "分段与原冻结文字不符")
            offset += ref.start
            current = parent
        raw = current.reference.model_dump(mode="json")
        allowed = {
            "kind",
            "asset_id",
            "source_version",
            "source_digest",
            "page_index",
            "page_label",
            "parser_version",
            "artifact_id",
            "revision_id",
            "block_id",
            "message_id",
            "answer_id",
            "question_revision_id",
            "segment_id",
            "transcript_revision_id",
            "start_ms",
            "end_ms",
            "url",
            "final_url",
            "title",
            "retrieved_at",
            "response_sha256",
            "evidence_kind",
            "start",
            "end",
            "offset_unit",
        }
        reference = {key: value for key, value in raw.items() if key in allowed}
        if "parts" in raw:
            reference["parts"] = [
                {
                    key: value
                    for key, value in part.items()
                    if key in {"page_index", "page_label", "start", "end", "parser_version"}
                }
                for part in raw["parts"]
            ]
        group = {
            key: reference[key]
            for key in (
                "asset_id",
                "source_version",
                "source_digest",
                "artifact_id",
                "revision_id",
                "message_id",
                "answer_id",
                "transcript_revision_id",
                "url",
                "response_sha256",
                "evidence_kind",
            )
            if key in reference
        }
        if not group:
            group = {"root_snapshot_id": current.snapshot_id}
        label = conn.execute(
            select(s.task_source_leaves.c.label)
            .join(s.run_inputs, s.run_inputs.c.leaf_id == s.task_source_leaves.c.leaf_id)
            .join(s.task_runs, s.task_runs.c.run_id == s.run_inputs.c.run_id)
            .where(
                s.task_runs.c.task_id == task["task_id"],
                s.run_inputs.c.snapshot_id == identity,
            )
            .order_by(s.task_runs.c.created_at)
            .limit(1)
        ).scalar_one_or_none()
        return {
            "snapshot_id": identity,
            "content_role": snapshot.content_role,
            "source_group_id": "source_group_"
            + _hash({"course": task["course_id"], "role": snapshot.content_role, "source": group})[
                :32
            ],
            "source_label": (
                label.rsplit(" · ", 1)[0]
                if label
                else reference.get("title") or reference.get("url") or "已读来源"
            ),
            "reference": reference,
            "is_fragment": depth > 0,
            "range_in_frozen_source": {
                "start": offset,
                "end": offset + len(snapshot.text),
                "source_length": len(current.text),
            },
        }

    @staticmethod
    def units(conn, task_id):
        return [
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

    @classmethod
    def frontier(cls, units, topic_id):
        owned = [
            u
            for u in units
            if (u["work_spec"] or {}).get("research", {}).get("topic_id") == topic_id
        ]
        consumed = {
            n["unit_id"] for u in owned for n in u["work_spec"]["research"].get("notes", [])
        }
        return sorted(
            (u for u in owned if u["unit_id"] not in consumed),
            key=lambda u: (u["work_spec"]["research"].get("level", 0), u["ordinal"]),
        )

    @classmethod
    def required(cls, conn, task):
        if task["kind"] != "research":
            return False
        target = conn.execute(
            select(s.task_targets.c.mode).where(s.task_targets.c.task_id == task["task_id"])
        ).scalar_one()
        if target != "create":
            return False
        units = cls.units(conn, task["task_id"])
        return any(len(cls.frontier(units, t["topic_id"])) > 1 for t in ResearchStore.topics(task))

    @classmethod
    def note(cls, conn, task, unit, *, depth=0, allow_retired=False):
        if depth > 16 or unit["state"] != "completed" or not unit["run_id"]:
            raise AppError("RESEARCH_NOTES_NOT_SETTLED", "研究中间结果未完成，先核对原执行")
        run = TaskRunStore._row(conn, unit["run_id"])
        if run["state"] != "completed" or not TaskRunStore.input_proofs_settled(
            conn, run["run_id"]
        ):
            raise AppError("RESEARCH_NOTES_NOT_SETTLED", "研究中间执行的出站事实尚未对账")
        receipt = conn.execute(
            select(s.operations.c.result).where(
                s.operations.c.operation_id == "publish_" + run["run_id"],
                s.operations.c.course_id == task["course_id"],
                s.operations.c.kind == "artifact_create",
            )
        ).scalar_one_or_none()
        if not receipt or receipt["outcome"] != "applied":
            raise AppError("RESEARCH_NOTES_NOT_PUBLISHED", "原研究结果未正式发布")
        published = receipt["artifact"]
        if published["artifact_id"] != unit["result_artifact_id"]:
            raise AppError("RESEARCH_NOTES_PUBLICATION_CONFLICT", "研究笔记与原成果回执不符")
        revision = ArtifactStore._revision(
            conn, published["artifact_id"], published["revision"]["revision_id"]
        )
        if revision.task_id != task["task_id"] or revision.disposition != "published":
            raise AppError("RESEARCH_NOTES_PUBLICATION_CONFLICT", "研究笔记不属于本任务的发布修订")
        prefix = unit["work_spec"]["output_prefix"] + "_"
        body = ArtifactBody(
            blocks=[b for b in revision.body.blocks if b.block_id.startswith(prefix)]
        )
        context = unit["work_spec"]["research"]
        if context["phase"] == "synthesis":
            cls.validate(
                conn,
                task,
                context,
                before_ordinal=unit["ordinal"],
                depth=depth + 1,
                allow_retired=allow_retired,
            )
            candidates = set(context["citation_sources"])
            groups = context["citation_groups"]
        else:
            prepared = [
                row
                for row in conn.execute(
                    select(s.run_inputs).where(
                        s.run_inputs.c.run_id == run["run_id"],
                    )
                ).mappings()
                if (row["proof"] or {}).get("state") in {"actual_outbound", "fixture_dispatch"}
            ]
            candidates = {row["snapshot_id"] for row in prepared}
            by_leaf = {}
            for row in prepared:
                by_leaf.setdefault(row["leaf_id"], set()).add(row["snapshot_id"])
            groups = [sorted(values) for values in by_leaf.values()]
            from .research_external import ResearchExternal

            external = ResearchExternal.cited_sources(conn, run["run_id"])
            candidates.update(external)
            actual_cited = {i for block in body.blocks for i in block.source_snapshot_ids}
            groups.extend([[identity] for identity in sorted(external & actual_cited)])
        cited = {identity for block in body.blocks for identity in block.source_snapshot_ids}
        citations = sorted(candidates & cited)
        if not citations or any(not set(group) & set(citations) for group in groups):
            raise AppError("RESEARCH_NOTES_EVIDENCE_MISSING", "研究笔记缺少实际已读证据")
        return {
            "unit_id": unit["unit_id"],
            "run_id": run["run_id"],
            "revision_id": revision.revision_id,
            "content_role": "ai_generated",
            "body": body.model_dump(mode="json"),
            "body_hash": _hash(body.model_dump(mode="json")),
            "citation_sources": citations,
            "citation_groups": [sorted(set(group) & set(citations)) for group in groups],
            "citation_details": [
                cls.citation_detail(conn, task, identity) for identity in citations
            ],
        }

    @classmethod
    def validate(
        cls,
        conn,
        task,
        context,
        *,
        before_ordinal=None,
        depth=0,
        require_current_scope=False,
        allow_retired=False,
    ):
        if context.get("phase") != "synthesis" or len(context.get("notes", [])) != 2:
            raise AppError("RESEARCH_SYNTHESIS_SCOPE_INVALID", "归纳须绑定两份已发布研究结果")
        if require_current_scope and context.get("notes_version", 1) >= 2:
            cls.require_complete_scope(conn, task, context)
        notes = []
        for expected in context["notes"]:
            row = (
                conn.execute(
                    select(s.task_units).where(
                        s.task_units.c.task_id == task["task_id"],
                        s.task_units.c.unit_id == expected["unit_id"],
                        *(
                            []
                            if allow_retired
                            else [s.task_units.c.excluded_at_scope_revision.is_(None)]
                        ),
                    )
                )
                .mappings()
                .first()
            )
            if (
                not row
                or (before_ordinal is not None and row["ordinal"] >= before_ordinal)
                or (row["work_spec"] or {}).get("research", {}).get("topic_id")
                != context["topic_id"]
            ):
                raise AppError(
                    "RESEARCH_NOTES_SCOPE_DENIED", "中间结果不属于本任务较早的同主题工作", 403
                )
            actual = cls.note(conn, task, row, depth=depth, allow_retired=allow_retired)
            if context.get("notes_version", 1) < 2:
                actual = {key: value for key, value in actual.items() if key != "citation_details"}
            notes.append(actual)
        if notes != context["notes"] or len({n["unit_id"] for n in notes}) != 2:
            raise AppError("RESEARCH_NOTES_CHANGED", "归纳依赖及原发布结果不能替换")
        citations = sorted({identity for n in notes for identity in n["citation_sources"]})
        groups = [group for note in notes for group in note["citation_groups"]]
        if citations != context.get("citation_sources") or groups != context.get("citation_groups"):
            raise AppError("RESEARCH_CITATION_SCOPE_DENIED", "归纳引用超出原任务实际证据闭包", 403)
        if len(json.dumps(notes, ensure_ascii=False)) > MAX_NOTES_CHARACTERS:
            raise AppError(
                "RESEARCH_SYNTHESIS_INPUT_LIMIT", "已保存研究笔记超过本次归纳上限，原稿保留"
            )
        return citations

    @staticmethod
    def require_complete_scope(conn, task, context):
        expected = {"authorized_scope_complete": True, "scope_revision": task["scope_revision"]}
        if context.get("coverage_fact") != expected:
            raise AppError("RESEARCH_SYNTHESIS_SCOPE_CHANGED", "归纳范围已变化，原结果保留")
        from .task_coverage import TaskCoverage

        sources = list(
            conn.execute(
                select(s.task_sources).where(
                    s.task_sources.c.task_id == task["task_id"],
                    s.task_sources.c.excluded_at_scope_revision.is_(None),
                )
            ).mappings()
        )
        completed = TaskCoverage.completed_inputs(conn, task["task_id"])
        leaves = list(
            conn.execute(
                select(s.task_source_leaves).where(
                    s.task_source_leaves.c.task_id == task["task_id"],
                    s.task_source_leaves.c.source_id.in_(
                        [source["source_id"] for source in sources]
                    ),
                    s.task_source_leaves.c.excluded_at_scope_revision.is_(None),
                )
            ).mappings()
        )
        if any(source["state"] != "ready" for source in sources) or not all(
            TaskCoverage.leaf_processed(conn, leaf, completed.get(leaf["leaf_id"], []))
            for leaf in leaves
        ):
            raise AppError(
                "RESEARCH_SYNTHESIS_COVERAGE_NOT_READY", "指定范围尚未完整读入，不能归纳为已完成"
            )

    @classmethod
    def validate_output(cls, conn, task, run, body):
        context = (run["input_contract"] or {}).get("research", {})
        if context.get("phase") != "synthesis":
            return set()
        citations = cls.validate(conn, task, context, require_current_scope=True)
        cited = {identity for block in body.blocks for identity in block.source_snapshot_ids}
        if any(not set(group) & cited for group in context["citation_groups"]):
            raise AppError(
                "RESEARCH_SYNTHESIS_COVERAGE_INVALID", "归纳遗漏了已完成资料组，原结果保留"
            )
        return set(citations)

    @classmethod
    def plan(cls, repository, course_id, task_id):
        with repository._lock, repository.engine.begin() as conn:
            task = TaskStore._row(conn, course_id, task_id)
            if task["state"] not in {"queued", "running"} or not cls.required(conn, task):
                return False
            units = cls.units(conn, task_id)
            if not units or any(u["state"] != "completed" for u in units):
                return False
            from .task_coverage import TaskCoverage

            completed = TaskCoverage.completed_inputs(conn, task_id)
            sources = list(
                conn.execute(
                    select(s.task_sources).where(
                        s.task_sources.c.task_id == task_id,
                        s.task_sources.c.excluded_at_scope_revision.is_(None),
                    )
                ).mappings()
            )
            if any(source["state"] != "ready" for source in sources):
                return False
            leaves = list(
                conn.execute(
                    select(s.task_source_leaves).where(
                        s.task_source_leaves.c.task_id == task_id,
                        s.task_source_leaves.c.source_id.in_(
                            [source["source_id"] for source in sources]
                        ),
                        s.task_source_leaves.c.excluded_at_scope_revision.is_(None),
                    )
                ).mappings()
            )
            if not all(
                TaskCoverage.leaf_processed(conn, leaf, completed.get(leaf["leaf_id"], []))
                for leaf in leaves
            ):
                return False
            plans = []
            for topic in ResearchStore.topics(task):
                frontier = cls.frontier(units, topic["topic_id"])
                if len(frontier) < 2:
                    continue
                notes = [cls.note(conn, task, u) for u in frontier[:2]]
                context = {
                    **topic,
                    "phase": "synthesis",
                    "notes_version": 2,
                    "coverage_fact": {
                        "authorized_scope_complete": True,
                        "scope_revision": task["scope_revision"],
                    },
                    "notes": notes,
                    "level": max(u["work_spec"]["research"].get("level", 0) for u in frontier[:2])
                    + 1,
                    "citation_sources": sorted({i for n in notes for i in n["citation_sources"]}),
                    "citation_groups": [g for n in notes for g in n["citation_groups"]],
                }
                cls.validate(conn, task, context, require_current_scope=True)
                plans.append(context)
            revision = task["plan_revision"] + 1
            last_ordinal = conn.execute(
                select(func.max(s.task_units.c.ordinal)).where(s.task_units.c.task_id == task_id)
            ).scalar_one()
            start = (last_ordinal if last_ordinal is not None else -1) + 1
            for offset, context in enumerate(plans):
                identity = "work_" + _hash({"task": task_id, "research": context})[:32]
                conn.execute(
                    insert(s.task_units).values(
                        task_id=task_id,
                        unit_id=identity,
                        ordinal=start + offset,
                        label="归纳 · " + context["title"][:180],
                        state="pending",
                        source_snapshot_id=None,
                        work_spec={
                            "version": 2,
                            "plan_revision": revision,
                            "phase": "content",
                            "inputs": [],
                            "output_prefix": identity,
                            "research": context,
                        },
                    )
                )
            if plans:
                TaskStore._event(
                    conn,
                    task,
                    "research_synthesis_planned",
                    {"work_count": len(plans)},
                    plan_revision=revision,
                    state="queued",
                )
            return bool(plans)
