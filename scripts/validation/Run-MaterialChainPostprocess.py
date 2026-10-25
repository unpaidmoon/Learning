"""Local saved-result index, exact revision exports and usage review; no model."""

import argparse
import hashlib
import json
import re
import shutil

from sqlalchemy import select

from learning_app.config import PROJECT_ROOT
from learning_app.storage import Repository
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.corpus import CourseCorpus
from learning_app.workbench.offline import OfflineExporter


def hashes(root):
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def run(args):
    for value in (args.run_id, args.source_run):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError("OWNED_RUN_ID_INVALID")
    base = PROJECT_ROOT / ".runtime/validation"
    old, out = base / args.source_run, base / args.run_id
    prior = json.loads((old / "summary.json").read_text("utf-8"))
    assert prior["status"] == "passed"
    before = hashes(old / "data")
    out.mkdir(exist_ok=False)
    shutil.copytree(old / "data", out / "data")
    repo = Repository(out / "data")
    course = prior["book_input"]["course_id"]
    record = {
        "status": "not_run",
        "source_run": args.source_run,
        "actual_outbound_attempts_new": 0,
        "host_calls": 0,
        "layer": "formal local corpus and exports on owned saved model outputs",
        "user_learning_experience": "not_run",
    }
    try:
        corpus = CourseCorpus(repo)
        corpus.discover(course, force=True)
        for _ in range(200):
            status = corpus.status(course)
            if status.scan_state == "running":
                corpus.discover(course)
            elif any(d.state in {"pending", "indexing"} for d in status.documents):
                corpus.index_next(course)
            else:
                break
        assert all(d.state == "ready" for d in corpus.status(course).documents)
        original_and_ai = corpus.search(course, "area")
        assert {"original", "ai_generated"} <= {
            r["snapshot"].content_role for r in original_and_ai
        }
        handout = corpus.search(course, "中文讲义")
        assert handout and all(
            r["snapshot"].content_role == "ai_generated" for r in handout
        )
        record["search"] = {
            "area": [
                {"label": r["label"], "source": r["snapshot"].model_dump(mode="json")}
                for r in original_and_ai
            ],
            "handout": [
                {"label": r["label"], "source": r["snapshot"].model_dump(mode="json")}
                for r in handout
            ],
        }
        artifacts = ArtifactStore(repo).list(course)
        record["artifacts"] = [a.model_dump(mode="json") for a in artifacts]
        exports = []
        for artifact in artifacts:
            destination = out / (artifact.artifact_id + ".zip")
            with OfflineExporter(repo).build(
                course, artifact.artifact_id, artifact.head_revision_id
            ) as stream:
                with destination.open("wb") as target:
                    shutil.copyfileobj(stream, target)
            exports.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "revision_id": artifact.head_revision_id,
                    "kind": artifact.kind,
                    "zip": destination.name,
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                }
            )
        record["exports"] = exports
        with repo.engine.connect() as conn:
            usage = [dict(r) for r in conn.execute(select(s.run_usage)).mappings()]
        assert (
            len(usage)
            == len({r["settlement_id"] for r in usage})
            == prior["actual_outbound_attempts"]
        )
        settled = [r for r in usage if r["raw_usage"] and r["raw_usage"].get("settled")]
        record["usage"] = {
            "unique_records": len(usage),
            "settled": len(settled),
            "unknown": len(usage) - len(settled),
            "known_subtotal": {
                key: sum(r["raw_usage"]["observed_cumulative"][key] for r in settled)
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                )
            },
            "reasoning_missing": sum(
                not r["field_presence"].get("reasoning_tokens") for r in usage
            ),
        }
        record["status"] = "passed"
    except Exception as error:
        record.update(
            status="failed",
            error=getattr(error, "code", type(error).__name__) + ": " + str(error),
        )
    finally:
        repo.close()
    record["original_owned_data_unchanged"] = before == hashes(old / "data")
    if not record["original_owned_data_unchanged"]:
        record["status"] = "failed"
    (out / "summary.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
    )
    print(
        json.dumps(
            {k: record.get(k) for k in ("status", "error", "usage")}, ensure_ascii=False
        )
    )
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--source-run", default="material-chain-practice-real-20261025-r1"
    )
    raise SystemExit(run(parser.parse_args()))
