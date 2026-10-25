"""Explicitly retry known rejected book work without repeating the outline."""

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
from learning_app.workbench import practice_schema as practice
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.books import BookStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import TaskAction
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_controls import TaskRecovery
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.tasks import TaskStore


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def freeze():
    return {
        p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in [
            *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
            *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
            PROJECT_ROOT / "harness/runtime.lock.json",
            Path(__file__),
        ]
    }


async def run(args):
    for value in (args.run_id, args.source_run):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("OWNED_RUN_ID_INVALID")
    base = (PROJECT_ROOT / ".runtime/validation").resolve()
    old, out = base / args.source_run, base / args.run_id
    assert old.resolve().parent == base and out.resolve().parent == base
    prior = json.loads((old / "summary.json").read_text("utf-8"))
    assert prior["task"]["state"] == "failed"
    original_data = hashes(old / "data")
    out.mkdir(exist_ok=False)
    shutil.copytree(old / "data", out / "data")
    record = {
        "status": "not_run",
        "source_before": freeze(),
        "source_run": args.source_run,
        "layer": "actual provider + fixed native Harness; explicit known-failed work retry",
        "user_learning_experience": "not_run",
    }
    repo = Repository(out / "data")
    course, identity = prior["task"]["course_id"], prior["task"]["task_id"]
    config = AppConfig(
        mode="test",
        harness_enabled=True,
        credential_route="api_key",
        model="deepseek-flash",
        thinking="high",
        max_output_tokens=16384,
        model_timeout_seconds=300,
        data_dir=out / "data",
        runtime_dir=out / "runtime",
    )
    service, executor = GenerationService(repo, config), None
    old_attempts = prior.get(
        "actual_outbound_attempts_cumulative", prior.get("actual_outbound_attempts")
    )
    try:
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        await asyncio.wait_for(executor._queue.join(), 90)
        assert service.execution.boundary.actual_outbound_attempts == old_attempts
        current = TaskStore(repo).get(course, identity)
        failed = next(u for u in current.units if u.state == "failed")
        old_run = TaskRunStore(repo).get(course, identity, failed.run_id)
        assert old_run.state == "completed" and old_run.usage_status == "known"
        assert BookStore(repo).outline(course, identity).confirmation_count == 1
        TaskRecovery(repo).retry_work(
            course,
            identity,
            failed.unit_id,
            TaskAction(
                operation_id=f"retry_stage_feedback_{failed.unit_id}_{current.revision}",
                expected_revision=current.revision,
            ),
        )
        executor.schedule(course, identity)
        await asyncio.wait_for(executor._queue.join(), 1800)
        current = TaskStore(repo).get(course, identity)
        record["task"] = current.model_dump(mode="json")
        if current.state != "completed":
            raise RuntimeError("BOOK_RECOVERY_NOT_COMPLETED:" + current.state)
        target = TaskCoverage(repo).get(course, identity).target
        artifact = ArtifactStore(repo).get(course, target.artifact_id)
        book = BookStore(repo).get(course, artifact.artifact_id)
        assert book.book_status == "ready" and len(book.chapters) == 2
        assert all(c.pages and c.state == "ready" for c in book.chapters)
        assert (
            len([b for b in artifact.revision.body.blocks if b.kind == "question"]) >= 2
        )
        assert BookStore(repo).outline(course, identity).confirmation_count == 1
        with repo.engine.connect() as conn:
            assert not conn.execute(select(practice.answers)).first()
        record.update(
            status="passed",
            artifact=artifact.model_dump(mode="json"),
            book=book.model_dump(mode="json"),
            coverage=TaskCoverage(repo).get(course, identity).model_dump(mode="json"),
            outline_confirmations=1,
            new_user_answers=0,
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
        record["actual_outbound_attempts_cumulative"] = (
            service.execution.boundary.actual_outbound_attempts
        )
        record["actual_outbound_attempts_new"] = (
            record["actual_outbound_attempts_cumulative"] - old_attempts
        )
        record["boundary_records"] = service.execution.boundary.records
        with repo.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(s.task_runs.c.task_id, s.task_runs.c.run_id)
                ).mappings()
            )
            record["raw_usage"] = [
                dict(x) for x in conn.execute(select(s.run_usage)).mappings()
            ]
        record["runs"] = [
            TaskRunStore(repo)
            .get(course, r["task_id"], r["run_id"])
            .model_dump(mode="json")
            for r in rows
        ]
        repo.close()
    record["source_after"] = freeze()
    record["source_changed_during_run"] = (
        record["source_before"] != record["source_after"]
    )
    record["original_owned_data_unchanged"] = original_data == hashes(old / "data")
    if (
        record["source_changed_during_run"]
        or not record["original_owned_data_unchanged"]
    ):
        record["status"] = "failed"
    (out / "summary.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
    )
    print(
        json.dumps(
            {
                k: record.get(k)
                for k in [
                    "status",
                    "error",
                    "actual_outbound_attempts_new",
                    "outline_confirmations",
                ]
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-run", default="material-chain-book-real-20261024-r1")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
