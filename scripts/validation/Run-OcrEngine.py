"""Formal OCR worker and installed assets; synthetic bitmap only, no cloud calls."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from learning_app.config import PROJECT_ROOT
from learning_app.processes import OwnedProcessGroup
from learning_app.workbench.ocr_resources import OcrResources


def run(run_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("RUN_ID_INVALID")
    output = PROJECT_ROOT / ".runtime/validation" / run_id
    output.mkdir(parents=True, exist_ok=False)
    temporary = output / "worker-tmp"
    temporary.mkdir()
    resources = OcrResources(PROJECT_ROOT)
    worker = PROJECT_ROOT / "backend/src/learning_app/workbench/ocr_worker.cjs"
    node = PROJECT_ROOT / ".tools/node/node.exe"
    image = Image.new("RGB", (1600, 800), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 64)
    draw.text((90, 130), "LEARNING WORKBENCH", font=font, fill="black")
    draw.text((90, 280), "Only selected text. 12345", font=font, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    image.close()
    data = buffer.getvalue()
    request = {
        "protocol_version": 1,
        "request_id": "synthetic_page_1",
        "epoch": 1,
        "spec": resources.launch_spec(),
        "image_base64": base64.b64encode(data).decode("ascii"),
        "image_sha256": hashlib.sha256(data).hexdigest(),
        "psm": 6,
    }
    environment = {
        name: os.environ[name]
        for name in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE")
        if name in os.environ
    }
    environment.update(
        {
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "PATH": str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32"),
        }
    )
    paths = [
        worker,
        PROJECT_ROOT / "backend/src/learning_app/workbench/ocr_resources.py",
        PROJECT_ROOT / "resources/workbench/engines.lock.json",
        PROJECT_ROOT / "resources/workbench/ocr-runtime.lock.json",
    ]
    before = {
        str(path.relative_to(PROJECT_ROOT)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }
    result = {
        "status": "running",
        "layer": "formal installed Node OCR, synthetic bitmap",
        "actual_cloud_model_attempts": 0,
        "local_ocr_pages": 1,
        "source_before": before,
        "fixture_sha256": request["image_sha256"],
        "node_sha256": hashlib.sha256(node.read_bytes()).hexdigest(),
        "limits": [
            "No product queue/selection layer, math/OCR accuracy or user materials claimed."
        ],
    }
    group = OwnedProcessGroup("ocr-installed-verification")
    try:
        process = group.spawn(
            [str(node), "--max-old-space-size=512", str(worker)],
            cwd=output,
            env=environment,
        )
        stdout, stderr = process.communicate(json.dumps(request) + "\n", timeout=120)
        (output / "worker.stdout.jsonl").write_text(stdout, "utf-8")
        (output / "worker.stderr.txt").write_text(stderr, "utf-8")
        rows = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        final = [row for row in rows if row["type"] in {"result", "error"}]
        if process.returncode != 0 or len(final) != 1 or final[0]["type"] != "result":
            raise RuntimeError("FORMAL_OCR_WORKER_FAILED")
        page = final[0]
        assert page["request_id"] == request["request_id"] and page["epoch"] == 1
        assert page["image_sha256"] == request["image_sha256"]
        assert "LEARNING WORKBENCH" in page["text"] and "12345" in page["text"]
        assert page["words"] and page["tsv"] and page["hocr"]
        symbols = sum(len(word["symbols"]) for word in page["words"])
        assert symbols > 20
        result.update(
            status="passed",
            word_count=len(page["words"]),
            symbol_count=symbols,
            text=page["text"],
            engine_version=page["engine_version"],
            parameters_version=page["parameters_version"],
        )
    except Exception as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
    finally:
        result["cleanup"] = group.close(grace_seconds=2)
        result["source_after"] = {
            str(path.relative_to(PROJECT_ROOT)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in paths
        }
        result["source_changed_during_run"] = (
            result["source_before"] != result["source_after"]
        )
        if result["source_changed_during_run"]:
            result["status"] = "failed"
        (output / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = run(args.run_id)
    print(
        json.dumps(
            {
                "status": result["status"],
                "error": result.get("error"),
                "evidence": f".runtime/validation/{args.run_id}/summary.json",
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if result["status"] == "passed" else 1)
