"""Research permissions do not leak into ordinary tasks or widen fixed URL scope."""

import pytest
from pydantic import ValidationError
from test_native_business import call, task_scope
from workbench_support import learning_course, settings

from learning_app.errors import AppError
from learning_app.execution.native_tools import ToolRejected
from learning_app.execution.types import ExecutionBinding
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.models import ArtifactBody
from learning_app.workbench.native_business import NativeBusiness
from learning_app.workbench.research_models import ResearchScope
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


@pytest.mark.parametrize("topics", [["One topic", " one  TOPIC "], [""], ["x" * 1001]])
def test_research_scope_rejects_duplicate_or_empty_topics(topics):
    with pytest.raises(ValidationError):
        ResearchScope(topics=topics)


def external_scope(tmp_path, *, urls):
    repo, course, _, source = learning_course(tmp_path)
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="external_only",
            kind="research",
            instruction="明确研究公开资料",
            coverage_mode="complete",
            sources=[],
            target={"mode": "create", "title": "公开研究"},
            settings=settings(),
            allow_external=True,
            research={"urls": urls},
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id)
    assert TaskPlanner(repo).plan(course, task.task_id)
    current = TaskStore(repo).get(course, task.task_id)
    run = TaskRunStore(repo).claim(
        course, task.task_id, current.units[0].unit_id, "synthetic scoped research"
    )
    binding = ExecutionBinding(
        task.task_id,
        "request_" + run.run_id,
        run.run_id,
        1,
        "sid_" + run.run_id,
        "request_" + run.run_id,
        native_floor=0,
        native_turn=1,
    )
    TaskRunStore(repo).persist_binding(run.run_id, binding, "synthetic_research_scope")
    port = NativeBusiness(repo)
    scope = port.ensure_task_scope(run.run_id, binding)
    TaskRunStore(repo).mark_dispatching(run.run_id)
    return repo, course, source, task, run, binding, port, scope


def test_empty_originals_permission_is_only_attested_explicit_external_research(tmp_path):
    repo, course, _, task, run, _, _, scope = external_scope(tmp_path, urls=[])
    assert {"search_research_web", "read_research_page"}.issubset(scope["allowed_tools"])
    private = TaskRunStore(repo).internal(run.run_id)
    assert (
        private["input_contract"]["inputs"] == []
        and private["input_contract"]["image_hashes"] == []
    )
    coverage = TaskCoverage(repo).get(course, task.task_id)
    assert (
        len(coverage.sources) == 1
        and coverage.sources[0].declaration["kind"] == "external_research"
    )
    assert not coverage.leaves
    repo.close()


@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_research_page", {"url": "https://other.example.net/"}),
        ("read_research_page", {"url": "http://127.0.0.1/private"}),
        ("read_research_page", {"url": "https://example.com/given", "headers": {"cookie": "no"}}),
        ("search_research_web", {"query": "must not broaden fixed URLs"}),
    ],
)
def test_fixed_external_urls_are_enforced_before_http(tmp_path, tool, args):
    repo, _, _, _, _, binding, port, scope = external_scope(
        tmp_path, urls=["https://example.com/given"]
    )
    assert "search_research_web" not in scope["allowed_tools"]
    with pytest.raises(ToolRejected):
        port.authorize(call(binding, tool, args), args)
    repo.close()


@pytest.mark.parametrize("kind", ["handout", "translation", "research"])
def test_ordinary_task_and_local_research_do_not_grant_external_reads(tmp_path, kind):
    repo, course, _, source = learning_course(tmp_path)
    _, _, binding, port, scope = task_scope(repo, course, source, kind=kind)
    assert not {"search_research_web", "read_research_page"} & set(scope["allowed_tools"])
    args = {"url": "https://example.com/public"}
    with pytest.raises(ToolRejected):
        port.authorize(call(binding, "read_research_page", args), args)
    repo.close()


def test_nonresearch_external_permission_and_unapproved_urls_are_invalid():
    common = dict(
        operation_id="invalid",
        instruction="scope",
        settings=settings(),
        target={"mode": "create", "title": "x"},
    )
    with pytest.raises(ValidationError):
        TaskAdmission(kind="handout", allow_external=True, **common)
    with pytest.raises(ValidationError):
        TaskAdmission(kind="research", research={"urls": ["https://example.com/"]}, **common)
    with pytest.raises(AppError):
        ResearchScope(urls=["file:///C:/private"])


def test_source_based_inference_is_distinct_from_quotes_and_unreferenced_supplements(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    task, run, _, _, _ = task_scope(repo, course, source, kind="research")
    body = ArtifactBody(
        blocks=[
            {
                "block_id": "inference",
                "markdown": "这是基于资料的比较推断，并非原文陈述。",
                "attribution": "source_inference",
                "source_snapshot_ids": [source.snapshot_id],
            }
        ]
    )
    private = TaskRunStore(repo).internal(run.run_id)
    with repo.engine.connect() as conn:
        row = TaskStore._row(conn, course, task.task_id)
        ArtifactStore.validate_run_body(conn, row, private, body)
        quote = ArtifactBody(
            blocks=[body.blocks[0].model_copy(update={"attribution": "source_quote"})]
        )
        with pytest.raises(AppError, match="实际文字"):
            ArtifactStore.validate_run_body(conn, row, private, quote)
        outside = ArtifactBody(
            blocks=[body.blocks[0].model_copy(update={"attribution": "ai_supplement"})]
        )
        with pytest.raises(AppError, match="只允许依据"):
            ArtifactStore.validate_run_body(conn, row, private, outside)
        foreign = ArtifactBody(
            blocks=[body.blocks[0].model_copy(update={"source_snapshot_ids": ["ungranted"]})]
        )
        with pytest.raises(AppError, match="范围外"):
            ArtifactStore.validate_run_body(conn, row, private, foreign)
    with pytest.raises(ValidationError):
        ArtifactBody(
            blocks=[
                {
                    "block_id": "unanchored",
                    "markdown": "No evidence",
                    "attribution": "source_inference",
                }
            ]
        )
    assert TaskRunStore(repo).get(course, task.task_id, run.run_id).actual_outbound_attempts == 0
    repo.close()


def test_planning_failure_with_existing_work_stays_failed_without_an_implicit_retry_loop(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    task, _, _, _, _ = task_scope(repo, course, source, kind="research", dispatch=False)
    TaskPlanner(repo)._fail_plan(course, task.task_id, "RESEARCH_SYNTHESIS_INPUT_LIMIT")
    current = TaskStore(repo).reconcile(course, task.task_id)
    assert current.state == "failed" and len(current.units) == 1
    assert not TaskPlanner(repo).plan(course, task.task_id)
    assert TaskStore(repo).reconcile(course, task.task_id).state == "failed"
    repo.close()
