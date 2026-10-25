"""Finite, cancellable local workers. Never shares or terminates the Harness Job."""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from learning_app.errors import AppError
from learning_app.processes import OwnedProcessGroup


def worker_environment(temporary: Path, resource_policy=None) -> dict[str, str]:
    value = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE")
        if key in os.environ
    }
    value.update(
        {
            "TEMP": str(temporary),
            "TMP": str(temporary),
            "PATH": str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_DISABLE_XET": "1",
            "OMP_NUM_THREADS": "2",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_WAIT_POLICY": "PASSIVE",
            "KMP_BLOCKTIME": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if resource_policy:
        value["OMP_NUM_THREADS"] = str(resource_policy["cpu_threads"])
    return value


def run_json_worker(
    arguments: list[str],
    request: dict,
    *,
    directory: Path,
    cancelled: threading.Event,
    on_progress,
    log_path: Path,
    timeout_seconds: float = 120,
    initialization: dict | None = None,
    omit_result_fields: tuple[str, ...] = (),
    resource_policy: dict | None = None,
) -> dict:
    group = OwnedProcessGroup("source-local-worker")
    records: queue.SimpleQueue = queue.SimpleQueue()
    readers = []
    output = None
    try:
        if cancelled.is_set():
            raise AppError("PREPARATION_INTERRUPTED", "资料处理已让位或暂停")
        process = group.spawn(arguments, cwd=directory, env=worker_environment(directory, resource_policy))
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            priority = ctypes.WinDLL("kernel32", use_last_error=True).SetPriorityClass
            priority.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            priority.restype = wintypes.BOOL
            priority_class = 0x20 if resource_policy and resource_policy["priority"] == "normal" else 0x4000
            if not priority(int(process._handle), priority_class):
                raise AppError("LOCAL_WORKER_PRIORITY_FAILED", "本地处理优先级未能生效")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        output = log_path.open("x", encoding="utf-8")
        if resource_policy:
            log_path.with_suffix(log_path.suffix + ".limits.json").write_text(json.dumps(resource_policy), "utf-8")

        def read_stdout():
            total = 0
            try:
                while line := process.stdout.readline(8 * 1024 * 1024 + 1):
                    total += len(line.encode("utf-8"))
                    if len(line) > 8 * 1024 * 1024 or total > 32 * 1024 * 1024:
                        records.put({"type": "error", "code": "LOCAL_WORKER_OUTPUT_LIMIT"})
                        return
                    records.put(line)
            finally:
                records.put(None)

        def read_stderr():
            with log_path.with_suffix(".stderr.txt").open("x", encoding="utf-8") as errors:
                total = 0
                for line in process.stderr:
                    total += len(line)
                    if total <= 1024 * 1024:
                        errors.write(line)

        def write_input():
            try:
                for message in (
                    [initialization, request] if initialization is not None else [request]
                ):
                    process.stdin.write(
                        json.dumps(message, ensure_ascii=True, allow_nan=False) + "\n"
                    )
                process.stdin.close()
            except (BrokenPipeError, OSError):
                # A failed/cancelled child can close input while its final
                # stdout error is still readable. Let that record decide.
                pass
            except (TypeError, ValueError):
                records.put({"type": "error", "code": "LOCAL_WORKER_INPUT_INVALID"})

        for function in (read_stdout, read_stderr, write_input):
            reader = threading.Thread(target=function, daemon=True)
            reader.start()
            readers.append(reader)
        started, last_progress, last_status, result = time.monotonic(), 0.0, None, None
        while True:
            if cancelled.is_set():
                raise AppError("PREPARATION_INTERRUPTED", "资料处理已让位或暂停")
            if time.monotonic() - started > timeout_seconds:
                raise AppError("LOCAL_WORKER_TIMEOUT", "本地单元处理超时，未截断或自动重试")
            try:
                item = records.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except ValueError as exc:
                    output.write(item)
                    output.flush()
                    raise AppError("LOCAL_WORKER_PROTOCOL", "本地处理返回了无效记录") from exc
                logged = (
                    {key: value for key, value in item.items() if key not in omit_result_fields}
                    if isinstance(item, dict) and item.get("type") == "result"
                    else item
                )
                output.write(json.dumps(logged, ensure_ascii=False, allow_nan=False) + "\n")
                output.flush()
            if not isinstance(item, dict):
                raise AppError("LOCAL_WORKER_PROTOCOL", "本地处理返回记录类型不符")
            if item.get("type") == "error":
                code = item.get("code", "LOCAL_WORKER_FAILED")
                if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9_]{1,100}", code):
                    code = "LOCAL_WORKER_FAILED"
                raise AppError(code, "本地处理未完成，已有结果和错误记录仍保留")
            if item.get("type") == "ready" and initialization is not None:
                if (
                    item.get("protocol_version") != 1
                    or item.get("manifest_sha256") != initialization["spec"]["manifest_sha256"]
                ):
                    raise AppError("LOCAL_WORKER_SCOPE_CONFLICT", "本地引擎初始化版本不符")
            if item.get("type") == "progress" and (
                item.get("status") != last_status or time.monotonic() - last_progress > 0.25
            ):
                if (
                    item.get("request_id") != request["request_id"]
                    or item.get("epoch") != request["epoch"]
                ):
                    raise AppError("LOCAL_WORKER_SCOPE_CONFLICT", "本地处理进度归属不符")
                on_progress({key: item.get(key) for key in ("status", "progress")})
                last_progress = time.monotonic()
                last_status = item.get("status")
            if item.get("type") == "result":
                if (
                    result is not None
                    or item.get("request_id") != request["request_id"]
                    or item.get("epoch") != request["epoch"]
                ):
                    raise AppError("LOCAL_WORKER_SCOPE_CONFLICT", "本地处理结果归属不符")
                result = item
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise AppError(
                "LOCAL_WORKER_SHUTDOWN_TIMEOUT", "本地单元退出超时，已停止所属进程"
            ) from exc
        if process.returncode != 0 or result is None:
            raise AppError("LOCAL_WORKER_FAILED", "本地处理没有完成结果")
        return result
    finally:
        group.close(grace_seconds=0.2)
        for reader in readers:
            reader.join(timeout=2)
        if output:
            output.close()
