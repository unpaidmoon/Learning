"""Root developer/local runner: readiness, isolation, and bounded rollback."""

from __future__ import annotations

import argparse
import http.cookiejar
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
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from learning_app.config import load_config
from learning_app.errors import AppError
from learning_app.processes import OwnedProcessGroup, ProcessError, require_free_port

BACKEND_RECOVERY_LIMIT = 2
BACKEND_RECOVERY_DELAYS = (1, 4)


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


def _request_owned_exit(record: dict[str, object], *, confirm_active: bool, timeout: float) -> None:
    """Use normal app authentication; disk records never authorize a PID kill."""
    identity = str(record["instance_id"])
    url = f"http://127.0.0.1:{record['backend_port']}/api/v1"
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        with opener.open(f"{url}/health", timeout=timeout) as response:
            health = json.load(response)
        if health.get("instance_id") != identity or health.get("ready") is not True:
            raise ProcessError("INSTANCE_MISMATCH", "端口服务不属于指定实例，未请求停止。")
        with opener.open(f"{url}/session", timeout=timeout) as response:
            csrf = json.load(response)["csrf_token"]
        with opener.open(f"{url}/runtime", timeout=timeout) as response:
            runtime = json.load(response)
        if runtime.get("instance_id") != identity:
            raise ProcessError("INSTANCE_MISMATCH", "后台实例已变化，未请求停止。")
        if (
            any(
                runtime.get(key)
                for key in (
                    "active_generations",
                    "active_tasks",
                    "active_preparations",
                    "active_data_operations",
                )
            )
            and not confirm_active
        ):
            if not sys.stdin.isatty():
                raise ProcessError(
                    "ACTIVE_GENERATIONS_CONFIRMATION_REQUIRED",
                    "仍有回答正在生成或结果待核对；确认保存并停止后请加 --confirm-active。",
                )
            try:
                answer = input("仍有回答正在生成或结果待核对。保存并请求停止后退出？[y/N] ").strip()
            except EOFError as exc:
                # Windows NUL may report isatty=True while having no interactive
                # input. An absent answer is never consent to stop active work.
                raise ProcessError(
                    "ACTIVE_GENERATIONS_CONFIRMATION_REQUIRED",
                    "无法读取确认；确认保存并停止后请加 --confirm-active。",
                ) from exc
            if answer.lower() not in {"y", "yes"}:
                raise ProcessError("EXIT_CANCELLED", "已取消退出，后台继续。")
            confirm_active = True
        request = Request(
            f"{url}/runtime/exit",
            data=json.dumps({"instance_id": identity, "confirm_active": confirm_active}).encode(),
            headers={"Content-Type": "application/json", "X-Learning-CSRF": csrf},
            method="POST",
        )
        with opener.open(request, timeout=timeout) as response:
            result = json.load(response)
        if result.get("instance_id") != identity or not result.get("exit_requested"):
            raise ProcessError("EXIT_NOT_CONFIRMED", "后台未确认本次退出请求，请检查实例状态。")
    except HTTPError as exc:
        try:
            detail = json.load(exc)
        except (ValueError, OSError):
            detail = {}
        raise ProcessError(
            detail.get("code", "EXIT_NOT_CONFIRMED"),
            detail.get("message", "后台未确认退出，未终止任何进程。"),
        ) from exc
    except (URLError, TimeoutError, ConnectionError) as exc:
        raise ProcessError(
            "EXIT_RESULT_UNKNOWN", "无法核对退出结果，请查看实例记录；未重复提交退出。"
        ) from exc


def _launch_background(arguments: argparse.Namespace, config) -> int:
    """Detach only a dedicated launcher; that launcher owns the service job."""
    if config.mode != "start":
        raise ProcessError("INVALID_MODE", "后台启动只用于已构建学习页面。")
    root = config.project_root
    if not (config.static_dir / "index.html").is_file():
        raise ProcessError("BUILD_MISSING", "缺少 frontend/dist/index.html，请先执行构建。")
    log_dir = config.runtime_dir / "launches" / uuid.uuid4().hex
    log_dir.mkdir(parents=True, exist_ok=False)
    command = [
        sys.executable,
        str(root / "scripts/app.py"),
        "start",
        "--timeout",
        str(arguments.timeout),
    ]
    for option in ("backend_port", "frontend_port"):
        value = getattr(arguments, option)
        if value is not None:
            command.extend([f"--{option.replace('_', '-')}", str(value)])
    if arguments.open:
        command.append("--open")
    if arguments.harness:
        command.append("--harness")
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    environment["LEARNING_APP_HARNESS_ENABLED"] = str(config.harness_enabled).lower()
    output_path = log_dir / "launcher.stdout.log"
    error_path = log_dir / "launcher.stderr.log"
    with (
        output_path.open("w", encoding="utf-8") as output,
        error_path.open("w", encoding="utf-8") as errors,
    ):
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=errors,
            **options,
        )
    deadline = time.monotonic() + arguments.timeout * 2 + 5
    while time.monotonic() < deadline:
        stdout = output_path.read_text(encoding="utf-8", errors="replace")
        ready = next(
            (line for line in stdout.splitlines() if line.startswith("LEARNING_APP_READY ")), None
        )
        if ready:
            print(ready, flush=True)
            return 0
        if process.poll() is not None:
            error = error_path.read_text(encoding="utf-8", errors="replace").strip()
            raise ProcessError("LAUNCH_FAILED", error or "学习应用未能启动，请检查本次启动日志。")
        time.sleep(0.1)
    # The child has its own bounded readiness timeouts. Do not kill a live
    # launcher merely because this observer timed out and might have missed ready.
    raise ProcessError(
        "LAUNCH_RESULT_UNKNOWN", "启动观察超时；请核对本次运行记录后再启动，避免重复实例。"
    )


