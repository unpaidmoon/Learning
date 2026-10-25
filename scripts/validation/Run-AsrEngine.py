"""Formal installed worker + synthetic finite silence; no cloud/model credential."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import wave
from pathlib import Path

from learning_app.config import PROJECT_ROOT
from learning_app.processes import OwnedProcessGroup
from learning_app.workbench.asr_resources import AsrResources


def run(run_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("RUN_ID_INVALID")
    output = PROJECT_ROOT / ".runtime/validation" / run_id
    output.mkdir(parents=True, exist_ok=False)
    temporary = output / "worker-tmp"
    temporary.mkdir()
    resources = AsrResources(PROJECT_ROOT)
    spec = resources.launch_spec()
    worker = PROJECT_ROOT / "backend/src/learning_app/workbench/asr_worker.py"
    raw = io.BytesIO()
    with wave.open(raw, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000 * 6)
    data = raw.getvalue()
    request = {
        "type": "transcribe",
        "request_id": "synthetic_silence_1",
        "epoch": 1,
        "audio_base64": base64.b64encode(data).decode("ascii"),
        "audio_sha256": hashlib.sha256(data).hexdigest(),
        "start_ms": 12345,
        "language": "zh",
    }
    environment = {
        name: os.environ[name]
        for name in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE")
        if name in os.environ
    }
    environment.update(resources.lock["worker_launch_contract"]["environment"])
    environment.update(
        {
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "PYTHONIOENCODING": "utf-8",
            "PATH": str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32"),
        }
    )
    paths = [
        worker,
        PROJECT_ROOT / "backend/src/learning_app/workbench/asr_resources.py",
        PROJECT_ROOT / "resources/workbench/engines.lock.json",
        PROJECT_ROOT / "resources/workbench/asr-runtime.lock.json",
    ]
    before = {
        str(path.relative_to(PROJECT_ROOT)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }
    result = {
        "status": "running",
        "layer": "formal installed ASR worker, synthetic silence",
        "actual_cloud_model_attempts": 0,
        "local_asr_windows": 1,
        "credential_environment_keys": [],
        "source_before": before,
        "audio_sha256": request["audio_sha256"],
        "audio_duration_ms": 6000,
        "input_scope": "generated PCM16 mono16k silence only; no user media/video frames",
        "limits": [
            "No real speech accuracy, product queue/cancellation or user learning acceptance."
        ],
    }
    group = OwnedProcessGroup("asr-installed-verification")
    try:
        process = group.spawn(
            [sys.executable, "-I", "-S", str(worker)], cwd=output, env=environment
        )
        payload = (
            json.dumps({"type": "initialize", "spec": spec})
            + "\n"
            + json.dumps(request)
            + "\n"
        )
        stdout, stderr = process.communicate(payload, timeout=180)
        (output / "worker.stdout.jsonl").write_text(stdout, "utf-8")
        (output / "worker.stderr.txt").write_text(stderr, "utf-8")
        responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        if (
            process.returncode != 0
            or len(responses) != 2
            or responses[0].get("type") != "ready"
            or responses[1].get("type") != "result"
        ):
            raise RuntimeError("FORMAL_ASR_WORKER_FAILED")
        recognized = responses[1]
        assert (
            recognized["request_id"] == request["request_id"]
            and recognized["epoch"] == 1
        )
        assert recognized["start_ms"] == 12345 and recognized["duration_ms"] == 6000
        assert recognized["duration_after_vad_ms"] == 0 and recognized["segments"] == []
        result.update(
            status="passed", worker_exit_code=process.returncode, response=recognized
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
