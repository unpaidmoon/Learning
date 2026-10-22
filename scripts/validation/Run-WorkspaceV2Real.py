"""Explicit real-provider document smoke after synthetic workspace gates pass.

Only generated two-page PDF/TXT/Markdown are uploaded through the formal local
ASGI API. No screen, course material, video or clipboard is read. Credentials
stay in the pinned Harness home or the established private API-key file.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/src"))

from learning_app.app import create_app  # noqa: E402
from learning_app.config import AppConfig  # noqa: E402
from learning_app.documents import expand_document_parts  # noqa: E402
from learning_app.execution.adapter import ExecutionAdapter  # noqa: E402
from learning_app.storage import Repository  # noqa: E402


def module_at(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("VALIDATION_HELPER_MISSING")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


base = module_at("workspace_real_base", ROOT / "scripts/validation/Run-RedesignReal.py")
save = base.save


def synthetic_documents(directory: Path) -> dict:
    directory.mkdir(parents=True)
    fixture = module_at(
        "workspace_document_fixture", ROOT / "tests/fixtures/workspace_v2/generate.py"
    )
    fixture.text_pdf(directory / "two-pages.pdf")
    (directory / "whole.txt").write_text(
        "SYNTHETIC TEXT START\n仅供附件传输验证。\n"
        + "synthetic middle\n" * 20
        + "TEXT_END_2718\n",
        encoding="utf-8",
    )
    (directory / "whole.md").write_text(
        "# SYNTHETIC MARKDOWN\n\n$M=\\begin{pmatrix}0&A\\\\B&E\\end{pmatrix}$.\n\nMARKDOWN_END_5926\n",
        encoding="utf-8",
    )
    return {
        "source": "generated synthetic documents; no user asset access",
        "files": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.iterdir()
        },
        "expected_pdf_pages": 2,
        "expected_markers": [
            "ALPHA 1729",
            "OMEGA 2718",
            "TEXT_END_2718",
            "MARKDOWN_END_5926",
        ],
    }


def freeze_sources(directory: Path) -> dict:
    digests = base.freeze_sources(directory)
    files = [
        Path(__file__).resolve(),
        ROOT / "backend/src/learning_app/documents.py",
        ROOT / "backend/src/learning_app/document_processing.py",
        ROOT / "backend/src/learning_app/app.py",
        ROOT / "backend/pyproject.toml",
        ROOT / "backend/uv.lock",
        ROOT / "tests/fixtures/workspace_v2/generate.py",
        *ROOT.glob("backend/src/learning_app/storage/migrations/versions/*.py"),
    ]
    for source in files:
        relative = source.relative_to(ROOT)
        target = directory / "private/source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        digests[relative.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
    return digests


async def run(args) -> dict:
    root = (ROOT / ".runtime/validation").resolve()
    directory = (root / args.run_id).resolve()
    if directory.parent != root:
        raise ValueError("RUN_DIRECTORY_ESCAPE")
    directory.mkdir(parents=True, exist_ok=False)
    fixture = synthetic_documents(directory / "private/synthetic")
    result = {
        "run_id": args.run_id,
        "client_date": "2026-10-22",
        "scope": "formal ASGI uploads and submissions, GenerationService, pinned Host, real provider; generated whole documents only",
        "route": args.route,
        "model": args.model,
        "thinking": args.thinking,
        "max_output_tokens": args.max_output_tokens,
        "timeout_seconds": args.timeout,
        "fixture": fixture,
        "source_digests": freeze_sources(directory),
        "cases": [],
        "limitations": [
            "This is not a real-browser or human learning/visual acceptance run.",
            "All document pages are rendered locally without another OCR/model call; exceeding bounds rejects the whole document.",
            "A completed response is additionally checked for the generated page and end-of-file markers; broader mathematics/format coverage is not claimed.",
            "Provider completion before the active-stop condition remains a failed/uncovered stop; no request is silently added or retried.",
            "Unknown executions reconcile their original identity without resubmission.",
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
    application = create_app(config, repository=repository, execution=adapter)
    lifetime = application.router.lifespan_context(application)
    entered = False
    service = None
    try:
        await adapter.open()
        await lifetime.__aenter__()
        entered = True
        service = application.state.service
        if not service.question_answering_ready:
            raise RuntimeError("QUESTION_ANSWERING_NOT_READY")
        await adapter.validate_choice(args.model, args.thinking)
        if not adapter.supports_model_images(args.model):
            raise RuntimeError("SELECTED_MODEL_IMAGE_UNSUPPORTED")
        result["profile_tools"] = (await adapter.host.audit_state())["tools"]
        course = repository.create_course("整份附件真实复测合成课程")
        chat = repository.create_chat(course["course_id"])
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8765", timeout=90
        ) as client:
            session = await client.get("/api/v1/session")
            session.raise_for_status()
            headers = {"X-Learning-CSRF": session.json()["csrf_token"]}
            uploaded = []
            for index, (name, mime) in enumerate(
                [
                    ("two-pages.pdf", "application/pdf"),
                    ("whole.txt", "text/plain"),
                    ("whole.md", "text/markdown"),
                ],
                1,
            ):
                data = (directory / "private/synthetic" / name).read_bytes()
                response = await client.post(
                    "/api/v1/document-attachments",
                    headers=headers,
                    json={
                        "chat_id": chat["chat_id"],
                        "filename": name,
                        "media_type": mime,
                        "data_base64": base64.b64encode(data).decode("ascii"),
                        "confirmed": True,
                        "upload_id": f"{args.run_id}-{index}",
                    },
                )
                response.raise_for_status()
                uploaded.append(response.json())
            result["uploaded"] = uploaded
            aids = [item["attachment_id"] for item in uploaded]
            pages, document_text = expand_document_parts(
                repository,
                chat["chat_id"],
                [repository.resolve_attachment(aid) for aid in aids],
            )
            expected_pages = [item["content_hash"] for item in pages]
            result["prepared_pages"] = [
                {
                    "attachment_id": item["attachment_id"],
                    "sha256": item["content_hash"],
                    "media_type": item["media_type"],
                }
                for item in pages
            ]
            result["full_text_prepared"] = all(
                marker in document_text
                for marker in ["TEXT_END_2718", "MARKDOWN_END_5926"]
            )
            sessions = []
            prompts = [
                (
                    "whole-documents",
                    "请读取全部附件。按PDF第1页、PDF第2页、TXT最后一行、Markdown最后一行的顺序，分别用纯文本准确抄出每处的英文标记与数字，不加公式或加粗；再简要说明两页PDF的区别。",
                    False,
                    aids,
                ),
                (
                    "active-stop",
                    "请逐页详细讲解这两页合成PDF：说明文字、数字标记与色块位置，再分别详细解释TXT和Markdown文件的全部结构、末尾标记，以及引用矩阵公式的含义。请完整逐步展开。",
                    True,
                    [],
                ),
                (
                    "after-stop-followup",
                    "刚才主动停止了回答。请继续依据同一对话已提交的整份文件，重新按PDF第1页、PDF第2页、TXT最后一行、Markdown最后一行用纯文本列出各自的英文标记和数字，不加公式或加粗，再解释PDF两页分别有何不同。",
                    False,
                    [],
                ),
            ]
            for index, (case_id, prompt, cancel, attachments) in enumerate(prompts, 1):
                response = await client.post(
                    "/api/v1/submissions",
                    headers=headers,
                    json={
                        "chat_id": chat["chat_id"],
                        "submission_id": f"{args.run_id}-turn-{index}",
                        "text": prompt,
                        "attachment_ids": attachments,
                        "conditions": [],
                        "corrections": [],
                        "model": args.model,
                        "thinking": args.thinking,
                    },
                )
                response.raise_for_status()
                receipt = response.json()
                case = await base.observe_case(
                    service,
                    repository,
                    adapter,
                    receipt["generation_id"],
                    cancel=cancel,
                    timeout=args.timeout,
                )
                case["case_id"] = case_id
                if not cancel:
                    body = service.generation_view(receipt["generation_id"])["body"]
                    # Whitespace around words is presentation; the marker letters
                    # and numbers must actually come from both pages and EOFs.
                    compact = re.sub(r"\s+", "", body)
                    case["response_markers"] = {
                        marker: re.sub(r"\s+", "", marker) in compact
                        for marker in fixture["expected_markers"]
                    }
                    case["passed"] = case["passed"] and all(
                        case["response_markers"].values()
                    )
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
            records = adapter.boundary.records
            result["provider_records"] = records
            result["actual_outbound_attempts"] = (
                adapter.boundary.actual_outbound_attempts
            )
            transmitted = [
                image["sha256"] for row in records for image in row.get("images", [])
            ]
            result["all_pdf_pages_transmitted"] = all(
                digest in transmitted for digest in expected_pages
            )
            result["only_prepared_pages_transmitted"] = bool(transmitted) and set(
                transmitted
            ) == set(expected_pages)
            result["all_pdf_pages_in_every_request"] = bool(records) and all(
                set(expected_pages).issubset(
                    {image["sha256"] for image in row.get("images", [])}
                )
                for row in records
                if row.get("actual_outbound_attempt")
            )
            result["original_pdf_transmitted"] = (
                fixture["files"]["two-pages.pdf"] in transmitted
            )
            raw_requests = [
                json.loads(path.read_text("utf-8"))
                for path in adapter.boundary.private.glob("*.request.json")
            ]
            result["full_text_markers_in_every_request"] = bool(raw_requests) and all(
                all(
                    marker in json.dumps(request, ensure_ascii=False)
                    for marker in ["TEXT_END_2718", "MARKDOWN_END_5926"]
                )
                for request in raw_requests
            )
            result["same_session"] = (
                len(sessions) == 3
                and len(set(sessions)) == 1
                and sessions[0] is not None
            )
            result["source_digests_match_after"] = all(
                hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
                for name, expected in result["source_digests"].items()
            )
            result["passed"] = (
                len(result["cases"]) == 3
                and all(case["passed"] for case in result["cases"])
                and result["same_session"]
                and result["all_pdf_pages_transmitted"]
                and result["only_prepared_pages_transmitted"]
                and result["all_pdf_pages_in_every_request"]
                and not result["original_pdf_transmitted"]
                and result["full_text_prepared"]
                and result["full_text_markers_in_every_request"]
                and result["source_digests_match_after"]
                and result["actual_outbound_attempts"] == 3
            )
    except Exception as exc:
        result["passed"] = False
        result["error_code"] = getattr(exc, "code", type(exc).__name__)
        save(
            directory / "private/failure.json",
            {"type": type(exc).__name__, "code": getattr(exc, "code", None)},
        )
    finally:
        try:
            if entered:
                await lifetime.__aexit__(None, None, None)
            elif service is not None:
                await service.close()
        except Exception as exc:
            result["passed"] = False
            result["lifecycle_cleanup_error"] = type(exc).__name__
        cleanup = await adapter.close()
        save(directory / "private/cleanup.json", cleanup)
        host = cleanup.get("host", {})
        result["cleanup"] = {
            "method": host.get("method"),
            "remaining_owned_processes": len(host.get("remaining_pids", [])),
            "host_port_closed": host.get("host_port_closed"),
        }
        repository.close()
        result.setdefault(
            "actual_outbound_attempts", adapter.boundary.actual_outbound_attempts
        )
        result.setdefault("provider_records", adapter.boundary.records)
        save(directory / "result.json", result)
        save(ROOT / "research/reports/workspace-v2" / f"{args.run_id}.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--route", choices=["account", "api_key"], default="account")
    parser.add_argument(
        "--account-home",
        type=Path,
        default=ROOT / ".runtime/app/data/execution/host/home",
    )
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument(
        "--thinking", choices=["none", "low", "high", "max"], default="high"
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
                "actual_outbound_attempts": result["actual_outbound_attempts"],
                "error_code": result.get("error_code"),
                "cleanup": result["cleanup"],
            }
        ),
        flush=True,
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
