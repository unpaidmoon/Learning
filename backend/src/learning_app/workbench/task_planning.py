"""Pack authorized leaves into finite generation work, retaining exact coverage."""

from sqlalchemy import insert, select

from learning_app.errors import AppError
from learning_app.storage.repository import _hash

from . import schema as s
from .sources import SourceStore
from .task_coverage import TaskCoverage
from .task_materials import TaskSourceInputError, material_size
from .tasks import TaskStore

MAX_WORK_CHARACTERS = 24_000
MAX_WORK_IMAGES = 6
MAX_WORK_IMAGE_BYTES = 14 * 1024 * 1024
MAX_WORK_INPUTS = 96
MAX_WORK_ENVELOPE_CHARACTERS = 96_000


class TaskPlanner:
    def __init__(self, repository):
        self.repository = repository
        self.sources = SourceStore(repository)

    def plan(self, course_id, task_id):
        with self.repository.engine.connect() as conn:
            task = TaskStore._row(conn, course_id, task_id)
            write_scope = conn.execute(
                select(s.task_targets.c.write_scope).where(s.task_targets.c.task_id == task_id)
            ).scalar_one_or_none()
        if write_scope and write_scope.get("range_edit"):
            return self._plan_regular(course_id, task_id)
        if task["kind"] == "book" and task["layout_version"] == 2:
            from .book_planning import BookPlanner

            return BookPlanner(self.repository, self).plan(course_id, task_id)
        if task["kind"] == "research" and task["layout_version"] == 2:
            from .research_synthesis import ResearchSynthesis

            if self._plan_regular(course_id, task_id):
                return True
            try:
                return ResearchSynthesis.plan(self.repository, course_id, task_id)
            except AppError as error:
                return self._fail_plan(course_id, task_id, error.code)
        return self._plan_regular(course_id, task_id)

    def _plan_regular(self, course_id, task_id):
        with self.repository.engine.connect() as conn:
            task = TaskStore._row(conn, course_id, task_id)
            if task["layout_version"] != 2 or task["state"] not in {
                "queued",
                "running",
                "needs_sources",
            }:
                return False
            if task["kind"] == "book" and not task["outline_confirmed"]:
                return False
            sources = list(
                conn.execute(
                    select(s.task_sources).where(
                        s.task_sources.c.task_id == task_id,
                        s.task_sources.c.excluded_at_scope_revision.is_(None),
                    )
                ).mappings()
            )
            if any(
                row["expected_leaf_count"] is None
                and row["state"] != "blocked"
                and row["declaration"]["kind"] != "external_research"
                for row in sources
            ):
                return False
            leaves = list(
                conn.execute(
                    select(s.task_source_leaves)
                    .join(s.task_sources)
                    .where(
                        s.task_source_leaves.c.task_id == task_id,
                        s.task_source_leaves.c.excluded_at_scope_revision.is_(None),
                        s.task_sources.c.excluded_at_scope_revision.is_(None),
                    )
                    .order_by(s.task_sources.c.ordinal, s.task_source_leaves.c.ordinal)
                ).mappings()
            )
            if any(row["state"] == "pending" for row in leaves):
                return False
            excluded_sources = {
                row["source_id"]
                for row in conn.execute(
                    select(s.task_sources).where(
                        s.task_sources.c.task_id == task_id,
                        s.task_sources.c.excluded_at_scope_revision.is_not(None),
                    )
                ).mappings()
            }
            works = list(
                conn.execute(
                    select(s.task_units)
                    .where(s.task_units.c.task_id == task_id)
                    .order_by(s.task_units.c.ordinal)
                ).mappings()
            )
            target = dict(
                conn.execute(select(s.task_targets).where(s.task_targets.c.task_id == task_id))
                .mappings()
                .one()
            )
            # Every prior plan is immutable. New work is only for a newly restored
            # source gap; an unknown/failed generation never becomes a new plan.
            assigned = {
                (
                    item["leaf_id"],
                    item["representation_snapshot_id"],
                    item["char_start"],
                    item["char_end"],
                )
                for work in works
                if work["excluded_at_scope_revision"] is None
                for item in (work["work_spec"] or {}).get("inputs", [])
            }
        try:
            range_scope = target["write_scope"].get("range_edit")
            if task["kind"] == "aligned_translation":
                from .aligned_translation import work_budget

                maximum = work_budget(task["settings"])
            elif range_scope:
                from .range_edits import RangeEdits

                maximum = RangeEdits.budget(task["settings"])
            else:
                maximum = MAX_WORK_CHARACTERS
            inputs = self.source_inputs(
                course_id,
                leaves,
                assigned,
                excluded_sources,
                chunker="aligned-paragraph-cp-v1"
                if range_scope
                else task["scope_spec"].get("source_chunker", "project-codepoint-v1"),
                maximum=maximum,
            )
        except AppError as error:
            return self._fail_plan(course_id, task_id, error.code)
        range_specs = {}
        if range_scope:
            # All supplementary evidence must be ready before a fixed edit
            # plan. A later gap repair never silently changes earlier patches.
            if any(source["state"] != "ready" for source in sources) or any(
                leaf["state"] not in {"ready", "empty"} for leaf in leaves
            ):
                return False
            target_sources = {
                source["source_id"]
                for source in sources
                if source["declaration"].get("target_field") == "markdown"
            }
            target_leaves = {
                leaf["leaf_id"]: leaf for leaf in leaves if leaf["source_id"] in target_sources
            }
            # Repeated bounded context is intentional; no input is silently
            # dropped to make a large edit fit. Oversized context keeps a gap.
            auxiliary = [item for item in inputs if item["leaf_id"] not in target_leaves]
            target_inputs = [item for item in inputs if item["leaf_id"] in target_leaves]
            if works:
                return False
            batches = []
            for item in target_inputs:
                packed = self._pack([item, *auxiliary], course_id)
                if isinstance(packed, str) or len(packed) != 1:
                    return self._fail_plan(course_id, task_id, "TASK_EDIT_CONTEXT_LIMIT")
                block_id = target_leaves[item["leaf_id"]]["locator"]["block_id"]
                batch = packed[0]
                range_specs[id(batch)] = {
                    "version": range_scope["version"],
                    "base_revision_id": range_scope["base_revision_id"],
                    "block_id": block_id,
                    "block_hash": range_scope["blocks"][block_id]["block_hash"],
                    "field": "markdown",
                    "start": item["char_start"],
                    "end": item["char_end"],
                    "snapshot_id": item["snapshot_id"],
                    "snapshot_hash": item["snapshot_hash"],
                }
                batches.append(batch)
        else:
            batches = self._pack(inputs, course_id, maximum=maximum)
        if isinstance(batches, str):
            return self._fail_plan(course_id, task_id, batches)
        # Explicit knowledge-only tasks still have one finite work item; they
        # are never claimed to have processed a missing or unknown source.
        if (
            not works
            and not sources
            and not batches
            and not task["scope_spec"]["sources"]
            and task["coverage_mode"] == "selection"
            and not task["sources_only"]
        ):
            batches = [[]]
        if not batches:
            from .research_external import ResearchExternal

            with self.repository.engine.connect() as conn:
                external = ResearchExternal.scope(conn, task)
            if (
                task["kind"] == "research"
                and external
                and not any(w["excluded_at_scope_revision"] is None for w in works)
            ):
                batches = [[]]
            else:
                return False
        if target["mode"] == "edit" and not range_scope and len(batches) > 1:
            # Replacing one target block independently for each source chunk
            # loses earlier chunks. Keep the target and draft intact instead of
            # reporting complete; large edits need disjoint patch scopes.
            return self._fail_plan(course_id, task_id, "TASK_EDIT_INPUT_LIMIT")
        if task["kind"] in {"answer_review", "essay_review"} and len(batches) > 1:
            return self._fail_plan(course_id, task_id, "ASSESSMENT_INPUT_LIMIT")
        with self.repository._lock, self.repository.engine.begin() as conn:
            current = TaskStore._row(conn, course_id, task_id)
            if current["revision"] != task["revision"] or current["state"] in {
                "paused",
                "unknown",
                "cancelled",
            }:
                return False
            revision = current["plan_revision"] + 1
            ordinal = max((work["ordinal"] for work in works), default=-1) + 1
            from .research import ResearchStore
            from .research_external import ResearchExternal

            plans = (
                ResearchStore.plans(task, batches, ResearchExternal.scope(conn, task))
                if task["kind"] == "research" and not range_scope
                else [(items, None) for items in batches]
            )
            for offset, (items, research) in enumerate(plans):
                identity = (
                    "work_"
                    + _hash(
                        {
                            "task": task_id,
                            "plan": revision,
                            "ordinal": ordinal + offset,
                            "inputs": items,
                            **({"research": research} if research else {}),
                        }
                    )[:32]
                )
                spec = {
                    "version": 2,
                    "plan_revision": revision,
                    "phase": "content",
                    "inputs": items,
                    "output_prefix": identity,
                    **({"research": research} if research else {}),
                    **({"edit_range": range_specs[id(items)]} if id(items) in range_specs else {}),
                }
                conn.execute(
                    insert(s.task_units).values(
                        task_id=task_id,
                        unit_id=identity,
                        ordinal=ordinal + offset,
                        label=research["title"][:200]
                        if research
                        else f"生成工作 {ordinal + offset + 1}",
                        source_snapshot_id=None,
                        work_spec=spec,
                        state="pending",
                    )
                )
            TaskStore._event(
                conn,
                current,
                "work_planned",
                {"plan_revision": revision, "work_count": len(plans)},
                plan_revision=revision,
                state="queued",
            )
        return True

    def source_inputs(
        self,
        course_id,
        leaves,
        assigned=(),
        excluded_sources=(),
        *,
        chunker="project-codepoint-v1",
        maximum=MAX_WORK_CHARACTERS,
    ):
        """Exact original intervals; chapter planners reuse the same material boundary."""
        from .aligned_translation import SOURCE_CHUNKER_VERSION, source_ranges
        from .content_processing import CHUNKER_VERSION, ContentProcessor

        if chunker not in {"project-codepoint-v1", CHUNKER_VERSION, SOURCE_CHUNKER_VERSION}:
            raise AppError(
                "TASK_SOURCE_CHUNKER_UNKNOWN", "该任务固定的切块版本尚未准备，未改变旧工作范围"
            )
        inputs = []
        for leaf in leaves:
            if leaf["source_id"] in excluded_sources or leaf["state"] != "ready":
                continue
            for representation in leaf["representations"]:
                parent = self.sources.snapshot(course_id, representation["snapshot_id"])
                if parent.content_hash != representation["content_hash"]:
                    raise AppError("TASK_SOURCE_SNAPSHOT_CONFLICT", "准备好的来源快照发生冲突")
                length = len(parent.text)
                image = representation["representation"] == "image"
                prior = sorted(
                    (start, end)
                    for identity, snapshot, start, end in assigned
                    if identity == leaf["leaf_id"] and snapshot == parent.snapshot_id
                )
                covered = 0
                for start, end in prior:
                    if start > covered:
                        break
                    covered = max(covered, end)
                if prior and covered >= length:
                    continue
                ranges = None
                method = None
                if not image and chunker == SOURCE_CHUNKER_VERSION:
                    ranges = source_ranges(parent.text, maximum)
                    method = SOURCE_CHUNKER_VERSION
                if (
                    ranges is None
                    and not image
                    and length > maximum
                    and parent.reference.kind == "subtitle"
                    and chunker == CHUNKER_VERSION
                ):
                    ranges = ContentProcessor().ranges(parent.text, maximum)
                    method = (
                        "deepstudent_line_hints"
                        if ranges is not None
                        else "exact_codepoint_fallback"
                    )
                if ranges is None:
                    ranges = (
                        [(0, 0)]
                        if image
                        else [
                            (start, min(start + maximum, length))
                            for start in range(0, length, maximum)
                        ]
                    )
                for start, end in ranges:
                    if (leaf["leaf_id"], parent.snapshot_id, start, end) in assigned:
                        continue
                    snapshot = (
                        parent
                        if start == 0 and end == length
                        else self.sources.slice(parent, start, end, owner_course_id=course_id)
                    )
                    inputs.append(
                        dict(
                            leaf_id=leaf["leaf_id"],
                            label=leaf["label"],
                            snapshot_id=snapshot.snapshot_id,
                            snapshot_hash=snapshot.content_hash,
                            content_role=snapshot.content_role,
                            representation=representation["representation"],
                            representation_snapshot_id=parent.snapshot_id,
                            char_start=start,
                            char_end=end,
                            **({"source_chunker": method} if method else {}),
                        )
                    )
        return inputs

    def _pack(self, inputs, course_id, *, maximum=MAX_WORK_CHARACTERS):
        # Keep a page's text and layout together. A long page's exact text
        # slices each retain that same authorized layout image as context.
        groups, facts = {}, {}
        for item in inputs:
            snapshot = self.sources.snapshot(course_id, item["snapshot_id"])
            image_bytes = (
                self.sources.image_path(snapshot.reference.image_sha256).stat().st_size
                if item["representation"] == "image"
                else 0
            )
            facts[id(item)] = (
                item["char_end"] - item["char_start"],
                int(item["representation"] == "image"),
                image_bytes,
                material_size(item, snapshot),
                1,
            )
            groups.setdefault(item["leaf_id"], []).append(item)

        def totals(items):
            return tuple(sum(facts[id(item)][i] for item in items) for i in range(5))

        limits = (
            maximum,
            MAX_WORK_IMAGES,
            MAX_WORK_IMAGE_BYTES,
            MAX_WORK_ENVELOPE_CHARACTERS,
            MAX_WORK_INPUTS,
        )

        def fits(items):
            return all(value <= limit for value, limit in zip(totals(items), limits, strict=True))

        bundles = []
        for group in groups.values():
            images = [item for item in group if item["representation"] == "image"]
            texts = [item for item in group if item["representation"] != "image"]
            part = []
            for item in texts:
                if part and not fits([*part, item, *images]):
                    bundles.append([*part, *images])
                    part = []
                part.append(item)
            if part or images:
                bundles.append([*part, *images])
        batches, current = [], []
        for bundle in bundles:
            if not fits(bundle):
                return "TASK_INPUT_REPRESENTATION_LIMIT"
            if current and not fits([*current, *bundle]):
                batches.append(current)
                current = []
            current.extend(bundle)
        if current:
            batches.append(current)
        return batches

    def _fail_plan(self, course_id, task_id, code):
        from sqlalchemy import update

        with self.repository._lock, self.repository.engine.begin() as conn:
            task = TaskStore._row(conn, course_id, task_id)
            if task["state"] not in {"queued", "running", "needs_sources"}:
                return False
            conn.execute(
                update(s.task_targets)
                .where(s.task_targets.c.task_id == task_id)
                .values(state="blocked", last_receipt={"error_code": code})
            )
            TaskStore._event(conn, task, "planning_failed", {"code": code}, state="failed")
        return False

    def inputs(self, course_id, task_id, unit_id):
        with self.repository.engine.connect() as conn:
            TaskStore._row(conn, course_id, task_id)
            row = (
                conn.execute(
                    select(s.task_units).where(
                        s.task_units.c.task_id == task_id, s.task_units.c.unit_id == unit_id
                    )
                )
                .mappings()
                .one()
            )
            spec = row["work_spec"]
        if not spec or spec["version"] != 2:
            raise AppError("TASK_WORK_SPEC_INVALID", "任务工作输入声明无效")
        result = []
        for item in spec["inputs"]:
            try:
                snapshot = self.sources.snapshot(course_id, item["snapshot_id"])
                if (
                    snapshot.content_hash != item["snapshot_hash"]
                    or snapshot.content_role != item["content_role"]
                ):
                    raise AppError("TASK_WORK_INPUT_CONFLICT", "任务冻结输入与来源不符")
                self.sources.require_current(snapshot, allow_historical_artifacts=True)
            except AppError as error:
                raise TaskSourceInputError(error, item["leaf_id"]) from error
            result.append((item, snapshot))
        return spec, result

    @staticmethod
    def state(conn, task, units):
        """Derive task status from both generation facts and full source coverage."""
        active = [unit for unit in units if unit.excluded_at_scope_revision is None]
        target_state = conn.execute(
            select(s.task_targets.c.state).where(s.task_targets.c.task_id == task["task_id"])
        ).scalar_one()
        if target_state == "blocked":
            return "failed"
        if any(unit.state == "unknown" for unit in active):
            return "unknown"
        if any(unit.state == "dispatching" for unit in active):
            return "running"
        if any(unit.state == "failed" for unit in active):
            if all(
                unit.error_code == "RESEARCH_EXTERNAL_GAPS"
                for unit in active
                if unit.state == "failed"
            ):
                return "needs_sources"
            return "failed"
        if any(unit.state in {"pending", "read"} for unit in active):
            return "queued"
        if task["kind"] == "book" and not task["outline_confirmed"]:
            book = (
                conn.execute(
                    select(s.task_targets.c.write_scope).where(
                        s.task_targets.c.task_id == task["task_id"]
                    )
                )
                .scalar_one()
                .get("book", {})
            )
            if book.get("proposal"):
                return "awaiting_outline"
        sources = list(
            conn.execute(
                select(s.task_sources).where(
                    s.task_sources.c.task_id == task["task_id"],
                    s.task_sources.c.excluded_at_scope_revision.is_(None),
                )
            ).mappings()
        )
        if not sources and task["scope_spec"]["sources"]:
            return "needs_sources"
        if any(row["state"] == "blocked" for row in sources):
            return "needs_sources"
        if any(row["state"] in {"declared", "preparing"} for row in sources):
            return "queued"
        leaves = list(
            conn.execute(
                select(s.task_source_leaves).where(
                    s.task_source_leaves.c.task_id == task["task_id"],
                    s.task_source_leaves.c.excluded_at_scope_revision.is_(None),
                    s.task_source_leaves.c.source_id.in_([row["source_id"] for row in sources]),
                )
            ).mappings()
        )
        if any(leaf["state"] == "blocked" for leaf in leaves):
            return "needs_sources"
        if task["kind"] == "book":
            specs = [
                row[0]
                for row in conn.execute(
                    select(s.task_units.c.work_spec).where(
                        s.task_units.c.task_id == task["task_id"],
                        s.task_units.c.excluded_at_scope_revision.is_(None),
                    )
                ).all()
                if (row[0] or {}).get("phase") == "content"
            ]
            if not specs:
                return (
                    "needs_sources"
                    if leaves and all(leaf["state"] == "empty" for leaf in leaves)
                    else "queued"
                )
            # A completed directory receipt is not a completed body. Newly
            # prepared source intervals must reach the chapter planner first;
            # missing dispatch proofs for already planned intervals stay unknown.
            planned = [item for spec in specs for item in spec["inputs"]]
            for leaf in leaves:
                if leaf["state"] != "ready":
                    continue
                for rep in leaf["representations"]:
                    matching = [
                        item
                        for item in planned
                        if item["leaf_id"] == leaf["leaf_id"]
                        and item["representation_snapshot_id"] == rep["snapshot_id"]
                    ]
                    if rep["representation"] == "image":
                        if not matching:
                            return "queued"
                    else:
                        covered = 0
                        for start, end in sorted(
                            (item["char_start"], item["char_end"]) for item in matching
                        ):
                            if start > covered:
                                break
                            covered = max(covered, end)
                        if covered < rep["text_length"]:
                            return "queued"
        completed = TaskCoverage.completed_inputs(conn, task["task_id"])
        if leaves and all(leaf["state"] == "empty" for leaf in leaves) and not active:
            return "needs_sources"
        if all(
            TaskCoverage.leaf_processed(conn, leaf, completed.get(leaf["leaf_id"], []))
            for leaf in leaves
        ) and (active or leaves):
            if task["kind"] == "research" and not conn.execute(
                select(s.task_targets.c.write_scope).where(
                    s.task_targets.c.task_id == task["task_id"]
                )
            ).scalar_one().get("range_edit"):
                from .research_synthesis import ResearchSynthesis

                if ResearchSynthesis.required(conn, task):
                    return "queued"
            return "completed"
        if active and all(unit.state == "completed" for unit in active):
            return "unknown"
        return "queued"
