"""Pinned Host external permissions/receipts; HTTP is deterministic, never live."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select
from test_native_business_host import runtime
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.errors import AppError
from learning_app.execution.native_tools import NativeCall
from learning_app.service import GenerationService
from learning_app.workbench import research_schema as r
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.external_http import PublicTextReader
from learning_app.workbench.models import TaskAction
from learning_app.workbench.research import ResearchStore
from learning_app.workbench.research_external import ResearchExternal
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_controls import TaskRecovery
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import SourceGapDecision, TaskAdmission
from learning_app.workbench.tasks import TaskStore


class ExternalFlow:
    def __init__(self, tmp_path, repo, source, *, fail=False, cite_unread=False, repeat_read=False):
        self.repo, self.fail, self.cite_unread = repo, fail, cite_unread
        self.repeat_read = repeat_read
        self.http, self.steps = [], {}
        self.adapter, self.port, self.registry, self.boundary = runtime(
            tmp_path, repo, source, bare=True
        )
        self.original = self.boundary._fixture
        self.boundary._fixture = self.fixture
        self.port.research_external.reader = PublicTextReader(
            resolver=lambda *_: ["93.184.216.34"],
            transport=httpx.MockTransport(self.page),
        )

    def page(self, request):
        self.http.append(request)
        if self.fail:
            return httpx.Response(503)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            stream=httpx.ByteStream(
                (
                    "<title>外部合成文字</title><p>公开资料条件与来源必须分别保留。</p>"
                    + "<p>继续核对公式和单位。</p>" * 4000
                ).encode("utf-8")
            ),
        )

    def fixture(self, handler, request, scenario, row, observer):
        run = TaskRunStore(self.repo).internal(row["generation_id"])
        context = run["input_contract"]["research"]
        step = self.steps[run["run_id"]] = self.steps.get(run["run_id"], 0) + 1
        actual = []
        calls = []
        if context.get("external"):
            assert "search_research_web" not in run["native_allowed_tools"]
            assert "read_research_page" in run["native_allowed_tools"]
            with self.repo.engine.connect() as conn:
                prior = list(
                    conn.execute(
                        select(r.external_reads).where(r.external_reads.c.run_id == run["run_id"])
                    ).mappings()
                )
                actual = sorted(ResearchExternal.cited_sources(conn, run["run_id"]))
                continuations = [
                    h
                    for entry in prior
                    if entry["result"]
                    for h in entry["result"].get("unread_continuations", [])
                ]
            if step == 1:
                calls = [
                    {
                        "id": "read_public_page",
                        "name": "read_research_page",
                        "input": {"url": context["external"]["urls"][0]},
                    }
                ]
            elif self.repeat_read and step == 2:
                calls = [
                    {
                        "id": "same_saved_page_again",
                        "name": "read_research_page",
                        "input": {"url": context["external"]["urls"][0]},
                    }
                ]
            elif not self.cite_unread:
                unread = [h for h in continuations if h["handle_id"] not in actual]
                if unread:
                    calls = [
                        {
                            "id": "read_continuation",
                            "name": "read_source_handle",
                            "input": {
                                key: unread[0][key] for key in ("handle_id", "source_version")
                            },
                        }
                    ]
            elif continuations:
                actual.append(continuations[0]["handle_id"])
        else:
            assert "read_research_page" not in run["native_allowed_tools"]
        if calls:
            fixture = {"text_chunks": [], "tool_calls": calls}
        else:
            ids = [i["snapshot_id"] for i in run["input_contract"]["inputs"]] + actual
            fixture = {
                "text_chunks": [
                    json.dumps(
                        {
                            "blocks": [
                                {
                                    "block_id": "comparison",
                                    "kind": "markdown",
                                    "markdown": "本地与外部资料均须保留条件和单位；未读部分不能作证据。",
                                    "attribution": "source_paraphrase",
                                    "source_snapshot_ids": ids,
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                ]
            }
        return self.original(handler, request, fixture, row, observer)


def admit(repo, course, source, *, bad=False):
    return TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="external_research",
            kind="research",
            instruction="只比较所选资料和这个指定网页，完整保留依据。",
            coverage_mode="selection" if bad else "complete",
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "外部对照报告"},
            settings=settings(),
            allow_external=True,
            research={"urls": ["https://example.com/evidence"]},
        ),
    )


@pytest.mark.host
@pytest.mark.parametrize("bad,repeat_read", [(False, False), (True, False), (False, True)])
async def test_external_text_chunks_need_actual_read_receipts_and_do_not_expand_native_network(
    tmp_path, bad, repeat_read
):
    repo, course, _, source = learning_course(tmp_path)
    task = admit(repo, course, source, bad=bad)
    flow = ExternalFlow(tmp_path, repo, source, cite_unread=bad, repeat_read=repeat_read)
    async with flow.adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=flow.adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), 35)
            done = TaskStore(repo).get(course, task.task_id)
            assert done.state == ("failed" if bad else "completed"), done.model_dump()
            assert len(flow.http) == 1 and flow.boundary.actual_outbound_attempts == 0
            with repo.engine.connect() as conn:
                reads = list(
                    conn.execute(
                        select(r.external_reads).order_by(r.external_reads.c.created_at)
                    ).mappings()
                )
                read = reads[0]
                assert (
                    read["state"] == "completed"
                    and read["request"]["transport_source"] == "fixture"
                )
                assert [e["kind"] for e in read["network_events"]] == [
                    "request_started",
                    "response_received",
                ]
                call = NativeCall(**read["native_call"])
                assert ResearchExternal.receipt(conn, call) == read["result"]
                assert ResearchExternal.receipt(conn, replace(call, args_sha256="f" * 64)) is None
                if repeat_read:
                    assert len(reads) == 2 and not reads[1]["network_events"]
                    assert reads[1]["result"]["reused_evidence"]
                    assert reads[1]["result"]["sources"] == read["result"]["sources"]
                    assert reads[1]["result"]["cached_from_effect_id"] == read["effect_id"]
            if bad:
                assert done.units[0].error_code == "TASK_SOURCE_DENIED"
                assert ArtifactStore(repo).list(course) == []
            else:
                artifact = ArtifactStore(repo).list(course)[0]
                assert artifact.kind == "report" and artifact.indexed
                evidence = ResearchStore(repo).evidence(course, task.task_id, done.units[0].unit_id)
                assert len(evidence) >= 3 and all(e.from_current_run for e in evidence)
                assert ResearchStore(repo).get(course, task.task_id).source_decision_required == []
                coverage = TaskCoverage(repo).get(course, task.task_id)
                assert len(coverage.sources) == 2 and all(
                    s.state == "ready" for s in coverage.sources
                )
            assert flow.adapter.host.process.poll() is None
            assert all(run in flow.registry.released for run in flow.registry.scopes)
        finally:
            await executor.close()
            await service.close()
    assert flow.adapter.host.cleanup["remaining_pids"] == []
    repo.close()


@pytest.mark.host
async def test_lost_external_read_commit_stays_unknown_after_cold_host_without_refetch(tmp_path):
    repo, course, _, source = learning_course(tmp_path)
    task = admit(repo, course, source)
    flow = ExternalFlow(tmp_path, repo, source)

    def fail(point, _detail):
        if point == "research_external_after_http":
            raise OSError("synthetic cut after HTTP response before evidence receipt")

    repo.fault_hook = fail
    async with flow.adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=flow.adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), 35)
            current = TaskStore(repo).get(course, task.task_id)
            assert current.state == "unknown", current.model_dump()
            assert len(flow.http) == 1 and not ArtifactStore(repo).list(course)
            with repo.engine.connect() as conn:
                read = conn.execute(select(r.external_reads)).mappings().one()
                assert read["state"] == "started" and read["result"] is None
            before = len(flow.boundary.records)
            with pytest.raises(AppError):
                TaskRecovery(repo).retry_work(
                    course,
                    task.task_id,
                    current.units[0].unit_id,
                    TaskAction(operation_id="deny_blind_retry", expected_revision=current.revision),
                )
        finally:
            repo.fault_hook = None
            await executor.close()
            await service.close()
    cold = ExternalFlow(tmp_path, repo, source)
    async with cold.adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=cold.adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), 35)
            assert TaskStore(repo).get(course, task.task_id).state == "unknown"
            assert cold.http == [] and len(cold.boundary.records) == before
        finally:
            await executor.close()
            await service.close()
    repo.close()


@pytest.mark.host
async def test_external_failure_pauses_scope_and_explicit_exclusion_replans_only_local_report(
    tmp_path,
):
    repo, course, _, source = learning_course(tmp_path)
    task = admit(repo, course, source)
    flow = ExternalFlow(tmp_path, repo, source, fail=True)
    async with flow.adapter:
        service = GenerationService(
            repo, AppConfig(mode="test", data_dir=repo.root), execution=flow.adapter
        )
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            await asyncio.wait_for(executor._queue.join(), 30)
            failed = TaskStore(repo).get(course, task.task_id)
            assert (
                failed.state == "needs_sources"
                and failed.units[0].error_code == "RESEARCH_EXTERNAL_GAPS"
            )
            assert len(flow.http) == 1 and len(flow.boundary.records) == 2
            original = TaskRunStore(repo).internal(failed.units[0].run_id)
            assert original["state"] == "completed" and original["body"]
            assert ArtifactStore(repo).list(course) == []
            gap = next(
                s
                for s in TaskCoverage(repo).get(course, task.task_id).sources
                if s.state == "blocked"
            )
            excluded = TaskRecovery(repo).source_gap(
                course,
                task.task_id,
                SourceGapDecision(
                    operation_id="exclude_failed_external",
                    expected_revision=failed.revision,
                    decision="exclude",
                    source_ids=[gap.source_id],
                ),
            )
            assert excluded.units[0].excluded_at_scope_revision == 1
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), 30)
            done = TaskStore(repo).get(course, task.task_id)
            assert done.state == "completed" and len(done.units) == 2
            assert len(flow.http) == 1 and len(flow.boundary.records) == 3
            assert len(ArtifactStore(repo).list(course)) == 1
            assert TaskRunStore(repo).internal(failed.units[0].run_id)["body"] == original["body"]
        finally:
            await executor.close()
            await service.close()
    repo.close()
