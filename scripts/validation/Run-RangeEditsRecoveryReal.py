"""Reconcile an owned frozen real run first, then execute only pending ranges."""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import re
import shutil

from sqlalchemy import select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import TaskAction
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_controls import TaskRecovery
from learning_app.workbench.tasks import TaskStore

support_spec = importlib.util.spec_from_file_location(
    "range_edit_validation", PROJECT_ROOT / "scripts/validation/Run-RangeEditsReal.py"
)
support = importlib.util.module_from_spec(support_spec)
support_spec.loader.exec_module(support)


async def run(args):
    if any(
        not re.fullmatch(r"[A-Za-z0-9_-]+", value)
        for value in (args.run_id, args.resume_from)
    ):
        raise ValueError("OWNED_RUN_ID_INVALID")
    original = PROJECT_ROOT / ".runtime/validation" / args.resume_from
    prior = json.loads((original / "summary.json").read_text("utf-8"))
    if prior["cleanup"]["host"]["remaining_pids"]:
        raise RuntimeError("ORIGINAL_OWNED_PROCESS_NOT_CLOSED")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    # Preserve the original failed run and its raw bytes; recovery has its own journal.
    shutil.copytree(original / "data", directory / "data")
    original_before = {
        path.relative_to(original).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in original.rglob("*")
        if path.is_file()
    }
    repo = Repository(directory / "data")
    course, task_id = prior["task"]["course_id"], prior["task"]["task_id"]
    previous_attempts = prior.get(
        "actual_outbound_attempts_cumulative", prior.get("actual_outbound_attempts")
    )
    with repo.engine.connect() as conn:
        target = (
            conn.execute(
                select(s.task_targets).where(s.task_targets.c.task_id == task_id)
            )
            .mappings()
            .one()
        )
        base = ArtifactStore._revision(
            conn, target["reserved_artifact_id"], target["base_revision_id"]
        )
    artifact_id = target["reserved_artifact_id"]
    settings = prior["task"]["settings"]
    config = AppConfig(
        mode="test",
        harness_enabled=True,
        credential_route="api_key",
        data_dir=repo.root,
        runtime_dir=directory / "runtime",
        model=settings["model"],
        thinking=settings["thinking"],
        max_output_tokens=settings["max_output_tokens"],
        model_timeout_seconds=settings["timeout_seconds"],
    )
    record = {
        "status": "not_run",
        "source_before": support.freeze(),
        "resumed_from": args.resume_from,
        "layer": "actual original terminal recovery, then pending native ranges only",
        "runner_sha256": hashlib.sha256(
            (
                PROJECT_ROOT / "scripts/validation/Run-RangeEditsRecoveryReal.py"
            ).read_bytes()
        ).hexdigest(),
        "user_learning_experience": "not_run",
    }
    service, executor = GenerationService(repo, config), None
    old_run = prior["task"]["units"][0]["run_id"]
    try:
        await service.open()
        before = service.execution.boundary.actual_outbound_attempts
        executor = TaskExecutor(repo, service)
        if prior["task"]["units"][0]["state"] != "completed":
            await executor.reconcile_run(course, task_id, old_run)
        recovered = TaskStore(repo).get(course, task_id)
        after = service.execution.boundary.actual_outbound_attempts
        if (
            after != before
            or recovered.units[0].state != "completed"
            or recovered.units[0].run_id != old_run
        ):
            raise RuntimeError("ORIGINAL_RANGE_NOT_RECONCILED_WITHOUT_RESEND")
        record["original_reconciliation"] = {
            "outbound_before": before,
            "outbound_after": after,
            "run_id": old_run,
            "unit": recovered.units[0].model_dump(mode="json"),
        }
        completed_before = {
            unit.unit_id: unit.run_id
            for unit in recovered.units
            if unit.state == "completed"
        }
        failed = [unit for unit in recovered.units if unit.state == "failed"]
        if failed:
            if not args.retry_known_failed:
                raise RuntimeError("EXPLICIT_KNOWN_FAILURE_RETRY_REQUIRED")
            for unit in failed:
                native = service.execution.native_tools
                native.registry.reconcile_effects(unit.run_id, native.port)
                if not native.registry.output_settled(unit.run_id):
                    raise RuntimeError("FAILED_RANGE_STILL_REQUIRES_RECONCILIATION")
                recovered = TaskRecovery(repo).retry_work(
                    course,
                    task_id,
                    unit.unit_id,
                    TaskAction(
                        operation_id="explicit_known_range_retry_" + unit.unit_id,
                        expected_revision=recovered.revision,
                    ),
                )
            record["explicit_known_failed_retries"] = [
                unit.model_dump(mode="json") for unit in failed
            ]
        await executor.open()
        await asyncio.wait_for(
            executor._queue.join(),
            settings["timeout_seconds"] * len(recovered.units) * 3 + 45,
        )
        current = TaskStore(repo).get(course, task_id)
        if current.state != "completed":
            raise RuntimeError("REMAINING_RANGE_EDIT_NOT_COMPLETED")
        product = ArtifactStore(repo).get(course, artifact_id)
        expected = base.body.blocks[0].markdown.replace("old", "modern")
        if product.revision.body.blocks[0].markdown != expected:
            raise RuntimeError("ACTUAL_EDIT_CHANGED_UNREQUESTED_CHARACTERS")
        if product.revision.body.blocks[1].model_dump(mode="json") != base.body.blocks[
            1
        ].model_dump(mode="json"):
            raise RuntimeError("UNEDITED_BLOCK_CHANGED")
        if any(
            unit.run_id != completed_before[unit.unit_id]
            for unit in current.units
            if unit.unit_id in completed_before
        ):
            raise RuntimeError("PREVIOUSLY_PUBLISHED_RANGE_WAS_REPLAYED")
        record.update(status="passed", artifact=product.model_dump(mode="json"))
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
        record["task"] = TaskStore(repo).get(course, task_id).model_dump(mode="json")
        with repo.engine.connect() as conn:
            identities = list(conn.execute(select(s.task_runs.c.run_id)).scalars())
        record["runs"] = [
            TaskRunStore(repo).get(course, task_id, identity).model_dump(mode="json")
            for identity in identities
        ]
        record["actual_outbound_attempts_cumulative"] = sum(
            item["actual_outbound_attempts"] for item in record["runs"]
        )
        record["new_actual_outbound_attempts"] = (
            record["actual_outbound_attempts_cumulative"] - previous_attempts
        )
        record["raw_usage"] = [
            usage for item in record["runs"] for usage in item["usage"]
        ]
        record["source_after"] = support.freeze()
        record["source_changed_during_run"] = (
            record["source_before"] != record["source_after"]
        )
        original_after = {
            path.relative_to(original).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in original.rglob("*")
            if path.is_file()
        }
        record["original_evidence_unchanged"] = original_before == original_after
        if (
            record["source_changed_during_run"]
            or not record["original_evidence_unchanged"]
        ):
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
                    "new_actual_outbound_attempts",
                    "actual_outbound_attempts_cumulative",
                    "original_reconciliation",
                )
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume-from", required=True)
    parser.add_argument("--retry-known-failed", action="store_true")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
