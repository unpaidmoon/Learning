"""Deterministic outline/source authority and incremental exact source planning."""

import os
from copy import deepcopy

import pytest
from sqlalchemy import select, update
from workbench_support import learning_course, settings

from learning_app.course_files import CourseFileStore
from learning_app.errors import AppError
from learning_app.workbench import schema as s
from learning_app.workbench.book_models import BookOutlineDraft
from learning_app.workbench.books import BookStore
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_controls import TaskRecovery
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import SourceGapDecision, TaskAdmission
from learning_app.workbench.tasks import TaskStore


def setup(tmp_path, *, long_first=False, missing_first=False, empty_first=False):
    repo, course, root, source = learning_course(tmp_path)
    files = CourseFileStore(repo)
    if empty_first:
        (root / "lesson.txt").write_text("  \n", "utf-8")
    if long_first:
        (root / "long.txt").write_text("Source interval must remain exact. " * 1800, "utf-8")
        first = files.open(course, "long.txt")
    else:
        first = files.open(course, "lesson.txt")
    (root / "second.txt").write_text("第二份合成资料，保留不同来源。", "utf-8")
    second = files.open(course, "second.txt")
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="book",
            kind="book",
            instruction="完整组织两份指定资料",
            coverage_mode="complete",
            sources=[
                {
                    "kind": "document",
                    "asset_id": item["asset_id"],
                    "source_version": item["source_version"],
                }
                for item in (first, second)
            ],
            target={"mode": "create", "title": "书"},
            settings=settings(),
            sources_only=True,
        ),
    )
    path = root / ("long.txt" if long_first else "lesson.txt")
    original, stat = path.read_bytes(), path.stat()
    if missing_first:
        path.unlink()  # Self-created file below this test's fresh tmp_path only.
    TaskCoverage(repo).refresh(course, task.task_id)
    return repo, course, task.task_id, path, original, stat


def seeded_confirmation(repo, course, task_id):
    """Synthetic control receipt to isolate local planning, not a real model claim."""
    with repo._lock, repo.engine.begin() as conn:
        task = TaskStore._row(conn, course, task_id)
        sources = BookStore.catalog(conn, task_id)
        draft = BookOutlineDraft(
            chapters=[
                {"chapter_id": f"part_{i}", "title": f"章节{i}", "source_ids": [item.source_id]}
                for i, item in enumerate(sources)
            ]
        )
        from learning_app.storage.repository import _hash

        BookStore._write_metadata(
            conn,
            BookStore.target(conn, task_id),
            {
                "confirmed": True,
                "confirmation_count": 1,
                "confirmed_outline": draft.model_dump(mode="json"),
                "outline_hash": _hash(draft.model_dump(mode="json")),
            },
        )
        TaskStore._event(
            conn, task, "synthetic_outline_fixture", {}, outline_confirmed=True, state="queued"
        )


def test_metadata_only_empty_input_permission_is_exactly_book_outline(tmp_path):
    repo, course, task_id, _, _, _ = setup(tmp_path)
    assert TaskPlanner(repo).plan(course, task_id)
    unit = TaskStore(repo).get(course, task_id).units[0]
    run = TaskRunStore(repo).claim(course, task_id, unit.unit_id, "exact frozen planning fixture")
    actual = TaskRunStore(repo).internal(run.run_id)
    assert (
        actual["input_contract"]["phase"] == "outline" and actual["input_contract"]["inputs"] == []
    )
    assert len(actual["input_contract"]["book_context"]["sources"]) == 2
    with repo._lock, repo.engine.begin() as conn:
        spec = deepcopy(
            conn.execute(
                select(s.task_units.c.work_spec).where(s.task_units.c.unit_id == unit.unit_id)
            ).scalar_one()
        )
        spec["book_context"]["sources"].pop()
        conn.execute(
            update(s.task_units)
            .where(s.task_units.c.unit_id == unit.unit_id)
            .values(work_spec=spec)
        )
    with pytest.raises(AppError, match="目录规划与原资料范围不符"):
        TaskRunStore(repo).claim(course, task_id, unit.unit_id, "exact frozen planning fixture")
    assert TaskRunStore(repo).get(course, task_id, run.run_id).actual_outbound_attempts == 0


@pytest.mark.parametrize("change", ["omit", "foreign", "empty_source", "change_supplied"])
def test_proposal_cannot_drop_sources_expand_scope_or_rewrite_supplied_outline(change):
    context = {
        "sources": [{"source_id": "s1"}, {"source_id": "s2"}],
        "sources_only": True,
        "supplied_outline": [],
    }
    raw = {
        "chapters": [
            {"chapter_id": "one", "title": "一", "objective": "", "source_ids": ["s1"]},
            {"chapter_id": "two", "title": "二", "objective": "", "source_ids": ["s2"]},
        ]
    }
    if change == "omit":
        raw["chapters"].pop()
    elif change == "foreign":
        raw["chapters"][0]["source_ids"].append("another_course")
    elif change == "empty_source":
        raw["chapters"].append({"chapter_id": "extra", "title": "无依据", "source_ids": []})
    else:
        context["supplied_outline"] = [
            {"chapter_id": "one", "title": "原指定目录", "objective": ""}
        ]
    with pytest.raises(AppError):
        BookStore.validate_proposal(context, BookOutlineDraft.model_validate(raw))


