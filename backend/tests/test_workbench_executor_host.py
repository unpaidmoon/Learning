"""Actual fixed Host plus project task DB; offline synthetic provider fixtures."""

import asyncio
import json
from pathlib import Path

import pytest
from workbench_support import learning_course, settings

from learning_app.config import AppConfig
from learning_app.execution.adapter import ExecutionAdapter
from learning_app.service import GenerationService
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.manifests import ManifestStore
from learning_app.workbench.models import (
    DocumentRangeInput,
    ManifestCreate,
    TaskAction,
    TaskCreate,
    TaskUnitInput,
)
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.tasks import TaskStore

REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.mark.host
async def test_multi_source_work_uses_actual_host_one_result_and_known_output_failure(tmp_path):
    from learning_app.course_files import CourseFileStore
    from learning_app.workbench.task_coverage import TaskCoverage
    from learning_app.workbench.task_planning import TaskPlanner
    from learning_app.workbench.task_scope_models import TaskAdmission

    repo, course_id, root, selection = learning_course(tmp_path)
    (root / "second.txt").write_text("第二份合成资料：先保持来源身份，再整理比较。", "utf-8")
    second = CourseFileStore(repo).open(course_id, "second.txt")
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "app_run")
    async with ExecutionAdapter(REPOSITORY, tmp_path / "host_runtime") as adapter:
        service = GenerationService(repo, config, execution=adapter)
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            task = TaskCoverage(repo).admit(
                course_id,
                TaskAdmission(
                    operation_id="two_sources",
                    kind="handout",
                    instruction="完整比较整理两份资料",
                    coverage_mode="complete",
                    sources=[
                        {
                            "kind": "document",
                            "asset_id": selection.reference.asset_id,
                            "source_version": selection.reference.source_version,
                        },
                        {
                            "kind": "document",
                            "asset_id": second["asset_id"],
                            "source_version": second["source_version"],
                        },
                    ],
                    target={"mode": "create", "title": "两份资料的讲义"},
                    sources_only=True,
                    settings=settings().model_copy(
                        update={"max_output_tokens": 512, "timeout_seconds": 60}
                    ),
                ),
            )
            TaskCoverage(repo).refresh(course_id, task.task_id)
            TaskPlanner(repo).plan(course_id, task.task_id)
            ids = [
                leaf.snapshot_ids[0]
                for leaf in TaskCoverage(repo).get(course_id, task.task_id).leaves
            ]
            adapter.boundary.set_scenario(
                {
                    "text_chunks": [
                        json.dumps(
                            {
                                "blocks": [
                                    {
                                        "block_id": "compare",
                                        "kind": "markdown",
                                        "markdown": "来自两份指定资料的讲义。",
                                        "attribution": "source_paraphrase",
                                        "source_snapshot_ids": ids,
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    ]
                }
            )
            executor.schedule(course_id, task.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=30)
            current = TaskStore(repo).get(course_id, task.task_id)
            assert current.state == "completed" and len(current.units) == 1
            coverage = TaskCoverage(repo).get(course_id, task.task_id)
            assert (
                coverage.target.materialized
                and sum(source.processed_leaf_count for source in coverage.sources) == 2
            )
            assert (
                len(ArtifactStore(repo).list(course_id)) == 1 and len(adapter.boundary.records) == 1
            )
            record = adapter.boundary.records[0]
            raw = json.loads(
                (adapter.boundary.private / f"{record['request_id']}.request.json").read_text(
                    "utf-8"
                )
            )
            text = json.dumps(raw["messages"], ensure_ascii=False)
            assert "Only this selected sentence." in text and "第二份合成资料" in text
            assert all(identity in text for identity in ids)
            run = TaskRunStore(repo).get(course_id, task.task_id, current.units[0].run_id)
            assert len(run.attempts) == 1 and run.actual_outbound_attempts == 0
            # A completed but invalid JSON response is a generation failure,
            # never a source gap and never an automatic second call.
            other = TaskCoverage(repo).admit(
                course_id,
                TaskAdmission(
                    operation_id="bad_json",
                    kind="handout",
                    instruction="整理选区",
                    sources=[{"kind": "selection", "snapshot_id": selection.snapshot_id}],
                    target={"mode": "create", "title": "失败留稿"},
                    settings=settings(),
                ),
            )
            adapter.boundary.set_scenario({"text_chunks": ["this is not JSON"]})
            executor.schedule(course_id, other.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=30)
            failed = TaskStore(repo).get(course_id, other.task_id)
            assert (
                failed.state == "failed"
                and failed.units[0].error_code == "TASK_OUTPUT_STRUCTURE_INVALID"
            )
            assert (
                TaskRunStore(repo).get(course_id, other.task_id, failed.units[0].run_id).body
                == "this is not JSON"
            )
            assert len(adapter.boundary.records) == 2
            assert not TaskCoverage(repo).get(course_id, other.task_id).target.materialized
            import runpy

            runpy.run_path(str(REPOSITORY / "tests/fixtures/workbench/generate.py"))["scanned_pdf"](
                root / "scanned.pdf"
            )
            scanned = CourseFileStore(repo).open(course_id, "scanned.pdf")
            image_task = TaskCoverage(repo).admit(
                course_id,
                TaskAdmission(
                    operation_id="whole_document_image",
                    kind="handout",
                    instruction="根据授权扫描文档页整理内容",
                    coverage_mode="complete",
                    sources=[
                        {
                            "kind": "document",
                            "asset_id": scanned["asset_id"],
                            "source_version": scanned["source_version"],
                        }
                    ],
                    target={"mode": "create", "title": "扫描页讲义"},
                    settings=settings(),
                    sources_only=True,
                ),
            )
            TaskCoverage(repo).refresh(course_id, image_task.task_id)
            TaskPlanner(repo).plan(course_id, image_task.task_id)
            image_ids = TaskCoverage(repo).get(course_id, image_task.task_id).leaves[0].snapshot_ids
            adapter.boundary.set_scenario(
                {
                    "text_chunks": [
                        json.dumps(
                            {
                                "blocks": [
                                    {
                                        "block_id": "page",
                                        "kind": "markdown",
                                        "markdown": "来自文档页图像的归纳。",
                                        "attribution": "source_paraphrase",
                                        "source_snapshot_ids": image_ids,
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    ]
                }
            )
            executor.schedule(course_id, image_task.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=30)
            image_final = TaskStore(repo).get(course_id, image_task.task_id)
            assert image_final.state == "completed"
            image_run = TaskRunStore(repo).internal(image_final.units[0].run_id)
            assert len(image_run["input_contract"]["image_hashes"]) == 1
            proof = image_run["request_image_proofs"][0]
            assert proof["source_sha256"] == image_run["input_contract"]["image_hashes"][0]
            assert proof["encoded_sha256"] == adapter.boundary.records[-1]["images"][0]["sha256"]
            assert proof["encoded_sha256"] != proof["source_sha256"]
            restored_policy = TaskExecutor._policy(image_run)
            assert (
                restored_policy.exact_image_sequence
                and restored_policy.allowed_image_sha256 == (proof["encoded_sha256"],)
            )
            assert len(adapter.boundary.records) == 3
            image_record = adapter.boundary.records[-1]
            image_request = json.loads(
                (adapter.boundary.private / f"{image_record['request_id']}.request.json").read_text(
                    "utf-8"
                )
            )
            assert (
                sum(
                    block["type"] == "image"
                    for message in image_request["messages"]
                    for block in message["content"]
                    if isinstance(block, dict)
                )
                == 1
            )
            # Same Host, another explicitly chosen text model. The guard must
            # validate after native selection has finished its middleware.
            pro = TaskCoverage(repo).admit(
                course_id,
                TaskAdmission(
                    operation_id="explicit_pro",
                    kind="translation",
                    instruction="翻译成中文",
                    sources=[{"kind": "selection", "snapshot_id": selection.snapshot_id}],
                    target={"mode": "create", "title": "显式Pro译文"},
                    settings=settings().model_copy(
                        update={"model": "deepseek-v4-pro", "max_output_tokens": 512}
                    ),
                ),
            )
            adapter.boundary.set_scenario({"text_chunks": ["显式选定模型的译文。"]})
            executor.schedule(course_id, pro.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=30)
            assert TaskStore(repo).get(course_id, pro.task_id).state == "completed"
            assert adapter.boundary.records[-1]["model"] == "deepseek-v4-pro"
            assert (
                sum(
                    source.processed_leaf_count
                    for source in TaskCoverage(repo).get(course_id, image_task.task_id).sources
                )
                == 1
            )
        finally:
            await executor.close()
            await service.close()
    repo.close()


@pytest.mark.host
async def test_task_model_stream_to_independent_saved_product_and_full_scope(tmp_path):
    repo, course_id, _, selection = learning_course(tmp_path)
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "app_run")
    async with ExecutionAdapter(REPOSITORY, tmp_path / "host_runtime") as adapter:
        service = GenerationService(repo, config, execution=adapter)
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            model = settings().model_copy(
                update={"thinking": "low", "max_output_tokens": 512, "timeout_seconds": 60}
            )
            translation = TaskStore(repo).create(
                course_id,
                TaskCreate(
                    operation_id="translation",
                    kind="translation",
                    instruction="翻译为中文",
                    settings=model,
                    units=[
                        TaskUnitInput(
                            unit_id="selected",
                            label="局部选区",
                            source_snapshot_id=selection.snapshot_id,
                        )
                    ],
                ),
            )
            adapter.boundary.set_scenario({"text_chunks": ["仅翻译", "所选句子。"]})
            executor.schedule(course_id, translation.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=20)
            completed = TaskStore(repo).get(course_id, translation.task_id)
            assert completed.state == "completed"
            run = TaskRunStore(repo).get(course_id, translation.task_id, completed.units[0].run_id)
            assert run.body == "仅翻译所选句子。" and run.usage_status == "known"
            assert run.actual_outbound_attempts == 0 and len(run.attempts) == 1
            product = ArtifactStore(repo).get(course_id, completed.units[0].result_artifact_id)
            assert product.content_role == "ai_generated" and product.kind == "translation"
            assert (
                ArtifactStore(repo).search(course_id, "所选句子")[0].artifact_id
                == product.artifact_id
            )
            record = next(
                row for row in adapter.boundary.records if row["generation_id"] == run.run_id
            )
            raw = json.loads(
                (adapter.boundary.private / f"{record['request_id']}.request.json").read_text(
                    "utf-8"
                )
            )
            assert "Only this selected sentence" in json.dumps(raw["messages"])
            assert "不应补发前后文" not in json.dumps(raw, ensure_ascii=False)
            assert raw["max_tokens"] == 512 and raw["output_config"]["effort"] == "low"
            assert repo.counts()["chats"] == repo.counts()["generations"] == 0

            manifest = ManifestStore(repo).create(
                course_id,
                ManifestCreate(
                    documents=[
                        DocumentRangeInput(
                            asset_id=selection.reference.asset_id,
                            source_version=selection.reference.source_version,
                        )
                    ]
                ),
            )
            handout = TaskStore(repo).create(
                course_id,
                TaskCreate(
                    operation_id="full_handout",
                    kind="handout",
                    instruction="完整归纳指定正文",
                    settings=model,
                    sources_only=True,
                    coverage_mode="complete",
                    scope_manifest_id=manifest.manifest_id,
                ),
            )
            structured = {
                "blocks": [
                    {
                        "block_id": "summary",
                        "kind": "markdown",
                        "markdown": "指定全文的讲义",
                        "attribution": "source_paraphrase",
                        "source_snapshot_ids": [manifest.units[0].source_snapshot_id],
                    }
                ]
            }
            adapter.boundary.set_scenario(
                {"text_chunks": [json.dumps(structured, ensure_ascii=False)]}
            )
            executor.schedule(course_id, handout.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=20)
            result = TaskStore(repo).get(course_id, handout.task_id)
            assert result.state == "completed" and result.coverage_mode == "complete"
            assert result.units[0].result_artifact_id
            assert len(adapter.boundary.records) == 2
        finally:
            await executor.close()
            await service.close()
    repo.close()


@pytest.mark.host
async def test_actual_task_cancel_and_invalid_structure_retain_draft_without_retry(tmp_path):
    repo, course_id, _, selection = learning_course(tmp_path)
    config = AppConfig(mode="test", data_dir=repo.root, runtime_dir=tmp_path / "app_run")
    async with ExecutionAdapter(REPOSITORY, tmp_path / "host_runtime") as adapter:
        service = GenerationService(repo, config, execution=adapter)
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        try:
            value = dict(
                instruction="处理所选来源",
                settings=settings(),
                units=[
                    TaskUnitInput(
                        unit_id="selected",
                        label="实际选区",
                        source_snapshot_id=selection.snapshot_id,
                    )
                ],
            )
            task = TaskStore(repo).create(
                course_id, TaskCreate(operation_id="cancel_task", kind="translation", **value)
            )
            adapter.boundary.set_scenario(
                {
                    "text_chunks": ["部分译文", "不应出现的尾部"],
                    "pause_after_event": "text_delta",
                    "gate": "task_cancel",
                }
            )
            executor.schedule(course_id, task.task_id)
            await asyncio.to_thread(adapter.boundary.wait_for_event, "text_delta", timeout=10)
            task = TaskStore(repo).get(course_id, task.task_id)
            TaskStore(repo).control(
                course_id,
                task.task_id,
                "pause",
                TaskAction(operation_id="pause", expected_revision=task.revision),
            )
            await executor.cancel_task(course_id, task.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=20)
            paused = TaskStore(repo).get(course_id, task.task_id)
            run = TaskRunStore(repo).get(course_id, task.task_id, paused.units[0].run_id)
            assert paused.state == "paused" and run.state == "cancelled" and run.body == "部分译文"
            assert run.usage_status == "unknown" and not paused.units[0].result_artifact_id
            assert adapter.host.process.poll() is None
            invalid = TaskStore(repo).create(
                course_id, TaskCreate(operation_id="invalid_handout", kind="handout", **value)
            )
            adapter.boundary.set_scenario({"text_chunks": ['{"blocks":[{"broken":']})
            executor.schedule(course_id, invalid.task_id)
            await asyncio.wait_for(executor._queue.join(), timeout=20)
            invalid = TaskStore(repo).get(course_id, invalid.task_id)
            assert (
                invalid.state == "needs_sources"
                and invalid.units[0].error_code == "TASK_OUTPUT_STRUCTURE_INVALID"
            )
            raw = TaskRunStore(repo).get(course_id, invalid.task_id, invalid.units[0].run_id)
            assert raw.state == "completed" and raw.body == '{"blocks":[{"broken":'
            assert (
                len(adapter.boundary.records) == 2
                and adapter.boundary.actual_outbound_attempts == 0
            )
            assert ArtifactStore(repo).list(course_id) == []
        finally:
            await executor.close()
            await service.close()
    repo.close()
