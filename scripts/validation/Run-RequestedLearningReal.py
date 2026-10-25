"""Owned natural request -> native admission -> saved learning task, real provider."""

import argparse
import asyncio
import hashlib
import io
import json
import re

from dotenv import dotenv_values
from sqlalchemy import select
from PIL import Image, ImageDraw

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.tasks import TaskStore


def freeze():
    paths = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "backend/uv.lock",
        PROJECT_ROOT / "scripts/validation/Run-RequestedLearningReal.py",
    ]
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


async def run(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "status": "not_run",
        "layer": "actual provider + pinned Host + native task admission and publication",
        "input": "self-created selected sentence only; no user materials, screen or video",
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
            json.dumps(record, ensure_ascii=False), "utf-8"
        )
        return 2
    materials = directory / "synthetic-course"
    materials.mkdir()
    sentence = "An even integer is divisible by two. The number 8 is even."
    text = "EXCLUDED_BEFORE_SENTINEL\n" + sentence + "\nEXCLUDED_AFTER_SENTINEL"
    path = materials / "lesson.txt"
    path.write_text(text, "utf-8")
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    repo = Repository(config.data_dir)
    files = CourseFileStore(repo)
    course = files.bind(None, str(materials), "合成明确请求验证")["course_id"]
    opened = files.open(course, "lesson.txt")
    actual_text, _ = SourceStore(repo).text(
        course, opened["asset_id"], opened["source_version"]
    )
    start = actual_text.index(sentence)
    source = SourceStore(repo).capture_text(
        course,
        opened["asset_id"],
        opened["source_version"],
        start,
        start + len(sentence),
    )
    if source.text != sentence:
        raise RuntimeError("SYNTHETIC_SELECTION_NOT_EXACT")
    attachments = []
    if args.with_image:
        picture = Image.new("RGB", (160, 64), "white")
        ImageDraw.Draw(picture).text((12, 16), "8 / 2 = 4", fill="black")
        data = io.BytesIO()
        picture.save(data, format="PNG")
        attachment = repo.stage_attachment(data.getvalue())
        attachments = [attachment["attachment_id"]]
        record["input"] = (
            "self-created exact English selection and synthetic confirmed equation crop; no user material or video/screen"
        )
        record["synthetic_image_sha256"] = attachment["content_hash"]
    repo.create_chat(course, "requested_real_chat")
    service, executor, generation_id = GenerationService(repo, config), None, None
    try:
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        result = await service.submit(
            {
                "chat_id": "requested_real_chat",
                "submission_id": "explicit_note",
                "text": "请根据这段资料制作学习笔记，用中文说明偶数定义并核对8这个例子。只依据所选资料，保存为可编辑成果。"
                if not attachments
                else "请根据所选资料和已提交题图制作学习笔记，用中文说明偶数定义并核对8这个例子。只依据所选文字和这张图片，保存为可编辑成果。",
                "source_snapshot_ids": [source.snapshot_id],
                "attachment_ids": attachments,
                "model": args.model,
                "thinking": args.thinking,
                "max_output_tokens": args.max_output_tokens,
                "timeout_seconds": args.timeout,
            }
        )
        generation_id = result["generation_id"]
        await asyncio.wait_for(service.tasks[generation_id], args.timeout + 35)
        await asyncio.wait_for(executor._queue.join(), args.timeout + 35)
        generation = repo.get_generation(generation_id)
        tasks = TaskStore(repo).list(course)
        if (
            generation["state"] != "completed"
            or len(tasks) != 1
            or tasks[0].state != "completed"
        ):
            raise RuntimeError("REQUESTED_TASK_NOT_COMPLETED")
        assets = ArtifactStore(repo).list(course)
        if (
            len(assets) != 1
            or assets[0].kind != "note"
            or assets[0].revision.author != "model"
        ):
            raise RuntimeError("REQUESTED_NOTE_NOT_PUBLISHED")
        with repo.engine.connect() as conn:
            origin = conn.execute(select(s.tasks.c.scope_spec)).scalar_one()["origin"]
            image_proofs = [
                row["proof"]["native_image"]
                for row in conn.execute(
                    select(s.run_inputs).where(s.run_inputs.c.representation == "image")
                ).mappings()
            ]
        if args.with_image and (
            len(image_proofs) != 1
            or image_proofs[0]["source_sha256"] != record["synthetic_image_sha256"]
        ):
            raise RuntimeError("SUBMITTED_IMAGE_PROOF_MISSING")
        record["image_proofs"] = image_proofs
        record["boundary_records"] = service.execution.boundary.records
        if args.with_image and not any(
            image.get("sha256") == image_proofs[0]["encoded_sha256"]
            for row in record["boundary_records"]
            for image in row.get("images", [])
        ):
            raise RuntimeError("ACTUAL_ENCODED_IMAGE_NOT_OBSERVED")
        if (
            origin["generation_id"] != generation_id
            or source.snapshot_id not in origin["source_snapshot_ids"]
        ):
            raise RuntimeError("ORIGINAL_REQUEST_SOURCE_LINK_MISSING")
        record.update(
            status="passed",
            task=tasks[0].model_dump(mode="json"),
            artifact=assets[0].model_dump(mode="json"),
            origin=origin,
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
        record["generation"] = repo.evidence(generation_id) if generation_id else None
        with repo.engine.connect() as conn:
            runs = list(
                conn.execute(
                    select(s.task_runs.c.task_id, s.task_runs.c.run_id)
                ).mappings()
            )
            admissions = list(
                conn.execute(
                    select(s.operations.c.result).where(
                        s.operations.c.kind == "native_task_admission"
                    )
                ).scalars()
            )
        record["admissions"] = admissions
        record["runs"] = [
            TaskRunStore(repo)
            .get(course, row["task_id"], row["run_id"])
            .model_dump(mode="json")
            for row in runs
        ]
        generation = record["generation"]["generation"] if generation_id else {}
        record["actual_outbound_attempts"] = generation.get(
            "real_outbound_attempt_count", 0
        ) + sum(run["actual_outbound_attempts"] for run in record["runs"])
        record["raw_usage"] = [
            *generation.get("usage", []),
            *(usage for run in record["runs"] for usage in run["usage"]),
        ]
        record["original_unchanged"] = (
            hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
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
    parser.add_argument("--with-image", action="store_true")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
