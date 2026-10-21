"""Root developer/local runner: readiness, isolation, and bounded rollback."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

from learning_app.config import load_config
from learning_app.processes import OwnedProcessGroup, ProcessError, require_free_port


def _write_record(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def wait_for_http(
    url: str,
    *,
    processes: list[subprocess.Popen[str]],
    timeout: float,
    instance_id: str | None = None,
    identity_header: bool = False,
) -> None:
    """A real endpoint and process identity must agree; a port alone is not ready."""
    deadline = time.monotonic() + timeout
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise ProcessError("SERVICE_EXITED", "所属服务在就绪前退出；请查看本次实例日志。")
        try:
            with opener.open(
                url, timeout=min(1.0, max(0.05, deadline - time.monotonic()))
            ) as response:
                if response.status == 200:
                    if instance_id is not None:
                        health = {} if identity_header else json.load(response)
                        observed_id = (
                            response.headers.get("X-Learning-Instance")
                            if identity_header
                            else health.get("instance_id")
                        )
                        if observed_id != instance_id:
                            raise ProcessError(
                                "INSTANCE_MISMATCH", "健康响应不属于本次实例，已回滚所属服务。"
                            )
                        if not identity_header and health.get("ready") is not True:
                            raise ProcessError("SERVICE_NOT_READY", "业务后台报告尚未就绪。")
                    return
        except (HTTPError, URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.05)
    raise ProcessError("READINESS_TIMEOUT", f"等待服务就绪超时：{url}")


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="统一启动学习应用开发或构建模式")
    parser.add_argument("mode", choices=["dev", "start", "stop"])
    parser.add_argument("--host")
    parser.add_argument("--backend-port", type=int)
    parser.add_argument("--frontend-port", type=int)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--instance-id", help="stop 时指定要停止的本项目实例")
    parser.add_argument("--open", action="store_true", help="就绪后打开浏览器")
    arguments = parser.parse_args(argv)
    if arguments.mode == "stop":
        config = load_config()
        requested = arguments.instance_id or ""
        if len(requested) != 32 or any(char not in "0123456789abcdef" for char in requested):
            raise ProcessError("INSTANCE_REQUIRED", "停止时须提供就绪输出中的 --instance-id。")
        run_dir = config.runtime_dir / "apps" / requested
        record_path = run_dir / "run.json"
        if not record_path.is_file():
            raise ProcessError("INSTANCE_NOT_FOUND", "找不到该实例运行记录。")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if record.get("instance_id") != requested or record.get("status") not in {
            "starting",
            "ready",
        }:
            raise ProcessError("INSTANCE_NOT_RUNNING", "该记录不属于活动实例。")
        # Request shutdown through an instance-bound mailbox, never terminate a
        # PID from a possibly stale record or scan by executable name.
        (run_dir / "stop.request").write_text(requested, encoding="ascii")
        print(f"已请求实例 {requested} 保存并停止。", flush=True)
        return 0
    instance_id = uuid.uuid4().hex
    overrides = {
        "mode": arguments.mode,
        "host": arguments.host,
        "backend_port": arguments.backend_port,
        "frontend_port": arguments.frontend_port,
        "instance_id": instance_id,
    }
    config = load_config({key: value for key, value in overrides.items() if value is not None})
    if arguments.timeout <= 0:
        raise ProcessError("INVALID_TIMEOUT", "就绪等待必须大于零。")
    require_free_port(config.host, config.backend_port)
    if arguments.mode == "dev":
        if config.frontend_port == config.backend_port:
            raise ProcessError("PORT_CONFLICT", "前后端开发端口不能相同。")
        require_free_port(config.host, config.frontend_port)
    elif not (config.static_dir / "index.html").is_file():
        raise ProcessError("BUILD_MISSING", "缺少 frontend/dist/index.html，请先执行构建。")

    local_node = config.project_root / ".tools/node/node.exe"
    node = str(local_node) if local_node.is_file() else shutil.which("node")
    vite = config.project_root / "frontend/node_modules/vite/bin/vite.js"
    if arguments.mode == "dev" and (node is None or not vite.is_file()):
        raise ProcessError(
            "FRONTEND_DEPENDENCY_MISSING", "缺少 Node 或项目 Vite，请先按锁文件安装依赖。"
        )

    # Unique directory and identity: two instances never overwrite each other's
    # records. Records are evidence, never authority to terminate a stored PID.
    run_dir = config.runtime_dir / "apps" / instance_id
    run_dir.mkdir(parents=True, exist_ok=False)
    record_path = run_dir / "run.json"
    record: dict[str, object] = {
        "instance_id": instance_id,
        "launcher_pid": os.getpid(),
        "mode": arguments.mode,
        "status": "starting",
        "backend_port": config.backend_port,
        "frontend_port": config.frontend_port if arguments.mode == "dev" else None,
        "started_at_ns": time.time_ns(),
        "owned_pids": [],
    }
    _write_record(record_path, record)
    environment = os.environ.copy()
    environment.update(
        {
            "LEARNING_APP_INSTANCE_ID": instance_id,
            "LEARNING_APP_MODE": arguments.mode,
            "LEARNING_APP_HOST": config.host,
            "LEARNING_APP_BACKEND_PORT": str(config.backend_port),
            "LEARNING_APP_FRONTEND_PORT": str(config.frontend_port),
            "LEARNING_APP_DATA_DIR": str(config.data_dir),
            "LEARNING_APP_RUNTIME_DIR": str(config.runtime_dir),
            "LEARNING_APP_OWNED_STDIN": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    try:
        group = OwnedProcessGroup(f"learning-app-{instance_id}")
    except OSError as exc:
        record.update(
            status="failed", failure_code="PROCESS_OWNERSHIP_FAILED", stopped_at_ns=time.time_ns()
        )
        _write_record(record_path, record)
        raise ProcessError(
            "PROCESS_OWNERSHIP_FAILED", "无法建立本次进程归属；未启动服务。"
        ) from exc
    logs = []
    backend: subprocess.Popen[str] | None = None
    exit_code = 0

    def request_interrupt(*_: object) -> None:
        raise KeyboardInterrupt

    original_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGINT)):
        if signum not in original_handlers:
            original_handlers[signum] = signal.signal(signum, request_interrupt)
    try:
        backend_log = (run_dir / "backend.log").open("w", encoding="utf-8")
        logs.append(backend_log)
        backend = group.spawn(
            [
                sys.executable,
                "-m",
                "learning_app",
                "serve",
                "--mode",
                arguments.mode,
                "--host",
                config.host,
                "--port",
                str(config.backend_port),
                "--instance-id",
                instance_id,
            ],
            cwd=config.project_root,
            env=environment,
            stdout=backend_log,
            stderr=subprocess.STDOUT,
        )
        record["backend_pid"] = backend.pid
        _write_record(record_path, record)
        wait_for_http(
            f"http://{config.host}:{config.backend_port}/api/v1/health",
            processes=[backend],
            timeout=arguments.timeout,
            instance_id=instance_id,
        )
        children = [backend]
        url = f"http://{config.host}:{config.backend_port}"
        if arguments.mode == "dev":
            frontend_log = (run_dir / "frontend.log").open("w", encoding="utf-8")
            logs.append(frontend_log)
            frontend = group.spawn(
                [
                    str(node),
                    str(vite),
                    "--host",
                    config.host,
                    "--port",
                    str(config.frontend_port),
                    "--strictPort",
                ],
                cwd=config.project_root / "frontend",
                env=environment,
                stdout=frontend_log,
                stderr=subprocess.STDOUT,
            )
            record["frontend_pid"] = frontend.pid
            _write_record(record_path, record)
            children.append(frontend)
            url = f"http://{config.host}:{config.frontend_port}"
            wait_for_http(
                url,
                processes=children,
                timeout=arguments.timeout,
                instance_id=instance_id,
                identity_header=True,
            )
        record.update(status="ready", owned_pids=group.pids(), ready_at_ns=time.time_ns(), url=url)
        _write_record(record_path, record)
        print(
            f"LEARNING_APP_READY {json.dumps({'instance_id': instance_id, 'url': url}, ensure_ascii=False)}",
            flush=True,
        )
        if arguments.open:
            webbrowser.open(url)
        while True:
            stop_request = run_dir / "stop.request"
            if stop_request.is_file() and stop_request.read_text(encoding="ascii") == instance_id:
                record["stop_reason"] = "instance_request"
                break
            for child in children:
                if child.poll() is not None:
                    raise ProcessError("SERVICE_EXITED", "所属服务意外退出，已回滚本次实例。")
            time.sleep(0.1)
    except KeyboardInterrupt:
        record["stop_reason"] = "interrupted"
    except (ProcessError, OSError) as exc:
        exit_code = 1
        record["failure_code"] = getattr(exc, "code", "START_FAILED")
        print(f"{record['failure_code']}: {exc}", file=sys.stderr, flush=True)
    finally:
        # Ask only our backend to finish lifespan. Vite and any uncooperative
        # descendants are then bounded by this instance's Job Object.
        if backend is not None and backend.poll() is None and backend.stdin is not None:
            try:
                backend.stdin.write("shutdown\n")
                backend.stdin.flush()
                backend.wait(timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                record["graceful_backend_timeout"] = True
        record.update(
            status="stopped" if exit_code == 0 else "failed",
            cleanup=group.close(),
            stopped_at_ns=time.time_ns(),
        )
        for handle in logs:
            handle.close()
        _write_record(record_path, record)
        for signum, handler in original_handlers.items():
            signal.signal(signum, handler)
    return exit_code


def main() -> int:
    try:
        return run()
    except (ProcessError, OSError, ValueError) as exc:
        print(f"{getattr(exc, 'code', 'CONFIGURATION_ERROR')}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
