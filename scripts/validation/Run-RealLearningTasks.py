"""Authorized real English selection and first grammar section via fixed Harness."""

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
from learning_app.workbench.models import (
    ModelSettings,
    PageRegion,
    PdfSelectionInput,
    PdfSelectionPage,
)
from learning_app.workbench.reading import DocumentReader
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
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


def select_range(reader, course, page, start, end):
    boxes = [
        g.region for g in page.glyphs if g.region and g.start < end and g.end > start
    ]
    if not boxes:
        raise RuntimeError("REAL_SELECTION_GEOMETRY_UNAVAILABLE")
    region = PageRegion(
        x0=min(r.x0 for r in boxes),
        y0=min(r.y0 for r in boxes),
        x1=max(r.x1 for r in boxes),
        y1=max(r.y1 for r in boxes),
    )
    text = page.text[start:end]
    return reader.capture(
        course,
        PdfSelectionInput(
            asset_id=page.asset_id,
            source_version=page.source_version,
            text=text,
            pages=[
                PdfSelectionPage(
                    page_index=page.page_index, text=text, regions=[region]
                )
            ],
        ),
    )


async def run(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    base = PROJECT_ROOT / ".runtime/validation"
    old = base / "real-learning-english-review-20261025-r1"
    out = base / args.run_id
    prior = json.loads((old / "summary.json").read_text("utf-8"))
    assert prior["status"] == "passed"
    before = hashes(old / "data")
    out.mkdir(exist_ok=False)
    shutil.copytree(old / "data", out / "data")
    repo = Repository(out / "data")
    course = prior["course_id"]
    reader = DocumentReader(repo)
    record = {
        "status": "not_run",
        "source_before": freeze(),
        "source_run": old.name,
        "steps": [],
        "layer": "authorized real PDF + actual fixed native Harness/provider",
        "authorization": "D:/Learning; one delegated course, selected English PDF only; no video/screen frames",
        "user_learning_experience": "not_run; scripted technical actions on authorized real material",
    }
    service = executor = None
    try:
        first = reader.page(course, prior["asset_id"], prior["source_version"], 1)
        sentence = "What he needs is money."
        start = first.text.index(sentence)
        selected = select_range(reader, course, first, start, start + len(sentence))
        assert selected.text == sentence
        boundary = reader.page(course, prior["asset_id"], prior["source_version"], 30)
        end = boundary.text.index("Part 2")
        tail = select_range(reader, course, boundary, 0, end)
        record["selected_sentence"] = selected.model_dump(mode="json")
        record["grammar_scope"] = {
            "first_page": 1,
            "last_page": 29,
            "final_page_index": 30,
            "final_selection": tail.model_dump(mode="json"),
        }
        config = AppConfig(
            mode="test",
            harness_enabled=True,
            credential_route="api_key",
            model="deepseek-flash",
            thinking="high",
            max_output_tokens=32768,
            model_timeout_seconds=300,
            data_dir=out / "data",
            runtime_dir=out / "runtime",
        )
        service = GenerationService(repo, config)
        await service.open()
        executor = TaskExecutor(repo, service)
        await executor.open()

        async def complete(value):
            task = TaskCoverage(repo).admit(course, value)
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), 7200)
            current = TaskStore(repo).get(course, task.task_id)
            record["steps"].append(current.model_dump(mode="json"))
            print(
                json.dumps(
                    {
                        "kind": current.kind,
                        "state": current.state,
                        "units": len(current.units),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if current.state != "completed":
                raise RuntimeError(
                    "REAL_LEARNING_TASK_NOT_COMPLETE:"
                    + current.kind
                    + ":"
                    + current.state
                )
            target = TaskCoverage(repo).get(course, task.task_id).target
            return task, ArtifactStore(repo).get(course, target.artifact_id)

        small = ModelSettings(
            model="deepseek-flash",
            thinking="high",
            max_output_tokens=8192,
            timeout_seconds=300,
        )
        _, translation = await complete(
            TaskAdmission(
                operation_id="real_exact_sentence_translation",
                kind="aligned_translation",
                sources_only=True,
                instruction="仅将实际选中的英语句子对照译成中文，保留原句，不补前后文或图片。",
                sources=[{"kind": "selection", "snapshot_id": selected.snapshot_id}],
                target={"mode": "create", "title": "主语从句例句对照"},
                settings=small,
            )
        )
        assert (
            "".join(b.translation.source_text for b in translation.revision.body.blocks)
            == sentence
        )
        _, note = await complete(
            TaskAdmission(
                operation_id="real_sentence_reading_note",
                kind="note",
                sources_only=False,
                instruction="明确保存一份精读笔记：解释所选句子的主语从句、what在从句中的成分和主句谓语。用中文简洁说明；可加两句短例子，AI补例单独标注，不冒充讲义原文。正文专注学习内容。",
                sources=[{"kind": "selection", "snapshot_id": selected.snapshot_id}],
                target={"mode": "create", "title": "主语从句精读笔记"},
                settings=small,
            )
        )
        task, handout = await complete(
            TaskAdmission(
                operation_id="real_grammar_part1_handout",
                kind="handout",
                coverage_mode="complete",
                sources_only=True,
                instruction="完整整理讲义Part1语法句型：PDF第2至30页，加上第31页Part2标题之前的练习末段。覆盖各语法主题、条件、例句、错误示例和练习，不逐字抄整篇，不把星标错误例句当正确；只依据这批材料，区分原资料与依据资料的推断。OCR或原图有歧义时明确待核，不静默改原文。练习若无材料内答案，保留题干，不冒充教材答案。讲解和来源引用清楚，正文不写内部ID、hash或技术字段，也不反复写任务状态。",
                sources=[
                    {
                        "kind": "document",
                        "asset_id": prior["asset_id"],
                        "source_version": prior["source_version"],
                        "first_page": 1,
                        "last_page": 29,
                    },
                    {"kind": "selection", "snapshot_id": tail.snapshot_id},
                ],
                target={"mode": "create", "title": "Part1语法句型完整整理"},
                settings=ModelSettings(
                    model="deepseek-flash",
                    thinking="high",
                    max_output_tokens=32768,
                    timeout_seconds=300,
                ),
            )
        )
        coverage = TaskCoverage(repo).get(course, task.task_id)
        assert all(
            x.processed_leaf_count == x.expected_leaf_count for x in coverage.sources
        )
        record.update(
            status="passed",
            translation=translation.model_dump(mode="json"),
            note=note.model_dump(mode="json"),
            handout=handout.model_dump(mode="json"),
            coverage=coverage.model_dump(mode="json"),
        )
    except Exception as error:
        record.update(
            status="failed",
            error=getattr(error, "code", type(error).__name__) + ": " + str(error),
        )
    finally:
        if executor:
            await executor.close()
        if service:
            await service.close()
            record["cleanup"] = service.cleanup
            record["actual_outbound_attempts"] = (
                service.execution.boundary.actual_outbound_attempts
                if service.execution
                else 0
            )
            record["boundary_records"] = (
                service.execution.boundary.records if service.execution else []
            )
        else:
            record["actual_outbound_attempts"] = 0
        with repo.engine.connect() as conn:
            rows = list(
                conn.execute(
                    select(s.task_runs.c.task_id, s.task_runs.c.run_id)
                ).mappings()
            )
            record["raw_usage"] = [
                dict(r) for r in conn.execute(select(s.run_usage)).mappings()
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
    record["original_owned_data_unchanged"] = before == hashes(old / "data")
    record["original_pdf_unchanged"] = (
        hashlib.sha256(
            Path(r"D:\Learning\27作文讲义  Part1.pdf").read_bytes()
        ).hexdigest()
        == prior["source_version"]
    )
    if (
        record["source_changed_during_run"]
        or not record["original_owned_data_unchanged"]
        or not record["original_pdf_unchanged"]
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
                    "actual_outbound_attempts",
                    "original_pdf_unchanged",
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
