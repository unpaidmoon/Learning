"""Real provider: explicit whole B sources, frozen membership, one owner A result."""

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
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import ModelSettings
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def freeze():
    files = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "backend/uv.lock",
        PROJECT_ROOT / "scripts/validation/Run-TaskSourceCoursesReal.py",
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
        "user_learning_experience": "not_run",
        "layer": "actual provider + fixed native Host + explicit whole B declaration + saved A result",
        "materials": "self-created three courses and a late B file; no user materials/images",
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
        runtime_dir=directory / "run",
    )
    if not dotenv_values(config.secret_file).get("DEEPSEEK_API_KEY"):
        record["missing_prerequisite"] = "PRIVATE_CREDENTIAL_NOT_CONFIGURED"
        (directory / "summary.json").write_text(json.dumps(record), "utf-8")
        return 2
    repo = Repository(config.data_dir)
    roots, courses, originals = [], [], {}
    for index, (title, files) in enumerate(
        [
            ("成果保存课A", {"definition.md": "OWNER_RAW_MUST_NOT_ENTER: G(x)=9999。"}),
            (
                "明确全文课B",
                {
                    "definition.md": "本课自定义猎户映射 F(x)=23*x-19；这是本课约定。",
                    "conditions.md": "本题只讨论整数x；请分别求x=2、x=3时的F值。定义只在同一课程definition.md。",
                },
            ),
            ("未指定课C", {"definition.md": "THIRD_COURSE_MUST_NOT_ENTER: F(x)=0。"}),
        ]
    ):
        root = directory / f"course-{index}"
        root.mkdir()
        for name, text in files.items():
            path = root / name
            path.write_text(text, "utf-8")
            originals[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        roots.append(root)
        courses.append(CourseFileStore(repo).bind(None, str(root), title)["course_id"])
    owner, origin, excluded = courses
    task = TaskCoverage(repo).admit(
        owner,
        TaskAdmission(
            operation_id="explicit_whole_foreign_course",
            kind="handout",
            coverage_mode="complete",
            instruction="只依据明确B课程的全部指定原始文字资料，完整整理定义及整数条件，计算F(2)、F(3)，逐项保留来源。",
            sources=[{"kind": "course", "source_course_id": origin}],
            sources_only=True,
            target={"mode": "create", "title": "猎户映射学习讲义"},
            settings=ModelSettings(
                model=args.model,
                thinking=args.thinking,
                max_output_tokens=args.max_output_tokens,
                timeout_seconds=args.timeout,
            ),
        ),
    )
    late = roots[1] / "late.md"
    late.write_text("LATE_MEMBER_MUST_NOT_ENTER: F(x)改成1。", "utf-8")
    originals[late] = hashlib.sha256(late.read_bytes()).hexdigest()
    service, executor = GenerationService(repo, config), None
    try:
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        await asyncio.wait_for(executor._queue.join(), args.timeout * 6 + 30)
        current = TaskStore(repo).get(owner, task.task_id)
        record["task"] = current.model_dump(mode="json")
        if current.state != "completed":
            raise RuntimeError("FOREIGN_WHOLE_TASK_NOT_COMPLETED")
        coverage = TaskCoverage(repo).get(owner, task.task_id)
        snapshots = [
            SourceStore(repo).snapshot(owner, identity)
            for leaf in coverage.leaves
            for identity in leaf.snapshot_ids
        ]
        if len(coverage.leaves) != 2 or any(
            source.course_id != origin for source in snapshots
        ):
            raise RuntimeError("SOURCE_MEMBERSHIP_OR_ORIGIN_EXPANDED")
        assets = ArtifactStore(repo).list(owner)
        if (
            len(assets) != 1
            or assets[0].course_id != owner
            or assets[0].revision.author != "model"
        ):
            raise RuntimeError("RESULT_OWNER_OR_IDENTITY_INVALID")
        if (
            ArtifactStore(repo).list(origin)
            or ArtifactStore(repo).list(excluded)
            or repo.list_chats(origin)
        ):
            raise RuntimeError("FOREIGN_ASSET_OR_CHAT_WAS_CREATED")
        record["coverage"] = coverage.model_dump(mode="json")
        record["artifact"] = assets[0].model_dump(mode="json")
        record["input_snapshots"] = [
            source.model_dump(mode="json") for source in snapshots
        ]
        record["status"] = "passed"
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
            record["raw_usage"] = [
                dict(row) for row in conn.execute(select(s.run_usage)).mappings()
            ]
        record["wire_checks"] = []
        if service.execution:
            boundary = service.execution.boundary
            for attempt in boundary.records:
                raw = json.loads(
                    (
                        boundary.private / f"{attempt['request_id']}.request.json"
                    ).read_text("utf-8")
                )
                body = json.dumps(raw.get("messages", []), ensure_ascii=False)
                forbidden = [
                    value
                    for value in (
                        "OWNER_RAW_MUST_NOT_ENTER",
                        "THIRD_COURSE_MUST_NOT_ENTER",
                        "LATE_MEMBER_MUST_NOT_ENTER",
                    )
                    if value in body
                ]
                record["wire_checks"].append(
                    {
                        "request_id": attempt["request_id"],
                        "forbidden_markers": forbidden,
                        "images": attempt.get("images", []),
                    }
                )
                if forbidden or attempt.get("images"):
                    record["status"] = "failed"
                    record["error"] = "UNDECLARED_MATERIAL_ENTERED_WIRE"
        record["originals_unchanged"] = all(
            hashlib.sha256(path.read_bytes()).hexdigest() == sha
            for path, sha in originals.items()
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
                for key in (
                    "status",
                    "error",
                    "actual_outbound_attempts",
                    "source_changed_during_run",
                )
            },
            ensure_ascii=False,
        )
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