def run(
    argv: list[str] | None = None,
    *,
    restart_budget: int = BACKEND_RECOVERY_LIMIT,
    recovery_parent: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="统一启动学习应用开发或构建模式")
    parser.add_argument("mode", choices=["dev", "start", "launch", "stop"])
    parser.add_argument("--host")
    parser.add_argument("--backend-port", type=int)
    parser.add_argument("--frontend-port", type=int)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--instance-id", help="stop 时指定要停止的本项目实例")
    parser.add_argument("--open", action="store_true", help="就绪后打开浏览器")
    execution_options = parser.add_mutually_exclusive_group()
    execution_options.add_argument(
        "--harness", action="store_true", help="启用既有后台私有配置的真实 Harness"
    )
    execution_options.add_argument(
        "--no-harness", action="store_true", help="本次启动不启用模型执行"
    )
    parser.add_argument(
        "--confirm-active", action="store_true", help="明确确认保存并停止活动或待核对回答后退出"
    )
    arguments = parser.parse_args(argv)
    if arguments.timeout <= 0:
        raise ProcessError("INVALID_TIMEOUT", "等待时限必须大于零。")
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
        if record.get("status") != "ready":
            raise ProcessError("INSTANCE_NOT_READY", "实例尚未就绪，请等待就绪或查看本次启动日志。")
        _request_owned_exit(
            record, confirm_active=arguments.confirm_active, timeout=arguments.timeout
        )
        print(f"已请求实例 {requested} 保存并停止。", flush=True)
        return 0
    instance_id = uuid.uuid4().hex
    overrides = {
        "mode": "start" if arguments.mode == "launch" else arguments.mode,
        "host": arguments.host,
        "backend_port": arguments.backend_port,
        "frontend_port": arguments.frontend_port,
        "instance_id": instance_id,
        "harness_enabled": True if arguments.harness else False if arguments.no_harness else None,
    }
    config = load_config({key: value for key, value in overrides.items() if value is not None})
    if arguments.mode == "launch":
        return _launch_background(arguments, config)
    require_free_port(config.host, config.backend_port)
    if arguments.mode == "dev":
        if config.frontend_port == config.backend_port:
            raise ProcessError("PORT_CONFLICT", "前后端开发端口不能相同。")
        require_free_port(config.host, config.frontend_port)
    elif not (config.static_dir / "index.html").is_file():
        raise ProcessError("BUILD_MISSING", "缺少 frontend/dist/index.html，请先执行构建。")

    from learning_app.data_location import ProfileLocation

    location = ProfileLocation(config.data_dir)
    if location.pending.exists():
        pending_id = json.loads(location.pending.read_text("utf-8"))["operation_id"]
        pending_state = json.loads(location.transfer(pending_id).read_text("utf-8"))["state"]
        if pending_state not in {"failed", "cancelled", "publish_blocked"}:
            with location.lease():
                location.apply_closed()

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
        "recovery_parent": recovery_parent,
        "automatic_restarts_remaining": restart_budget,
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
            "LEARNING_APP_HARNESS_ENABLED": str(config.harness_enabled).lower(),
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
    restart_backend = False

    def request_interrupt(*_: object) -> None:
        if record.get("status") == "ready":
            try:
                _request_owned_exit(record, confirm_active=False, timeout=arguments.timeout)
            except ProcessError as exc:
                print(f"{exc.code}: {exc}", file=sys.stderr, flush=True)
            return
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
                    if child is backend and restart_budget > 0:
                        restart_backend = True
                        record["recovery"] = {
                            "state": "scheduled",
                            "reason": "BACKEND_PROCESS_EXITED",
                            "remaining_after_restart": restart_budget - 1,
                        }
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
    if restart_backend:
        # The old complete owned group is gone before the new process can open
        # the same profile. New instance/cookies; original execution journals
        # are restored read-only by GenerationService, never replayed here.
        delay = BACKEND_RECOVERY_DELAYS[
            min(BACKEND_RECOVERY_LIMIT - restart_budget, len(BACKEND_RECOVERY_DELAYS) - 1)
        ]
        print(
            "LEARNING_APP_RECOVERING "
            + json.dumps(
                {
                    "previous_instance": instance_id,
                    "remaining": restart_budget - 1,
                    "delay_seconds": delay,
                }
            ),
            flush=True,
        )
        time.sleep(delay)
        return run(argv, restart_budget=restart_budget - 1, recovery_parent=instance_id)
    if record.get("stop_reason") == "instance_request" and not record.get(
        "graceful_backend_timeout"
    ):
        from learning_app.data_location import ProfileLocation

        location = ProfileLocation(config.data_dir)
        with location.lease():
            switched = location.apply_closed()
        if switched:
            print(
                "LEARNING_DATA_SWITCHED "
                + json.dumps(
                    {
                        "operation_id": switched["operation_id"],
                        "data_directory": switched["active_data"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return exit_code


def main() -> int:
    try:
        return run()
    except (AppError, ProcessError, OSError, ValueError) as exc:
        print(f"{getattr(exc, 'code', 'CONFIGURATION_ERROR')}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