def test_missing_source_retry_adds_original_chapter_work_without_replanning_existing_pages(
    tmp_path,
):
    repo, course, task_id, path, original, stat = setup(
        tmp_path, long_first=True, missing_first=True
    )
    seeded_confirmation(repo, course, task_id)
    assert TaskPlanner(repo).plan(course, task_id)
    first = TaskStore(repo).get(course, task_id)
    assert len(first.units) == 1
    with repo.engine.connect() as conn:
        spec = conn.execute(select(s.task_units.c.work_spec)).scalar_one()
        assert spec["book"]["chapter_id"] == "part_1"
        before_hash = BookStore.target(conn, task_id)["write_scope"]["book"]["outline_hash"]
    missing = next(
        s for s in TaskCoverage(repo).get(course, task_id).sources if s.state == "blocked"
    )
    path.write_bytes(original)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    TaskRecovery(repo).source_gap(
        course,
        task_id,
        SourceGapDecision(
            operation_id="retry_missing",
            expected_revision=first.revision,
            decision="retry",
            source_ids=[missing.source_id],
        ),
    )
    TaskCoverage(repo).refresh(course, task_id)
    assert TaskPlanner(repo).plan(course, task_id)
    with repo.engine.connect() as conn:
        rows = list(conn.execute(select(s.task_units).order_by(s.task_units.c.ordinal)).mappings())
        assert rows[0]["unit_id"] == first.units[0].unit_id and len(rows) == 4
        restored = [r["work_spec"] for r in rows[1:]]
        assert all(
            spec["book"]["chapter_id"] == "part_0" and spec["book"]["order_key"][0] == 0
            for spec in restored
        )
        assert BookStore.target(conn, task_id)["write_scope"]["book"]["outline_hash"] == before_hash
        intervals = sorted(
            (item["char_start"], item["char_end"]) for spec in restored for item in spec["inputs"]
        )
        assert intervals[0][0] == 0 and intervals[-1][1] == len(original.decode("utf-8"))
        assert all(a[1] == b[0] for a, b in zip(intervals, intervals[1:]))
    assert not TaskPlanner(repo).plan(course, task_id)


def test_empty_book_source_is_an_actionable_gap_and_exclusion_preserves_scope_receipt(tmp_path):
    repo, course, task_id, _, _, _ = setup(tmp_path, empty_first=True)
    seeded_confirmation(repo, course, task_id)
    assert TaskPlanner(repo).plan(course, task_id)  # Only the second source can generate.
    coverage = TaskCoverage(repo).get(course, task_id)
    gap = next(s for s in coverage.sources if s.error_code == "TASK_SOURCE_EMPTY")
    assert gap.state == "blocked" and gap.expected_leaf_count == 1
    empty = next(leaf for leaf in coverage.leaves if leaf.source_id == gap.source_id)
    assert empty.state == "empty" and empty.locator["empty_proof"]["local_empty_text"]
    current = TaskStore(repo).get(course, task_id)
    excluded = TaskRecovery(repo).source_gap(
        course,
        task_id,
        SourceGapDecision(
            operation_id="exclude_empty",
            expected_revision=current.revision,
            decision="exclude",
            source_ids=[gap.source_id],
        ),
    )
    assert excluded.scope_revision == 1
    remaining = TaskCoverage(repo).get(course, task_id)
    assert (
        next(
            s for s in remaining.sources if s.source_id == gap.source_id
        ).excluded_at_scope_revision
        == 1
    )
    assert len(remaining.sources) == 2 and len(remaining.leaves) == 2
    assert not TaskPlanner(repo).plan(course, task_id)


def test_course_book_waits_for_complete_metadata_inventory_and_omits_container(tmp_path):
    repo, course, root, _ = learning_course(tmp_path)
    for index in range(3):
        (root / f"part-{index}.txt").write_text(f"独立来源 {index}", "utf-8")
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="whole_course_book",
            kind="book",
            instruction="整理整课为交互书",
            coverage_mode="complete",
            sources=[{"kind": "course", "include_generated": False}],
            target={"mode": "create", "title": "整课书"},
            settings=settings(),
            sources_only=True,
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id, budget=1)
    assert not TaskPlanner(repo).plan(course, task.task_id)
    TaskCoverage(repo).refresh(course, task.task_id, budget=100)
    assert TaskPlanner(repo).plan(course, task.task_id)
    with repo.engine.connect() as conn:
        context = conn.execute(select(s.task_units.c.work_spec)).scalar_one()["book_context"]
        assert len(context["sources"]) == 4
        assert all(item["kind"] == "course_member" for item in context["sources"])
        root_source = (
            conn.execute(select(s.task_sources).where(s.task_sources.c.ordinal == 0))
            .mappings()
            .one()
        )
        assert root_source["state"] == "ready"
        assert root_source["source_id"] not in {item["source_id"] for item in context["sources"]}
