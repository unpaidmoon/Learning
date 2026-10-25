"""Confirmed synthetic crop -> deep solution -> chat checks -> visualization/path."""

import argparse
import asyncio
import hashlib
import io
import json
import re
import shutil
from pathlib import Path

from dotenv import dotenv_values
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.paths import PathStore
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.models import ModelSettings, SubmittedImageRef
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def frozen():
    paths = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "backend/uv.lock",
        Path(__file__).resolve(),
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
        "stages": {},
        "source_before": frozen(),
        "layer": "actual provider + native admission/task/chat + saved visualization/path",
        "user_learning_experience": "not_run",
        "materials": "self-created math question crop only; no user video/screen/material",
    }

    def save():
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
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
        runtime_dir=directory / "run",
    )
    if not dotenv_values(config.secret_file).get("DEEPSEEK_API_KEY"):
        record["missing_prerequisite"] = "PRIVATE_CREDENTIAL_NOT_CONFIGURED"
        save()
        return 2
    frame = Image.new("RGB", (920, 410), "#dd3040")
    draw = ImageDraw.Draw(frame)
    draw.rectangle((0, 0, 919, 259), fill="white")
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 27)
    for y, text in zip(
        (18, 65, 112, 159, 206),
        (
            "Real parameter m; find every real solution x of:",
            "sqrt(x + 1) + sqrt(3 - x) = m",
            "Discuss the number of solutions for all real m.",
            "Check endpoints and extraneous roots after squaring.",
            "Give complete reasoning and a second method.",
        ),
        strict=True,
    ):
        draw.text((20, y), text, font=font, fill="black")
    draw.text((20, 320), "UNSUBMITTED NEIGHBOUR: WRONG ANSWER", font=font, fill="black")
    frame.save(directory / "unsubmitted-local-frame.png")
    image = frame.crop((0, 0, 920, 260))
    data = io.BytesIO()
    image.save(data, format="PNG")
    approved = data.getvalue()
    (directory / "confirmed-question.png").write_bytes(approved)
    record["confirmed_sha256"] = hashlib.sha256(approved).hexdigest()
    record["unsubmitted_sha256"] = hashlib.sha256(
        (directory / "unsubmitted-local-frame.png").read_bytes()
    ).hexdigest()
    original_generation = None
    prior_request_ids = set()
    expected_task_count = 1
    if args.resume_from:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args.resume_from):
            raise ValueError("OWNED_RESUME_ID_INVALID")
        previous = PROJECT_ROOT / ".runtime/validation" / args.resume_from
        prior = json.loads((previous / "summary.json").read_text("utf-8"))
        prior_request_ids = {
            row["request_id"] for row in prior.get("boundary_records", [])
        }
        if prior.get("confirmed_sha256") != record["confirmed_sha256"]:
            raise RuntimeError("RESUME_CONFIRMED_IMAGE_CHANGED")
        shutil.copytree(previous / "data", config.data_dir)
        record["resume_from"] = args.resume_from
        record["stages"] = prior.get("stages", {})
        repo = Repository(config.data_dir)
        course = repo.get_chat("deep_chain_chat")["course_id"]
        original_generation = prior.get("preserved_original_generation", {}).get(
            "generation_id"
        )
        if not original_generation:
            with repo.engine.connect() as conn:
                original_generation = conn.execute(
                    select(business.generations.c.generation_id)
                ).scalar_one()
        original = repo.get_generation(original_generation)
        if original["state"] != "truncated":
            raise RuntimeError("RESUME_ORIGINAL_TERMINAL_NOT_KNOWN")
        record["preserved_original_generation"] = {
            "generation_id": original_generation,
            "state": original["state"],
        }
        attachment = None
    else:
        repo = Repository(config.data_dir)
        course = repo.create_course("自造参数方程组合验证")["course_id"]
        repo.create_chat(course, "deep_chain_chat")
        attachment = repo.stage_attachment(approved)
    service, executor = GenerationService(repo, config), None
    active_task_id = None
    if args.retry_truncated_task:
        previous_tasks = TaskStore(repo).list(course)
        if (
            not args.resume_from
            or not previous_tasks
            or any(
                value.kind != "deep_solution" or value.state != "failed"
                for value in previous_tasks
            )
        ):
            raise RuntimeError("EXPLICIT_TRUNCATED_TASK_RETRY_SCOPE_INVALID")
        old = max(previous_tasks, key=lambda value: value.settings.max_output_tokens)
        if old.state != "failed" or not old.units or not old.units[0].run_id:
            raise RuntimeError("EXPLICIT_RETRY_REQUIRES_KNOWN_FAILED_TASK")
        old_run = TaskRunStore(repo).get(course, old.task_id, old.units[0].run_id)
        if (
            old_run.state != "truncated"
            or args.max_output_tokens <= old.settings.max_output_tokens
        ):
            raise RuntimeError("EXPLICIT_RETRY_REQUIRES_KNOWN_TRUNCATION_AND_NEW_LIMIT")
        with repo.engine.connect() as conn:
            old_scope = conn.execute(
                select(s.tasks.c.scope_spec).where(s.tasks.c.task_id == old.task_id)
            ).scalar_one()
        fresh = TaskCoverage(repo).admit(
            course,
            TaskAdmission(
                operation_id=f"explicit_higher_output_after_known_truncation_{args.max_output_tokens}",
                chat_id="deep_chain_chat",
                kind="deep_solution",
                instruction=old.instruction,
                sources=old_scope["sources"],
                sources_only=old.sources_only,
                target={"mode": "create", "title": "参数方程深入解答"},
                settings=ModelSettings(
                    model=args.model,
                    thinking=args.thinking,
                    max_output_tokens=args.max_output_tokens,
                    timeout_seconds=args.timeout,
                ),
            ),
        )
        active_task_id = fresh.task_id
        expected_task_count = len(previous_tasks) + 1
        record["explicit_retry"] = {
            "original_task_id": old.task_id,
            "original_run_id": old_run.run_id,
            "old_limit": old.settings.max_output_tokens,
            "new_limit": args.max_output_tokens,
            "new_task_id": fresh.task_id,
            "reason": "known output truncation; new explicit validation operation",
        }

    if args.resume_from and not args.retry_truncated_task:
        existing = TaskStore(repo).list(course)
        completed = [
            value
            for value in existing
            if value.kind == "deep_solution" and value.state == "completed"
        ]
        if len(completed) == 1:
            active_task_id = completed[0].task_id
            expected_task_count = len(existing)

    async def submit(identity, text, *, source_ids=None, attachments=None):
        prior_stage = record["stages"].get(identity, {})
        if args.resume_from and prior_stage.get("generation_id"):
            saved_generation = repo.get_generation(prior_stage["generation_id"])
            if saved_generation["state"] != "completed":
                raise RuntimeError("RESUMED_CHAT_STAGE_IS_NOT_COMPLETE")
            record.setdefault("reused_completed_chat_stages", []).append(identity)
            return saved_generation
        response = await service.submit(
            {
                "chat_id": "deep_chain_chat",
                "submission_id": identity,
                "text": text,
                "source_snapshot_ids": source_ids or [],
                "attachment_ids": attachments or [],
                "model": args.model,
                "thinking": args.thinking,
                "max_output_tokens": args.max_output_tokens,
                "timeout_seconds": args.timeout,
            }
        )
        await asyncio.wait_for(
            service.tasks[response["generation_id"]], args.timeout * 4 + 30
        )
        generation = repo.get_generation(response["generation_id"])
        if generation["state"] != "completed":
            raise RuntimeError(
                "CHAT_STAGE_NOT_COMPLETED:" + identity + ":" + generation["state"]
            )
        record["stages"][identity] = {
            "generation_id": generation["generation_id"],
            "body": generation["body"],
        }
        save()
        return generation

    def capture(artifact):
        return [
            SourceStore(repo).capture_artifact(
                course, artifact.artifact_id, artifact.head_revision_id, block.block_id
            )
            for block in artifact.revision.body.blocks
        ]

    async def task(identity, kind, instruction, snapshots, title):
        operation = identity
        prior_stage = record["stages"].get(identity, {})
        if args.retry_stage == identity:
            old = TaskStore(repo).get(course, prior_stage["task"]["task_id"])
            old_run = TaskRunStore(repo).get(course, old.task_id, old.units[0].run_id)
            if (
                old.state != "failed"
                or old_run.state != "truncated"
                or args.max_output_tokens <= old.settings.max_output_tokens
            ):
                raise RuntimeError(
                    "STAGE_RETRY_REQUIRES_KNOWN_TRUNCATION_AND_NEW_LIMIT"
                )
            with repo.engine.connect() as conn:
                scope = conn.execute(
                    select(s.tasks.c.scope_spec).where(s.tasks.c.task_id == old.task_id)
                ).scalar_one()
            declarations = [
                {"kind": "selection", "snapshot_id": snapshot.snapshot_id}
                for snapshot in snapshots
            ]
            if scope["sources"] != declarations:
                raise RuntimeError("STAGE_RETRY_SOURCE_SCOPE_CHANGED")
            operation = f"{identity}_explicit_retry_{args.max_output_tokens}"
            record.setdefault("explicit_stage_retries", []).append(
                {
                    "old_task_id": old.task_id,
                    "old_run_id": old_run.run_id,
                    "old_limit": old.settings.max_output_tokens,
                    "new_limit": args.max_output_tokens,
                    "new_operation_id": operation,
                }
            )
        value = TaskCoverage(repo).admit(
            course,
            TaskAdmission(
                operation_id=operation,
                chat_id="deep_chain_chat",
                kind=kind,
                instruction=instruction,
                sources=[
                    {"kind": "selection", "snapshot_id": snapshot.snapshot_id}
                    for snapshot in snapshots
                ],
                target={"mode": "create", "title": title},
                sources_only=True,
                settings=ModelSettings(
                    model=args.model,
                    thinking=args.thinking,
                    max_output_tokens=args.max_output_tokens,
                    timeout_seconds=args.timeout,
                ),
            ),
        )
        executor.schedule(course, value.task_id)
        await asyncio.wait_for(executor._queue.join(), args.timeout * 4 + 30)
        current = TaskStore(repo).get(course, value.task_id)
        record["stages"][identity] = {"task": current.model_dump(mode="json")}
        if current.state != "completed":
            save()
            raise RuntimeError(
                "TASK_STAGE_NOT_COMPLETED:" + identity + ":" + current.state
            )
        target = TaskCoverage(repo).get(course, value.task_id).target
        artifact = ArtifactStore(repo).get(course, target.artifact_id)
        record["stages"][identity]["artifact"] = artifact.model_dump(mode="json")
        save()
        return artifact

    try:
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        if attachment:
            await submit(
                "deep_request",
                "请生成完整解答，依据这张已提交题图讨论图中参数的所有情况并检验增根，保存为可编辑成果。",
                attachments=[attachment["attachment_id"]],
            )
        await asyncio.wait_for(executor._queue.join(), args.timeout * 4 + 30)
        tasks = TaskStore(repo).list(course)
        if active_task_id:
            tasks = [value for value in tasks if value.task_id == active_task_id]
        if (
            len(tasks) != 1
            or tasks[0].kind != "deep_solution"
            or tasks[0].state != "completed"
        ):
            raise RuntimeError("NATIVE_DEEP_SOLUTION_NOT_ADMITTED_OR_COMPLETED")
        target = TaskCoverage(repo).get(course, tasks[0].task_id).target
        solution = ArtifactStore(repo).get(course, target.artifact_id)
        record["stages"]["deep_solution"] = {
            "task": tasks[0].model_dump(mode="json"),
            "artifact": solution.model_dump(mode="json"),
        }
        sources = capture(solution)
        with repo.engine.connect() as conn:
            images = [
                SourceStore.in_transaction(conn, course, row)
                for row in conn.execute(select(s.snapshots.c.snapshot_id)).scalars()
            ]
        confirmed = [
            snapshot
            for snapshot in images
            if isinstance(snapshot.reference, SubmittedImageRef)
        ]
        if (
            len(confirmed) != 1
            or confirmed[0].reference.image_sha256 != record["confirmed_sha256"]
        ):
            raise RuntimeError("CONFIRMED_QUESTION_SCOPE_NOT_EXACT")
        source_ids = [source.snapshot_id for source in sources]
        await submit(
            "verify_same_question",
            "请验证选中的刚才解答。我自己的判断是只要m不大于2√2就有解，所以负m也行；请指出这一步哪里错，并核对端点。只在当前对话回复，不生成新的学习成果。",
            source_ids=source_ids,
        )
        await submit(
            "alternate_same_question",
            "请沿用原图条件，用三角代换或几何给另一种解法，区分m=2和m=2√2。只回复此对话，不另建成果。",
            source_ids=source_ids,
        )
        if (
            len(TaskStore(repo).list(course)) != expected_task_count
            or len(ArtifactStore(repo).list(course)) != 1
        ):
            raise RuntimeError("ORDINARY_VERIFY_OR_ALTERNATE_CREATED_UNREQUESTED_ASSET")
        visualization = await task(
            "explicit_visualization",
            "visualization",
            "为原图参数方程制作恰好一个cartesian互动图，表达式sqrt(x+1)+sqrt(3-x)-m，x范围[-1,3]、y范围[-3,3]，可调参数m范围[-3,3]、步长0.05、初值2.5，probe_x=1；说明零点与解数、负m和两个端点。不要动画，不补外部资料。",
            [*confirmed, *sources],
            "参数方程零点互动图",
        )
        path = await task(
            "explicit_path",
            "learning_path",
            "按原题、解答和互动图给出恰好三步中文建议学习路线：先核定义域，再做参数分类，最后核对平方增根与另一种方法。每步有回忆问题和讲解，questions为空；可以自由跳过，不写用户已掌握、作答或评分。",
            [*confirmed, *sources, *capture(visualization)],
            "参数方程建议学习路线",
        )
        progress = PathStore(repo).get(course, path.artifact_id)
        if len(progress.steps) != 3 or any(
            step.visited or step.answers or step.self_assessment
            for step in progress.steps
        ):
            raise RuntimeError("PATH_GENERATION_FABRICATED_USER_LEARNING_EVIDENCE")
        record["path_state"] = progress.model_dump(mode="json")
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
            record["qa_raw_usage"] = [
                dict(row)
                for row in conn.execute(select(business.usage_records)).mappings()
            ]
            record["task_raw_usage"] = [
                dict(row) for row in conn.execute(select(s.run_usage)).mappings()
            ]
            record["image_proofs"] = [
                row["proof"]["native_image"]
                for row in conn.execute(
                    select(s.run_inputs).where(s.run_inputs.c.representation == "image")
                ).mappings()
                if (row["proof"] or {}).get("native_image")
            ]
        if any(
            proof["source_sha256"] != record["confirmed_sha256"]
            for proof in record["image_proofs"]
        ):
            record.update(status="failed", error="UNCONFIRMED_IMAGE_SOURCE_IN_TASK")
        record["boundary_records"] = (
            service.execution.boundary.records if service.execution else []
        )
        if original_generation and any(
            row.get("generation_id") == original_generation
            and row["request_id"] not in prior_request_ids
            for row in record["boundary_records"]
        ):
            record.update(
                status="failed", error="ORIGINAL_TRUNCATED_REQUEST_WAS_REPLAYED"
            )
        record["source_after"] = frozen()
        record["source_changed_during_run"] = (
            record["source_before"] != record["source_after"]
        )
        if record["source_changed_during_run"]:
            record["status"] = "failed"
        save()
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
    parser.add_argument(
        "--resume-from", help="仅复制指定自有验证数据；原冻结目录保持不变"
    )
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument(
        "--retry-truncated-task",
        action="store_true",
        help="显式新任务及更高输出参数，保留原已知截断任务",
    )
    parser.add_argument("--thinking", default="high")
    parser.add_argument(
        "--retry-stage", choices=["explicit_visualization", "explicit_path"]
    )
    parser.add_argument("--max-output-tokens", type=int, default=6144)
    parser.add_argument("--timeout", type=float, default=240)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
