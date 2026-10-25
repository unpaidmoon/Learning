"""Actual owned Electron -> formal folder API -> owned Windows dialog proof.

Only this runner's synthetic page/windows/folders are driven. No screenshot,
desktop capture, user media, global input or model request is used.
"""

from __future__ import annotations

import argparse
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.request import ProxyHandler, build_opener
import uuid

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/desktop"))
from bootstrap import verified_executable  # noqa: E402
from check_shell import Debugger, debugger_targets  # noqa: E402
from learning_app.processes import OwnedProcessGroup  # noqa: E402
from test_native_course_picker import CoursePicker  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(operation, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = operation()
            if result:
                return result
        except (OSError, ConnectionError):
            pass
        time.sleep(.05)
    raise RuntimeError("NATIVE_OWNER_TEST_TIMEOUT")


def serve(directory: Path, port: int):
    import uvicorn
    from fastapi.responses import HTMLResponse
    from starlette.routing import Route
    from learning_app.app import create_app
    from learning_app.config import AppConfig

    app = create_app(AppConfig(
        mode="test", backend_port=port, frontend_port=port,
        data_dir=directory / "data", runtime_dir=directory / "run",
        harness_enabled=False,
    ))
    async def synthetic_page(request):
        return HTMLResponse("""<!doctype html><title>Picker owner synthetic page</title>
<h1>Picker owner synthetic page</h1><button id="pick">选择合成课程文件夹</button>
<script>
const session=fetch('/api/v1/session').then(r=>r.json());
document.getElementById('pick').onclick=async()=>{
 window.pickResult=undefined;
 const auth=await session;
 const response=await fetch('/api/v1/courses/folder/pick',{method:'POST',
 headers:{'Content-Type':'application/json','X-Learning-CSRF':auth.csrf_token},body:'{}'});
 window.pickResult={status:response.status,body:await response.json()};
};
window.duplicate=async()=>{const auth=await session;
 const r=await fetch('/api/v1/courses/folder/pick',{method:'POST',
 headers:{'Content-Type':'application/json','X-Learning-CSRF':auth.csrf_token},body:'{}'});
 window.duplicateResult={status:r.status,body:await r.json()};};
</script>""")
    app.router.routes.insert(0, Route("/desktop/", synthetic_page))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False))
    def stop():
        for line in sys.stdin:
            if line.strip() == "shutdown":
                break
        server.should_exit = True
    threading.Thread(target=stop, daemon=True).start()
    server.run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--run-id", default="desktop-picker-owner-" + uuid.uuid4().hex[:8])
    args = parser.parse_args()
    if args.serve:
        serve(args.serve, args.port)
        return 0
    if os.name != "nt" or not args.run_id.replace("-", "").replace("_", "").isalnum():
        parser.error("Windows and safe run id required")
    directory = ROOT / ".runtime/validation" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    synthetic = directory / "synthetic folder 中文"
    synthetic.mkdir()
    (synthetic / "generated.txt").write_text("synthetic only", encoding="utf-8")
    port, chrome_port, inspector_port = free_port(), free_port(), free_port()
    env = {key: value for key, value in os.environ.items()
           if key not in {"ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "DEEPSEEK_API_KEY"}}
    env.update(
        LEARNING_DESKTOP_OWNER_FILE=str(directory / "native-owner.json"),
        LEARNING_DESKTOP_URL=f"http://127.0.0.1:{port}/desktop/",
        LEARNING_DESKTOP_OWNS_BACKEND="0", LEARNING_DESKTOP_TEST_HIDDEN="0",
        LEARNING_DESKTOP_USER_DATA=str(directory / "browser"), PYTHONUNBUFFERED="1",
    )
    group = OwnedProcessGroup("learning-picker-owner-check")
    output = (directory / "process.log").open("w", encoding="utf-8")
    backend = group.spawn(
        [sys.executable, str(Path(__file__).resolve()), "--serve", str(directory), "--port", str(port)],
        cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
    )
    opener = build_opener(ProxyHandler({}))
    def healthy():
        with opener.open(f"http://127.0.0.1:{port}/api/v1/health", timeout=1) as response:
            return json.load(response)["ready"]
    result = {"run_id": args.run_id, "status": "failed", "real_model_calls": 0,
              "scope": "real owned Electron window, formal course HTTP route, native IFileOpenDialog"}
    native_picker = None
    try:
        wait_until(healthy)
        electron = group.spawn([
            str(verified_executable()), f"--inspect=127.0.0.1:{inspector_port}",
            f"--remote-debugging-port={chrome_port}", str(ROOT / "desktop"),
        ], cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT)
        host_target = debugger_targets(inspector_port)[0]
        page_target = next(value for value in debugger_targets(chrome_port) if value["type"] == "page")
        instance = type("OwnedBackend", (), {"record": {"backend_pid": backend.pid, "owned_pids": group.pids()}})()
        native_picker = CoursePicker(instance, directory)
        native_picker.user.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        native_picker.user.GetWindow.restype = wintypes.HWND
        native_picker.user.GetForegroundWindow.restype = wintypes.HWND
        with connect(host_target["webSocketDebuggerUrl"]) as host_socket, connect(page_target["webSocketDebuggerUrl"]) as page_socket:
            native, page = Debugger(host_socket), Debugger(page_socket)
            wait_until(lambda: page.evaluate("Boolean(document.getElementById('pick'))"))
            owner = json.loads((directory / "native-owner.json").read_text("utf-8"))
            expected_owner = int(owner["hwnd"])
            assert owner["pid"] == electron.pid
            native.evaluate("process.mainModule.require('electron').BrowserWindow.getAllWindows()[0].minimize()")
            page.evaluate("document.getElementById('pick').click()")
            wait_until(native_picker.find)
            dialog = native_picker.dialog
            actual_owner = native_picker.user.GetWindow(dialog, 4)  # GW_OWNER.
            foreground = native_picker.user.GetForegroundWindow()
            assert actual_owner == expected_owner, (actual_owner, expected_owner)
            assert foreground == dialog, (foreground, dialog)
            restored = native.evaluate("(()=>{const w=process.mainModule.require('electron').BrowserWindow.getAllWindows()[0];return {visible:w.isVisible(),minimized:w.isMinimized()}})()")
            assert restored["visible"] and not restored["minimized"]
            page.evaluate("window.duplicate()")
            duplicate = wait_until(lambda: page.evaluate("window.duplicateResult"))
            assert duplicate["body"]["code"] == "PICKER_ACTIVE", duplicate
            assert native_picker.user.GetForegroundWindow() == dialog, {
                "foreground": native_picker.user.GetForegroundWindow(), "dialog": dialog,
                "owner_enabled": native.evaluate("process.mainModule.require('electron').BrowserWindow.getAllWindows()[0].isEnabled()"),
            }
            native_picker.finish()
            cancelled = wait_until(lambda: page.evaluate("window.pickResult"))
            assert cancelled == {"status": 200, "body": None}, cancelled
            page.evaluate("document.getElementById('pick').click()")
            wait_until(native_picker.find)
            assert native_picker.user.GetWindow(native_picker.dialog, 4) == expected_owner
            assert native_picker.user.GetForegroundWindow() == native_picker.dialog
            native_picker.finish(synthetic)
            selected = wait_until(lambda: page.evaluate("window.pickResult"))
            assert selected["status"] == 200 and Path(selected["body"]["path"]) == synthetic, selected
            stale = {**owner, "registered_at_ms": 1}
            (directory / "native-owner.json").write_text(json.dumps(stale), encoding="utf-8")
            page.evaluate("document.getElementById('pick').click()")
            stale_result = wait_until(lambda: page.evaluate("window.pickResult"))
            assert stale_result["status"] == 503 and stale_result["body"]["code"] == "PICKER_OWNER_UNAVAILABLE"
            foreign = {**owner, "pid": owner["pid"] + 1}
            (directory / "native-owner.json").write_text(json.dumps(foreign), encoding="utf-8")
            page.evaluate("document.getElementById('pick').click()")
            foreign_result = wait_until(lambda: page.evaluate("window.pickResult"))
            assert foreign_result["status"] == 503 and foreign_result["body"]["code"] == "PICKER_OWNER_UNAVAILABLE"
            (directory / "native-owner.json").write_text(json.dumps(owner), encoding="utf-8")
            result.update(status="passed", owner_matches=True, dialog_is_foreground=True,
                          minimized_owner_restored=restored, duplicate=duplicate["body"]["code"],
                          cancellation=cancelled, retry_selected_synthetic_folder=True,
                          stale_owner_rejected=True, mismatched_pid_rejected=True)
            try:
                native.evaluate("process.mainModule.require('electron').app.quit()")
            except Exception:
                pass
        electron.wait(timeout=15)
        backend.stdin.write("shutdown\n")
        backend.stdin.flush()
        backend.wait(timeout=15)
        result["electron_exit_code"] = electron.returncode
        assert electron.returncode == 0
    except Exception as error:
        result["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if native_picker:
            if native_picker.dialog and native_picker.user.IsWindow(native_picker.dialog):
                native_picker.finish()
            native_picker.kernel.CloseHandle(native_picker.handle)
        result["cleanup"] = group.close()
        output.close()
        (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
