"""One course: local comparison -> explicit NIST research -> saved-report retrieval."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.workbench import corpus_schema as c
from learning_app.workbench import research_schema as r
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.corpus import CourseCorpus
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import ModelSettings
from learning_app.workbench.research import ResearchStore
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore

URLS = [
    "https://www.nist.gov/pml/owm/si-units-length",
    "https://www.nist.gov/pml/owm/metric-si/si-units-area",
]


def freeze():
    paths = [
        *PROJECT_ROOT.joinpath("backend/src/learning_app").rglob("*.py"),
        *PROJECT_ROOT.joinpath("harness/plugins").glob("*.mjs"),
        *PROJECT_ROOT.joinpath("third_party/deeptutor/src").rglob("*.py"),
        PROJECT_ROOT / "backend/uv.lock",
        PROJECT_ROOT / "harness/runtime.lock.json",
        Path(__file__).resolve(),
    ]
    return {
        p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in paths
    }


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


async def run(args):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,100}", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(exist_ok=False)
    record = {
        "status": "running",
        "layer": "actual provider + pinned native Harness + two formal research tasks and course-read",
        "material_scope": "two self-created local text files and two explicitly allowed NIST HTML pages; no user materials, image, video or screen",
        "user_learning_experience": "not_run",
        "source_before": freeze(),
        "cases": [],
    }
    save(directory / "source-before.json", record["source_before"])
    (directory / "runner-frozen.py").write_bytes(Path(__file__).read_bytes())
    config = AppConfig(
        mode="test",
        harness_enabled=True,
        credential_route="api_key",
        model=args.model,
        thinking=args.thinking,
        max_output_tokens=args.max_output_tokens,
        model_timeout_seconds=args.timeout,
        data_dir=directory / "data",
        runtime_dir=directory / "runtime",
    )
    if not dotenv_values(config.secret_file).get("DEEPSEEK_API_KEY"):
        record.update(status="not_run", error="PRIVATE_CREDENTIAL_NOT_CONFIGURED")
        save(directory / "summary.json", record)
        return 2
    materials = directory / "synthetic-course"
    materials.mkdir()
    texts = {
        "a.txt": "矩形单位。Source A. Rectangle area equals length times width. Width 3 cm and length 4 cm are perpendicular adjacent sides of a plane rectangle, so the area is 12 square centimetres. Synthetic study text, not an official quotation.",
        "b.txt": "矩形单位。Source B. Use matching units before multiplication. Width 30 mm and length 40 mm give 1200 square mm, equal to 12 square cm. This is a rectangle, not an arbitrary nonrectangular shape. Synthetic study text, not an official quotation.",
    }
    for name, text in texts.items():
        (materials / name).write_text(text, "utf-8")
    hashes = {
        name: hashlib.sha256((materials / name).read_bytes()).hexdigest()
        for name in texts
    }
    repo = Repository(config.data_dir)
    files = CourseFileStore(repo)
    course = files.bind(None, str(materials), "组合4矩形单位对照")["course_id"]
    opened = {name: files.open(course, name) for name in texts}
    declarations = [
        {
            "kind": "document",
            "asset_id": value["asset_id"],
            "source_version": value["source_version"],
        }
        for value in opened.values()
    ]
    record.update(course_id=course, original_sources=opened, original_hashes=hashes)
    service, executor, generation = GenerationService(repo, config), None, None
    try:
        await service.open()
        if not service.question_answering_ready:
            raise RuntimeError(service.execution_error or "MODEL_ROUTE_NOT_READY")
        executor = TaskExecutor(repo, service)
        await executor.open()
        settings = ModelSettings(
            model=args.model,
            thinking=args.thinking,
            max_output_tokens=args.max_output_tokens,
            timeout_seconds=args.timeout,
        )
        for external in [False, True]:
            instruction = (
                "只依据这两份自造资料比较矩形条件、面积公式和单位。3cm乘4cm与30mm乘40mm是否矛盾，区分资料明示和比较推断。每份资料均须实际引用。简洁中文报告，正文包含概念词‘矩形单位’，不联网、不编练习，不声称SI官方认证。"
                if not external
                else "现在明确授权read_research_page读取给定两份NIST官方HTML网页，只读这两个URL，不全网搜索、不跟随其他链接、不读图片视频或其他课程。对照同一版本两份本地资料，核验长度和面积SI单位、10mm=1cm与100平方mm=1平方cm，以及3cm乘4cm=12平方cm和30mm乘40mm=1200平方mm=12平方cm。每份本地资料和两个实际网页都须有引用，推导标source_inference，不把自造例子称NIST原文。若有unread_continuations，继续read_source_handle读取每个句柄和source_version，不重复请求同URL代替续读。证据完整后stage_result暂存简洁中文报告，正文包含‘矩形单位’；使用服务器给定的task/object/expected_version，不把stage_revision当对象版本。"
            )
            intent = TaskAdmission(
                operation_id="combo4_external_nist" if external else "combo4_local",
                kind="research",
                coverage_mode="complete",
                sources=declarations,
                sources_only=not external,
                allow_external=external,
                research={"topics": ["矩形单位"], "urls": URLS if external else []},
                instruction=instruction,
                target={
                    "mode": "create",
                    "title": "矩形单位SI核验报告"
                    if external
                    else "矩形单位本地对照报告",
                },
                settings=settings,
            )
            task = TaskCoverage(repo).admit(course, intent)
            item = {
                "kind": "external_nist" if external else "local",
                "task_id": task.task_id,
                "status": "running",
            }
            record["cases"].append(item)
            save(directory / "progress.json", record["cases"])
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), args.task_timeout)
            current = TaskStore(repo).get(course, task.task_id)
            progress = ResearchStore(repo).get(course, task.task_id)
            item.update(
                task=current.model_dump(mode="json"),
                research=progress.model_dump(mode="json"),
                coverage=TaskCoverage(repo)
                .get(course, task.task_id)
                .model_dump(mode="json"),
            )
            if (
                current.state != "completed"
                or progress.reconciliation_required
                or progress.source_decision_required
            ):
                raise RuntimeError("RESEARCH_DECISION_REQUIRED:" + current.state)
            artifact = ArtifactStore(repo).get(
                course, TaskCoverage(repo).get(course, task.task_id).target.artifact_id
            )
            item["artifact"] = artifact.model_dump(mode="json")
            if (
                artifact.kind != "report"
                or not artifact.indexed
                or artifact.content_role != "ai_generated"
            ):
                raise RuntimeError("SAVED_REPORT_ROLE_OR_INDEX_INVALID")
            if external and {
                row.url
                for row in progress.external_reads
                if row.kind == "read_research_page" and row.state == "completed"
            } != set(URLS):
                raise RuntimeError("EXPLICIT_PRIMARY_PAGES_NOT_READ")
            if not external and progress.external_reads:
                raise RuntimeError("LOCAL_COMPARISON_UNEXPECTED_NETWORK")
            item["status"] = "passed"
            save(directory / "progress.json", record["cases"])
            print(
                json.dumps(
                    {"case": item["kind"], "state": "passed", "task_id": task.task_id}
                ),
                flush=True,
            )
        corpus = CourseCorpus(repo)
        corpus.discover(course, force=True)
        while corpus.index_next(course):
            if all(row.state == "ready" for row in corpus.status(course).documents):
                break
        hits = corpus.search(course, "矩形单位")
        record["saved_search"] = [
            {"label": hit["label"], "snapshot": hit["snapshot"].model_dump(mode="json")}
            for hit in hits
        ]
        if not {"original", "ai_generated"} <= {
            hit["snapshot"].content_role for hit in hits
        }:
            raise RuntimeError("SAVED_REPORT_AND_ORIGINAL_NOT_SEPARATELY_RETRIEVABLE")
        artifact_count = len(ArtifactStore(repo).list(course))
        with repo.engine.connect() as connection:
            external_ids_before = set(
                connection.execute(select(r.external_reads.c.effect_id)).scalars()
            )
        repo.create_chat(course, "combo4_retrieval_chat")
        result = await service.submit(
            {
                "chat_id": "combo4_retrieval_chat",
                "submission_id": "combo4_saved_report_query",
                "text": "请用当前课程的course-read原生skill按需检索‘矩形单位’，分别阅读原始资料和已经保存的SI核验报告，指出哪些是自造原始资料、哪些是AI生成报告，并解释两种尺寸为何面积一致。只依据当前课程已保存内容，不联网、不读图片、不生成新的学习成果；引用实际snapshot_id和来源名称。",
                "model": args.model,
                "thinking": args.thinking,
            }
        )
        generation = result["generation_id"]
        await asyncio.wait_for(service.tasks[generation], args.task_timeout)
        current = repo.get_generation(generation)
        record["retrieval_generation"] = current
        with repo.engine.connect() as connection:
            queries = [
                dict(row)
                for row in connection.execute(
                    select(c.query_receipts).where(
                        c.query_receipts.c.run_id == generation
                    )
                ).mappings()
            ]
            external_ids_after = set(
                connection.execute(select(r.external_reads.c.effect_id)).scalars()
            )
        record["native_query_receipts"] = queries
        roles = {
            item["source"]["content_role"]
            for row in queries
            for item in row["result"].get("results", [])
        }
        if current["state"] != "completed" or not {"original", "ai_generated"} <= roles:
            raise RuntimeError(
                "NATIVE_RETRIEVAL_ROLE_EVIDENCE_MISSING:" + current["state"]
            )
        if len(ArtifactStore(repo).list(course)) != artifact_count:
            raise RuntimeError("ORDINARY_RETRIEVAL_CREATED_UNREQUESTED_ARTIFACT")
        if external_ids_before != external_ids_after:
            raise RuntimeError("ORDINARY_COURSE_RETRIEVAL_STARTED_EXTERNAL_RESEARCH")
        record["retrieval_new_external_read_count"] = len(
            external_ids_after - external_ids_before
        )
        record["status"] = "passed"
    except Exception as error:
        record.update(
            status="unknown"
            if generation and repo.get_generation(generation)["state"] == "unknown"
            else "failed",
            error=getattr(error, "code", str(error)),
        )
    finally:
        if executor:
            await executor.close()
        await service.close()
        record["cleanup"] = service.cleanup
        with repo.engine.connect() as connection:
            run_rows = [
                dict(row)
                for row in connection.execute(
                    select(s.task_runs.c.task_id, s.task_runs.c.run_id)
                ).mappings()
            ]
            record["external_reads"] = [
                dict(row)
                for row in connection.execute(select(r.external_reads)).mappings()
            ]
            record["chat_raw_usage"] = [
                dict(row)
                for row in connection.execute(select(business.usage_records)).mappings()
            ]
        record["runs"] = [
            TaskRunStore(repo)
            .get(course, row["task_id"], row["run_id"])
            .model_dump(mode="json")
            for row in run_rows
        ]
        record["task_raw_usage"] = [
            usage for row in record["runs"] for usage in row["usage"]
        ]
        record["actual_outbound_attempts"] = (
            service.execution.boundary.actual_outbound_attempts
            if service.execution
            else 0
        )
        record["external_http_requests_started"] = sum(
            event["kind"] == "request_started"
            for row in record["external_reads"]
            for event in row["network_events"]
        )
        record["source_after"] = freeze()
        record["source_unchanged"] = record["source_before"] == record["source_after"]
        record["originals_unchanged"] = hashes == {
            name: hashlib.sha256((materials / name).read_bytes()).hexdigest()
            for name in texts
        }
        if not record["source_unchanged"] or not record["originals_unchanged"]:
            record["status"] = "failed"
        save(directory / "summary.json", record)
        repo.close()
    print(
        json.dumps(
            {
                "status": record["status"],
                "actual_outbound_attempts": record["actual_outbound_attempts"],
                "error": record.get("error"),
            }
        ),
        flush=True,
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=16384)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--task-timeout", type=float, default=2700)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
