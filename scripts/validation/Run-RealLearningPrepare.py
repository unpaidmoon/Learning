"""Authorized real English course PDF, in-place source and local OCR only."""

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path

from learning_app.config import PROJECT_ROOT
from learning_app.course_files import CourseFileStore
from learning_app.storage import Repository
from learning_app.workbench.corpus import CourseCorpus
from learning_app.workbench.corpus_models import CorpusPolicy
from learning_app.workbench.processing import PreparationService
from learning_app.workbench.processing_models import PreparationCreate
from learning_app.workbench.reading import DocumentReader


async def run(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    path = Path(r"D:\Learning\27作文讲义  Part1.pdf").resolve(strict=True)
    if path.parent != Path(r"D:\Learning") or path.is_symlink():
        raise ValueError("CONFIRMED_REAL_MATERIAL_SCOPE_INVALID")
    out = PROJECT_ROOT / ".runtime/validation" / args.run_id
    out.mkdir(exist_ok=False)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    record = {
        "status": "not_run",
        "layer": "authorized real PDF + actual fixed local OCR; no Host/provider",
        "material_path": str(path),
        "source_sha256": digest,
        "authorization": "2026-10-25 user provided D:/Learning and delegated one course: documents/subtitles, local audio ASR if needed",
        "selected_scope": "only this English-writing PDF; in-place original; no video/screen frames",
        "actual_outbound_attempts": 0,
        "host_calls": 0,
        "user_learning_experience": "not_run",
    }
    repo = Repository(out / "data")
    preparations = PreparationService(repo, PROJECT_ROOT)
    try:
        files = CourseFileStore(repo)
        course = files.bind(None, str(path.parent), "真实验收：英语写作Part1")[
            "course_id"
        ]
        exclusions = [p.name for p in path.parent.iterdir() if p.name != path.name]
        CourseCorpus(repo).configure(
            course,
            CorpusPolicy(
                operation_id="fixed_real_english_scope",
                expected_revision=0,
                exclusions=exclusions,
            ),
        )
        opened = files.open(course, path.name)
        info = DocumentReader(repo).info(
            course, opened["asset_id"], opened["source_version"]
        )
        record.update(course_id=course, asset=opened, pdf_info=info.model_dump(mode="json"))
        job = preparations.create_pdf(
            course,
            PreparationCreate(
                kind="pdf_ocr",
                asset_id=opened["asset_id"],
                source_version=opened["source_version"],
                priority_unit=0,
            ),
        )
        await preparations.open()
        deadline = asyncio.get_running_loop().time() + 3600
        last = None
        while True:
            current = preparations.store.get(course, job.job_id)
            progress = (
                current.state,
                sum(u.state == "completed" for u in current.units),
                len(current.units),
            )
            if progress != last:
                print(
                    json.dumps({"local_pdf_preparation": progress}, ensure_ascii=False),
                    flush=True,
                )
                last = progress
            if current.state == "completed":
                break
            if current.state in {"failed", "needs_source", "cancelled"}:
                raise RuntimeError("REAL_PDF_PREPARATION_NOT_COMPLETE:" + current.state)
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("REAL_PDF_PREPARATION_DEADLINE")
            await asyncio.sleep(0.2)
        record["preparation"] = current.model_dump(mode="json")
        previews = []
        for index in range(min(8, info.page_count)):
            page = DocumentReader(repo).page(
                course, opened["asset_id"], opened["source_version"], index
            )
            previews.append(
                {
                    "page_index": index,
                    "text_status": page.text_status,
                    "parser_version": page.parser_version,
                    "text": page.text[:1600],
                }
            )
        record["private_first_page_previews"] = previews
        record["status"] = "passed"
    except Exception as error:
        record.update(
            status="failed",
            error=getattr(error, "code", type(error).__name__) + ": " + str(error),
        )
    finally:
        await preparations.close()
        repo.close()
    record["original_unchanged"] = (
        hashlib.sha256(path.read_bytes()).hexdigest() == digest
    )
    if not record["original_unchanged"]:
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
                    "actual_outbound_attempts",
                    "original_unchanged",
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
    raise SystemExit(asyncio.run(run(parser.parse_args())))
