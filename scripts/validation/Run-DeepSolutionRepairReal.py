"""Explicitly repair three saved assets; preserve all failed runs and old revisions."""

import argparse
import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import ModelSettings, SubmittedImageRef
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def frozen():
    files = [
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
        for path in files
    }


async def run(args):
    if not all(
        re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in (args.run_id, args.from_run)
    ):
        raise ValueError("OWNED_RUN_ID_INVALID")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    previous = PROJECT_ROOT / ".runtime/validation" / args.from_run
    prior = json.loads((previous / "summary.json").read_text("utf-8"))
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
        raise RuntimeError("PRIVATE_CREDENTIAL_NOT_CONFIGURED")
    shutil.copytree(previous / "data", config.data_dir)
    repo = Repository(config.data_dir)
    course = repo.get_chat("deep_chain_chat")["course_id"]
    record = {
        "status": "running",
        "source_before": frozen(),
        "stages": {},
        "from_run": args.from_run,
        "baseline_request_ids": [
            row["request_id"] for row in prior["boundary_records"]
        ],
        "user_learning_experience": "not_run",
        "layer": "actual provider + explicit same-asset fixes + history preserved",
    }

    def save():
        (directory / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )

    store = ArtifactStore(repo)
    solution = store.get(
        course, prior["stages"]["deep_solution"]["artifact"]["artifact_id"]
    )
    plot = store.get(
        course, prior["stages"]["explicit_visualization"]["artifact"]["artifact_id"]
    )
    path = store.get(
        course, prior["stages"]["explicit_path"]["artifact"]["artifact_id"]
    )
    originals = {
        asset.artifact_id: asset.model_dump(mode="json")
        for asset in (solution, plot, path)
    }
    with repo.engine.connect() as conn:
        snapshots = [
            SourceStore.in_transaction(conn, course, identity)
            for identity in conn.execute(select(s.snapshots.c.snapshot_id)).scalars()
        ]
    confirmed = [
        snapshot
        for snapshot in snapshots
        if isinstance(snapshot.reference, SubmittedImageRef)
    ]
    if (
        len(confirmed) != 1
        or confirmed[0].reference.image_sha256 != prior["confirmed_sha256"]
    ):
        raise RuntimeError("CONFIRMED_IMAGE_SCOPE_CHANGED")
    service, executor = GenerationService(repo, config), None

    def capture(asset, block_ids=None):
        return [
            SourceStore(repo).capture_artifact(
                course, asset.artifact_id, asset.head_revision_id, block.block_id
            )
            for block in asset.revision.body.blocks
            if block_ids is None or block.block_id in block_ids
        ]

    async def edit(identity, asset, kind, block_ids, instruction, sources):
        task = TaskCoverage(repo).admit(
            course,
            TaskAdmission(
                operation_id=identity,
                chat_id="deep_chain_chat",
                kind=kind,
                instruction=instruction,
                sources=[
                    {"kind": "selection", "snapshot_id": source.snapshot_id}
                    for source in sources
                ],
                target={
                    "mode": "edit",
                    "artifact_id": asset.artifact_id,
                    "expected_revision_id": asset.head_revision_id,
                    "block_ids": block_ids,
                },
                sources_only=True,
                settings=ModelSettings(
                    model=args.model,
                    thinking=args.thinking,
                    max_output_tokens=args.max_output_tokens,
                    timeout_seconds=args.timeout,
                ),
            ),
        )
        executor.schedule(course, task.task_id)
        await asyncio.wait_for(executor._queue.join(), args.timeout * 4 + 30)
        current = TaskStore(repo).get(course, task.task_id)
        record["stages"][identity] = {"task": current.model_dump(mode="json")}
        save()
        if current.state != "completed":
            raise RuntimeError(
                "REPAIR_STAGE_NOT_COMPLETED:" + identity + ":" + current.state
            )
        updated = store.get(course, asset.artifact_id)
        if updated.head_revision_id == asset.head_revision_id:
            raise RuntimeError("SAME_ASSET_REVISION_NOT_UPDATED")
        unchanged = {
            block.block_id: block.model_dump(mode="json")
            for block in asset.revision.body.blocks
            if block.block_id not in block_ids
        }
        if any(
            unchanged.get(block.block_id, block.model_dump(mode="json"))
            != block.model_dump(mode="json")
            for block in updated.revision.body.blocks
        ):
            raise RuntimeError("UNTARGETED_BLOCK_WAS_CHANGED")
        record["stages"][identity]["artifact"] = updated.model_dump(mode="json")
        save()
        return updated

    try:
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        typo = [
            block.block_id
            for block in solution.revision.body.blocks
            if "m=0" in block.markdown and "左边为 2" in block.markdown
        ]
        if len(typo) != 1:
            raise RuntimeError("EXACT_TYPO_BLOCK_NOT_IDENTIFIED")
        solution = await edit(
            "explicit_correct_solution_example",
            solution,
            "deep_solution",
            typo,
            "只修改指定段中m=0、x=1的反例：原方程左侧应为2√2而不是2。修正这个数值，其余推导、段落和原条件保持；不要改写其他块，不新增成果。",
            [*confirmed, *capture(solution, typo)],
        )
        if "左边为 2≠0" in "\n".join(
            block.markdown for block in solution.revision.body.blocks
        ):
            raise RuntimeError("SOLUTION_TYPO_NOT_CORRECTED")
        plot = await edit(
            "explicit_replace_placeholder_plot",
            plot,
            "visualization",
            [block.block_id for block in plot.revision.body.blocks],
            "将指定旧占位图实际替换为本题cartesian互动图。唯一表达式sqrt(x+1)+sqrt(3-x)-m；参数m最小-3最大3、step=0.05、initial=2.5；x_range[-1,3]，y_range[-3,3]，probe_x=1。不做动画，省略duration_seconds。保留原block_id，只输出这个visualization块；不要提交test/x占位草稿；expected_version用本次固定目标版本，stage_revision不是目标版本。",
            [*confirmed, *capture(solution), *capture(plot)],
        )
        charts = [
            block
            for block in plot.revision.body.blocks
            if block.kind == "visualization"
        ]
        if (
            len(charts) != 1
            or charts[0].visualization.payload.data.expression.replace(" ", "")
            != "sqrt(x+1)+sqrt(3-x)-m"
        ):
            raise RuntimeError("PLOT_DOES_NOT_MATCH_REQUESTED_EQUATION")
        path = await edit(
            "explicit_update_path_after_corrections",
            path,
            "learning_path",
            [block.block_id for block in path.revision.body.blocks],
            "仅更新现有三个学习步骤，保持原block_id。根据已修正解答与实际根式互动图，删除旧占位图不能使用的提示，修正任何m=0、x=1时左侧为2的错误；正确为2√2。保留三步顺序：定义域、参数分类、增根与另一种方法，每步有回忆问题和完整解释，questions为空。允许自由跳过；不写用户已掌握或评分。",
            [*confirmed, *capture(solution), *capture(plot), *capture(path)],
        )
        if len(store.list(course)) != 3 or len(repo.list_chats(course)) != 1:
            raise RuntimeError("REPAIR_CREATED_EXTRA_ASSET_OR_CHAT")
        record["status"] = "passed"
        record["original_versions"] = originals
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
        record["actual_outbound_attempts_cumulative"] = (
            service.execution.boundary.actual_outbound_attempts
            if service.execution
            else None
        )
        record["boundary_records"] = (
            service.execution.boundary.records if service.execution else []
        )
        baseline = set(record["baseline_request_ids"])
        record["new_request_ids"] = [
            row["request_id"]
            for row in record["boundary_records"]
            if row["request_id"] not in baseline
        ]
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
            proof["source_sha256"] != prior["confirmed_sha256"]
            for proof in record["image_proofs"]
        ):
            record.update(status="failed", error="IMAGE_SCOPE_EXPANDED")
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
                    "actual_outbound_attempts_cumulative",
                    "new_request_ids",
                )
            },
            ensure_ascii=False,
        )
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--from-run", default="deep-solution-chain-real-20261024-r6")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=65536)
    parser.add_argument("--timeout", type=float, default=600)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
