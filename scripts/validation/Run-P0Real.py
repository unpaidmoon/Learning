"""Explicit real acceptance through the formal same-origin application API.

Only an already approved M01 image is accepted. No video/screen acquisition,
automatic HTTP retry or unknown execution resubmission exists in this runner.
Private responses, the database and provider ledger stay in the isolated run.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from learning_app.processes import OwnedProcessGroup
from learning_app.storage import Repository

ROOT = Path(__file__).resolve().parents[2]
APPROVED_M01 = "ca17df7fe252dd9dbd37d75c086b24848aaac7dd668adf87c980511c039aaa13"
TERMINAL = {"completed", "cancelled", "failed", "truncated"}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", "utf-8"
    )


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class ApplicationInstance:
    def __init__(self, run_dir, port, label):
        self.run_dir, self.port, self.label = run_dir, port, label
        self.group = OwnedProcessGroup(f"p0-real-{label}")
        self.lines = queue.Queue()
        self.ready = None

    async def start(self):
        environment = {
            **os.environ,
            "LEARNING_APP_HARNESS_ENABLED": "true",
            "LEARNING_APP_MODEL": "deepseek-flash",
            "LEARNING_APP_THINKING": "high",
            "LEARNING_APP_MAX_OUTPUT_TOKENS": "16384",
            "LEARNING_APP_MODEL_TIMEOUT_SECONDS": "300",
            "LEARNING_APP_DATA_DIR": str(self.run_dir / "data"),
            "LEARNING_APP_RUNTIME_DIR": str(self.run_dir / "run"),
            "PYTHONUNBUFFERED": "1",
        }
        self.log = (self.run_dir / f"{self.label}.stderr.log").open(
            "w", encoding="utf-8"
        )
        self.process = self.group.spawn(
            [
                sys.executable,
                str(ROOT / "scripts/app.py"),
                "start",
                "--backend-port",
                str(self.port),
                "--timeout",
                "45",
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=self.log,
        )

        def read_lines():
            for line in self.process.stdout:
                self.lines.put(line)
            self.lines.put(None)

        threading.Thread(target=read_lines, daemon=True).start()
        async with asyncio.timeout(50):
            while True:
                line = await asyncio.to_thread(self.lines.get)
                if line is None:
                    raise RuntimeError("APPLICATION_EXITED_BEFORE_READY")
                if line.startswith("LEARNING_APP_READY "):
                    self.ready = json.loads(line.split(" ", 1)[1])
                    return self.ready

    async def stop(self):
        if self.ready and self.process.poll() is None:
            record = self.run_dir / "run/apps" / self.ready["instance_id"]
            (record / "stop.request").write_text(self.ready["instance_id"], "utf-8")
            try:
                await asyncio.wait_for(asyncio.to_thread(self.process.wait), 15)
            except TimeoutError:
                pass
        cleanup = self.group.close()
        self.log.close()
        return cleanup


async def run(args):
    image = args.image.read_bytes()
    if hashlib.sha256(image).hexdigest() != APPROVED_M01:
        raise ValueError("IMAGE_NOT_PREVIOUSLY_APPROVED_M01")
    directory = (ROOT / ".runtime/validation" / args.run_id).resolve()
    if not directory.is_relative_to((ROOT / ".runtime/validation").resolve()):
        raise ValueError("RUN_DIRECTORY_ESCAPE")
    directory.mkdir(parents=True, exist_ok=False)
    # Migrate approved fixture bytes, never use the probe environment as a runtime dependency.
    (directory / "approved-M01.png").write_bytes(image)
    port = free_port()
    instance = ApplicationInstance(directory, port, "initial")
    result = {
        "run_id": args.run_id,
        "scope": "formal_application_HTTP_SQLite_Host_real_provider",
        "model": "deepseek-flash",
        "thinking": "high",
        "output_tokens": 16384,
        "timeout_seconds": 300,
        "approved_image_sha256": APPROVED_M01,
        "cases": [],
    }
    try:
        await instance.start()
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=20
        ) as client:
            health = (await client.get("/api/v1/health")).json()
            assert health["capabilities"]["harness"] == "ready", health
            token = (await client.get("/api/v1/session")).json()["csrf_token"]
            client.headers["X-Learning-CSRF"] = token

            async def post(path, payload=None):
                response = await client.post("/api/v1" + path, json=payload)
                response.raise_for_status()
                return response.json()

            course = await post("/courses", {"title": "P0真实图文验收（合成M01）"})
            chat = await post(f"/courses/{course['course_id']}/chats")
            attachment = await post(
                "/attachments",
                {
                    "data_base64": base64.b64encode(image).decode(),
                    "media_type": "image/png",
                    "confirmed": True,
                },
            )
            started = time.monotonic()
            first = await post(
                "/submissions",
                {
                    "chat_id": chat["chat_id"],
                    "submission_id": "p0-api-real-1",
                    "text": "请读取已确认题图，先核对负号和三次方，再分别用洛必达和泰勒完整讲解。",
                    "attachment_ids": [attachment["attachment_id"]],
                    "model": "deepseek-flash",
                    "thinking": "high",
                },
            )
            gid = first["generation_id"]
            observed, cancelled, first_body_ms = [], False, None

            async def watch(generation_id, cancel_body=False):
                nonlocal cancelled, first_body_ms
                previous = None
                terminal_observed_at = None
                async with asyncio.timeout(330):
                    while True:
                        response = await client.get(
                            f"/api/v1/generations/{generation_id}"
                        )
                        response.raise_for_status()
                        value = response.json()
                        key = (
                            value["event_watermark"],
                            value["state"],
                            value["usage"]["status"],
                        )
                        if key != previous:
                            observed.append(
                                {
                                    "at_ms": round(
                                        (time.monotonic() - started) * 1000, 3
                                    ),
                                    "state": value["state"],
                                    "watermark": value["event_watermark"],
                                    "body_length": len(value["body"]),
                                    "reasoning_length": len(value["reasoning"]),
                                    "usage_status": value["usage"]["status"],
                                }
                            )
                            previous = key
                        if value["body"] and first_body_ms is None:
                            first_body_ms = (time.monotonic() - started) * 1000
                        if (
                            cancel_body
                            and not cancelled
                            and len(re.sub(r"[\s*#`$]", "", value["body"])) >= 24
                        ):
                            cancelled = True
                            await post(f"/generations/{generation_id}/cancel")
                        if value["state"] in TERMINAL:
                            terminal_observed_at = (
                                terminal_observed_at or time.monotonic()
                            )
                            if (
                                value["usage"]["raw"] is not None
                                or time.monotonic() - terminal_observed_at > 5
                            ):
                                return value
                        if value["state"] == "unknown":
                            # Query only. This runner has no path that repeats a submitted prompt.
                            await post(f"/generations/{generation_id}/reconcile")
                        await asyncio.sleep(
                            0.05
                        )  # Read polling; every action is gated by actual state.

            stopped = await watch(gid, True)
            save(directory / "private/first-generation.json", stopped)
            assert cancelled and stopped["state"] == "cancelled" and stopped["body"]
            result["cases"].append(
                {
                    "id": "APP-REAL-CANCEL",
                    "passed": True,
                    "submission_id": first["submission_id"],
                    "generation_id": gid,
                    "body_chars": len(stopped["body"]),
                    "reasoning_chars": len(stopped["reasoning"]),
                    "first_body_ms": first_body_ms,
                    "usage": stopped["usage"],
                }
            )
            second = await post(
                "/submissions",
                {
                    "chat_id": chat["chat_id"],
                    "submission_id": "p0-api-real-2",
                    "text": "更正分母为2x³，分子不变。请沿前题继续解释泰勒展开是否合法并给出更正后的完整结果。",
                    "attachment_ids": [],
                    "corrections": ["分母应为2x³，先前x³作废"],
                    "model": "deepseek-flash",
                    "thinking": "high",
                },
            )
            finished = await watch(second["generation_id"])
            save(directory / "private/second-generation.json", finished)
            assert finished["state"] == "completed" and finished["body"]
            assert re.search(
                r"(?:frac\s*\{?1\}?\s*\{?6\}?|1\s*/\s*6|六分之一)", finished["body"]
            )
            result["cases"].append(
                {
                    "id": "APP-REAL-CORRECTION-FOLLOWUP",
                    "passed": True,
                    "generation_id": second["generation_id"],
                    "math_result": "1/6",
                    "usage": finished["usage"],
                    "chat_id": chat["chat_id"],
                }
            )
            save(directory / "private/observations.json", observed)
            original_chat = (
                await client.get(f"/api/v1/chats/{chat['chat_id']}")
            ).json()
            assert len(original_chat["messages"]) == 4
            result["cleanup_initial"] = await instance.stop()
            instance = ApplicationInstance(directory, port, "restart")
            await instance.start()
            await client.get("/api/v1/session")
            restored = (await client.get(f"/api/v1/chats/{chat['chat_id']}")).json()
            assert restored["messages"] == original_chat["messages"]
            assert all(
                "adapter_metadata" not in generation
                for generation in restored["generations"]
            )
            result["cases"].append(
                {
                    "id": "APP-REAL-READONLY-RESTART",
                    "passed": True,
                    "messages": len(restored["messages"]),
                    "no_resubmit": True,
                }
            )
        result["status"] = "passed"
    finally:
        result["cleanup_final"] = await instance.stop()
        repo = Repository(directory / "data")
        result["sqlite"] = repo.runtime_info()
        result["counts"] = repo.counts()
        result["business_evidence"] = []
        if "chat" in locals():
            for generation in repo.get_chat(chat["chat_id"])["generations"]:
                evidence = repo.evidence(generation["generation_id"])
                save(
                    directory
                    / "private"
                    / f"business-{generation['generation_id']}.json",
                    evidence,
                )
                binding = evidence["binding"] or {}
                result["business_evidence"].append(
                    {
                        "generation_id": generation["generation_id"],
                        "submission_id": generation["submission_id"],
                        "chat_id": generation["chat_id"],
                        "state": generation["state"],
                        "event_watermark": generation["event_watermark"],
                        "body_sha256": hashlib.sha256(
                            generation["body"].encode()
                        ).hexdigest(),
                        "body_chars": len(generation["body"]),
                        "binding": {
                            key: binding.get(key)
                            for key in (
                                "binding_id",
                                "session_id",
                                "epoch",
                                "last_event_watermark",
                                "state",
                            )
                        },
                        "attempts": evidence["attempts"],
                        "usage_records": generation["usage"],
                    }
                )
        repo.close()
        ledger_path = directory / "data/execution/boundary/boundary-ledger.jsonl"
        ledger = (
            [json.loads(line) for line in ledger_path.read_text("utf-8").splitlines()]
            if ledger_path.exists()
            else []
        )
        result["actual_outbound_attempts"] = sum(
            row.get("event") == "dispatch_started" for row in ledger
        )
        save(directory / "run-record.json", result)
        if args.export:
            save(ROOT / "research/reports/p0/P0-application-real-2026-10-21.json", result)
        print(
            json.dumps(
                {
                    "run_id": args.run_id,
                    "status": result.get("status", "failed"),
                    "actual_outbound_attempts": result["actual_outbound_attempts"],
                }
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--run-id", default=f"p0-application-real-{time.time_ns()}")
    parser.add_argument("--export", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
