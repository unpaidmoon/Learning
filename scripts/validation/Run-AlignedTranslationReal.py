"""Owned exact English paragraphs -> native NDJSON -> saved paired translation."""

import argparse
import asyncio
import hashlib
import json
import re
import shutil

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
from learning_app.workbench.offline import OfflineExporter
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def freeze():
    files = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        *sorted((PROJECT_ROOT / "resources/workbench").glob("*.lock.json")),
        PROJECT_ROOT / "scripts/validation/Run-AlignedTranslationReal.py",
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "backend/uv.lock",
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
        "layer": "actual provider + pinned Host + original strict scanner adaptation + saved paired translation",
        "input": "self-created exact English paragraphs; no user material, images, video, screen or history",
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
            json.dumps(record, ensure_ascii=False), "utf-8"
        )
        return 2
    materials = directory / "synthetic-course"
    materials.mkdir()
    text = "An even integer is divisible by two. For example, 8 / 2 = 4.\n\nA square has four equal sides and four right angles."
    original = "EXCLUDED_BEFORE_SENTINEL\n" + text + "\nEXCLUDED_AFTER_SENTINEL"
    path = materials / "english.txt"
    path.write_bytes(original.encode("utf-8"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    repo = Repository(config.data_dir)
    files = CourseFileStore(repo)
    course = files.bind(None, str(materials), "合成英文对照验证")["course_id"]
    opened = files.open(course, "english.txt")
    actual, _ = SourceStore(repo).text(
        course, opened["asset_id"], opened["source_version"]
    )
    start = actual.index(text)
    source = SourceStore(repo).capture_text(
        course, opened["asset_id"], opened["source_version"], start, start + len(text)
    )
    if source.text != text:
        raise RuntimeError("EXACT_SELECTION_REQUIRED")
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="aligned_real",
            kind="aligned_translation",
            instruction="将指定英文逐段对照翻译为中文，保留原文和算式，不添加资料外解释。",
            sources_only=True,
            sources=[{"kind": "selection", "snapshot_id": source.snapshot_id}],
            target={"mode": "create", "title": "英文段落对照"},
            settings=ModelSettings(
                model=args.model,
                thinking=args.thinking,
                max_output_tokens=args.max_output_tokens,
                timeout_seconds=args.timeout,
            ),
        ),
    )
    service, executor = GenerationService(repo, config), None
    try:
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        await asyncio.wait_for(executor._queue.join(), args.timeout + 35)
        task = TaskStore(repo).get(course, task.task_id)
        if task.state != "completed":
            raise RuntimeError("ALIGNED_TRANSLATION_NOT_COMPLETED")
        assets = ArtifactStore(repo).list(course)
        if len(assets) != 1 or assets[0].kind != "translation":
            raise RuntimeError("PAIRED_TRANSLATION_NOT_PUBLISHED")
        artifact = assets[0]
        if any(block.kind != "translation" for block in artifact.revision.body.blocks):
            raise RuntimeError("UNPAIRED_RESULT")
        if (
            "".join(
                block.translation.source_text for block in artifact.revision.body.blocks
            )
            != text
        ):
            raise RuntimeError("SOURCE_CHARACTERS_CHANGED")
        if (
            artifact.revision.author != "model"
            or artifact.revision.content_role != "ai_generated"
        ):
            raise RuntimeError("TRANSLATION_ROLE_CHANGED")
        with OfflineExporter(repo).build(
            course, artifact.artifact_id, artifact.head_revision_id
        ) as stream:
            with (directory / "offline.zip").open("wb") as target:
                shutil.copyfileobj(stream, target)
        record.update(
            status="passed",
            task=task.model_dump(mode="json"),
            artifact=artifact.model_dump(mode="json"),
            boundary_records=service.execution.boundary.records,
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
        with repo.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(s.task_runs.c.task_id, s.task_runs.c.run_id)
                ).mappings()
            )
        record["runs"] = [
            TaskRunStore(repo)
            .get(course, row["task_id"], row["run_id"])
            .model_dump(mode="json")
            for row in rows
        ]
        record["actual_outbound_attempts"] = sum(
            run["actual_outbound_attempts"] for run in record["runs"]
        )
        record["raw_usage"] = [
            usage for run in record["runs"] for usage in run["usage"]
        ]
        record["task"] = (
            TaskStore(repo).get(course, task.task_id).model_dump(mode="json")
        )
        record["original_unchanged"] = (
            hashlib.sha256(path.read_bytes()).hexdigest() == digest
        )
        record["source_after"] = freeze()
        record["source_changed_during_run"] = (
            record["source_before"] != record["source_after"]
        )
        if record["source_changed_during_run"] or not record["original_unchanged"]:
            record["status"] = "failed"
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        repo.close()
    print(
        json.dumps(
            {
                "status": record["status"],
                "actual_outbound_attempts": record["actual_outbound_attempts"],
                "evidence": str(directory / "summary.json"),
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
    parser.add_argument("--timeout", type=float, default=120)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
