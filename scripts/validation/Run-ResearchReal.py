"""Owned actual-provider research validation: synthetic local text and public primary pages."""

from __future__ import annotations

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
from learning_app.workbench import research_schema as r
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.executor import TaskExecutor
from learning_app.workbench.models import ModelSettings
from learning_app.workbench.research import ResearchStore
from learning_app.workbench.runs import TaskRunStore
from learning_app.workbench.task_coverage import TaskCoverage
from learning_app.workbench.task_scope_models import TaskAdmission
from learning_app.workbench.tasks import TaskStore


def freeze():
    files = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        *sorted((PROJECT_ROOT / "third_party/deeptutor/src").rglob("*.py")),
        PROJECT_ROOT / "backend/uv.lock",
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "scripts/validation/Run-ResearchReal.py",
    ]
    return {
        p.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in files
    }


async def run(args):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,100}", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "status": "not_run",
        "layer": "actual provider + pinned native Harness + formal research tasks and external receipts",
        "input": "self-created local texts and explicitly scoped public IANA/RFC pages/search; no images, video, screen or user assets",
        "user_learning_experience": "not_run",
        "source_hashes_before": freeze(),
        "cases": [],
    }
    (directory / "source-before.json").write_text(
        json.dumps(record["source_hashes_before"], ensure_ascii=False, indent=2),
        "utf-8",
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
            json.dumps(record, ensure_ascii=False, indent=2), "utf-8"
        )
        return 2
    materials = directory / "synthetic-course"
    materials.mkdir()
    values = {
        "a.txt": "Source A. Rectangle area equals length times width. The width is 3 cm and the length is 4 cm, so the area is 12 square centimetres. The dimensions refer to perpendicular adjacent sides of a plane rectangle. Synthetic study text.",
        "b.txt": "Source B. Use matching units before multiplying rectangle dimensions. Width 30 mm and length 40 mm give 1200 square mm, equal to 12 square cm. This does not give the area of an arbitrary nonrectangular figure. Synthetic study text.",
        "long-a.txt": "Source A: rectangle area = length times width, with matching units. Width 3 cm and length 4 cm give 12 square cm. These are adjacent sides of a plane rectangle. Synthetic repeated evidence.\n"
        * 180,
        "web-note.txt": "Synthetic note to verify, not an IANA quotation: example domain names are intended for examples; do not assume their public website is a production dependency. Compare this note to the explicitly authorized primary references.",
    }
    for name, text in values.items():
        (materials / name).write_text(text, "utf-8")
    original_hashes = {
        name: hashlib.sha256((materials / name).read_bytes()).hexdigest()
        for name in values
    }
    repo = Repository(config.data_dir)
    files = CourseFileStore(repo)
    course = files.bind(None, str(materials), "独立研究验证合成资料")["course_id"]
    opened = {name: files.open(course, name) for name in values}
    service, executor = GenerationService(repo, config), None
    cases = {
        "local": {
            "files": ["a.txt", "b.txt"],
            "instruction": "只依据两份指定自造资料，简洁比较矩形面积知识的共同点、给定尺寸与单位差异、适用条件和局限。不要新编练习或把不同单位的数值当矛盾。每个资料至少有实际引用。",
        },
        "long": {
            "files": ["long-a.txt", "b.txt"],
            "instruction": "完整整理并比较全部指定资料。先保存每批证据笔记，保持1500中文字内，再综合共同公式、不同给定单位和适用范围。不得只归纳前一部分；综合归纳必须保留每个资料组的引用。",
        },
        "external_urls": {
            "files": ["web-note.txt"],
            "external": True,
            "urls": [
                "https://www.iana.org/help/example-domains",
                "https://www.rfc-editor.org/rfc/rfc2606.html",
            ],
            "instruction": "明确授权读取下列指定IANA和RFC公开网页文字。逐一调用read_research_page读取两个网页，长网页继续读取全部分段，然后对照合成笔记：哪些域名/顶级域保留作示例、用途与生产依赖的限制。简洁中文报告，分别引用本地笔记和两个实际网页。不能用知识记忆代替实际网页，不补图片、视频或其他网址。",
        },
        "external_search": {
            "files": [],
            "external": True,
            "instruction": "明确联网研究：调用search_research_web短查询，检索IANA或RFC关于保留示例域名的官方资料，依据实际返回的摘要或网页比较用途与使用限制。优先IANA/RFC官方来源；如仅看到检索摘要就明确标注，不称已读全文。简洁中文报告，每项有实际引用；不涉及图像、视频或用户素材。",
        },
    }
    try:
        await service.open()
        if not service.question_answering_ready:
            raise RuntimeError(service.execution_error or "MODEL_ROUTE_NOT_READY")
        executor = TaskExecutor(repo, service)
        await executor.open()
        for name, case in cases.items():
            if (args.case == "local_long" and name not in {"local", "long"}) or (
                args.case not in {"all", "local_long"} and args.case != name
            ):
                continue
            item = {"case": name, "status": "not_run"}
            record["cases"].append(item)
            task = TaskCoverage(repo).admit(
                course,
                TaskAdmission(
                    operation_id="research_" + name,
                    kind="research",
                    instruction=case["instruction"],
                    coverage_mode="complete",
                    sources=[
                        {
                            "kind": "document",
                            "asset_id": opened[f]["asset_id"],
                            "source_version": opened[f]["source_version"],
                        }
                        for f in case["files"]
                    ],
                    target={"mode": "create", "title": "研究验证 · " + name},
                    sources_only=not case.get("external", False),
                    allow_external=case.get("external", False),
                    research={"urls": case.get("urls", [])},
                    settings=ModelSettings(
                        model=args.model,
                        thinking=args.thinking,
                        max_output_tokens=args.max_output_tokens,
                        timeout_seconds=args.timeout,
                    ),
                ),
            )
            executor.schedule(course, task.task_id)
            await asyncio.wait_for(executor._queue.join(), args.timeout * 8 + 60)
            current = TaskStore(repo).get(course, task.task_id)
            item["task"] = current.model_dump(mode="json")
            item["progress"] = (
                ResearchStore(repo).get(course, task.task_id).model_dump(mode="json")
            )
            item["coverage"] = (
                TaskCoverage(repo).get(course, task.task_id).model_dump(mode="json")
            )
            if current.state != "completed":
                item["status"] = "failed"
                item["code"] = "RESEARCH_TASK_NOT_COMPLETE:" + current.state
                (directory / "case-results.json").write_text(
                    json.dumps(record["cases"], ensure_ascii=False, indent=2), "utf-8"
                )
                continue
            target = TaskCoverage(repo).get(course, task.task_id).target
            artifact = ArtifactStore(repo).get(course, target.artifact_id)
            item["artifact"] = artifact.model_dump(mode="json")
            progress = ResearchStore(repo).get(course, task.task_id)
            if (
                not artifact.indexed
                or artifact.kind != "report"
                or progress.reconciliation_required
                or progress.source_decision_required
            ):
                item["status"], item["code"] = (
                    "failed",
                    "REPORT_OR_EVIDENCE_NOT_SETTLED",
                )
                continue
            if name == "long" and not any(
                c.phase == "synthesis" and c.evidence_from_prior_runs
                for c in progress.checkpoints
            ):
                item["status"], item["code"] = (
                    "failed",
                    "LONG_COMPARISON_MISSING_SYNTHESIS",
                )
                continue
            if (
                name == "external_urls"
                and len(
                    [
                        read
                        for read in progress.external_reads
                        if read.kind == "read_research_page"
                        and read.state == "completed"
                    ]
                )
                < 2
            ):
                item["status"], item["code"] = (
                    "failed",
                    "SPECIFIED_PUBLIC_PAGES_NOT_READ",
                )
                continue
            if name == "external_search" and not any(
                read.kind == "search_research_web" and read.state == "completed"
                for read in progress.external_reads
            ):
                item["status"], item["code"] = "failed", "EXPLICIT_SEARCH_NOT_RUN"
                continue
            item["status"] = "passed"
            (directory / "case-results.json").write_text(
                json.dumps(record["cases"], ensure_ascii=False, indent=2), "utf-8"
            )
            print(
                json.dumps(
                    {
                        "case": name,
                        "status": item["status"],
                        "task_id": current.task_id,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        record["status"] = (
            "passed"
            if record["cases"] and all(c["status"] == "passed" for c in record["cases"])
            else "failed"
        )
    except Exception as error:
        record["status"], record["error"] = (
            "failed",
            getattr(error, "code", type(error).__name__),
        )
    finally:
        if executor:
            await executor.close()
        await service.close()
        record["cleanup"] = service.cleanup
        with repo.engine.connect() as conn:
            runs = list(
                conn.execute(
                    select(s.task_runs.c.run_id, s.task_runs.c.task_id)
                ).mappings()
            )
            record["external_http_reads"] = [
                dict(row) for row in conn.execute(select(r.external_reads)).mappings()
            ]
        record["runs_final"] = [
            TaskRunStore(repo)
            .get(course, row["task_id"], row["run_id"])
            .model_dump(mode="json")
            for row in runs
        ]
        record["actual_outbound_attempts"] = sum(
            row["actual_outbound_attempts"] for row in record["runs_final"]
        )
        record["raw_usage"] = [
            usage for row in record["runs_final"] for usage in row["usage"]
        ]
        record["originals_unchanged"] = original_hashes == {
            name: hashlib.sha256((materials / name).read_bytes()).hexdigest()
            for name in values
        }
        record["source_hashes_after"] = freeze()
        record["source_changed_during_run"] = (
            record["source_hashes_before"] != record["source_hashes_after"]
        )
        if record["source_changed_during_run"] or not record["originals_unchanged"]:
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--case",
        choices=[
            "all",
            "local_long",
            "local",
            "long",
            "external_urls",
            "external_search",
        ],
        default="all",
    )
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--timeout", type=float, default=180)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
