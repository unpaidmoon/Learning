"""V3 synthetic mathematics through the formal local HTTP / Harness execution path.

Preparation never loads a model key. Real execution is explicit (--real), uses
generated synthetic images or an explicitly selected historical approved image,
and keeps requests, provider SSE and the DB in an isolated .runtime directory.
Execution completion never claims mathematics or user learning-value acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend/src"))
FIXTURES = ROOT / "tests/fixtures/v3"
TERMINAL = {"completed", "cancelled", "failed", "truncated"}
APPROVED_H02_HASH = "37e7e83aadf641d46d0113a2c74530159ca17be783bea5fe1567eed46ac6c3fb"


def approved_h02(directory: Path, matrix: dict) -> tuple[dict, dict]:
    """Reuse only the exact request image named by the historical approved manifest."""
    previous_result = ROOT / "research/reports/v1/V1-2026-10-21.json"
    previous = json.loads(previous_result.read_text("utf-8"))["assets"]["H02"]
    prior_directory = ROOT / ".runtime/validation/v1-b-real-20261021"
    prior_manifest_path = prior_directory / "manifest.json"
    prior_manifest = json.loads(prior_manifest_path.read_text("utf-8"))
    exact_path = "private/H02-request.jpg"
    item = next(row for row in prior_manifest["items"] if row["path"] == exact_path)
    if not (
        previous["user_confirmed_scope"] is True
        and previous["request_sha256"] == APPROVED_H02_HASH == item["sha256"]
        and previous["request_size"] == [1280, 540]
    ):
        raise RuntimeError("H02_HISTORICAL_SCOPE_OR_HASH_MISMATCH")
    content = (prior_directory / exact_path).read_bytes()
    if digest(content) != APPROVED_H02_HASH or len(content) != item["bytes"]:
        raise RuntimeError("H02_APPROVED_REQUEST_IMAGE_CHANGED")
    # No re-encoding, source-original read, frame acquisition or screen capture.
    target = directory / "samples/H02.jpg"
    target.write_bytes(content)
    original_case = next(case for case in matrix["cases"] if case["id"] == "H02")
    case = {
        **original_case,
        "image_file": "H02.jpg",
        "image_media_type": "image/jpeg",
        "source_kind": "user_provided_previously_confirmed_exact_request_handwriting",
        "executable": True,
    }
    provenance = {
        "fixture_id": "H02",
        "revision": previous["revision"],
        "sha256": APPROVED_H02_HASH,
        "dimensions": [1280, 540],
        "prior_result_ref": str(previous_result.relative_to(ROOT)),
        "prior_manifest_sha256": digest(prior_manifest_path.read_bytes()),
        "prior_input_ref": str((prior_directory / exact_path).relative_to(ROOT)),
        "user_confirmed_scope": True,
        "operations": "reuse exact previously submitted JPEG; no crop, resize or re-encoding",
    }
    return case, provenance


def ambiguity_variant(directory: Path) -> tuple[dict, dict]:
    """Different numerator, still only the result-determining exponent missing."""
    from PIL import Image, ImageDraw, ImageFont

    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = ImageFont.truetype(str(font_path), 42)
    image = Image.new("RGB", (1440, 600), "white")
    draw = ImageDraw.Draw(image)
    lines = [
        "B01ALT · 合成数学题（不同同类缺条件复测）",
        "求下列极限：",
        "lim (e^x − 1 − x) / x^(无法辨认)",
        "x → 0",
        "本图为自生成题图；仅分母指数缺失。",
    ]
    for index, line in enumerate(lines):
        draw.text((55, 55 + index * 98), line, font=font, fill="#142d47")
    path = directory / "samples/B01ALT.png"
    image.save(path, format="PNG")
    case = {
        "id": "B01ALT",
        "question": "求x趋于0时(e^x−1−x)/x^(无法辨认)的极限。",
        "rounds": [{
            "text": "请根据已确认题图求极限。",
            "attachment": True,
            "expected": {
                "reading": "仅分母指数缺失，分子为e^x−1−x。",
                "method": "请求补充必要指数，不默认成常见的2。",
                "calculation": "分子二阶等价于x²/2，极限取决于缺失的指数。",
                "conclusion": "先询问指数，不按猜测条件输出本题确定答案。",
            },
        }],
    }
    provenance = {
        "case_id": "B01ALT",
        "revision": "v3-ambiguity-alternate-r1",
        "source": "synthetic",
        "sha256": digest(path.read_bytes()),
        "dimensions": [1440, 600],
        "font_sha256": digest(font_path.read_bytes()),
        "scope": "full_self_generated_image; denominator_exponent_missing_no_hidden_answer",
        "question": case["question"],
        "expected": case["rounds"][0]["expected"],
    }
    return case, provenance


def taylor_text_variant() -> dict:
    return {
        "id": "T01",
        "question": "求x趋于0时(sin x−x)/x³的极限。请列出至x⁵阶的泰勒展开，并核清减法中的符号。",
        "source_kind": "synthetic_text_mathematics",
        "rounds": [{
            "text": "请完整解答，写清关键条件和推导。",
            "attachment": False,
            "expected": {
                "reading": "分子为sin x−x、分母x³，x趋于0。",
                "method": "在0展开正弦并消去一次项。",
                "calculation": "sin x−x=−x³/6+x⁵/120+O(x⁷)。",
                "conclusion": "极限−1/6；五次项为正。",
            },
        }],
    }


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def source_digests() -> dict[str, str]:
    paths = [
        *sorted((ROOT / "backend/src/learning_app").rglob("*.py")),
        *sorted((ROOT / "frontend/src").rglob("*.tsx")),
        *sorted((ROOT / "frontend/src").rglob("*.ts")),
        *sorted((ROOT / "frontend/src").rglob("*.css")),
        ROOT / "package.json",
        ROOT / "frontend/package.json",
        ROOT / "backend/pyproject.toml",
        ROOT / "scripts/app.py",
        ROOT / "harness/runtime.lock.json",
        ROOT / "harness/profiles/learning.patch.json",
        ROOT / "harness/plugins/learning-guard.mjs",
        ROOT / "pnpm-lock.yaml",
        ROOT / "backend/uv.lock",
        FIXTURES / "cases.json",
        FIXTURES / "generate_samples.py",
        Path(__file__).resolve(),
    ]
    return {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in paths}


def generated_samples(directory: Path) -> dict:
    spec = importlib.util.spec_from_file_location("v3_samples", FIXTURES / "generate_samples.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("V3_SAMPLE_GENERATOR_MISSING")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate(directory)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class ApplicationInstance:
    """Own only the application created by this run, including its Host tree."""

    def __init__(self, directory: Path, args: argparse.Namespace):
        from learning_app.processes import OwnedProcessGroup

        self.directory, self.args = directory, args
        self.port = free_port()
        self.group = OwnedProcessGroup("v3-real")
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.ready: dict | None = None
        self.process: subprocess.Popen | None = None
        self.log = None

    async def start(self) -> dict:
        environment = {
            **os.environ,
            "LEARNING_APP_HARNESS_ENABLED": "true",
            "LEARNING_APP_MODEL": self.args.model,
            "LEARNING_APP_THINKING": self.args.thinking,
            "LEARNING_APP_MAX_OUTPUT_TOKENS": str(self.args.max_output_tokens),
            "LEARNING_APP_MODEL_TIMEOUT_SECONDS": str(self.args.timeout),
            "LEARNING_APP_DATA_DIR": str(self.directory / "data"),
            "LEARNING_APP_RUNTIME_DIR": str(self.directory / "run"),
            "PYTHONUNBUFFERED": "1",
        }
        self.log = (self.directory / "application.stderr.log").open("x", encoding="utf-8")
        self.process = self.group.spawn(
            [
                sys.executable,
                str(ROOT / "scripts/app.py"),
                "start",
                "--backend-port",
                str(self.port),
                "--timeout",
                "60",
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=self.log,
        )

        def read_lines() -> None:
            assert self.process is not None and self.process.stdout is not None
            for line in self.process.stdout:
                self.lines.put(line)
            self.lines.put(None)

        threading.Thread(target=read_lines, daemon=True).start()
        async with asyncio.timeout(65):
            while True:
                line = await asyncio.to_thread(self.lines.get)
                if line is None:
                    raise RuntimeError("APPLICATION_EXITED_BEFORE_READY")
                if line.startswith("LEARNING_APP_READY "):
                    self.ready = json.loads(line.split(" ", 1)[1])
                    return self.ready

    async def stop(self) -> dict:
        if self.ready and self.process is not None and self.process.poll() is None:
            record = self.directory / "run/apps" / self.ready["instance_id"]
            (record / "stop.request").write_text(self.ready["instance_id"], "utf-8")
            try:
                await asyncio.wait_for(asyncio.to_thread(self.process.wait), 15)
            except TimeoutError:
                pass
        cleanup = self.group.close()
        if self.log is not None:
            self.log.close()
        return cleanup


def boundary_rows(directory: Path) -> tuple[list[dict], list[dict]]:
    ledger, latest = [], {}
    for path in sorted((directory / "data").rglob("boundary-ledger.jsonl")):
        ledger.extend(json.loads(line) for line in path.read_text("utf-8").splitlines())
    for entry in ledger:
        if entry.get("request_id"):
            latest.setdefault(entry["request_id"], {}).update(entry)
    return ledger, list(latest.values())


def request_audit(directory: Path, generation_id: str, approved_hashes: set[str]) -> list[dict]:
    _, rows = boundary_rows(directory)
    audited = []
    for row in rows:
        if row.get("generation_id") != generation_id:
            continue
        paths = list((directory / "data").rglob(f"{row['request_id']}.request.json"))
        if len(paths) != 1:
            raise RuntimeError("PROVIDER_REQUEST_EVIDENCE_MISSING")
        request = json.loads(paths[0].read_text("utf-8"))
        # Every image is supplied by the generated-sample whitelist. Requests
        # cannot contain a frame, source path or a crop outside that whitelist.
        images = row.get("images", [])
        actual_hashes = {image.get("sha256") for image in images}
        all_approved = actual_hashes == approved_hashes
        audit = {
            "request_id": row["request_id"],
            "generation_id": generation_id,
            "body_sha256": row.get("body_sha256"),
            "model": request.get("model"),
            "thinking": request.get("thinking"),
            "output_config": request.get("output_config"),
            "max_tokens": request.get("max_tokens"),
            "tools": row.get("tools", []),
            "images": images,
            "all_request_images_whitelisted": all_approved,
            "exact_current_chat_image_set": all_approved,
            "usage": row.get("usage"),
            "actual_outbound_attempt": row.get("actual_outbound_attempt"),
            "provider_status": row.get("status"),
            "provider_cancel_observed": row.get("cancel_observed"),
            "provider_timed_out": row.get("timed_out"),
            "provider_disconnect_ns": row.get("disconnect_ns"),
            "provider_upstream_status": row.get("upstream_status"),
            "private_request_ref": str(paths[0].relative_to(directory)),
        }
        audited.append(audit)
        if not all_approved:
            raise RuntimeError("PROVIDER_IMAGE_OUTSIDE_APPROVED_SYNTHETIC_SCOPE")
    return audited


def provider_activity(directory: Path, generation_id: str) -> dict:
    from learning_app.execution.boundary import UsageObserver

    _, rows = boundary_rows(directory)
    observations = []
    for row in rows:
        if row.get("generation_id") != generation_id:
            continue
        paths = list((directory / "data").rglob(f"{row['request_id']}.response.sse"))
        raw = paths[0].read_bytes() if len(paths) == 1 else b""
        observer = UsageObserver()
        observer.feed(raw)
        observations.append({
            "request_id": row["request_id"],
            "actual_outbound_started": row.get("attempt") is not None,
            "request_finished_recorded": row.get("event") == "request_finished",
            "provider_terminal_sse_observed": observer.complete,
            "raw_sse_bytes_at_observation": len(raw),
            "raw_sse_sha256_at_observation": digest(raw),
        })
    return {
        "observed_at_ns": time.time_ns(),
        "provider_active_before_cancel": any(
            row["actual_outbound_started"]
            and not row["request_finished_recorded"]
            and not row["provider_terminal_sse_observed"]
            for row in observations
        ),
        "requests": observations,
    }


async def observe(
    client: httpx.AsyncClient,
    generation_id: str,
    started: float,
    directory: Path,
    args: argparse.Namespace,
    *,
    cancel_on_body: bool = False,
) -> tuple[dict, dict]:
    previous = None
    snapshots = []
    first_delta = None
    terminal_at = None
    terminal_ms = None
    cancelled = False
    cancel_state = None
    reconciled = False
    deadline = time.monotonic() + args.timeout + 45
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/generations/{generation_id}")
        response.raise_for_status()
        value = response.json()
        at_ms = round((time.monotonic() - started) * 1000, 3)
        current = (value["state"], value["event_watermark"], value["usage"])
        if current != previous:
            snapshots.append({"at_ms": at_ms, **value})
            previous = current
        if value["body"] and first_delta is None:
            first_delta = at_ms
        if (
            cancel_on_body
            and not cancelled
            and value["state"] not in TERMINAL | {"unknown"}
            and len(re.sub(r"[\s*#`$]", "", value["body"])) >= 32
        ):
            cancel_state = {
                "at_ms": at_ms,
                "state_before_cancel": value["state"],
                "body_chars_before_cancel": len(value["body"]),
                "body_sha256_before_cancel": digest(value["body"].encode()),
                "provider_before_cancel": provider_activity(directory, generation_id),
            }
            response = await client.post(f"/api/v1/generations/{generation_id}/cancel")
            response.raise_for_status()
            cancelled = True
        if value["state"] in TERMINAL:
            terminal_at = terminal_at or time.monotonic()
            terminal_ms = terminal_ms if terminal_ms is not None else at_ms
            if value["usage"].get("raw") is not None or time.monotonic() - terminal_at > 3:
                save(directory / "private" / f"{generation_id}.snapshots.json", snapshots)
                return value, {
                    "time_to_first_body_delta_ms": first_delta,
                    "time_to_useful_content_ms": None,
                    "useful_content_review": "pending_independent_content_review",
                    "time_to_complete_ms": terminal_ms,
                    "time_to_final_usage_observation_ms": at_ms,
                    "distinct_observations": len(snapshots),
                    "distinct_nonempty_body_observations": len(
                        {item["body"] for item in snapshots if item["body"]}
                    ),
                    "cancel_requested_during_body": cancelled,
                    "cancel_observation": cancel_state,
                }
        if value["state"] == "unknown" and not reconciled:
            # Reconciliation only, never another /submissions POST for this input.
            response = await client.post(f"/api/v1/generations/{generation_id}/reconcile")
            response.raise_for_status()
            reconciled = True
        await asyncio.sleep(0.05)
    save(directory / "private" / f"{generation_id}.snapshots.json", snapshots)
    raise TimeoutError("GENERATION_OBSERVATION_TIMEOUT_NO_RESUBMISSION")


async def execute_case(
    case: dict,
    client: httpx.AsyncClient,
    course_id: str,
    directory: Path,
    args: argparse.Namespace,
    approved_hashes: set[str],
    *,
    cancel_first: bool = False,
) -> dict:
    async def post(path: str, payload: dict | None = None) -> dict:
        response = await client.post("/api/v1" + path, json=payload)
        response.raise_for_status()
        return response.json()

    chat = await post(f"/courses/{course_id}/chats")
    case_result = {
        "case_id": case["id"],
        "chat_id": chat["chat_id"],
        "source": case.get("source_kind", "synthetic_printed_math_not_real_video_or_handwriting"),
        "question": case["question"],
        "mathematics_status": "pending_review",
        "rounds": [],
    }
    attachment = None
    if not args.text_only:
        image = (directory / "samples" / case.get("image_file", f"{case['id']}.png")).read_bytes()
        attachment = await post(
            "/attachments",
            {
                "data_base64": base64.b64encode(image).decode("ascii"),
                "media_type": case.get("image_media_type", "image/png"),
                "confirmed": True,
            },
        )
        case_result["attachment_id"] = attachment["attachment_id"]
        case_result["submitted_image_sha256"] = digest(image)
    for index, round_input in enumerate(case["rounds"]):
        started = time.monotonic()
        submission_id = f"v3-{case['id']}-{index + 1}"
        payload = {
            "chat_id": chat["chat_id"],
            "submission_id": submission_id,
            "text": (
                case["question"] + "\n请根据以上文字完整解答，写清关键条件和推导。"
                if args.text_only and index == 0 else round_input["text"]
            ),
            "attachment_ids": [attachment["attachment_id"]]
            if round_input.get("attachment") and attachment is not None
            else [],
            "corrections": round_input.get("corrections", []),
            "conditions": round_input.get("conditions", []),
            "model": args.model,
            "thinking": args.thinking,
        }
        submitted = await post("/submissions", payload)
        pending_row = {
            "round": index + 1,
            "submission_id": submission_id,
            "generation_id": submitted["generation_id"],
            "input": round_input,
            "submitted_as_text_only": args.text_only,
            "state": "observing",
        }
        case_result["rounds"].append(pending_row)
        save(directory / "case-results" / f"{case['id']}.json", case_result)
        value, metrics = await observe(
            client,
            submitted["generation_id"],
            started,
            directory,
            args,
            cancel_on_body=cancel_first and index == 0,
        )
        save(directory / "private" / f"{submitted['generation_id']}.final.json", value)
        expected_images = {case_result["submitted_image_sha256"]} if attachment else set()
        if not expected_images.issubset(approved_hashes):
            raise RuntimeError("CASE_IMAGE_NOT_IN_FROZEN_APPROVED_WHITELIST")
        audit = request_audit(directory, submitted["generation_id"], expected_images)
        row = {
            "round": index + 1,
            "submission_id": submission_id,
            "generation_id": submitted["generation_id"],
            "input": round_input,
            "submitted_as_text_only": args.text_only,
            "state": value["state"],
            "body": value["body"],
            "body_sha256": digest(value["body"].encode()),
            "reasoning_chars": len(value["reasoning"]),
            "usage": value["usage"],
            "metrics": metrics,
            "request_audit": audit,
        }
        case_result["rounds"][-1] = row
        save(directory / "case-results" / f"{case['id']}.json", case_result)
        expected_state = "cancelled" if cancel_first and index == 0 else "completed"
        if value["state"] != expected_state:
            case_result["execution_status"] = "failed"
            case_result["failure"] = f"EXPECTED_{expected_state.upper()}_GOT_{value['state'].upper()}"
            return case_result
        if cancel_first and index == 0 and not metrics["cancel_observation"][
            "provider_before_cancel"
        ]["provider_active_before_cancel"]:
            case_result["execution_status"] = "failed"
            case_result["failure"] = "PROVIDER_NOT_ACTIVE_WHEN_BODY_CANCEL_SENT"
            return case_result
        if not audit:
            raise RuntimeError("NO_ACTUAL_PROVIDER_REQUEST_EVIDENCE")
        print(json.dumps({"case_id": case["id"], "round": index + 1, "state": value["state"]}), flush=True)
    restored_response = await client.get(f"/api/v1/chats/{chat['chat_id']}")
    restored_response.raise_for_status()
    restored = restored_response.json()
    case_result["saved_message_count"] = len(restored["messages"])
    case_result["execution_status"] = "completed"
    return case_result


def stop_case() -> dict:
    return {
        "id": "STOP",
        "question": "M01 正文活动停止后改分母为 2x³，沿原题继续。",
        "rounds": [
            {
                "text": "请读取题图，用泰勒展开和洛必达两种方法逐步完整讲解，并解释每次等价变换为何有效。",
                "attachment": True,
                "expected": {"conclusion": "活动正文停止，保留部分文本"},
            },
            {
                "text": "更正：分母应为2x³，分子不变。请沿用已提交题图，按新条件完整给出结果，解释泰勒展开为什么合法。",
                "attachment": False,
                "corrections": ["分母从x³更正为2x³，分子不变"],
                "expected": {"conclusion": "1/6，保留原题和更正"},
            },
        ],
    }


async def run(args: argparse.Namespace) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", args.run_id):
        raise ValueError("RUN_ID_INVALID")
    directory = (ROOT / ".runtime/validation" / args.run_id).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    matrix = json.loads((FIXTURES / "cases.json").read_text("utf-8"))
    generated_samples(directory / "samples")
    manifest = json.loads((directory / "samples/manifest.json").read_text("utf-8"))
    selected_ids = set(args.cases.split(",")) if args.cases else None
    cases = [
        case for case in matrix["cases"]
        if case["executable"] and (selected_ids is None or case["id"] in selected_ids)
    ]
    allowable_ids = {case["id"] for case in cases}
    if args.with_approved_h02:
        allowable_ids.add("H02")
    if selected_ids and selected_ids - allowable_ids:
        raise ValueError("UNKNOWN_OR_NONEXECUTABLE_CASE_ID")
    reused_inputs = []
    if args.with_approved_h02:
        handwriting_case, provenance = approved_h02(directory, matrix)
        cases.append(handwriting_case)
        reused_inputs.append(provenance)
    alternate_inputs = []
    if args.with_ambiguity_variant:
        alternate_case, alternate_provenance = ambiguity_variant(directory)
        cases.append(alternate_case)
        alternate_inputs.append(alternate_provenance)
    if args.with_taylor_variant:
        if not args.text_only:
            raise ValueError("TAYLOR_VARIANT_REQUIRES_EXPLICIT_TEXT_ONLY")
        alternate_case = taylor_text_variant()
        cases.append(alternate_case)
        alternate_inputs.append(alternate_case)
    record = {
        "run_id": args.run_id,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "validation_id": "V3",
        "execution_status": "prepared",
        "scope": "formal_application_HTTP_SQLite_Harness_real_provider" if args.real else "preparation_only",
        "model_requested": args.model,
        "thinking_requested": args.thinking,
        "max_output_tokens": args.max_output_tokens,
        "model_timeout_seconds": args.timeout,
        "text_only": args.text_only,
        "real_request_limit": None,
        "selected_cases": [case["id"] for case in cases] + (["STOP"] if args.with_stop else []),
        "source_digests_before": source_digests(),
        "fixture_manifest": manifest,
        "reused_approved_inputs": reused_inputs,
        "alternate_synthetic_inputs": alternate_inputs,
        "cases": [],
        "uncovered": [
            "H01/H03 本轮未提交真实手写；不以打印题替代手写识别"
            if args.with_approved_h02 else "H01/H02/H03 本轮未提交真实手写；不以打印题替代手写识别",
            "真实网课题图及用户数次学习评价待用户主动提交与操作",
            "首次有用内容须结合完整回答核查，不把首字符或thinking当有用内容",
            "小型合成题不能证明考研数学一综合题普遍适用性或数学准确率",
        ],
    }
    save(directory / "frozen-plan.json", record)
    if not args.real:
        save(directory / "run-record.json", record)
        print(json.dumps({"run_id": args.run_id, "status": "prepared", "real_attempts": 0}))
        return 0
    instance = ApplicationInstance(directory, args)
    try:
        ready = await instance.start()
        record["instance_id"] = ready["instance_id"]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{instance.port}", timeout=20) as client:
            health_response = await client.get("/api/v1/health")
            health_response.raise_for_status()
            health = health_response.json()
            if health["capabilities"]["harness"] != "ready":
                raise RuntimeError("HARNESS_NOT_READY")
            if (health["configured_model"], health["configured_thinking"]) != (args.model, args.thinking):
                raise RuntimeError("CONFIGURATION_NOT_EFFECTIVE")
            session_response = await client.get("/api/v1/session")
            session_response.raise_for_status()
            client.headers["X-Learning-CSRF"] = session_response.json()["csrf_token"]
            response = await client.post("/api/v1/courses", json={"title": "V3 合成数学验收"})
            response.raise_for_status()
            course_id = response.json()["course_id"]
            approved_hashes = {
                digest(path.read_bytes()) for path in (directory / "samples").iterdir()
                if path.suffix in {".png", ".jpg"}
            }
            if args.with_stop:
                (directory / "samples/STOP.png").write_bytes((directory / "samples/M01.png").read_bytes())
                cases.append(stop_case())
            for case in cases:
                try:
                    result = await execute_case(
                        case, client, course_id, directory, args, approved_hashes,
                        cancel_first=case["id"] == "STOP",
                    )
                except (httpx.HTTPError, TimeoutError, RuntimeError) as error:
                    partial = directory / "case-results" / f"{case['id']}.json"
                    result = {
                        **(json.loads(partial.read_text("utf-8")) if partial.exists() else {}),
                        "case_id": case["id"],
                        "execution_status": "failed",
                        "failure": type(error).__name__,
                        "failure_detail": str(error),
                        "automatic_resubmit": False,
                        "mathematics_status": "not_reviewable",
                    }
                record["cases"].append(result)
                save(directory / "run-record.partial.json", record)
                print(json.dumps({"case_id": case["id"], "execution_status": result["execution_status"]}), flush=True)
            record["execution_status"] = (
                "completed" if all(case["execution_status"] == "completed" for case in record["cases"])
                else "failed"
            )
    except (httpx.HTTPError, RuntimeError, TimeoutError, OSError) as error:
        record["execution_status"] = "failed"
        record["failure"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        record["cleanup"] = await instance.stop()
        with socket.socket() as listener:
            listener.settimeout(0.5)
            record["cleanup"]["owned_port_closed"] = listener.connect_ex(
                ("127.0.0.1", instance.port)
            ) != 0
        ledger, rows = boundary_rows(directory)
        record["actual_outbound_attempts"] = sum(row.get("event") == "dispatch_started" for row in ledger)
        record["provider_boundary_records"] = rows
        record["source_digests_after"] = source_digests()
        record["sources_unchanged_during_run"] = record["source_digests_before"] == record["source_digests_after"]
        if (directory / "data/business.sqlite3").exists() or (directory / "data").exists():
            from learning_app.storage import Repository

            repository = Repository(directory / "data")
            record["sqlite"] = repository.runtime_info()
            record["counts"] = repository.counts()
            for case in record["cases"]:
                for result in case.get("rounds", []):
                    evidence = repository.evidence(result["generation_id"])
                    save(directory / "private" / f"{result['generation_id']}.business.json", evidence)
                    result["outbound_attempts"] = evidence["attempts"]
                    result["usage_records"] = evidence["generation"].get("usage", [])
            repository.close()
        save(directory / "run-record.json", record)
        if args.export:
            target = ROOT / "research/reports/v3" / f"{args.run_id}.json"
            if target.exists():
                raise FileExistsError("PUBLIC_RESULT_EXISTS_USE_NEW_RUN_ID")
            save(target, record)
        print(json.dumps({"run_id": args.run_id, "status": record["execution_status"], "actual_outbound_attempts": record["actual_outbound_attempts"]}), flush=True)
    return 0 if record["execution_status"] == "completed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=f"v3-{time.time_ns()}")
    parser.add_argument("--real", action="store_true", help="明确启动正式真实模型链路；默认只准备")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--thinking", choices=["none", "low", "medium", "high", "xhigh", "max"], default="high")
    parser.add_argument("--max-output-tokens", type=int, default=16384)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--cases", help="逗号分隔的可执行fixture ID；默认全部合成数学")
    parser.add_argument("--with-stop", action="store_true", help="追加正文活动停止和更正追问两回合")
    parser.add_argument("--with-approved-h02", action="store_true", help="复用manifest核准的唯一H02既有请求JPEG")
    parser.add_argument("--with-ambiguity-variant", action="store_true", help="追加不同同类缺关键指数合成题")
    parser.add_argument("--text-only", action="store_true", help="显式将题文作为文字提交，不附题图")
    parser.add_argument("--with-taylor-variant", action="store_true", help="追加另一道纯文字三阶Taylor极限")
    parser.add_argument("--export", action="store_true", help="保存新公开脱敏结果，不覆盖历史")
    args = parser.parse_args()
    if not 256 <= args.max_output_tokens <= 131072 or not 0 < args.timeout <= 3600:
        parser.error("输出量或观察时限不在应用契约范围")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
