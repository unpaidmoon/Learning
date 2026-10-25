"""Owned real-provider translation, saved-result reuse, and explicit fresh generation."""

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
from learning_app.workbench.models import ModelSettings, TaskCreate, TaskUnitInput
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.tasks import TaskStore


def freeze():
    paths = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "backend/uv.lock",
        PROJECT_ROOT / "scripts/validation/Run-TranslationReuseReal.py",
    ]
    return {
        p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in paths
    }


async def run(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "status": "not_run",
        "layer": "actual provider + pinned Host + formal saved-translation reuse; original LRU browser evidence is separate",
        "input": "self-created selected English sentence; no user material, images, screen or video",
        "user_learning_experience": "not_run",
        "source_before": freeze(),
    }
    (directory / "source-before.json").write_text(
        json.dumps(record["source_before"], indent=2), "utf-8"
    )
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
    material = directory / "synthetic-course"
    material.mkdir()
    sentence = "An even integer is divisible by two. The number 8 is even."
    original_text = (
        "EXCLUDED_BEFORE_SENTINEL\n" + sentence + "\nEXCLUDED_AFTER_SENTINEL"
    )
    path = material / "lesson.txt"
    path.write_text(original_text, "utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    repo = Repository(config.data_dir)
    files = CourseFileStore(repo)
    course = files.bind(None, str(material), "合成翻译缓存验证")["course_id"]
    opened = files.open(course, "lesson.txt")
    start = original_text.index(sentence)
    source = SourceStore(repo).capture_text(
        course,
        opened["asset_id"],
        opened["source_version"],
        start,
        start + len(sentence),
    )
    value = TaskCreate(
        operation_id="first",
        kind="translation",
        instruction="翻译成中文，保留公式和术语。",
        settings=ModelSettings(
            model=args.model,
            thinking=args.thinking,
            max_output_tokens=args.max_output_tokens,
            timeout_seconds=args.timeout,
        ),
        units=[
            TaskUnitInput(
                unit_id="selection_0",
                label="选区 1",
                source_snapshot_id=source.snapshot_id,
            )
        ],
    )
    first = TaskStore(repo).create(course, value)
    service = GenerationService(repo, config)
    executor = None
    try:
        await service.open()
        service.translation_reuse.remember_admission(first)
        executor = TaskExecutor(repo, service)
        await executor.open()
        await asyncio.wait_for(executor._queue.join(), args.timeout + 30)
        first = TaskStore(repo).get(course, first.task_id)
        if first.state != "completed":
            raise RuntimeError("FIRST_TRANSLATION_NOT_COMPLETED")
        original_run = (
            TaskRunStore(repo)
            .get(course, first.task_id, first.units[0].run_id)
            .model_dump(mode="json")
        )
        original_artifact = ArtifactStore(repo).get(
            course, first.units[0].result_artifact_id
        )
        entry = service.translation_reuse.entry(course, first.task_id)
        if entry.status != "eligible":
            raise RuntimeError("ACTUAL_RESULT_NOT_CACHE_ELIGIBLE")
        reused = service.translation_reuse.reuse(
            course,
            value.model_copy(update={"operation_id": "reuse_saved"}),
            first.task_id,
        )
        if (
            not reused
            or not reused.translation_reuse
            or reused.translation_reuse.kind != "saved_result"
        ):
            raise RuntimeError("SAVED_RESULT_REUSE_FAILED")
        after = (
            TaskRunStore(repo)
            .get(course, first.task_id, first.units[0].run_id)
            .model_dump(mode="json")
        )
        if (
            original_run != after
            or ArtifactStore(repo).get(course, original_artifact.artifact_id)
            != original_artifact
        ):
            raise RuntimeError("REUSE_CHANGED_ORIGINAL_FACTS")
        with repo.engine.connect() as conn:
            runs_after_reuse = list(
                conn.execute(select(s.task_runs.c.run_id)).scalars()
            )
        if runs_after_reuse != [original_run["run_id"]]:
            raise RuntimeError("REUSE_CREATED_ANOTHER_RUN")
        record.update(
            first_task=first.model_dump(mode="json"),
            reuse_receipt=reused.model_dump(mode="json"),
            original_run_unchanged_after_reuse=True,
            actual_new_outbound_for_reuse=0,
            first_artifact=original_artifact.model_dump(mode="json"),
        )
        fresh = TaskStore(repo).create(
            course, value.model_copy(update={"operation_id": "explicit_fresh"})
        )
        service.translation_reuse.remember_admission(fresh)
        executor.schedule(course, fresh.task_id)
        await asyncio.wait_for(executor._queue.join(), args.timeout + 30)
        fresh = TaskStore(repo).get(course, fresh.task_id)
        if fresh.state != "completed" or fresh.task_id == first.task_id:
            raise RuntimeError("EXPLICIT_FRESH_TRANSLATION_FAILED")
        record["fresh_task"] = fresh.model_dump(mode="json")
        record["fresh_artifact"] = (
            ArtifactStore(repo)
            .get(course, fresh.units[0].result_artifact_id)
            .model_dump(mode="json")
        )
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
        with repo.engine.connect() as conn:
            identities = list(
                conn.execute(
                    select(s.task_runs.c.task_id, s.task_runs.c.run_id)
                ).mappings()
            )
        record["runs"] = [
            TaskRunStore(repo)
            .get(course, row["task_id"], row["run_id"])
            .model_dump(mode="json")
            for row in identities
        ]
        record["actual_outbound_attempts"] = sum(
            run["actual_outbound_attempts"] for run in record["runs"]
        )
        record["raw_usage"] = [
            usage for run in record["runs"] for usage in run["usage"]
        ]
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
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=120)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
