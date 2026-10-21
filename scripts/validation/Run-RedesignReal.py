"""Explicit real-provider smoke after synthetic UI/boundary checks have passed.

The runner generates its own bounded image. It reads no screen or user video.
Business storage and raw evidence are isolated; the account route reuses the
official credential home in place and never copies or prints its contents.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import re
import shutil
import sys
import time
from pathlib import Path

from dotenv import dotenv_values
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/src"))

from learning_app.config import AppConfig  # noqa: E402
from learning_app.drafts import AttachmentSourceInput, save_attachment_source  # noqa: E402
from learning_app.execution.adapter import ExecutionAdapter  # noqa: E402
from learning_app.service import GenerationService  # noqa: E402
from learning_app.storage import Repository  # noqa: E402

TERMINAL = {"completed", "cancelled", "failed", "truncated"}


def save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", "utf-8"
    )


def make_confirmed_image(directory: Path) -> tuple[bytes, dict]:
    original = Image.new("RGB", (640, 360), "#e8e8eb")
    draw = ImageDraw.Draw(original)
    draw.text((15, 20), "OUTSIDE CONFIRMED AREA - NEVER SEND", fill="#7d1743")
    draw.rectangle((80, 80, 559, 279), fill="white")
    font_path = Path("C:/Windows/Fonts/consola.ttf")
    font = (
        ImageFont.truetype(str(font_path), 25)
        if font_path.is_file()
        else ImageFont.load_default(size=25)
    )
    draw.text((110, 115), "Solve the quadratic equation:", font=font, fill="#151515")
    draw.text((155, 180), "x^2 - 5x + 6 = 0", font=font, fill="#151515")
    stream = io.BytesIO()
    original.save(stream, "PNG")
    original_bytes = stream.getvalue()
    (directory / "synthetic-original-local-only.png").write_bytes(original_bytes)
    stream = io.BytesIO()
    original.crop((80, 80, 560, 280)).save(stream, "PNG")
    confirmed = stream.getvalue()
    (directory / "synthetic-confirmed.png").write_bytes(confirmed)
    return confirmed, {
        "origin": "generated synthetic workspace image",
        "original_local_only_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "confirmed_sha256": hashlib.sha256(confirmed).hexdigest(),
        "source_width": 640,
        "source_height": 360,
        "crop": {"x": 80, "y": 80, "width": 480, "height": 200},
        "expected_roots": [2, 3],
    }


def freeze_sources(directory: Path) -> dict[str, str]:
    files = [
        *ROOT.glob("backend/src/learning_app/execution/*.py"),
        *ROOT.glob("backend/src/learning_app/storage/*.py"),
        ROOT / "backend/src/learning_app/accounts.py",
        ROOT / "backend/src/learning_app/service.py",
        ROOT / "backend/src/learning_app/drafts.py",
        ROOT / "backend/src/learning_app/dto.py",
        ROOT / "harness/runtime.lock.json",
        ROOT / "harness/profiles/learning.patch.json",
        ROOT / "harness/plugins/learning-guard.mjs",
        Path(__file__).resolve(),
    ]
    digests = {}
    for source in files:
        relative = source.relative_to(ROOT)
        destination = directory / "private/source" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        digests[relative.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    return digests


async def observe_case(
    service, repository, adapter, gid: str, *, cancel: bool, timeout: float
):
    started = time.monotonic()
    first_body_ms = None
    cancel_evidence = None
    observed = []
    previous = None
    terminal_at = None
    async with asyncio.timeout(timeout + 30):
        while True:
            generation = service.generation_view(gid)
            body = generation["body"]
            if body and first_body_ms is None:
                first_body_ms = round((time.monotonic() - started) * 1000, 3)
            mark = (
                generation["state"],
                generation["event_watermark"],
                generation["usage"]["status"],
            )
            if mark != previous:
                observed.append(
                    {
                        "at_ms": round((time.monotonic() - started) * 1000, 3),
                        "state": generation["state"],
                        "watermark": generation["event_watermark"],
                        "body_length": len(body),
                        "reasoning_length": len(generation["reasoning"]),
                        "usage_status": generation["usage"]["status"],
                    }
                )
                previous = mark
            rows = [
                row
                for row in adapter.boundary.records
                if row.get("generation_id") == gid
            ]
            active_provider = any(
                row.get("dispatch_ns")
                and row.get("upstream_status") == 200
                and not row.get("finished_ns")
                for row in rows
            )
            body_chars = len(re.sub(r"[\s*_#`$\\{}\[\]()]", "", body))
            if (
                cancel
                and cancel_evidence is None
                and body_chars >= 24
                and active_provider
            ):
                cancel_evidence = {
                    "provider_active_before_cancel": True,
                    "body_characters_before_cancel": body_chars,
                    "host_alive_before_cancel": adapter.host.process.poll() is None,
                }
                await service.cancel(gid)
            if generation["state"] == "unknown":
                # Reconcile the existing identity only. Never repeat the prompt.
                await service.reconcile(gid, resume_observer=True)
            if generation["state"] in TERMINAL:
                terminal_at = terminal_at or time.monotonic()
                if (
                    generation["usage"]["raw"] is not None
                    or time.monotonic() - terminal_at >= 3
                ):
                    break
            await asyncio.sleep(0.025)
    final = service.generation_view(gid)
    rows = [row for row in adapter.boundary.records if row.get("generation_id") == gid]
    expected_status = "cancelled" if cancel else "completed"
    passed = final["state"] == expected_status and bool(final["body"])
    if cancel:
        passed = (
            passed
            and bool(cancel_evidence)
            and any(row.get("cancel_observed") for row in rows)
        )
    save(
        adapter.run_dir.parent / "private" / f"{gid}.json",
        {
            "generation": repository.get_generation(gid),
            "view": final,
            "observed": observed,
            "boundary_records": rows,
            "cancel_evidence": cancel_evidence,
        },
    )
    return {
        "generation_id": gid,
        "passed": bool(passed),
        "state": final["state"],
        "body_characters": len(final["body"]),
        "body_sha256": hashlib.sha256(final["body"].encode()).hexdigest(),
        "first_body_ms": first_body_ms,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "cancel_evidence": cancel_evidence,
        "provider_cancel_observed": any(row.get("cancel_observed") for row in rows),
        "host_alive_after_case": adapter.host.process.poll() is None,
        "usage": final["usage"],
        "actual_attempts": len(
            [row for row in rows if row.get("actual_outbound_attempt")]
        ),
        "error_code": final.get("error_code"),
    }


async def run(args) -> dict:
    root = (ROOT / ".runtime/validation").resolve()
    directory = (root / args.run_id).resolve()
    if directory.parent != root:
        raise ValueError("RUN_DIRECTORY_ESCAPE")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "private").mkdir()
    image, fixture = make_confirmed_image(directory / "private")
    result = {
        "run_id": args.run_id,
        "client_date": "2026-10-21",
        "scope": "isolated business SQLite, formal GenerationService, pinned Host, real official provider; synthetic confirmed image only",
        "route": args.route,
        "model": args.model,
        "thinking": args.thinking,
        "max_output_tokens": args.max_output_tokens,
        "timeout_seconds": args.timeout,
        "fixture": fixture,
        "source_digests": freeze_sources(directory),
        "cases": [],
        "limitations": [
            "This is not a visual or user learning-experience acceptance run.",
            "Mathematical correctness requires separate inspection of the private synthetic-answer evidence; transport completion alone does not establish correctness.",
            "Unknown executions are reconciled and never automatically resubmitted.",
            "Activity cancellation is reported as uncovered if the provider completes before the real stop condition; no extra model request is silently added.",
        ],
    }
    key = None
    if args.route == "api_key":
        key = dotenv_values(ROOT / ".local/secrets/deepseek.env").get(
            "DEEPSEEK_API_KEY"
        )
        if not key:
            raise RuntimeError("MODEL_KEY_NOT_CONFIGURED")
    adapter = ExecutionAdapter(
        ROOT,
        directory / "execution",
        mode="official",
        api_key=key,
        credential_route=args.route,
        account_home=args.account_home.resolve() if args.route == "account" else None,
        model=args.model,
        thinking=args.thinking,
        max_tokens=args.max_output_tokens,
        timeout=args.timeout,
    )
    key = None
    repository = Repository(directory / "business")
    config = AppConfig(
        mode="test",
        data_dir=directory / "business",
        runtime_dir=directory / "run",
        model=args.model,
        thinking=args.thinking,
        max_output_tokens=args.max_output_tokens,
        model_timeout_seconds=args.timeout,
        credential_route=args.route,
    )
    service = GenerationService(repository, config, execution=adapter)
    try:
        await adapter.open()
        await service.open()
        if not service.execution_ready:
            raise RuntimeError("EXECUTION_NOT_READY")
        await adapter.validate_choice(args.model, args.thinking)
        if not adapter.supports_model_images(args.model):
            raise RuntimeError("SELECTED_MODEL_IMAGE_UNSUPPORTED")
        if args.route == "account":
            result["official_models"] = list(adapter._account_models)
        result["profile_tools"] = (await adapter.host.audit_state())["tools"]
        course = repository.create_course("重设计真实复测合成课程")
        chat = repository.create_chat(course["course_id"])
        staged = repository.stage_attachment(image, confirmed=True)
        aid = staged["attachment_id"]
        source = save_attachment_source(
            repository,
            aid,
            AttachmentSourceInput(
                chat_id=chat["chat_id"],
                origin="workspace",
                source_width=fixture["source_width"],
                source_height=fixture["source_height"],
                crop=fixture["crop"],
                edit_root_id="synthetic-redesign-real",
            ),
        )
        result["confirmed_source"] = {
            "origin": source["origin"],
            "source_width": source["source_width"],
            "source_height": source["source_height"],
            "crop": source["crop"],
            "confirmed_sha256": staged["content_hash"],
        }
        sessions = []
        prompts = [
            (
                "image-complete",
                "请读取已确认题图，先给出答案，再分别用因式分解和求根公式完整解答，并代入原方程检验。",
                False,
                [aid],
            ),
            (
                "active-stop",
                "请详细讲解这道方程的两种解法，逐步解释每一步的依据、符号及验算，最后总结常见错误。",
                True,
                [],
            ),
            (
                "after-stop-followup",
                "刚才主动停止了回答。请继续完整解答同一题图，分别用因式分解与求根公式，并代入检验。",
                False,
                [],
            ),
        ]
        for index, (case_id, text, cancel, attachments) in enumerate(prompts, 1):
            value = {
                "chat_id": chat["chat_id"],
                "submission_id": f"{args.run_id}-{index}",
                "text": text,
                "attachment_ids": attachments,
                "conditions": [],
                "corrections": [],
                "model": args.model,
                "thinking": args.thinking,
            }
            receipt = await service.submit(value)
            case = await observe_case(
                service,
                repository,
                adapter,
                receipt["generation_id"],
                cancel=cancel,
                timeout=args.timeout,
            )
            case["case_id"] = case_id
            generation = repository.get_generation(receipt["generation_id"])
            sessions.append(
                (generation.get("adapter_metadata") or {}).get("session_id")
            )
            result["cases"].append(case)
            print(
                json.dumps(
                    {
                        "case_id": case_id,
                        "passed": case["passed"],
                        "state": case["state"],
                        "actual_attempts": case["actual_attempts"],
                    }
                ),
                flush=True,
            )
            if not case["passed"]:
                break
        result["same_session"] = (
            bool(sessions) and len(set(sessions)) == 1 and sessions[0] is not None
        )
        records = adapter.boundary.records
        result["actual_outbound_attempts"] = adapter.boundary.actual_outbound_attempts
        result["provider_records"] = records
        transmitted = [
            image["sha256"] for row in records for image in row.get("images", [])
        ]
        result["confirmed_image_present"] = fixture["confirmed_sha256"] in transmitted
        result["only_confirmed_image_transmitted"] = bool(transmitted) and all(
            digest == fixture["confirmed_sha256"] for digest in transmitted
        )
        result["uncropped_original_transmitted"] = (
            fixture["original_local_only_sha256"] in transmitted
        )
        result["passed"] = (
            len(result["cases"]) == 3
            and all(case["passed"] for case in result["cases"])
            and result["same_session"]
            and result["only_confirmed_image_transmitted"]
        )
    except Exception as exc:
        result["passed"] = False
        result["error_code"] = getattr(exc, "code", type(exc).__name__)
        # Keep diagnostic text private; public output never includes provider
        # error bodies, authorization values or account identity.
        save(
            directory / "private/failure.json",
            {"type": type(exc).__name__, "code": getattr(exc, "code", None)},
        )
    finally:
        await service.close()
        cleanup = await adapter.close()
        save(directory / "private/cleanup.json", cleanup)
        host = cleanup.get("host", {})
        result["cleanup"] = {
            "method": host.get("method"),
            "remaining_owned_processes": len(host.get("remaining_pids", [])),
            "host_port_closed": host.get("host_port_closed"),
        }
        repository.close()
        save(directory / "result.json", result)
        save(ROOT / "research/reports/redesign" / f"{args.run_id}.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--route", required=True, choices=["account", "api_key"])
    parser.add_argument(
        "--account-home",
        type=Path,
        default=ROOT / ".runtime/app/data/execution/host/home",
    )
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument(
        "--thinking", default="high", choices=["none", "low", "high", "max"]
    )
    parser.add_argument("--max-output-tokens", type=int, default=16384)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.run_id):
        parser.error(
            "run-id must contain 1-80 ASCII letters, numbers, underscores or hyphens"
        )
    if not 256 <= args.max_output_tokens <= 131072 or not 1 <= args.timeout <= 3600:
        parser.error("invalid output or timeout bound")
    result = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "passed": result["passed"],
                "actual_outbound_attempts": result.get("actual_outbound_attempts"),
                "error_code": result.get("error_code"),
                "cleanup": result.get("cleanup"),
            }
        ),
        flush=True,
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
