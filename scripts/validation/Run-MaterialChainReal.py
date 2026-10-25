"""Run the already-admitted mixed-PDF and foreign full-video subtitle task."""

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
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import DocumentImageRef
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.sources import SourceStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.tasks import TaskStore


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def freeze():
    paths = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        PROJECT_ROOT / "harness/runtime.lock.json",
        Path(__file__),
    ]
    return {
        p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in paths
    }


async def run(args):
    for value in (args.run_id, args.source_run):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("OWNED_RUN_ID_INVALID")
    base = (PROJECT_ROOT / ".runtime/validation").resolve()
    old, out = base / args.source_run, base / args.run_id
    assert old.resolve().parent == base and out.resolve().parent == base
    prepared = json.loads((old / "summary.json").read_text("utf-8"))
    assert prepared["status"] == "passed" and prepared["actual_outbound_attempts"] == 0
    original_data = hashes(old / "data")
    out.mkdir(exist_ok=False)
    shutil.copytree(old / "data", out / "data")
    record = {
        "status": "not_run",
        "source_before": freeze(),
        "source_run": args.source_run,
        "layer": "actual provider + fixed native Harness; prepared mixed PDF and foreign full video ASR",
        "user_learning_experience": "not_run",
    }
    course, foreign = prepared["owner_course_id"], prepared["origin_course_id"]
    repo = Repository(out / "data")
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
    try:
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()
        await asyncio.wait_for(executor._queue.join(), 1800)
        task = TaskStore(repo).get(course, prepared["task_id"])
        record["handout_task"] = task.model_dump(mode="json")
        if task.state != "completed":
            raise RuntimeError("MIXED_HANDOUT_NOT_COMPLETE:" + task.state)
        coverage = TaskCoverage(repo).get(course, task.task_id)
        assert len(coverage.leaves) == 4 and all(
            x.processed_leaf_count == x.expected_leaf_count for x in coverage.sources
        )
        target = ArtifactStore(repo).get(course, coverage.target.artifact_id)
        assert target.course_id == course and target.content_role == "ai_generated"
        assert ArtifactStore(repo).list(foreign) == []
        record["handout"] = target.model_dump(mode="json")
        origins, image_sources = {}, []
        for leaf in coverage.leaves:
            for identity in leaf.snapshot_ids:
                snapshot = SourceStore(repo).snapshot(course, identity)
                origins[identity] = {
                    "course_id": snapshot.course_id,
                    "role": snapshot.content_role,
                    "kind": snapshot.reference.kind,
                }
                if hasattr(snapshot.reference, "image_sha256"):
                    assert isinstance(snapshot.reference, DocumentImageRef)
                    assert snapshot.course_id == course
                    image_sources.append(snapshot.reference.image_sha256)
        assert {row["course_id"] for row in origins.values()} == {course, foreign}
        assert len(image_sources) == 2
        with repo.engine.connect() as conn:
            inputs = [
                dict(row) for row in conn.execute(select(s.run_inputs)).mappings()
            ]
        encoded = {
            row["proof"]["native_image"]["encoded_sha256"]
            for row in inputs
            if (row["proof"] or {}).get("native_image")
        }
        actual_images = {
            picture["sha256"]
            for request in service.execution.boundary.records
            for picture in request.get("images", [])
        }
        assert encoded and actual_images and actual_images <= encoded
        record.update(
            status="passed",
            coverage=coverage.model_dump(mode="json"),
            source_origins=origins,
            source_image_sha256=image_sources,
            encoded_image_sha256=sorted(encoded),
            actual_image_sha256=sorted(actual_images),
            image_scope="only two explicitly declared PDF pages; no video frames",
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
        record["actual_outbound_attempts"] = (
            service.execution.boundary.actual_outbound_attempts
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
        record["original_materials_unchanged"] = all(
            hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest
            for p, digest in prepared["originals"].items()
        )
        if (
            record["source_changed_during_run"]
            or not record["original_owned_data_unchanged"]
            or not record["original_materials_unchanged"]
        ):
            record["status"] = "failed"
        (out / "summary.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
    print(
        json.dumps(
            {
                k: record.get(k)
                for k in ["status", "error", "actual_outbound_attempts", "image_scope"]
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-run", default="material-chain-prepare-20261024-r1")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
