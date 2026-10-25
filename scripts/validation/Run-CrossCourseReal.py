"""Explicit two-course query -> exact foreign evidence -> saved owner-course report."""

import argparse
import asyncio
import hashlib
import json
import re

from dotenv import dotenv_values
from sqlalchemy import select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.workbench import corpus_schema as c
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.corpus import CourseCorpus
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import ModelSettings
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def freeze():
    files = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "backend/uv.lock",
        PROJECT_ROOT / "scripts/validation/Run-CrossCourseReal.py",
    ]
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in files
    }


async def run(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "status": "not_run",
        "source_before": freeze(),
        "layer": "actual provider + fixed native Harness + explicit two-course scope + saved report",
        "input": "self-created three synthetic text courses; only two explicitly authorized",
        "user_learning_experience": "not_run",
    }
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
        record["missing_prerequisite"] = "PRIVATE_CREDENTIAL_NOT_CONFIGURED"
        (directory / "summary.json").write_text(json.dumps(record), "utf-8")
        return 2
    repo = Repository(config.data_dir)
    originals, ids = {}, []
    for name, text in [
        (
            "课程甲",
            "本课定义：璇玑变换 T(x)=(7/13)*x+5。这是本课自定义，不是通用常量。",
        ),
        (
            "课程乙",
            "本课定义：璇玑变换 T(x)=(11/17)*x+5。这是本课自定义，不是通用常量。",
        ),
        ("未指定课程", "璇玑变换 UNAUTHORIZED_THIRD_COURSE_SENTINEL: T(x)=999*x。"),
    ]:
        root = directory / ("course-" + str(len(ids)))
        root.mkdir()
        path = root / "definition.md"
        path.write_bytes(text.encode("utf-8"))
        originals[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        identity = CourseFileStore(repo).bind(None, str(root), name)["course_id"]
        ids.append(identity)
        corpus = CourseCorpus(repo)
        corpus.discover(identity, force=True)
        for _ in range(30):
            status = corpus.status(identity)
            if status.scan_state == "running":
                corpus.discover(identity)
            elif any(doc.state in {"pending", "indexing"} for doc in status.documents):
                corpus.index_next(identity)
            else:
                break
    owner, other, excluded = ids
    repo.create_chat(owner, "cross_course_real")
    service, executor, generation, task = (
        GenerationService(repo, config),
        None,
        None,
        None,
    )
    try:
        await service.open()
        response = await service.submit(
            {
                "chat_id": "cross_course_real",
                "submission_id": "two_course_comparison",
                "text": "请依据本次明确选定的两课程，按需查阅各自的璇玑变换定义，分别计算T(221)，保留两课定义差异并注明所属课程，不能合并成统一定义。",
                "read_course_ids": [owner, other],
                "model": args.model,
                "thinking": args.thinking,
            }
        )
        generation = response["generation_id"]
        await asyncio.wait_for(service.tasks[generation], args.timeout * 6 + 30)
        answer = repo.get_generation(generation)
        if answer["state"] != "completed":
            raise RuntimeError("CROSS_COURSE_QA_NOT_COMPLETED")
        with repo.engine.connect() as conn:
            queries = list(
                conn.execute(
                    select(c.query_receipts).where(
                        c.query_receipts.c.run_id == generation
                    )
                ).mappings()
            )
        evidence = {
            item["source"]["snapshot_id"]: item["source"]
            for row in queries
            for item in row["result"].get("results", [])
        }
        if {source["source_course_id"] for source in evidence.values()} != {
            owner,
            other,
        }:
            raise RuntimeError("EXPLICIT_COURSE_EVIDENCE_MISSING_OR_EXPANDED")
        if "UNAUTHORIZED_THIRD_COURSE_SENTINEL" in str(queries):
            raise RuntimeError("UNAUTHORIZED_COURSE_READ")
        if not all(
            re.search(r"(?<!\d)" + value + r"(?!\d)", answer["body"])
            for value in ["124", "148"]
        ):
            raise RuntimeError("CONTROLLED_TWO_COURSE_CALCULATION_MISSING")
        if ArtifactStore(repo).list(owner):
            raise RuntimeError("ORDINARY_QA_CREATED_UNREQUESTED_ASSET")
        task = TaskCoverage(repo).admit(
            owner,
            TaskAdmission(
                operation_id="explicit_saved_comparison",
                kind="research",
                instruction="只依据本次实际片段写一份简短对照报告，按所属课程保留两种璇玑变换，分别计算T(221)，同名文件不是同一来源。课程身份为："
                + json.dumps({owner: "课程甲", other: "课程乙"}, ensure_ascii=False),
                coverage_mode="complete",
                sources_only=True,
                sources=[
                    {"kind": "selection", "snapshot_id": identity}
                    for identity in evidence
                ],
                target={"mode": "create", "title": "两课璇玑变换对照"},
                settings=ModelSettings(
                    model=args.model,
                    thinking=args.thinking,
                    max_output_tokens=args.max_output_tokens,
                    timeout_seconds=args.timeout,
                ),
            ),
        )
        executor = TaskExecutor(repo, service)
        await executor.open()
        await asyncio.wait_for(executor._queue.join(), args.timeout * 6 + 30)
        current = TaskStore(repo).get(owner, task.task_id)
        if current.state != "completed":
            raise RuntimeError("CROSS_COURSE_REPORT_NOT_COMPLETED")
        assets = ArtifactStore(repo).list(owner)
        if (
            len(assets) != 1
            or assets[0].kind != "report"
            or assets[0].revision.author != "model"
        ):
            raise RuntimeError("REPORT_OWNER_OR_IDENTITY_INVALID")
        if ArtifactStore(repo).list(other) or ArtifactStore(repo).list(excluded):
            raise RuntimeError("FOREIGN_ASSET_WAS_WRITTEN")
        record.update(
            status="passed",
            answer=answer["body"],
            evidence=evidence,
            report=assets[0].model_dump(mode="json"),
            task=current.model_dump(mode="json"),
        )
    except Exception as error:
        record.update(
            status="failed",
            error=getattr(error, "code", type(error).__name__) + ": " + str(error),
        )
    finally:
        if executor:
            await executor.close()
        await service.close()
        record["cleanup"] = service.cleanup
        record["actual_outbound_attempts"] = (
            service.execution.boundary.actual_outbound_attempts
            if service.execution
            else None
        )
        with repo.engine.connect() as conn:
            record["raw_usage_qa"] = [
                dict(row)
                for row in conn.execute(select(business.usage_records)).mappings()
            ]
            from learning_app.workbench import schema as learning

            record["raw_usage_tasks"] = [
                dict(row) for row in conn.execute(select(learning.run_usage)).mappings()
            ]
        if generation:
            record["generation"] = repo.get_generation(generation)
        if task:
            record["task_final"] = (
                TaskStore(repo).get(owner, task.task_id).model_dump(mode="json")
            )
        record["originals_unchanged"] = all(
            hashlib.sha256(__import__("pathlib").Path(path).read_bytes()).hexdigest()
            == digest
            for path, digest in originals.items()
        )
        record["source_after"] = freeze()
        record["source_changed_during_run"] = (
            record["source_before"] != record["source_after"]
        )
        if not record["originals_unchanged"] or record["source_changed_during_run"]:
            record["status"] = "failed"
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        repo.close()
    print(
        json.dumps(
            {
                key: record.get(key)
                for key in [
                    "status",
                    "error",
                    "actual_outbound_attempts",
                    "source_changed_during_run",
                ]
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=180)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
