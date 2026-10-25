"""Self-created long note -> actual fixed native model -> original-CP edits."""

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
from learning_app.workbench.models import ArtifactCreate, ModelSettings
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_planning import TaskPlanner
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def freeze():
    files = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        *sorted((PROJECT_ROOT / "resources/workbench").glob("*.lock.json")),
        PROJECT_ROOT / "scripts/validation/Run-RangeEditsReal.py",
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
        "layer": "actual provider + fixed native Harness + long target CP patch publications",
        "input": "self-created long English note; no user material, images, history, video or screen",
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
    materials = directory / "synthetic-course"
    materials.mkdir()
    text = "".join(
        f"Paragraph {index:03d}: the old note keeps the formula $8 / 2 = 4$ and symbol 🧠.\n\n"
        for index in range(360)
    )
    assert len(text) > 24_000
    path = materials / "long-note.txt"
    path.write_bytes(
        ("EXCLUDED_BEFORE_SENTINEL\n" + text + "EXCLUDED_AFTER_SENTINEL").encode(
            "utf-8"
        )
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    repo = Repository(config.data_dir)
    files = CourseFileStore(repo)
    course = files.bind(None, str(materials), "合成长笔记分段修改验证")["course_id"]
    opened = files.open(course, path.name)
    actual, _ = SourceStore(repo).text(
        course, opened["asset_id"], opened["source_version"]
    )
    start = actual.index(text)
    source = SourceStore(repo).capture_text(
        course, opened["asset_id"], opened["source_version"], start, start + len(text)
    )
    assert source.text == text
    artifact = (
        ArtifactStore(repo)
        .create(
            course,
            ArtifactCreate(
                operation_id="long_note",
                kind="note",
                title="长篇学习笔记",
                body={
                    "blocks": [
                        {
                            "block_id": "body",
                            "kind": "markdown",
                            "markdown": text,
                            "attribution": "source_paraphrase",
                            "source_snapshot_ids": [source.snapshot_id],
                        },
                        {
                            "block_id": "guard",
                            "kind": "markdown",
                            "markdown": "UNEDITED_BLOCK_SENTINEL",
                            "attribution": "user_authored",
                            "source_snapshot_ids": [],
                        },
                    ]
                },
            ),
        )
        .artifact
    )
    task = TaskCoverage(repo).admit(
        course,
        TaskAdmission(
            operation_id="range_edit_real",
            kind="note",
            instruction="只把指定正文内每一个英文单词 old 替换为 modern。保留其他全部字符、段落编号、空白、换行、公式和🧠。不要概括、删行、补写或改写其他文字。",
            sources=[],
            coverage_mode="complete",
            sources_only=True,
            target={
                "mode": "edit",
                "artifact_id": artifact.artifact_id,
                "expected_revision_id": artifact.head_revision_id,
                "block_ids": ["body"],
            },
            settings=ModelSettings(
                model=args.model,
                thinking=args.thinking,
                max_output_tokens=args.max_output_tokens,
                timeout_seconds=args.timeout,
            ),
        ),
    )
    TaskCoverage(repo).refresh(course, task.task_id)
    TaskPlanner(repo).plan(course, task.task_id)
    task = TaskStore(repo).get(course, task.task_id)
    assert len(task.units) > 1
    service, executor = GenerationService(repo, config), None
    try:
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        await asyncio.wait_for(
            executor._queue.join(), args.timeout * len(task.units) * 3 + 45
        )
        task = TaskStore(repo).get(course, task.task_id)
        if task.state != "completed":
            raise RuntimeError("RANGE_EDIT_NOT_COMPLETED")
        current = ArtifactStore(repo).get(course, artifact.artifact_id)
        if current.revision.body.blocks[0].markdown != text.replace("old", "modern"):
            raise RuntimeError("ACTUAL_EDIT_CHANGED_UNREQUESTED_CHARACTERS")
        if current.revision.body.blocks[1] != artifact.revision.body.blocks[1]:
            raise RuntimeError("UNEDITED_BLOCK_CHANGED")
        if (
            len(ArtifactStore(repo).list(course)) != 1
            or current.content_role != "mixed"
        ):
            raise RuntimeError("TARGET_IDENTITY_CHANGED")
        record.update(status="passed", artifact=current.model_dump(mode="json"))
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
            rows = list(conn.execute(select(s.task_runs.c.run_id)).scalars())
            receipts = list(
                conn.execute(
                    select(s.operations.c.result).where(
                        s.operations.c.kind == "task_range_patch"
                    )
                ).scalars()
            )
        record["runs"] = [
            TaskRunStore(repo)
            .get(course, task.task_id, identity)
            .model_dump(mode="json")
            for identity in rows
        ]
        record["actual_outbound_attempts"] = sum(
            item["actual_outbound_attempts"] for item in record["runs"]
        )
        record["raw_usage"] = [
            usage for item in record["runs"] for usage in item["usage"]
        ]
        record["patch_receipts"] = receipts
        record["task"] = (
            TaskStore(repo).get(course, task.task_id).model_dump(mode="json")
        )
        record["artifact_current"] = (
            ArtifactStore(repo)
            .get(course, artifact.artifact_id)
            .model_dump(mode="json")
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
    parser.add_argument("--max-output-tokens", type=int, default=12000)
    parser.add_argument("--timeout", type=float, default=180)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
