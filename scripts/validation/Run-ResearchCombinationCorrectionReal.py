"""Explicit correction in the same saved conversation; never replay its old answer."""

import argparse
import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

from sqlalchemy import select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.workbench import research_schema as r
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.sources import SourceStore


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def freeze():
    paths = [
        *PROJECT_ROOT.joinpath("backend/src/learning_app").rglob("*.py"),
        *PROJECT_ROOT.joinpath("harness/plugins").glob("*.mjs"),
        PROJECT_ROOT / "harness/runtime.lock.json",
        Path(__file__).resolve(),
    ]
    return {
        p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in paths
    }


async def run(args):
    if any(
        not re.fullmatch(r"[A-Za-z0-9_-]+", value)
        for value in [args.run_id, args.source_run]
    ):
        raise ValueError("OWNED_RUN_ID_INVALID")
    base = (PROJECT_ROOT / ".runtime/validation").resolve()
    old, output = base / args.source_run, base / args.run_id
    previous = json.loads((old / "summary.json").read_text("utf-8"))
    assert previous["status"] == "passed"
    before = hashes(old / "data")
    output.mkdir(exist_ok=False)
    shutil.copytree(old / "data", output / "data")
    (output / "runner-frozen.py").write_bytes(Path(__file__).read_bytes())
    record = {
        "status": "running",
        "source_run": args.source_run,
        "source_before": freeze(),
        "layer": "new explicit correction in same business chat; actual provider; owned clone; old answer unchanged",
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
        data_dir=output / "data",
        runtime_dir=output / "runtime",
    )
    repo = Repository(config.data_dir)
    course = previous["course_id"]
    old_generation = previous["retrieval_generation"]
    old_id = old_generation["generation_id"]
    service, generation = GenerationService(repo, config), None
    try:
        old_answer = repo.get_generation(old_id)
        reports = [
            (value.artifact_id, value.head_revision_id)
            for value in ArtifactStore(repo).list(course)
        ]
        with repo.engine.connect() as connection:
            external = [
                dict(row)
                for row in connection.execute(select(r.external_reads)).mappings()
            ]
        ids = [
            source["source"]["snapshot_id"]
            for row in external
            for source in row["result"]["sources"]
        ]
        for identity in ids:
            assert SourceStore(repo).snapshot(course, identity).text
        assert len(external) == 2 and len(ids) >= 2
        await service.open()
        assert service.question_answering_ready
        initial = service.execution.boundary.actual_outbound_attempts
        assert initial == previous["actual_outbound_attempts"]
        question = (
            "这是对上一答的明确更正：程序已核对，本轮两次NIST网页读取各有已保存的来源快照和回执。"
            "刚才普通概念检索只返回4个有界命中，不能据此推断‘课程没有保存NIST原始快照’。"
            "另一个措辞也应更正：改变单位后，是边长与面积的数值表示分别变化10倍和100倍，矩形的实际尺寸没有放大。"
            "请在同一对话明确更正这两处结论，保留original与ai_generated的身份区分，只依据当前已提交历史。"
            "不需要重新检索，不联网、不读图、不新建或改写学习成果，也不重发上一问。"
        )
        result = await service.submit(
            {
                "chat_id": old_answer["chat_id"],
                "submission_id": "combo4_explicit_correction",
                "text": question,
                "model": args.model,
                "thinking": args.thinking,
            }
        )
        generation = result["generation_id"]
        await asyncio.wait_for(service.tasks[generation], args.timeout * 6)
        current = repo.get_generation(generation)
        record["generation"] = current
        if current["state"] != "completed":
            raise RuntimeError("CORRECTION_NOT_COMPLETE:" + current["state"])
        assert repo.get_generation(old_id) == old_answer
        assert reports == [
            (value.artifact_id, value.head_revision_id)
            for value in ArtifactStore(repo).list(course)
        ]
        with repo.engine.connect() as connection:
            after_external = [
                dict(row)
                for row in connection.execute(select(r.external_reads)).mappings()
            ]
        assert after_external == external
        record.update(
            status="passed",
            old_answer_unchanged=True,
            reports_unchanged=True,
            new_external_reads=0,
            cumulative_actual_outbound_attempts=service.execution.boundary.actual_outbound_attempts,
            new_actual_outbound_attempts=service.execution.boundary.actual_outbound_attempts
            - initial,
            saved_primary_snapshot_count=len(ids),
        )
    except Exception as error:
        record.update(
            status="unknown"
            if generation and repo.get_generation(generation)["state"] == "unknown"
            else "failed",
            error=getattr(error, "code", str(error)),
        )
    finally:
        await service.close()
        record["cleanup"] = service.cleanup
        with repo.engine.connect() as connection:
            record["chat_raw_usage"] = [
                dict(row)
                for row in connection.execute(select(business.usage_records)).mappings()
            ]
        record["source_after"] = freeze()
        record["source_unchanged"] = record["source_before"] == record["source_after"]
        record["original_owned_data_unchanged"] = before == hashes(old / "data")
        if (
            not record["source_unchanged"]
            or not record["original_owned_data_unchanged"]
        ):
            record["status"] = "failed"
        (output / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        repo.close()
    print(
        json.dumps(
            {
                k: record.get(k)
                for k in [
                    "status",
                    "new_actual_outbound_attempts",
                    "cumulative_actual_outbound_attempts",
                    "error",
                ]
            }
        ),
        flush=True,
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-run", default="research-combination-real-20261025-r1")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--timeout", type=float, default=300)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
