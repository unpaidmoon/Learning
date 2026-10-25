"""Launch the local learning desktop and only its owned backend processes."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from bootstrap import ROOT, prepare, verified_executable

from learning_app.errors import AppError


class StartupGate:
    """Serialize startup for one data location, not the lifetime of its window."""

    def __init__(self, identity: str):
        self.handle = None
        self.acquired = True
        if os.name == "nt":
            from ctypes import wintypes

            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            self.kernel.CreateMutexW.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            name = "Local\\LearningDesktopStart-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            self.handle = self.kernel.CreateMutexW(None, False, name)
            if not self.handle:
                raise OSError("无法建立桌面启动锁")
            result = self.kernel.WaitForSingleObject(self.handle, 0)
            self.acquired = result in {0, 0x80}
            if result not in {0, 0x80, 0x102}:
                self.close()
                raise OSError("无法核对桌面启动状态")

    def close(self):
        if self.handle:
            if self.acquired:
                self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def ready_launches(run_root: Path, port: int | None = None):
    for logs in sorted((run_root / "desktop-launches").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            lines = (logs / "backend.stdout.log").read_text("utf-8", errors="replace").splitlines()
            ready = next(line for line in reversed(lines) if line.startswith("LEARNING_APP_READY "))
            identity = json.loads(ready.removeprefix("LEARNING_APP_READY "))["instance_id"]
            if not isinstance(identity, str) or len(identity) != 32 or any(c not in "0123456789abcdef" for c in identity):
                continue
            record = json.loads((run_root / "apps" / identity / "run.json").read_text("utf-8"))
            if (record.get("status") != "ready" or record.get("instance_id", identity) != identity
                    or (port is not None and record.get("backend_port") != port)):
                continue
            yield {**record, "instance_id": identity}, logs / "native-owner.json"
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            continue


def data_matches(record: dict, data_dir: Path) -> bool:
    import httpx

    from learning_app.data_location import ProfileLocation
    from learning_app.processes import ProcessError

    base = f"http://127.0.0.1:{record['backend_port']}"
    try:
        with httpx.Client(base_url=base, timeout=5, trust_env=False) as client:
            client.get("/api/v1/session").raise_for_status()
            response = client.get("/api/v1/data")
            response.raise_for_status()
            actual = Path(response.json()["data_directory"]).resolve()
            health = client.get("/api/v1/health").json()
        if health.get("instance_id") != record["instance_id"]:
            raise ValueError("instance changed")
        return actual == ProfileLocation(data_dir).current().resolve()
    except (httpx.HTTPError, OSError, ValueError, KeyError) as error:
        raise ProcessError("DESKTOP_EXISTING_CHECK_FAILED", "已发现学习后台，但暂时无法确认资料位置。请稍后重新打开；未另启后台。") from error


def find_running_backend(port: int | None, run_root: Path, data_dir: Path):
    from learning_app.dev import wait_for_http
    from learning_app.processes import ProcessError

    for record, owner in ready_launches(run_root, port):
        try:
            with socket.create_connection(("127.0.0.1", record["backend_port"]), timeout=0.2):
                pass
            wait_for_http(f"http://127.0.0.1:{record['backend_port']}/api/v1/health", processes=[], timeout=1, instance_id=record["instance_id"])
        except (OSError, ProcessError):
            continue
        if data_matches(record, data_dir):
            return record, owner
    return None


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def activate_existing(port: int, run_root: Path | None = None, *, data_dir: Path | None = None) -> bool:
    """Restore only a live window whose backend identity and HWND are verified."""
    from learning_app.dev import wait_for_http
    from learning_app.native_files import desktop_picker_owner

    run_root = run_root or ROOT / ".runtime/desktop/app/run"
    for record, owner in ready_launches(run_root, port):
        try:
            identity = record["instance_id"]
            wait_for_http(f"http://127.0.0.1:{port}/api/v1/health", processes=[], timeout=1, instance_id=identity)
            if data_dir is not None and not data_matches(record, data_dir):
                continue
            if desktop_picker_owner(owner):
                print("LEARNING_DESKTOP_ACTIVATED", flush=True)
                return True
        except (AppError, OSError, RuntimeError, ValueError, KeyError, StopIteration):
            continue
    return False


def main() -> int:
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
    parser.add_argument("--show-errors", action="store_true", help="通过桌面提示显示启动失败")
    args = parser.parse_args()
    from learning_app.data_location import ProfileLocation

    identity = args.url or str(ProfileLocation(args.data_dir.resolve()).current()).casefold()
    gate = StartupGate(identity)
    try:
        if not gate.acquired:
            print("学习桌面正在启动，请稍候。", flush=True)
            return 0
        return launch(args, gate, parser)
    finally:
        gate.close()


def launch(args, startup_gate, parser) -> int:
    from learning_app.dev import _request_owned_exit, wait_for_http
    from learning_app.processes import OwnedProcessGroup, ProcessError, require_free_port

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
    existing = None
    if not args.url:
        try:
            require_free_port("127.0.0.1", args.backend_port)
        except ProcessError:
            if activate_existing(args.backend_port, args.runtime_dir.resolve(), data_dir=args.data_dir.resolve()):
                return 0
            existing = find_running_backend(None, args.runtime_dir.resolve(), args.data_dir.resolve())
            if existing is None:
                args.backend_port = available_port()
                print("原端口暂不可用，已自动选择可用端口。", flush=True)
        else:
            existing = find_running_backend(None, args.runtime_dir.resolve(), args.data_dir.resolve())
        if existing:
            args.backend_port = existing[0]["backend_port"]
            if activate_existing(args.backend_port, args.runtime_dir.resolve(), data_dir=args.data_dir.resolve()):
                return 0
            print("已连接现有学习后台，正在重新打开窗口。", flush=True)
        url = f"http://127.0.0.1:{args.backend_port}/desktop/"
        address = urlsplit(url)
    if not args.no_prepare and existing is None:
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
        LEARNING_DESKTOP_OWNER_FILE=str(existing[1] if existing else logs / "native-owner.json"),
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
    record = existing[0] if existing else None
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
            if not args.url and existing is None:
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
                wait_for_http(f"{address.scheme}://{address.netloc}/api/v1/health", processes=[], timeout=args.timeout,
                              **({"instance_id": record["instance_id"]} if existing else {}))
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
            startup_gate.close()
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
    except (OSError, RuntimeError, ValueError, AppError) as error:
        if sys.stderr is not None:
            print(f"{getattr(error, 'code', 'DESKTOP_START_FAILED')}: {error}", file=sys.stderr)
        if os.name == "nt" and "--show-errors" in sys.argv:
            ctypes.windll.user32.MessageBoxW(None, "学习桌面暂时无法打开。\n\n" + str(error), "AI 辅助学习", 0x10)
        raise SystemExit(1) from None
