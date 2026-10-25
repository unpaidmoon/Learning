"""Launch the local learning desktop and only its owned backend processes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from bootstrap import ROOT, prepare, verified_executable


def activate_existing(port: int, run_root: Path | None = None) -> bool:
    """Restore only a live window whose backend identity and HWND are verified."""
    from learning_app.dev import wait_for_http
    from learning_app.errors import AppError
    from learning_app.native_files import desktop_picker_owner

    run_root = run_root or ROOT / ".runtime/desktop/app/run"
    for logs in (run_root / "desktop-launches").glob("*"):
        try:
            lines = (logs / "backend.stdout.log").read_text("utf-8").splitlines()
            ready = next(line for line in lines if line.startswith("LEARNING_APP_READY "))
            value = json.loads(ready.removeprefix("LEARNING_APP_READY "))
            identity = value["instance_id"]
            if not isinstance(identity, str) or len(identity) != 32 or not all(c in "0123456789abcdef" for c in identity):
                continue
            record = json.loads((run_root / "apps" / identity / "run.json").read_text("utf-8"))
            if record.get("status") != "ready" or record.get("backend_port") != port:
                continue
            wait_for_http(f"http://127.0.0.1:{port}/api/v1/health", processes=[], timeout=1, instance_id=identity)
            if desktop_picker_owner(logs / "native-owner.json"):
                print("LEARNING_DESKTOP_ACTIVATED", flush=True)
                return True
        except (AppError, OSError, RuntimeError, ValueError, KeyError, StopIteration):
            continue
    return False


def main() -> int:
    from learning_app.dev import _request_owned_exit, wait_for_http
    from learning_app.processes import OwnedProcessGroup, ProcessError, require_free_port

    parser = argparse.ArgumentParser(description="启动沿用 Harness 原始界面的学习桌面试用")
    parser.add_argument("--url", help="连接已经运行的本项目 /desktop/ 页面，不管理该后台")
    parser.add_argument("--backend-port", type=int, default=8766)
    parser.add_argument("--data-dir", type=Path, default=ROOT / ".runtime/desktop/app/data", help="桌面试用独立业务数据目录；默认不接管原应用数据")
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / ".runtime/desktop/app/run", help="本次桌面的实例与启动记录目录")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--debug-port", type=int, help="仅本地桌面验收使用的 Chromium 调试端口")
    parser.add_argument("--hidden", action="store_true", help="仅验收使用：加载但不显示窗口")
    parser.add_argument("--user-data-dir", type=Path, help="使用独立的桌面浏览器状态目录")
    parser.add_argument("--no-prepare", action="store_true", help="只使用已经准备的固定运行时")
    args = parser.parse_args()
    if args.timeout <= 0 or not 1024 <= args.backend_port <= 65535:
        parser.error("等待时限和本地端口无效")
    if args.debug_port is not None and not 1024 <= args.debug_port <= 65535:
        parser.error("--debug-port 无效")
    url = args.url or f"http://127.0.0.1:{args.backend_port}/desktop/"
    address = urlsplit(url)
    if (
        address.scheme != "http" or address.hostname != "127.0.0.1"
        or address.username or address.password or address.path != "/desktop/"
        or address.query or address.fragment or address.port is None
    ):
        parser.error("--url 必须是 http://127.0.0.1:<port>/desktop/，不得包含凭据或查询参数")
    # Validate debug options before starting any service. Check again at the
    # actual Electron launch to detect another process winning the port race.
    if args.debug_port is not None:
        require_free_port("127.0.0.1", args.debug_port)
    if not args.url:
        try:
            require_free_port("127.0.0.1", args.backend_port)
        except ProcessError:
            if activate_existing(args.backend_port, args.runtime_dir.resolve()):
                return 0
            raise
    if not args.no_prepare:
        prepare()
    electron = verified_executable()
    runtime_root = args.runtime_dir.resolve()
    logs = runtime_root / "desktop-launches" / uuid.uuid4().hex
    logs.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    # Runtime keys are read only by the existing private credential module.
    for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_AUTH_TOKEN", "NODE_OPTIONS", "ELECTRON_RUN_AS_NODE"):
        env.pop(key, None)
    env.update(
        LEARNING_APP_DESKTOP_ENABLED="true",
        LEARNING_APP_RUNTIME_DIR=str(runtime_root),
        LEARNING_APP_DATA_DIR=str(args.data_dir.resolve()),
        LEARNING_DESKTOP_URL=url,
        LEARNING_DESKTOP_OWNS_BACKEND="0" if args.url else "1",
        LEARNING_DESKTOP_OWNER_FILE=str(logs / "native-owner.json"),
        PYTHONUNBUFFERED="1",
        PYTHONIOENCODING="utf-8",
    )
    if args.hidden:
        env["LEARNING_DESKTOP_TEST_HIDDEN"] = "1"
    else:
        env.pop("LEARNING_DESKTOP_TEST_HIDDEN", None)
    if args.user_data_dir:
        env["LEARNING_DESKTOP_USER_DATA"] = str(args.user_data_dir.resolve())
    group = OwnedProcessGroup("learning-desktop")
    backend = None
    record = None
    exit_attempted = False

    def cleanup_backend() -> None:
        nonlocal exit_attempted
        if exit_attempted or backend is None or backend.poll() is not None or record is None:
            return
        exit_attempted = True
        marker = "LEARNING_DESKTOP_EXIT_CONFIRMED " + str(record["instance_id"])
        electron_log = logs / "electron.stdout.log"
        confirmed = electron_log.is_file() and marker in electron_log.read_text("utf-8", errors="replace").splitlines()
        try:
            if not confirmed:
                _request_owned_exit(record, confirm_active=False, timeout=5)
            backend.wait(timeout=12)
        except Exception as exc:
            # An active or uncertain backend remains owned by its separate
            # launcher. Never turn window rollback into a Host termination,
            # and never repeat an exit whose result was not confirmed.
            print("DESKTOP_BACKEND_CONTINUES " + json.dumps({"instance_id": record["instance_id"], "code": getattr(exc, "code", "DESKTOP_EXIT_RESULT_UNKNOWN")}, ensure_ascii=False), flush=True)
    try:
        with (
            (logs / "backend.stdout.log").open("w", encoding="utf-8") as backend_output,
            (logs / "backend.stderr.log").open("w", encoding="utf-8") as backend_errors,
            (logs / "electron.stdout.log").open("w", encoding="utf-8") as output,
            (logs / "electron.stderr.log").open("w", encoding="utf-8") as errors,
        ):
            if not args.url:
                require_free_port("127.0.0.1", args.backend_port)
                # The existing launcher owns its own Job Object. Keeping it
                # separate means a window crash or uncertain exit cannot kill
                # a generation before the business endpoint reconciles it.
                # pythonw has no reliable standard streams for a nested CLI
                # delegate. Use the bundled sibling console interpreter with
                # explicit log handles and CREATE_NO_WINDOW for the service.
                service_python = Path(sys.executable)
                if service_python.name.lower() == "pythonw.exe":
                    service_python = service_python.with_name("python.exe")
                backend = subprocess.Popen(
                    [str(service_python), str(ROOT / "scripts/app.py"), "start", "--harness", "--backend-port", str(args.backend_port), "--timeout", str(args.timeout)],
                    cwd=ROOT, env=env, stdout=backend_output, stderr=backend_errors,
                    stdin=subprocess.DEVNULL,
                    creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
                )
                deadline = time.monotonic() + args.timeout * 2 + 5
                while time.monotonic() < deadline:
                    lines = (logs / "backend.stdout.log").read_text("utf-8", errors="replace").splitlines()
                    ready = next((line for line in lines if line.startswith("LEARNING_APP_READY ")), None)
                    if ready:
                        ready_value = json.loads(ready.removeprefix("LEARNING_APP_READY "))
                        identity = ready_value["instance_id"]
                        record_path = runtime_root / "apps" / identity / "run.json"
                        record = json.loads(record_path.read_text("utf-8"))
                        break
                    if backend.poll() is not None:
                        raise ProcessError("DESKTOP_BACKEND_FAILED", f"后台启动失败，请查看 {logs}。")
                    time.sleep(0.1)
                if record is None:
                    raise ProcessError("DESKTOP_READINESS_UNKNOWN", f"启动观察超时，请核对 {logs} 后再重启。")
                wait_for_http(
                    f"{address.scheme}://{address.netloc}/api/v1/health",
                    processes=[backend], timeout=args.timeout, instance_id=str(record["instance_id"]),
                )
            else:
                wait_for_http(f"{address.scheme}://{address.netloc}/api/v1/health", processes=[], timeout=args.timeout)
            command = [str(electron)]
            if args.debug_port is not None:
                require_free_port("127.0.0.1", args.debug_port)
                command.extend([f"--remote-debugging-port={args.debug_port}", "--remote-debugging-address=127.0.0.1"])
            command.append(str(ROOT / "desktop"))
            desktop = group.spawn(command, cwd=ROOT, env=env, stdout=output, stderr=errors, stdin=subprocess.DEVNULL)
            deadline = time.monotonic() + args.timeout
            while "LEARNING_DESKTOP_WINDOW_READY" not in (logs / "electron.stdout.log").read_text("utf-8", errors="replace").splitlines():
                if desktop.poll() is not None:
                    raise ProcessError("DESKTOP_WINDOW_FAILED", f"桌面窗口启动失败，请查看 {logs}。")
                if time.monotonic() >= deadline:
                    raise ProcessError("DESKTOP_WINDOW_TIMEOUT", f"桌面窗口未就绪，请查看 {logs}。")
                time.sleep(.1)
            print("LEARNING_DESKTOP_READY " + json.dumps({"url": url, "pid": desktop.pid, "logs": str(logs), "owns_backend": not bool(args.url)}, ensure_ascii=False), flush=True)
            while desktop.poll() is None:
                if backend is not None and backend.poll() is None:
                    # A bounded backend recovery retains this desktop carrier.
                    # Follow only records emitted by our live launcher.
                    lines = (logs / "backend.stdout.log").read_text("utf-8", errors="replace").splitlines()
                    ready_line = next((line for line in reversed(lines) if line.startswith("LEARNING_APP_READY ")), None)
                    if ready_line:
                        try:
                            observed = json.loads(ready_line.removeprefix("LEARNING_APP_READY "))
                            identity = observed["instance_id"]
                            if len(identity) == 32 and all(c in "0123456789abcdef" for c in identity) and record is not None and identity != record["instance_id"]:
                                candidate = runtime_root / "apps" / identity / "run.json"
                                updated = json.loads(candidate.read_text("utf-8"))
                                if updated.get("launcher_pid") == backend.pid and updated.get("status") == "ready" and updated.get("backend_port") == args.backend_port:
                                    record = updated
                        except (OSError, ValueError, KeyError):
                            # A partially flushed line/file is not a new owner.
                            pass
                if backend is not None and backend.poll() is not None:
                    # A business exit from the page is explicit. Close the
                    # now-disconnected carrier after the owned backend exits.
                    break
                time.sleep(0.2)
            cleanup_backend()
            return desktop.poll() or 0
    except KeyboardInterrupt:
        cleanup_backend()
        return 130
    finally:
        # This also handles a post-readiness Electron spawn/port failure.
        cleanup_backend()
        group.close(grace_seconds=3)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(f"{getattr(error, 'code', 'DESKTOP_START_FAILED')}: {error}", file=sys.stderr)
        raise SystemExit(1) from None
