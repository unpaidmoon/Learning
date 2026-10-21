"""Explicit official-provider execution acceptance; never captures screen/video."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import shutil
import time

from dotenv import dotenv_values

from learning_app.execution.adapter import ExecutionAdapter
from learning_app.execution.types import ApprovedAttachment


async def run(args) -> dict:
    repository = Path(__file__).resolve().parents[2]
    secrets = dotenv_values(repository / ".local/secrets/deepseek.env")
    key = secrets.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("PROJECT_PROVIDER_KEY_MISSING")
    run_dir = args.run_dir.resolve()
    if run_dir.exists():
        raise RuntimeError("REAL_ACCEPTANCE_OUTPUT_ALREADY_EXISTS")
    frozen = run_dir / "private/source"
    frozen.mkdir(parents=True)
    source_files = [
        *repository.glob("backend/src/learning_app/execution/*.py"),
        repository / "backend/src/learning_app/processes.py",
        repository / "harness/profiles/learning.patch.json",
        repository / "harness/plugins/learning-guard.mjs",
        repository / "harness/runtime.lock.json",
        Path(__file__).resolve(),
    ]
    source_digests = {}
    for source in source_files:
        relative = source.relative_to(repository)
        destination = frozen / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        source_digests[relative.as_posix()] = hashlib.sha256(
            source.read_bytes()
        ).hexdigest()
    image = args.image.resolve()
    attachment = ApprovedAttachment(
        "approved-M01", image, hashlib.sha256(image.read_bytes()).hexdigest()
    )
    adapter = ExecutionAdapter(
        repository,
        run_dir,
        mode="official",
        api_key=key,
        model="deepseek-flash",
        thinking="high",
        max_tokens=16384,
        timeout=240,
    )
    key, secrets = None, None
    result = {
        "scope": "formal_execution_adapter_official_provider",
        "model": "deepseek-flash",
        "thinking": "high",
        "max_tokens": 16384,
        "timeout_seconds": 240,
        "approved_image_sha256": attachment.sha256,
        "source_digests": source_digests,
        "cases": [],
    }
    try:
        await adapter.open()
        state = await adapter.host.audit_state()
        (run_dir / "profile-state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        result["profile_tools"] = state["tools"]
        for index, cancel in enumerate([True, False], 1):
            generation_id = f"p0-real-body-{index}"
            binding = await adapter.prepare(
                "p0-real-math", f"p0-real-submit-{index}", generation_id, 1
            )
            prompt = (
                "请完整解答确认题图的极限。请先给出结论，再分别用洛必达与泰勒说明，保留必要公式。"
                if cancel
                else "刚才主动停止了回答。请继续完整解答已确认题图，确认分子负号和分母三次方，并用泰勒展开说明结果。"
            )
            started = time.monotonic()
            await adapter.start(binding, prompt, [attachment])
            events, cancel_ack, cancelled_after_text = [], None, False
            body_so_far = ""
            body_chars_before_cancel = 0
            first_text_ms = None
            async for event in adapter.observe(binding):
                events.append(event.to_dict())
                if event.kind == "text_delta":
                    body_so_far += event.text
                    if first_text_ms is None:
                        first_text_ms = round((time.monotonic() - started) * 1000, 2)
                    # A deterministic body-content threshold avoids treating
                    # a Markdown opening marker as the substantive body phase.
                    body_chars = len(re.sub(r"[\s*_#`$\\{}\[\]()]", "", body_so_far))
                    if cancel and not cancelled_after_text and body_chars >= 24:
                        cancelled_after_text = True
                        body_chars_before_cancel = body_chars
                        cancel_ack = await adapter.cancel(binding)
            reconcile = await adapter.reconcile(
                binding, events[-1]["seq"] if events else 0
            )
            evidence = {
                "binding": binding.to_dict(),
                "events": events,
                "reconciled": {
                    **reconcile,
                    "binding": reconcile["binding"].to_dict(),
                    "events": [e.to_dict() for e in reconcile["events"]],
                },
            }
            private = run_dir / "private"
            private.mkdir(exist_ok=True)
            (private / f"case-{index}.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            rows = [
                r
                for r in adapter.boundary.records
                if r.get("generation_id") == generation_id
            ]
            snapshots = [e for e in reconcile["events"] if e.kind == "snapshot"]
            body = "".join(e["text"] for e in events if e["kind"] == "text_delta")
            preserved = bool(body) and any(body in e.text for e in snapshots)
            passed = bool(body) and (
                reconcile["status"] == ("cancelled" if cancel else "completed")
            )
            if cancel:
                passed &= (
                    bool(cancel_ack and cancel_ack.get("accepted"))
                    and preserved
                    and any(r.get("cancel_observed") for r in rows)
                )
            result["cases"].append(
                {
                    "case_id": "P0-REAL-BODY-CANCEL" if cancel else "P0-REAL-CONTINUE",
                    "passed": bool(passed),
                    "binding": binding.to_dict(),
                    "text_delta_count": sum(e["kind"] == "text_delta" for e in events),
                    "reasoning_delta_count": sum(
                        e["kind"] == "reasoning_delta" for e in events
                    ),
                    "cancel_after_body": cancelled_after_text,
                    "body_characters_before_cancel": body_chars_before_cancel,
                    "cancel_ack": cancel_ack,
                    "partial_body_preserved": preserved,
                    "status": reconcile["status"],
                    "time_to_first_text_ms": first_text_ms,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                    "host_alive": adapter.host.process.poll() is None,
                    "attempts": adapter.attempts_for(generation_id),
                    "provider_records": rows,
                }
            )
            if not passed:
                break
        result["real_attempts"] = adapter.boundary.actual_outbound_attempts
    finally:
        result["cleanup"] = await adapter.close()
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", type=Path, required=True, help="Explicitly approved PNG/JPEG only"
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    print(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": r["case_id"],
                        "passed": r["passed"],
                        "status": r["status"],
                    }
                    for r in result["cases"]
                ],
                "real_attempts": result.get("real_attempts"),
                "result": str(args.run_dir / "result.json"),
            }
        )
    )
    raise SystemExit(
        0
        if len(result["cases"]) == 2 and all(r["passed"] for r in result["cases"])
        else 1
    )


if __name__ == "__main__":
    main()
