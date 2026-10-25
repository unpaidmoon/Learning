"""Real native document image tools on a self-created color-only PDF figure."""

import argparse
import asyncio
import hashlib
import importlib.util
import io
import json
import re

from dotenv import dotenv_values
from PIL import Image
from sqlalchemy import select

from learning_app.config import PROJECT_ROOT, AppConfig
from learning_app.course_files import CourseFileStore
from learning_app.service import GenerationService
from learning_app.storage import Repository
from learning_app.storage import schema as business
from learning_app.workbench import document_image_schema as d
from learning_app.workbench import schema as s
from learning_app.workbench.artifacts import ArtifactStore
from learning_app.workbench.corpus import CourseCorpus
from learning_app.workbench.sources import SourceStore


def freeze():
    files = [
        *sorted((PROJECT_ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((PROJECT_ROOT / "harness/plugins").glob("*.mjs")),
        PROJECT_ROOT / "harness/runtime.lock.json",
        PROJECT_ROOT / "backend/uv.lock",
        PROJECT_ROOT / "scripts/validation/Run-DocumentImageToolsReal.py",
        PROJECT_ROOT / "tests/fixtures/workbench/generate.py",
        PROJECT_ROOT / "backend/tests/pdf_graphics_support.py",
    ]
    return {
        path.relative_to(PROJECT_ROOT).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in files
    }


async def run(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        raise ValueError("OWNED_RUN_ID_INVALID")
    directory = PROJECT_ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "status": "not_run",
        "source_before": freeze(),
        "input": "self-created blue figure PDF, caption contains no color; no user material/video/screen",
        "fixture_layout": args.fixture_layout,
        "layer": "actual provider + fixed native Harness + typed image tool-result + frozen image history",
        "user_learning_experience": "not_run",
        "expected_figure_rgb": [20, 60, 210],
    }
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
        (directory / "summary.json").write_text(json.dumps(record), "utf-8")
        return 2
    materials = directory / "synthetic-course"
    materials.mkdir()
    fixture_spec = importlib.util.spec_from_file_location(
        "figure_fixture", PROJECT_ROOT / "tests/fixtures/workbench/generate.py"
    )
    fixtures = importlib.util.module_from_spec(fixture_spec)
    fixture_spec.loader.exec_module(fixtures)
    pdf = materials / "diagram.pdf"
    if args.fixture_layout == "raster":
        fixtures.document_regions_pdf(
            pdf,
            [(60, 470, 260, 100, (20, 60, 210))],
            [(60, 445, "Figure 1: local diagram.")],
        )
    else:
        graphics_spec = importlib.util.spec_from_file_location(
            "graphics_fixture", PROJECT_ROOT / "backend/tests/pdf_graphics_support.py"
        )
        graphics = importlib.util.module_from_spec(graphics_spec)
        graphics_spec.loader.exec_module(graphics)
        fixture = graphics.GraphicsPDF()
        resources = ""
        if args.fixture_layout == "form":
            image = fixture.raster((20, 60, 210))
            inner = fixture.form(
                "q 100 0 0 100 0 0 cm /Im Do Q",
                f"/XObject << /Im {image} 0 R >>",
                bbox=".21 .24 40.32 30.41",
                matrix="1 0 0 1 80 240",
            )
            outer = fixture.form(
                "/Inner Do",
                f"/XObject << /Inner {inner} 0 R >>",
                bbox="0 0 200 350",
                matrix="1.2 0 0 .8 20 30",
            )
            body, resources = (
                "/Fm Do\n1 0 0 rg 200 270 20 20 re f",
                f"/XObject << /Fm {outer} 0 R >>",
            )
        else:
            body = ".078 .235 .824 rg 60 240 80 50 re f\n1 0 0 rg 290 70 50 40 re f"
        fixture.write(
            pdf,
            body,
            resources,
            anchor="BT /F0 12 Tf 50 215 Td (Figure 1: local diagram.) Tj ET\n",
        )
    original = hashlib.sha256(pdf.read_bytes()).hexdigest()
    repo = Repository(config.data_dir)
    course = CourseFileStore(repo).bind(None, str(materials), "合成文档局部图真实验证")[
        "course_id"
    ]
    corpus = CourseCorpus(repo)
    corpus.discover(course, force=True)
    for _ in range(30):
        status = corpus.status(course)
        if status.scan_state == "running":
            corpus.discover(course)
        elif any(item.state in {"pending", "indexing"} for item in status.documents):
            corpus.index_next(course)
        else:
            break
    repo.create_chat(course, "document_image_real")
    service = GenerationService(repo, config)
    generations = []
    try:
        await service.open()
        for ordinal, question in enumerate(
            (
                "请查阅本课local diagram对应的实际文字，再按需读取Figure 1对应的局部图。图中主体是什么颜色？请给明确色名并引用本次实际图像来源。不能仅凭文字或元数据猜颜色。",
                "沿用刚才实际看到的局部图，再简要说明主体颜色与文字依据的区别，并引用原图来源。",
            )
        ):
            result = await service.submit(
                {
                    "chat_id": "document_image_real",
                    "submission_id": f"figure_real_{ordinal}",
                    "text": question,
                    "model": args.model,
                    "thinking": args.thinking,
                }
            )
            generations.append(result["generation_id"])
            await asyncio.wait_for(
                service.tasks[result["generation_id"]], args.timeout * 8 + 45
            )
            current = repo.get_generation(result["generation_id"])
            if current["state"] != "completed":
                raise RuntimeError(
                    "DOCUMENT_IMAGE_QA_NOT_COMPLETED: " + current["state"]
                )
        with repo.engine.connect() as conn:
            reads = list(conn.execute(select(d.image_reads)).mappings())
            allowances = list(conn.execute(select(d.image_allowances)).mappings())
            scopes = list(conn.execute(select(s.native_scopes)).mappings())
        if not any(row["state"] == "completed" for row in reads):
            raise RuntimeError("NO_ACTUAL_DOCUMENT_IMAGE_TOOL_READ")
        crop_reviews = []
        for row in reads:
            if row["state"] != "completed":
                continue
            snapshot = SourceStore(repo).snapshot(course, row["snapshot_id"])
            raw = SourceStore(repo).cached_image(snapshot)
            with Image.open(io.BytesIO(raw)) as image:
                pixels = list(image.convert("RGB").get_flattened_data())
                blue = sum(b > 130 and r < 100 and g < 120 for r, g, b in pixels) / len(
                    pixels
                )
                red = sum(r > 150 and g < 80 and b < 80 for r, g, b in pixels)
            crop_reviews.append(
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "region_parser_version": snapshot.reference.region_parser_version,
                    "blue_pixel_fraction": blue,
                    "red_pixels": red,
                    "image_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        record["actual_crop_reviews"] = crop_reviews
        if not crop_reviews or any(
            row["blue_pixel_fraction"] < 0.9 or row["red_pixels"]
            for row in crop_reviews
        ):
            raise RuntimeError("ACTUAL_CROP_CONTAINS_NEIGHBOR_OR_WRONG_FIGURE")
        rows = service.execution.boundary.records
        if not any(row.get("images") for row in rows):
            raise RuntimeError("NO_NATIVE_IMAGE_IN_ACTUAL_PROVIDER_REQUEST")
        if not any(
            scope["run_id"] == generations[1]
            and scope["grants"].get("document_image_history")
            for scope in scopes
        ):
            raise RuntimeError("FOLLOWUP_DID_NOT_FREEZE_IMAGE_HISTORY")
        if ArtifactStore(repo).list(course):
            raise RuntimeError("ORDINARY_QA_CREATED_AN_ARTIFACT")
        answers = [repo.get_generation(identity)["body"] for identity in generations]
        record["answer_color_label_present"] = [
            bool(re.search(r"蓝|\bblue\b", answer, re.I)) for answer in answers
        ]
        if not all(record["answer_color_label_present"]):
            raise RuntimeError("CONTROLLED_COLOR_LABEL_MISSING")
        record.update(
            status="passed",
            answers=answers,
            image_reads=[dict(row) for row in reads],
            image_allowances=[dict(row) for row in allowances],
            boundary_records=rows,
            semantic_review="pending explicit reading of final answers; label matching is only a sample check",
        )
    except Exception as error:
        record.update(
            status="failed",
            error=getattr(error, "code", type(error).__name__) + ": " + str(error),
        )
    finally:
        await service.close()
        record["cleanup"] = service.cleanup
        record["generations"] = [
            repo.get_generation(identity) for identity in generations
        ]
        record["actual_outbound_attempts"] = (
            service.execution.boundary.actual_outbound_attempts
            if service.execution
            else None
        )
        with repo.engine.connect() as conn:
            record["raw_usage"] = [
                dict(row)
                for row in conn.execute(select(business.usage_records)).mappings()
            ]
        record["original_unchanged"] = (
            hashlib.sha256(pdf.read_bytes()).hexdigest() == original
        )
        record["source_after"] = freeze()
        record["source_changed_during_run"] = (
            record["source_before"] != record["source_after"]
        )
        if not record["original_unchanged"] or record["source_changed_during_run"]:
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
                    "actual_outbound_attempts",
                    "answer_color_label_present",
                    "source_changed_during_run",
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
    parser.add_argument(
        "--fixture-layout", choices=["raster", "form", "vector"], default="raster"
    )
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", default="high")
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=180)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
