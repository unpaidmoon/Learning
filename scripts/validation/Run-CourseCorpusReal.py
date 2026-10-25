"""Real provider, synthetic current-course concept evidence, no media input."""

from __future__ import annotations

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
from learning_app.workbench.corpus_models import CorpusPolicy


def freeze():
    paths = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        *sorted((PROJECT_ROOT / "third_party/deeptutor/src").rglob("*.py")),
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "backend/uv.lock",
    ]
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


async def run(args):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,100}", args.run_id):
        raise ValueError("invalid owned evidence identity")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "layer": "real_provider_fixed_native_harness",
        "input": "synthetic UTF-8 course text only",
        "status": "not_run",
        "source_hashes_before": freeze(),
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
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        return 2
    materials = directory / "synthetic-course"
    materials.mkdir()
    original = "合成课程定义：璇玑系数 k 定义为 7/13。输入 x 的璇玑变换是 T(x)=k*x+5。本段定义标记为 CORPUS_REAL_DEFINITION_7135，引用时保留它是本课自定义而非通行术语。"
    (materials / "definition.md").write_bytes(original.encode("utf-8"))
    (materials / "excluded.md").write_bytes(
        "璇玑系数 OUTSIDE_EXCLUSION_SENTINEL".encode("utf-8")
    )
    (materials / "captions.srt").write_bytes(b"FORBIDDEN_GLOBAL_SUBTITLE_SENTINEL")
    repo = Repository(config.data_dir)
    course = CourseFileStore(repo).bind(None, str(materials), "合成课程检索真实验证")[
        "course_id"
    ]
    corpus = CourseCorpus(repo)
    corpus.configure(
        course,
        CorpusPolicy(
            operation_id="exclude_fixture",
            expected_revision=0,
            exclusions=["excluded.md"],
        ),
    )
    corpus.discover(course, force=True)
    while corpus.index_next(course):
        if all(doc.state == "ready" for doc in corpus.status(course).documents):
            break
    repo.create_chat(course, "real_corpus_chat")
    service = GenerationService(repo, config)
    generation = None
    try:
        await service.open()
        if not service.question_answering_ready:
            raise RuntimeError(service.execution_error or "MODEL_ROUTE_NOT_READY")
        result = await service.submit(
            {
                "chat_id": "real_corpus_chat",
                "submission_id": "real_concept_query",
                "text": "请通过当前课程按需读取的原生 skill 查阅本课对‘璇玑系数’的定义，算出输入26的变换结果，并注明实际来源位置和snapshot_id。不要使用外部资料，不要保存为学习成果。",
                "model": args.model,
                "thinking": args.thinking,
            }
        )
        generation = result["generation_id"]
        await asyncio.wait_for(service.tasks[generation], args.timeout * 4 + 30)
        run = repo.get_generation(generation)
        record["generation"] = run
        with repo.engine.connect() as conn:
            queries = list(
                conn.execute(
                    select(c.query_receipts).where(
                        c.query_receipts.c.run_id == generation
                    )
                ).mappings()
            )
        record["query_receipts"] = [dict(row) for row in queries]
        if run["state"] != "completed":
            raise RuntimeError("COURSE_REAL_NOT_COMPLETE:" + run["state"])
        if not queries or not any(
            "CORPUS_REAL_DEFINITION_7135" in str(row["result"]) for row in queries
        ):
            raise RuntimeError("COURSE_REAL_NO_ORIGINAL_RETRIEVAL")
        if ArtifactStore(repo).list(course):
            raise RuntimeError("COURSE_REAL_UNREQUESTED_ARTIFACT")
        requests = [
            json.loads(
                (
                    service.execution.boundary.private
                    / f"{attempt['request_id']}.request.json"
                ).read_text("utf-8")
            )
            for attempt in service.execution.boundary.records
            if attempt.get("generation_id") == generation
        ]
        record["provider_request_count"] = len(requests)
        if (
            not requests
            or len(requests) != service.execution.boundary.actual_outbound_attempts
        ):
            raise RuntimeError("COURSE_REAL_OUTBOUND_EVIDENCE_MISSING")
        for sentinel in (
            "OUTSIDE_EXCLUSION_SENTINEL",
            "FORBIDDEN_GLOBAL_SUBTITLE_SENTINEL",
        ):
            if any(
                sentinel in json.dumps(request, ensure_ascii=False)
                for request in requests
            ):
                raise RuntimeError("COURSE_REAL_INPUT_SCOPE_VIOLATION")
        if requests and "CORPUS_REAL_DEFINITION_7135" in json.dumps(
            requests[0], ensure_ascii=False
        ):
            raise RuntimeError("COURSE_REAL_AUTOMATIC_PREFETCH")
        snapshot_ids = [
            item["source"]["snapshot_id"]
            for query in queries
            for item in query["result"]["results"]
        ]
        record["known_fixture_calculation"] = (
            "passed" if re.search(r"(?<!\d)19(?!\d)", run["body"]) else "failed"
        )
        record["explicit_snapshot_citation"] = any(
            identity in run["body"] for identity in snapshot_ids
        )
        if (
            record["known_fixture_calculation"] != "passed"
            or not record["explicit_snapshot_citation"]
        ):
            raise RuntimeError("COURSE_REAL_ANSWER_OR_CITATION_MISSING")
        record["status"] = "passed"
    except BaseException as error:
        record.update(status="failed", failure=f"{type(error).__name__}: {error}")
    finally:
        await service.close()
        record["cleanup"] = service.cleanup
        record["actual_outbound_attempts"] = (
            service.execution.boundary.actual_outbound_attempts
            if service.execution
            else None
        )
        with repo.engine.connect() as conn:
            record["raw_usage"] = [
                dict(row)
                for row in conn.execute(select(business.usage_records)).mappings()
            ]
        record["source_hashes_after"] = freeze()
        record["source_changed_during_run"] = (
            record["source_hashes_before"] != record["source_hashes_after"]
        )
        record["original_preserved"] = (
            materials / "definition.md"
        ).read_bytes().decode("utf-8") == original
        if record["source_changed_during_run"] or not record["original_preserved"]:
            record["status"] = "failed"
        if generation and repo.get_generation(generation)["state"] == "unknown":
            record["status"] = "unknown"
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        print(
            json.dumps(
                {
                    key: record[key]
                    for key in (
                        "status",
                        "actual_outbound_attempts",
                        "source_changed_during_run",
                    )
                },
                ensure_ascii=False,
            )
        )
        repo.close()
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--timeout", type=float, default=180)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
